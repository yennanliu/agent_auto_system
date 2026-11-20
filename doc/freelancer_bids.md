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
FREELANCER_OAUTH_TOKEN=...   # Freelancer → developer settings / app token for your account
```

With `submit_bids` on, the run:

1. **Checks the token first** (`GET /api/users/0.1/self/`), which also gives the bidder id.
   If the check fails, the run is drafts-only and you get a warning.
2. **Bids only on projects it drafted** (`POST /api/projects/0.1/bids/`), up to **Proposals
   To Draft**. Each bid uses the suggested amount, **Delivery Time** and **Milestone %**.
3. **Never sends a bid** whose amount falls outside the project's budget, or whose proposal
   is shorter than 100 characters. Proposals are capped at 1,500.
4. **Reports the outcome on every draft** as `drafted[].bid`: `placed` (with `bid_id`),
   `not_placed` (with the reason) or `failed` (the API's message). An authentication error
   stops any further bids in that run; a quota error ("no bids left") fails only that one.
5. **Records placed bids** in the ledger with `"status": "bid_placed"`, so later runs skip
   them as "already bid on".

Placed bids may use your account's bid credits. Start with **Proposals To Draft** at 1.

## Tests and evals

`tests/unit/test_freelancer_bids.py` runs against a trimmed real API response; the flow's
LLM is mocked. Golden eval cases are in `evals/freelancer_bids/` (a project that fits plus
one that doesn't, and budget filtering across currencies). The read-only canary checks the
search endpoint.
