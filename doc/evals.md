# Eval regression set

A fixed set of inputs with known-good outputs for each automation, so a prompt, model
or flow change gets a measurable quality gate instead of "it looked fine when I ran it".

```bash
uv run python scripts/run_evals.py           # offline tier — no API keys, also runs in PR CI
uv run python scripts/run_evals.py --live    # live tier — real LLM + judge (costs a few cents)
```

| Tier | Runs | Needs | Checks |
|---|---|---|---|
| **Offline** | every PR, as `tests/unit/test_evalset.py` | nothing | case files match the schema · every registered automation has a case **or** a reason in `evals/skip.yaml` · each `reference_output` passes the deterministic validator and its own expectations |
| **Live** | nightly, `.github/workflows/evals-nightly.yml` | a provider API key | runs each case's real LLM step from fixtures → validator + expectations → the existing LLM judge → compares the score with `evals/baseline.json` |

The code is `src/automation/harness/evalset.py`; `scripts/run_evals.py` is a thin CLI over it.

## Layout

```
evals/
  <job_type>/<case-id>.yaml   one golden case (custom:<slug> cases live in evals/custom/)
  skip.yaml                   job_type → why it has no cases
  baseline.json               judge score per case, recorded from a live run
```

## A case

```yaml
id: top3-infra-mix               # must equal the file name
job_type: hacker_news_digest     # must match the directory
description: What this case is for and what would make it fail.
payload: {limit: 3}              # exactly what the job would be created with
fixtures:                        # stands in for every tool / scrape / upload
  tools:
    hn_top_stories: [...]        # the tool's canned return value
expect:
  valid: true                    # validator verdict (default true)
  contains: ["Tidepool"]         # must-contain facts (case-insensitive, anywhere in the output)
  not_contains: ["I cannot"]
  assertions:                    # structural checks on a dotted path; [*] fans out over a list
    - {path: "stories", type: list, min_len: 3, max_len: 3}
    - {path: "stories[*].summary", type: str, non_empty: true}
    - {path: "stories[0].score", equals: 912}
  min_judge_score: 70            # live tier: absolute floor, independent of the baseline
reference_output: {...}          # a known-good result; the offline tier holds it to `expect`
```

Assertion checks: `type` (`str|int|number|bool|list|dict`), `equals`, `one_of`, `non_empty`,
`min_len`/`max_len`, `min`/`max`.

Fixtures per covered automation (the *adapter* in `evalset._ADAPTERS` defines them):

| Job type | Fixture | What runs for real |
|---|---|---|
| `hacker_news_digest`, `web_scraper`, `google_sheet_reader` | `tools.<tool_name>` — the tool's return value | the flow + crew; the tool's `_run` is stubbed |
| `profit_health_check` | `upload` — `sales.csv`/`cost.csv`(/`ads.csv`/`returns.csv`) as text | the flow, the 4-agent crew and the real `profit_calc` (PDF render stubbed). The report's numbers and flags must equal `profit_calc`'s for the fixture, offline and live |
| `email_collect` | `businesses` — discovered rows, each with its `emails` | the funnel and the qualify crew; discovery, site extraction and email verification are stubbed |
| `custom:<slug>` | `custom` — `{name, instructions, output_hint, temperature}` | the same no-tools `DynamicCrew` the executor uses (the definition comes from the case, not the DB) |

**All fixture data must be synthetic**: invented companies and people, `example.com` /
`*.example` hosts, `555-01xx`-style phone numbers. Never paste a real scrape.

## Adding a case

1. Copy a neighbouring file in `evals/<job_type>/`; set `id` to the new file name.
2. Write the fixture, then the expectations a correct output must meet. Prefer facts the
   model could get wrong (a number, a count, a name that must survive) over ones any output
   satisfies. Two or three sharp cases per automation beat ten thin ones.
3. Write `reference_output` — the easiest way is a live run of just that case
   (`--live --only <job_type>/<id> --report-dir /tmp/r`, then copy `output` from
   `report.json` after checking it by eye).
4. `uv run python scripts/run_evals.py` until it is green.

## Adding an automation

A new `AutomationSpec` with no case and no skip fails `tests/unit/test_evalset.py`. Either:

- **cover it** — add an adapter to `_ADAPTERS` in `evalset.py` (for a single-crew flow with
  one tool this is one `_tool_flow(...)` line) and 2–3 cases; or
- **skip it** — add `job_type: reason` to `evals/skip.yaml`. The reason must say why its LLM
  step cannot be replayed from fixtures; the skip list is the visible backlog of what the
  eval set does not protect.

## The live tier

- **Run model**: `--provider/--model`, else `EVAL_RUN_PROVIDER`/`EVAL_RUN_MODEL`, else the
  first of openai → gemini → anthropic with a key, at that provider's default model.
- **Judge**: `evaluator.evaluate()`, unchanged — so the independence rule holds (configured
  judge → a sibling model in the run's provider → the run's own model as a last resort).
  Pin one with `EVAL_JUDGE_PROVIDER`/`EVAL_JUDGE_MODEL`. A result that could only be scored
  heuristically or by the run model itself is reported **unscored**: it fails the run and is
  never compared with, or written to, the baseline.
- **Settings**: the live tier uses a throwaway settings DB, so keys and the judge come from
  the environment (`.env` included), not from Admin → Settings.
- **A case fails** when the run raises, the validator or an expectation fails, the score is
  under `min_judge_score`, or the score is more than `--tolerance` (default 10) points below
  its baseline. One run per case, so the tolerance has to absorb judge noise; tighten it
  only after watching a few nightly reports.
- **Report**: `--report-dir DIR` writes `report.md`, `report.json` (with every output) and
  `baseline.candidate.json`. The nightly workflow uploads them as the `eval-report` artifact
  and puts the table in the run summary.

### Baseline

`evals/baseline.json` holds the judge score per case, plus the run model it was recorded
with. A case with no entry is reported as `new` and cannot regress — only its
`min_judge_score` floor applies — so the file starts empty and the gate tightens once a
baseline is committed. A baseline recorded with a different run model or judge is not
compared (`skipped: …` in the report).

To record or refresh it:

```bash
uv run python scripts/run_evals.py --live --update-baseline      # all cases
uv run python scripts/run_evals.py --live --update-baseline --only web_scraper
git diff evals/baseline.json     # review, then commit
```

or download `baseline.candidate.json` from a nightly run's artifact and commit it as
`evals/baseline.json`. Use the same run model and judge as the nightly job. Refresh the
baseline deliberately — after an intended prompt or model change, with the before/after
scores in the PR — never just to turn a red night green.

### Nightly workflow

`.github/workflows/evals-nightly.yml` runs on a schedule and on manual dispatch (optional
`provider`, `model`, `only` inputs). It is not part of PR CI.

- Secrets: at least one of `OPENAI_API_KEY`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`. With one
  key the judge is a sibling model of the same provider; with two (for example OpenAI to run
  and Gemini to judge) it is a different vendor, which is the better setup.
- Optional repository variables: `EVAL_RUN_PROVIDER`, `EVAL_RUN_MODEL`,
  `EVAL_JUDGE_PROVIDER`, `EVAL_JUDGE_MODEL`.
- With no key secret set, the job prints a notice and finishes green.
