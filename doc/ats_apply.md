# ATS Apply (`ats_apply`)

Auto-apply on employers' job boards across applicant-tracking systems, with no login.
It applies on **Greenhouse, Lever, Ashby and Workable**. **SmartRecruiters** jobs are
discovered and listed for manual apply. A sibling of `greenhouse_apply`: same profile
fields, same LLM gates, same answer rules (shared through `tools/ats_common.py`).

## Boards

`boards` takes any mix of board or job URLs and `platform:token` shorthand, separated by
commas. `tools/ats_router.py` resolves each entry:

| Platform | URL forms | Shorthand |
|---|---|---|
| Greenhouse | `boards.greenhouse.io/<token>[/jobs/<id>]`, `job-boards.greenhouse.io/…` | `greenhouse:figma` |
| Lever | `jobs.lever.co/<site>[/<uuid>[/apply]]` (and `jobs.eu.lever.co`) | `lever:palantir` |
| Ashby | `jobs.ashbyhq.com/<page>[/<uuid>]` (the page name is **case-sensitive**) | `ashby:openai` |
| Workable | `apply.workable.com/<account>[/j/<SHORTCODE>]`, `<account>.workable.com/j/…` | `workable:huggingface` |
| SmartRecruiters | `jobs.smartrecruiters.com/<Company>[/<id>-slug]` (case-sensitive) | `smartrecruiters:BoschGroup` |

A job URL applies to that one job and bypasses the keyword and location filters. Workday
URLs are recognised but not supported, so they show up as a warning. A bare token
(`palantir`) is ambiguous and is rejected.

## Funnel

Each platform is an `Adapter` (`tools/ats_<platform>.py`). The runner
(`tools/ats_apply_tool.run_ats_apply`) does everything else: title and location matching,
round-robin across boards, the "already applied" ledger, the relevance gate, caps, and
dry-run.

| Step | Greenhouse | Lever | Ashby | Workable |
|---|---|---|---|---|
| Discover | Job Board API | Postings API `api.lever.co/v0/postings/<site>` | Job Board API `api.ashbyhq.com/posting-api/job-board/<page>` | `POST apply.workable.com/api/v3/accounts/<acct>/jobs` |
| Read the form (no browser) | `…/jobs/<id>?questions=true` | the server-rendered apply page's HTML (`li.application-question`) | the hosted page's public GraphQL `ApiJobPosting` | `GET apply.workable.com/api/v1/jobs/<code>/form` |
| Fill | react-select by input id | inputs by `name`. Radio and checkbox values are the option texts. Location is an autocomplete | `[data-field-path=<path>]` containers. Yes/No buttons, location combobox, date picker (MM/DD/YYYY) | inputs by id (phone by `name`). The dropdown is a combobox with its own listbox. Booleans are radio buttons valued `true`/`false`. The cookie banner is declined first |
| Confirmed when | `/confirmation` page | `/thanks` page | "Thank you for applying" | "Application submitted" |

Answers are planned **in Python before any browser opens**, using the same rules as
Greenhouse (`ats_common.answer_question`): extra answers, profile defaults, sponsorship
vs. authorization, languages, "decline" for EEO questions, and acknowledgements. A
required question with no confident answer **skips the job**, never guessed. The LLM
answerer only runs with `candidate_background`. Platform specifics:

- **Ashby date questions** ("When can you start?") are answered from the notice period
  (`2 weeks` → today + 14 days) or from an explicit `start date: 2027-01-15` extra answer.
- **Contact fields** on Ashby and Workable are recognised by label
  (`ats_common.standard_for_label`). A sentence that only mentions GitHub or LinkedIn
  ("Share 2–3 contributions (GitHub PRs…)") is treated as a question, not the URL field.
- **Ledger:** `ATS_LEDGER` (default `data/ats_applied.json`), keyed
  `<platform>:<token>/<job id>`. Greenhouse jobs also check and write the
  `greenhouse_apply` ledger, so switching between the two job types never re-applies.
- Radio buttons and checkboxes are ticked and then **verified**. A click that lands on an
  overlay falls back to a DOM click (`ats_apply_tool.tick`).

## SmartRecruiters: discovery only

The Posting API lists jobs, but the one-click apply page sits behind a DataDome bot wall.
Headless browsers get "Access is temporarily restricted"; headed Chrome gets through. The
form is rendered in web components, and there is no public form-definition API, so
questions can't be planned before the browser opens. Matching jobs are returned under
`manual[]` with their `apply_url` instead. That adapter is a follow-up.

## Not verified live

Every adapter has been **dry-run against real boards** (Palantir on Lever, OpenAI on
Ashby, Hugging Face on Workable): the forms were opened and filled and the Submit button
was found. The **post-submit confirmation** for Lever, Ashby and Workable has **not**
been observed on a live site, because that would mean sending a real application. The
success markers (`/thanks`, the "Thank you for applying" and "Application submitted"
texts) come from the platforms' documented flows and are covered by the hermetic e2e
fixtures. When no marker appears, the job counts as `unconfirmed` (not applied), so a
wrong marker under-reports rather than over-reports.

## Env

```
ATS_RESUME_PATH=/abs/path/resume.pdf   # falls back to GREENHOUSE_RESUME_PATH
ATS_LEDGER=data/ats_applied.json
ATS_HEADLESS=0                         # headed real Chrome by default (captchas)
```

## Tests

| File | What |
|---|---|
| `tests/unit/test_ats_apply.py` | router, each adapter's discovery, form parsing and planning against **real captured responses** (`tests/fixtures/ats/`), the shared runner with fake adapters, and the flow |
| `tests/e2e/test_ats_apply_browser.py` | hermetic Chromium runs on locally served copies of each platform's **real form DOM** (`tests/e2e/ats_fixture/`), plus a full `run_ats_apply` funnel through Lever. Checks the exact values submitted |
| `tests/e2e/test_ats_apply_live.py` | opt-in (`ATS_LIVE_E2E=1`) dry-run against the live Lever, Ashby and Workable boards. Never submits |
