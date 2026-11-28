# Upwork Drafts (`upwork_drafts`)

Finds Upwork jobs that match you, ranks them, and drafts a proposal and a suggested rate for each
from your own background. **It is draft-only: it never submits a proposal and never spends
Connects.** You open each job's link, review the draft, answer any screening questions, and click
Submit on Upwork yourself.

## The draft-only rule

Submitting costs money (Connects), and Upwork bans accounts that automate it. So:

- No code path submits. The tool sends GraphQL through one helper, `upwork_tool.graphql`, and
  `assert_read_only` makes it refuse any document that is not a plain `query`. That rules out
  `createJobProposal` and every other mutation. There's no browser and nothing that clicks.
- `tests/unit/test_upwork_drafts.py` guards this. It refuses mutations before any network call.
  It checks statically that the tool and flow contain no proposal mutation, Playwright or
  `.click(`. It also checks that a full run sends only read-only queries.
- The result always has `submitted: 0` and a note saying nothing was sent. The spec's validator
  fails a result that reports anything submitted, and the judge rubric starts with the rule.
- Ask Upwork only for read scopes (below). Then the token itself can't write either.

## Discovery: what works today (checked 2026-11-28)

| Option | Status | Used? |
|---|---|---|
| Job RSS feeds (`/ab/feed/jobs/rss`) | Switched off on **2024-08-20**, according to Upwork's support article "RSS deprecation". The URL now answers HTTP 403 (a Cloudflare block page) | No |
| Public job and search pages (`/nx/search/jobs/`, `/freelance-jobs/…`) | Every one answers a plain HTTP client with a **Cloudflare 403** ("Attention Required"). A logged-in browser could read them, but Upwork's ToS forbid scraping, and its bot detection bans accounts | **No, on purpose** |
| Official GraphQL API `POST https://api.upwork.com/graphql`, query `marketplaceJobPostingsSearch` | Works with **your OAuth2 token** (unauthenticated calls answer `401 {"message":"Authentication failed"}`). Needs the scopes "Common Entities - Read-Only Access" and "Read marketplace Job Postings". 300 requests/min per IP | **Yes**, when a token is configured |
| Pasted postings (`jobs_text`) | You paste jobs copied from Upwork or from its job-alert e-mails, separated by `---` lines. No network | **Yes**, always available |

**Why not a logged-in browser fallback?** The other session-based automations (tasker, 104,
LinkedIn) use one. Here it would put your account at risk to save a copy-paste, which defeats the
purpose of a draft-only tool. Pasting covers the case where you don't have API access yet.

## Setup (API search)

1. Request an API key at <https://www.upwork.com/developer/keys/apply>. Pick key type
   **OAuth 2.0** and only the two read scopes above. Upwork reviews key requests.
2. Add the key to `.env`:
   ```
   UPWORK_CLIENT_ID=...
   UPWORK_CLIENT_SECRET=...
   UPWORK_REDIRECT_URI=...        # the callback URL registered with the key
   # optional: UPWORK_TENANT_ID=… (X-Upwork-API-TenantId; defaults to your main organization)
   ```
3. Run `uv run python scripts/upwork_oauth.py`. Open the URL it prints, approve, and paste the
   redirect URL back. Tokens are saved to `data/upwork_token.json` (git-ignored, mode 0600).
   Access tokens last 24 h, and runs refresh them automatically. The refresh token stays valid for
   2 weeks after its last use. If it expires, a run warns you to run the script again.

As an alternative to steps 2–3, set `UPWORK_ACCESS_TOKEN` (plus `UPWORK_REFRESH_TOKEN` and the
client ID/secret if you want refresh).

## Funnel (deterministic; the LLM only judges and writes)

| Step | How | Where |
|---|---|---|
| Search | One API search per keyword (max 5, 50 newest each, 2 s apart), merged by id. If the token's scopes don't cover a field, it retries once with the minimal field set | `upwork_tool.search` |
| Pasted | `---`-separated blocks (or one per job link). It reads the title, link, hourly range or fixed budget, the proposals count ("Less than 5" → 4, "50+" → 50) and "Payment verified" | `upwork_tool.parse_pasted` |
| Filter | Already applied (API flag); type; fixed budget below `min_fixed_budget`; hourly range topping out below `min_hourly_rate`; more than `max_applicants` proposals; an unverified client when `payment_verified_only` is on; keyword in title / skills (API hits only, since pasted jobs were your choice); already drafted in an earlier run (`UPWORK_LEDGER`, default `data/upwork_drafted.json`). An unknown value always passes | `upwork_tool.skip_reason` / `find_jobs` |
| Rank | A transparent 0–100 score, with its reasons shown on each draft. It counts the keyword in the title or skills, payment verified, past hires, a client rating ≥ 4.5, few proposals so far, and posted within 24 h / 3 days | `upwork_tool.rank` |
| Relevance (optional) | `task_filter`, judged by the shared relevance crew. It fails open | flow |
| Draft | The **shared** `FreelancerProposalCrew` (also used by `freelancer_bids`; its prompt names the platform). It writes 80–180 words using only facts from `candidate_background`, with no price and no placeholders, and ends with a question. When the background doesn't fit, it answers `SKIP: <reason>` → listed as declined | flow + crew |
| Suggest a rate | Fixed: the client's budget. Hourly: your `hourly_rate`, with a note when it's outside the client's range, or else the middle of the range | `upwork_tool.suggested_rate` |

The ledger stores job **ids only** (no titles or descriptions), because Upwork's API terms don't
allow keeping its data longer than 24 h.

## Result

```json
{"summary": "…", "sources": ["api", "pasted"], "jobs_found": 42, "drafted_count": 3,
 "skipped_count": 30, "submitted": 0,
 "drafted": [{"rank", "rank_score", "rank_reasons", "id", "title", "url", "type", "budget",
              "applicants", "source", "skills", "client", "suggested_rate": {"amount", "unit", "basis"},
              "proposal", "status": "draft — not submitted"}],
 "skipped": [{"id", "title", "url", …, "reason"}], "warnings": [], "note": "Draft-only: …"}
```

## Verified vs. needs a live check

Verified on this machine (2026-11-28): the RSS feed and the public pages answer 403 (Cloudflare),
and `api.upwork.com/graphql` answers 401 without a token.

Verified from the docs, **not yet against a live token** (no Upwork account or key was used
building this):

- The `marketplaceJobPostingsSearch` query shape, the `searchExpression_eq` / `pagination_eq`
  filter and the node fields. The test fixture follows the documented schema. It was not captured
  from a real response.
- Whether a freelancer key gets the read scopes above, and whether `totalSpent`,
  `verificationStatus` and similar fields need extra scopes. The minimal-field retry covers the
  second risk.
- The token endpoint and refresh behaviour (`/api/v3/oauth2/token`), and `scripts/upwork_oauth.py`.
- Pasted-text parsing against real copied postings and job-alert e-mails. The tests use realistic
  text written by hand, not real copies.

To check it live: run `scripts/upwork_oauth.py`, then run the job with one keyword and
`max_drafts: 1`, and compare `jobs_found`, the links and the budgets with the Upwork search page.

## Tests and evals

`tests/unit/test_upwork_drafts.py` covers the tool, the flow (LLM mocked) and the draft-only
guards. Golden eval cases are in `evals/upwork_drafts/`: API jobs with fit and misfit, both
filters, and rank order; and pasted jobs with the budget filter. No canary: discovery needs your
token or your pasted text (see `canary_skip` in the spec).
