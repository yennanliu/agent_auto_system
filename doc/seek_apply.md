# Seek / JobStreet Quick Apply (`seek_apply`)

One adapter for every market on Seek's platform: **Seek AU / NZ** and **JobStreet SG / MY /
PH / ID** (JobsDB HK / TH answer the search API with HTTP 403, so they are not offered). It
applies with **your own logged-in account** through Seek's Quick Apply wizard. Phase 5 of the
job-platform plan.

| | |
|---|---|
| Spec | `seek_apply` in `src/automation/spec.py` (manifest form, `dry_run` on by default) |
| Flow | `src/automation/flows/seek_apply_flow.py` (deterministic funnel) |
| Tool | `src/automation/tools/seek_apply_tool.py` |
| Login | `uv run python scripts/seek_login.py --site au` (or Admin → Sessions → *Seek* / *JobStreet*) |
| Sessions | `data/seek_state.json` (AU / NZ), `data/jobstreet_state.json` (SG / MY / PH / ID) — git-ignored |
| Ledger | `data/seek_applied.json`, key `seek:<job id>` (one id space across all markets) |
| Canary | HTTP: a live job id from the AU search API → Seek's GraphQL `jobDetails` (both GETs) |
| Evals | skipped (`evals/skip.yaml`, same reason as the other browser auto-apply types) |

## Funnel

1. **Search** — `ats_sources.seek_search` (the same public search API the `seek_au` /
   `jobstreet_*` discovery sources use), one search per keyword phrase (max 5), up to
   `max_pages` pages of 50. A job is kept when its title contains every word of a phrase.
2. **Ledger** (applied / pending → skip) and the optional **relevance gate** (fail-open).
3. **Job details without a browser** — Seek's GraphQL answers a plain GET with no login and no
   Cloudflare check. It tells:
   * `isExpired` / `status` → skip;
   * `isLinkOut` → the job applies on the employer's own site (step 5);
   * `products.questionnaire.questions` → the employer's questions (labels only). A question the
     shared rules refuse to guess skips the job before any browser opens.
4. **Quick Apply** in a headed Chrome with the saved session, at `{site}/job/<id>/apply`:
   * *Choose documents* — keep Seek's pre-selected résumé, or pick a saved one whose name contains
     `resume_name`; "Write a cover letter" with `cover_letter` (`{company}` / `{title}` filled in),
     else "Don't include a cover letter".
   * *Employer questions* — read from the page (select / radio / checkbox group / text), planned
     with `ats_common` (the same answer rules as Greenhouse / ATS Apply, `extra_answers`, the saved
     `data/default_answers.txt`, and the LLM only with `candidate_background`), with Seek's own
     rules first:
     * **Right to work** ("Which statement best describes your right to work in Australia?") is
       answered only from your Extra Answers, or — when the market's country is not in
       `authorized_countries` — with the sponsorship statement. Never inferred otherwise: the
       shared planner would read an inferred "No" into "…visa with **no** restrictions".
     * **Notice period** → `notice_period` mapped onto the options; **salary** → the band nearest
       `salary_expectation` (in the job's currency).
     * Any required question still unanswered → the job is **skipped** and listed with the
       question, so you can add it to Extra Answers.
   * *Update profile* — continued past, never edited.
   * *Review* — `dry_run` stops here (`prepared`). Otherwise the ledger marks the job **pending**
     right before Submit; only Seek's success page turns it into **applied**. An unconfirmed
     submit stays pending and is never resent.
   * Sign-in page → the session expired, the run stops. Cloudflare page that doesn't clear →
     the run stops. Any page the walker can't classify → that job is skipped with a warning
     (nothing is sent).
5. **Link-out jobs** — an ATS *job* link the ad presents as how to apply, else the page Seek's
   apply link lands on, goes through `ats_router.detect_ats`. A supported-ATS job is handed to
   `run_ats_apply` with the remaining budget (needs `first_name`, `last_name`, `email` and a
   résumé file — its ledger is keyed by the ATS's canonical job id); anything else is a
   `manual[]` lead.

## Needs a live check

There was no Seek account on the machine this was built on, and the job / apply pages are behind
Cloudflare for anything but a real browser, so **everything inside the logged-in wizard is
unverified**. Verified live (Nov 2026): the search API, GraphQL `jobDetails` (link-out flag,
expiry, questionnaire, content) and the canary.

Unverified, all kept in one place in `seek_apply_tool.py`:

| What | Where |
|---|---|
| Apply URL `{site}/job/<id>/apply` and the step paths (`/apply`, `/role-requirements`, `/profile`, `/review`, `/success`) | `_STEP_PATHS` (headings in `_STEP_HEADINGS` are the fallback) |
| Continue / Submit buttons | `_CONTINUE_SELECTORS`, `_SUBMIT_SELECTORS` |
| Success page | `_SUCCESS_URL_PARTS`, `_SUCCESS_TEXTS` |
| Documents step (résumé select, "Write a cover letter" radio + textarea) | `_DOCUMENTS_JS`, `PlaywrightWizard.choose_documents` |
| Question markup (labels / legends / `aria-labelledby`, required markers) | `read_questions`, `_FILL_JS` |
| Signed-in vs signed-out header; sign-in URL `/oauth/login` | `looks_logged_out`, `browser_session._seek_logged_in`, `_LOGIN_HOSTS` / `_LOGIN_PATHS` |
| Where a link-out job's apply link lands | `PlaywrightWizard.resolve_link_out` |
| Whether one JobStreet login covers SG / MY / PH / ID | separate sessions per family are assumed |

To check it:

```bash
uv run python scripts/seek_login.py --site au      # sign in by hand; saves data/seek_state.json
```

Then run **Seek Quick Apply** with `dry_run` on (the default), 1–2 applications, and read the
run log: each job logs its wizard steps (`· step: documents`, `· step: questions`, …). A
`skipped — unrecognised Quick Apply page (...)` entry names the path and headings to add to the
tables above. Save one page of each step (`page.content()`) to replace the synthetic fixtures in
`tests/fixtures/seek/`. Only then try a live run with `max_applications: 1`.

## Limits

* One run per type at a time (one browser session per site — `per_type_default.browser`).
* No daily-limit detection: Seek publishes none. Keep `max_applications` small.
* JobsDB HK / TH are not supported (their API answers 403).
