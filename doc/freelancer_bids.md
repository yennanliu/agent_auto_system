# Freelancer Bids (`freelancer_bids`)

Finds matching **Freelancer.com** projects and drafts a bid proposal for each, from your
own background. It **never places a bid**: placing one needs your Freelancer account, so
the result is a list of projects, each with a drafted proposal and a suggested bid, for you
to review and submit. This is the draft-only first step of Phase 4 in the job-platform plan.

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

## Not done yet

**Placing bids** needs a Freelancer OAuth token from the user's account (`POST
/api/projects/0.1/bids/`). The drafts and suggested amounts are shaped to feed that step
once a token is available.

## Tests and evals

`tests/unit/test_freelancer_bids.py` runs against a trimmed real API response; the flow's
LLM is mocked. Golden eval cases are in `evals/freelancer_bids/` (a project that fits plus
one that doesn't, and budget filtering across currencies). The read-only canary checks the
search endpoint.
