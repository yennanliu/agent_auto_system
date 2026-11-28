# Freelancer Bids (`freelancer_bids`)

Finds matching **Freelancer.com** projects and drafts a bid proposal for each, from your
own background. **By default it places no bids**: the result is a list of projects, each
with a drafted proposal and a suggested bid, for you to review. Tick **Place the bids**
(`submit_bids`), with your token configured, to have it place them. See
[Placing bids](#placing-bids).

## Funnel (deterministic, the LLM only judges and writes)

| Step | How | Where |
|---|---|---|
| Search | Official public API `GET /api/projects/0.1/projects/active/?query=…` (no login), one search per keyword (max 5, 50 each), merged | `freelancer_tool.search` |
| Filter | Budget in **USD** (each currency carries an `exchange_rate`), type (fixed / hourly), competition (`max_bids`), keyword on title or skills (role shorthand expanded), not public / not open, **already drafted in an earlier run** (`FREELANCER_LEDGER`, default `data/freelancer_drafted.json`) | `freelancer_tool.find_projects` |
| Relevance (optional) | `task_filter`, judged by the shared relevance crew. It fails open | flow |
| Draft | `FreelancerProposalCrew`: 80–180 words, the client's goal in your words, how you'd approach it, a closing question. It uses **only facts from `candidate_background`**, and has no price or placeholders. When the background doesn't fit the project's core requirement it answers `SKIP: <reason>`, and the project is listed as declined, not drafted | flow + crew |
| Suggest a bid | The competition's average when it falls inside the budget, otherwise the budget's midpoint (in the project's currency) | `freelancer_tool.suggested_bid` |

## Result

```json
{"summary": "…", "projects_found": 96, "drafted_count": 2, "skipped_count": 92,
 "drafted": [{"id", "title", "url", "type", "currency", "budget_min", "budget_max",
              "budget_min_usd", "budget_max_usd", "bid_count", "bid_avg", "skills",
              "suggested_bid", "proposal"}],
 "skipped": [{"id", "title", "url", …, "reason"}], "warnings": [], "note": "Draft-only…"}
```

## Checked live (Nov 2026)

A search for `web scraping, python` (fixed price, at most 60 bids, 2 drafts) found 96
projects, and 11 passed the filters. The LLM declined 7, each with a reason (mobile,
robotics, sales, Power BI, video, Visio, and an anti-cheat memory reader). It drafted 2
proposals using only the stated background.

## Placing bids

Placing bids is opt-in and needs your own Freelancer OAuth token on the server:

```
FREELANCER_OAUTH_TOKEN=...   # personal access token, see "Verifying with a real token"
FREELANCER_API_BASE=...      # optional: https://www.freelancer-sandbox.com to run against the sandbox
```

With `submit_bids` on, the run:

1. **Checks the token first** (`GET /api/users/0.1/self/`), which also gives the bidder id.
   If the check fails, the run is drafts-only and you get a warning.
2. **Bids only on projects it drafted** (`POST /api/projects/0.1/bids/`, JSON body with the
   documented required fields `project_id`, `bidder_id`, `amount`, `period`,
   `milestone_percentage`, plus `description`), up to **Proposals To Draft**. Each bid uses
   the suggested amount, **Delivery Time** and **Milestone %** (Freelancer requires 20–100;
   lower values are raised to 20).
3. **Never sends a bid** whose amount falls outside the project's budget, whose proposal is
   shorter than 100 characters, or whose proposal contains an email address (the API rejects
   it with `BID_DESCRIPTION_CONTAINS_EMAIL`). Proposals are capped at 1,500.
4. **Reports the outcome on every draft** as `drafted[].bid`: `placed` (with `bid_id`),
   `already_bid` (`DUPLICATE_BID`, counted as done), `not_placed` (with the reason) or
   `failed` (the API's message, `error_code` and a hint).
5. **Stops bidding for the rest of the run** on an error every later bid would hit too:
   authentication (`401 NOT_AUTHENTICATED`, `PERMISSION_DENIED`) or an account limit
   (`BID_LIMIT_EXCEEDED` = no bids left, `EMAIL_VERIFICATION_REQUIRED`,
   `RESTRICTED_FROM_BIDDING`, `MUST_RESPOND_TO_EXISTING_PROJECT_AWARD`, `429 RATE_LIMITED`).
   Errors about one project (`PROJECT_NOT_ACTIVE`, `NDA_NOT_SIGNED`,
   `SKILL_REQUIREMENTS_NOT_MET`, `UNLISTED_NOT_PREFERRED`, `BID_AMOUNT_INVALID`, …) fail
   only that bid.
6. **Records placed (and already-placed) bids** in the ledger with `"status": "bid_placed"`,
   so later runs skip them as "already bid on".

Placed bids use your account's bid credits (the membership's monthly bid limit). Start
with **Proposals To Draft** at 1.

> **Freelancer's API policy.** The developer docs list "Automatic Bidders" as a prohibited
> integration (exceptions are sometimes made for tools an agency or freelancer builds for its
> own use). Keep `submit_bids` for your own account, review the drafts, and keep volumes low.

## Verifying with a real token

The bid path has offline tests against the documented request/response shapes, but placing
a bid can only be proven with a real account. `scripts/freelancer_live_check.py` does that
in one command and is **read-only by default**.

### 1. Get a token

- **Production:** sign in, open <https://accounts.freelancer.com/settings/develop> and
  generate a **Personal Access Token**.
- **Sandbox (recommended first):** register at <https://www.freelancer-sandbox.com>, use
  its developer control panel to mark the account email / payment / phone verified, then
  generate a token at <https://accounts.freelancer-sandbox.com/settings/develop>. Post a
  test project from a second sandbox account to have something to bid on.

Tokens are locked to their environment (a sandbox token only works against the sandbox),
you can have one active token per environment, and a personal token is **valid for 30
days**, after which calls answer `401 RestExceptionCodes.NOT_AUTHENTICATED`.

Scopes the job needs (an OAuth app must request them; a personal token acts as you):

| Call | Scopes |
|---|---|
| `GET /api/users/0.1/self/` (token check, bidder id) | `basic`, `fln:user_information` |
| `POST /api/projects/0.1/bids/` (place a bid) | `basic`, `fln:project_manage` |
| `GET /api/projects/0.1/projects/active/` (search) | none |

Your account must also be able to bid on the site: email verified, the project's skills on
your profile, bids left on your membership.

### 2. Run the check

```bash
export FREELANCER_OAUTH_TOKEN=...            # never commit it

uv run python scripts/freelancer_live_check.py                      # production, read-only
uv run python scripts/freelancer_live_check.py --keywords "django"  # a different search
uv run python scripts/freelancer_live_check.py --sandbox            # sandbox token, read-only
```

It reports, without sending anything:

| Check | Passes when |
|---|---|
| `account` | the token is accepted; prints the user id (username masked), email / payment verified, suspended, membership and bid limit, and bids remaining when the token may see it (`account_balances` needs the `financial:get` permission, so it is often "unknown"). Fails if the API says the account cannot bid |
| `search` | the public search returns projects (on the sandbox an empty search is `skipped`) |
| `payload` | the exact body `place_bid` would POST for a real searched project has every documented required field with the right type (printed, description truncated) |

Exit code: `0` all ok, `1` a check failed, `2` `FREELANCER_OAUTH_TOKEN` is not set. The
token is never printed in full.

### 3. Place one real bid (optional)

Only with an explicit project id **and** your own proposal text, and only after you type
the project id at the prompt (it refuses without a terminal):

```bash
uv run python scripts/freelancer_live_check.py --sandbox \
    --place-bid <project_id> --proposal-file my_proposal.txt [--amount 250] [--period 7] [--milestone 100]
```

`DUPLICATE_BID` counts as ok. Anything else prints the API's `error_code` and what it means.
This is the step that proves `fln:project_manage` and the bid body against the live API —
the read-only checks cannot.

## Tests and evals

`tests/unit/test_freelancer_bids.py` runs against a trimmed real API response and the
documented bid / error answers; the flow's LLM is mocked. `tests/unit/test_freelancer_live_check.py`
covers the live-check script offline. Golden eval cases are in `evals/freelancer_bids/` (a project that fits plus
one that doesn't, and budget filtering across currencies). The read-only canary checks the
search endpoint.
