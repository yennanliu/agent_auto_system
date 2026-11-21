# Greenhouse Apply (`greenhouse_apply`)

Auto-apply to jobs on employers' Greenhouse job boards. Sibling of `tw104_apply` and
`linkedin_apply`, but with no login: Greenhouse is an applicant-tracking system, so every
employer has a public board (`boards.greenhouse.io/<token>`) and there's no candidate
account to authenticate.

## Funnel

| Step | How | Where |
|---|---|---|
| Discover | Public Job Board API `GET /v1/boards/<token>/jobs`, filtered by title keywords and location, interleaved across boards | `discover_jobs` |
| Dedupe | Local ledger of submitted jobs (`GREENHOUSE_LEDGER`, default `data/greenhouse_applied.json`). The site can't tell us "already applied" without a login. A submission whose confirmation never appeared (`unconfirmed`, or `needs_verification` when Greenhouse e-mailed a code) is recorded as `"status": "pending"`. It is **never resent automatically** (check by hand, then delete the entry to retry) and it counts toward `max_applications`. The ledger is shared with `ats_apply`'s Greenhouse jobs | `load_ledger` / `record_applied` / `ledger_status` |
| Gate (optional) | `task_filter` → `GreenhouseRelevanceCrew`, fails open | flow `relevance_fn` |
| Plan | `GET /v1/boards/<token>/jobs/<id>?questions=true` returns every form question (label, required, field name, options). Answers are planned **in Python before the browser opens** | `plan_answers` |
| Apply | Playwright fills `job-boards.greenhouse.io/embed/job_app?for=<token>&token=<id>` (never redirects to the employer's own careers site), uploads the resume, clicks Submit | `_apply_to_job` |
| Confirm | Counted only on the `/confirmation` page or a "Thank you for applying" message. An e-mailed security code → `needs_verification` | `_apply_to_job` |

## Answer planning

The rules below are the shared, platform-neutral core in `src/automation/tools/ats_common.py`
(reused by every ATS adapter). Greenhouse-specific parts (standard fields, résumé / cover letter,
education / employment blocks, EEOC and demographic surveys) stay in `greenhouse_apply_tool.py`.

Rules, in order, applied to each custom question's label:

1. `extra_answers`: `keyword: answer` lines from the user. These win.
2. **Default answers**, each a field on the run form with a default:
   salary expectation (`70000 SGD / year`), current / last drawn salary (blank),
   notice period (`1 month`), spoken languages (`English, Chinese`), programming
   languages (`Python, Java, JavaScript`), AI / LLM experience (`5 years`), reason
   for leaving, and a criminal-record flag (off). Spoken languages pick every
   matching option (Chinese matches Mandarin, never Cantonese) and answer "Can you
   speak X fluently?" Yes/No. Programming options that pair a language with a level
   ("Java Advanced") are only picked for entries written with one
   (`Python: Advanced`) — never a guessed proficiency.
   From the profile: City / State / Country, "are you located in X?", and citizen /
   PR / residency-status questions for a country outside `authorized_countries`
   (answered No / Others). A sponsorship question about "the country where you
   currently reside" is judged on where you are based, not on the job.
3. Built-in rules: LinkedIn / GitHub / website URLs, sponsorship vs. work authorization
   (they're kept apart: "authorized … *without* sponsorship" is an authorization
   question), "worked here before" → No, how-did-you-hear, years of experience
   (mapped onto range options), expected salary (a "last drawn / current salary" question uses its own field, never the expectation), location, preferred/full name, consent.
4. A **required single-option select** ("I agree", "Aceito os termos…") is an
   acknowledgement, so the one option is picked.
5. Required EEOC/demographic selects → the "decline to self-identify" option, never a
   guessed identity.
6. If `candidate_background` is set, `GreenhouseAnswerCrew` answers the remaining
   **required** questions from that text only. It returns `confident: false` to abstain.

Any required question still unanswered → the job is **skipped** with those questions
listed in `reason` / `unanswered`, so the user can add them to `extra_answers`. Optional
questions without a confident answer are left blank.

## DOM notes (verified against live boards, Oct 2026)

- Input ids equal the API field names (`first_name`, `question_12497125004`, …).
- Selects are react-select comboboxes. Options have ids `react-select-<field>-option-N`
  and **must be scoped by that prefix**: the phone widget's hidden country list also
  uses `role=option`.
- The form is server-rendered and then hydrated. Filling before hydration finishes is
  silently reset (values and the uploaded resume vanish), so `_open_form` waits for
  network idle first.
- The location autocomplete returns nothing for "Taipei, Taiwan" but does for "Taipei",
  so only the first comma part is typed.
- Forms carry invisible reCAPTCHA, so the browser runs headed real Chrome by default
  (`GREENHOUSE_HEADLESS=1` to opt out).

## Env

```
GREENHOUSE_RESUME_PATH=/abs/path/resume.pdf   # default when the run doesn't set resume_path
GREENHOUSE_LEDGER=data/greenhouse_applied.json
GREENHOUSE_HEADLESS=0
```

## Finding boards by country

`scripts/greenhouse_country_boards.py` builds the "which Greenhouse companies hire engineers
in <country>" lists (issues #74–#78, #102–#104) whose `boards:` line is pasted into a job.

```bash
uv run python scripts/greenhouse_country_boards.py scan                 # ~25 min; caches data/greenhouse_boards.json
uv run python scripts/greenhouse_country_boards.py report spain netherlands
uv run python scripts/greenhouse_country_boards.py report hong_kong --create-issue
uv run python scripts/greenhouse_country_boards.py report --list        # country presets
```

`scan` checks ~13.7k candidate board names (harvested from public job-list repos) against the
public Greenhouse jobs API; `report` filters that cache by location + engineering title, so any
number of countries come from one scan. Keep the scan slow: the API answers **403 for every
board** for several minutes once an IP goes too fast (40 concurrent requests tripped it after
~9k calls). The script pauses and retries on a block; `scan --resume` continues an interrupted
run. A country without a preset: `report --name France --match "france|paris" --location "Paris, France"`.
