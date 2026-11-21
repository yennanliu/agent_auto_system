# ATS Apply (`ats_apply`)

Auto-apply on employers' job boards across applicant-tracking systems, with no login.
It applies on **Greenhouse, Lever, Ashby, Workable and SmartRecruiters**, and on **Workday**
at employers where you have an account and stored its password. Workday jobs at employers
without a stored account are discovered and listed for manual apply. A sibling of `greenhouse_apply`: same profile
fields, same LLM gates, same answer rules (shared through `tools/ats_common.py`).

## Boards

`boards` takes any mix of board or job URLs and `platform:token` shorthand, separated by
commas. `tools/ats_router.py` resolves each entry:

| Platform | URL forms | Shorthand |
|---|---|---|
| Greenhouse | `boards.greenhouse.io/<token>[/jobs/<id>]`, `job-boards.greenhouse.io/…` | `greenhouse:figma` |
| Lever | `jobs.lever.co/<site>[/<uuid>[/apply]]` (and `jobs.eu.lever.co`) | `lever:palantir` |
| Ashby | `jobs.ashbyhq.com/<page>[/<uuid>]` | `ashby:openai` |
| Workable | `apply.workable.com/<account>[/j/<SHORTCODE>]`, `<account>.workable.com/j/…` | `workable:huggingface` |
| SmartRecruiters | `jobs.smartrecruiters.com/<Company>[/<id>-slug]` | `smartrecruiters:BoschGroup` |
| Recruitee | `<company>.recruitee.com[/o/<slug>]` (custom careers domains: use the shorthand) | `recruitee:channable` |
| Teamtailor | `<company>.teamtailor.com[/jobs/<id>-<slug>]` (custom careers domains: use the shorthand) | `teamtailor:anyfin` |
| Personio (discovery) | `<company>.jobs.personio.de[/job/<id>]` (or `.com`) | `personio:clark` |
| BambooHR (discovery) | `<company>.bamboohr.com/careers[/<id>]` | `bamboohr:scribd` |
| Workday | `<tenant>.<wdN>.myworkdayjobs.com/[<lang>/]<site>[/job/…]`, `<wdN>.myworkdaysite.com/recruiting/<tenant>/<site>/…` | `workday:ngc.wd1/Northrop_Grumman_External_Site` |

A job URL applies to that one job and bypasses the keyword and location filters. A bare
token (`palantir`) is ambiguous and is rejected.

## Discovery sources (`sources`)

Besides boards you name, `sources` finds jobs elsewhere (`tools/ats_sources.py`). Each
source splits what it finds in two:

- **links to a supported ATS** are added to the run like a board would be. Unlike a job URL
  you paste, they are **still filtered** by `keywords` and `location`. When a post links
  specific jobs, only those jobs are used, not the company's whole board;
- **everything else** (a careers page, an e-mail address, a job-board listing) is
  returned in `manual[]` with `source` and a `source_url` linking back.

`boards` is optional when `sources` is set. A run that finds only manual leads returns
them without needing a résumé.

| Source | Where | What it yields (checked live, Nov 2026) |
|---|---|---|
| `hn` | the latest "Ask HN: Who is hiring?" thread (official Algolia API) | The best source. Of 243 posts, 36 linked a supported ATS directly (mostly Ashby, then Lever, Greenhouse, Workable) and 74 gave an e-mail address. Keywords and location are matched against the whole post. Posts marked "no longer hiring" are skipped. |
| `remoteok` | `remoteok.com/api` | **Discovery only.** The apply button (`/l/<id>`) redirects back to Remote OK even in a real browser. 2 of 97 listings had an ATS link in their text, and those get routed. |
| `remotive` | `remotive.com/api/remote-jobs` (one `search=` per keyword, up to 5, merged) | **Discovery only.** The employer link isn't on the job page. 1 of 16 listings had an ATS link in its text. |
| `weworkremotely` | the programming and devops RSS feeds | **Discovery only.** Job pages need a job-seeker account and sit behind a Cloudflare check. The RSS descriptions had no ATS links. |
| `seek_au`, `seek_nz`, `jobstreet_sg`, `jobstreet_my`, `jobstreet_ph`, `jobstreet_id` | Seek's search API (`/api/jobsearch/v5/search`, one per market; your first location is sent as Seek's `where`) | **Discovery only.** It's one platform: Seek in AU/NZ, JobStreet in SE Asia. Applying is Seek's own logged-in Quick Apply, and job pages are behind a bot challenge, so every match is a manual lead linking to the job. The site's country is added to each location (JobStreet SG says just "Central Region"). The API is internal and undocumented, so a run makes at most 5 searches per market. **JobsDB HK / TH** answer with HTTP 403 and aren't included. |

**Which links count.** On a job-board listing (`remoteok`, `remotive`, `weworkremotely`),
an ATS link is routed only when it is **presented as how to apply** ("Apply here",
"To apply: …"). Links to other roles ("take a look at our Senior DevOps position") and
footers (an EEO or AAP policy, privacy notice or benefits page) are not routed, and the
listing stays a manual lead. Real listings had both: one employer's "AAP Policy Statement"
footer pointed at a *different* Lever job. HN hiring posts are about hiring, so their ATS
links route unless they are boilerplate. Links to an ATS we can't apply on stay in the
lead as a manual link.

The job boards' API terms ask for a link back and credit. Every lead keeps the board's own
job URL and names the board in `reason`.

**Not built:** **Built In** only shows the employer link to signed-in users (`applyUrl` is
empty when signed out), and its Apply button opens a login popup. **Google Jobs** has no
official API, and the usual route (SerpAPI) needs a paid key, so it couldn't be tested
here. A new source is one function that returns `{"refs", "manual", "warnings"}`,
registered in `SOURCES`.

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

## Recruitee: planned from the API, filled by question text

No account. The careers-site API (`<company>.recruitee.com/api/offers/` and
`/api/offers/<slug>`) lists every offer and describes its whole form, so it is planned
**before** the browser opens, as on Lever / Ashby / Workable:

- built-ins: `options_cv`, `options_cover_letter`, `options_phone`, `options_photo`, each
  `required` / `optional` / `off`. A required one the profile can't fill skips the job; a
  required photo always does.
- `open_questions`: `boolean` (a Yes/No pair), `single_choice` / `multi_choice` (one radio or
  checkbox per option), `string` / `text` / `salary` (text; a `salary` box is `type=number`,
  so only the amount is typed). `infobox` is skipped; `file`, `video` and `date` are never
  guessed (required → skipped).

The hosted form names its inputs after the API (`candidate.name`, `candidate.email`,
`candidate.phone`, `candidate.cv`, `candidate.coverLetterFile`, `candidate.agreements.<n>.consent`),
**but its question ids are the form's own, not the API's** (they are server-rendered and no
API exposes them). Each API question is therefore found by its **text**: the hidden
`candidate.openQuestionAnswers.<form id>.openQuestionId` input's parent starts with the
question. Only an exact match, or a long (40+ characters) first paragraph, counts, and it
must be unique; otherwise the job fails rather than filling the wrong question. Typing the
phone as `+886…` sets the country picker. Only **required** consent boxes are ticked.

Custom-domain careers sites may show a Cookiebot banner late, with only "Manage
preferences" / "Allow all". The adapter opens the preferences and saves with every optional
category off (never "Allow all"), again just before Send, since the banner covers it.

Some employers (bunq) send applicants to their own careers site instead of the hosted
form: that job fails with "form did not load".

**Verified live (dry run, Channable, 2026-11-20):** planned from the API, then every field
filled and the Send button found, not clicked. The confirmation after Send hasn't been
observed live.

## Teamtailor: planned from the server-rendered form

No account. Jobs come from the career site's JSON Feed (`<company>.teamtailor.com/jobs.json`:
`url` = `…/jobs/<id>-<slug>`, `title`, and a schema.org `_jobposting` with the location).
`<job url>/applications/new` returns the application form as server-rendered HTML, so it is
planned **before** the browser opens:

- built-ins `candidate[first_name|last_name|email|phone]` (required when the label says
  "Required"), the résumé upload (`forms--inputs--upload`, `…-required-value`), the cover
  letter, and consent boxes;
- questions: each `[data-question-mandatory]` block, with a hidden `question_id` and a
  `forms--inputs--<kind>` control: `text` (`[text]` / `[number]`), `boolean` (Yes/No),
  `choice` (radios, or checkboxes when multiple choice). Anything else is never guessed.

In the browser each question is found by its `question_id`, the résumé goes into the upload
component's file input, and only the **required** consent box is ticked ("contact me about
future jobs" and SMS consent are left alone). The hidden `verify_token` / `ctoken` fields
are set by the page's own script, as for any visitor.

**Verified live (dry run, Anyfin *Site Reliability Engineer*, 2026-11-21):** planned over
HTTP, filled, résumé uploaded, Submit found and not clicked. A long "Background check?"
question there showed a shared-rule bug: the text mentions "criminal record", so the
criminal-record rule answered **No**, but the question asks whether you *accept* the check.
That rule now answers only questions about having a record. The confirmation after Submit
hasn't been seen live.

## Personio and BambooHR: discovery only (a bot check guards the form)

Both have public job feeds, so their matching jobs are found and filtered like any other
board, then listed under `manual[]` with the apply link and the reason:

- **Personio**: `https://<company>.jobs.personio.de/xml` (some boards use `.com`; both are
  tried). Only each `<position>`'s direct children are read, so a description heading such
  as "DEIN TEAM" is never taken for a job title. The hosted form is simple and
  server-rendered, but **Submit stays disabled until Cloudflare Turnstile issues a token**,
  which it doesn't do for an automated browser (checked live on Clark and 1KOMMA5°,
  2026-11-21).
- **BambooHR**: `https://<company>.bamboohr.com/careers/list` (JSON; an unknown company
  redirects to bamboohr.com). `/careers/<id>/detail` even describes the form, but it has a
  visible **reCAPTCHA "I'm not a robot" checkbox** next to Submit (checked live on Scribd).
  The form also has a honeypot field ("Please leave this field blank").

A CAPTCHA or bot check is **never solved or bypassed** by the automation, so these stay
discovery-only.

## SmartRecruiters: guest apply, planned in the browser

No account is needed. The Posting API lists jobs; each posting's `applyUrl` opens the
one-click apply page. That page sits behind DataDome: **headless browsers get "Access is
temporarily restricted", headed Chrome gets through** (the default, `ATS_HEADLESS=0`). A
blocked job is reported as `blocked` and not retried. When DataDome shows its interactive
check ("Verification Required", a slider in a `captcha-delivery.com` iframe), the job goes to
`manual[]` with its link: **the automation never solves or bypasses it**, and DataDome tends
to reject even a person's slide inside the automated browser. Apply in your own browser.
(`ATS_HUMAN_CHECK_WAIT` lets a headed run wait for a person, but expect it to fail once the IP
has been flagged.) Keep SmartRecruiters runs small: repeated automated visits get the IP
flagged. There is no public form-definition API
and the screening questions are rendered into the page, so the adapter plans in the browser
(`plans_in_browser`), as on Workday:

1. **Page 1.** The résumé goes into the *Resume* drop zone **first**: SmartRecruiters parses
   it and rewrites name / e-mail / phone, so the profile's values are typed after it. The
   city is an autocomplete: only a suggestion naming that city (and country, when the
   profile gives one) is picked, never "London, Ontario" for London, UK. The phone is typed as
   the full international number. LinkedIn / website, and the cover letter as the message to
   the hiring team. *Next*.
2. **Screening questions** (`/screening`, when the job has any). Each `sr-question-field-<type>`
   (TEXT, SELECT incl. multi-select, RADIO, LANGUAGES, INFO) is read and answered with the
   shared rules. **Diversity questions** (age, gender, ethnicity, disability, first
   generation, veteran, …) only ever get the "prefer not to answer" option. A required
   question with no confident answer skips the job and is listed in `unanswered`; add it to
   `extra_answers`. The required privacy-notice box is ticked; other boxes are left alone.
3. **Submit** unless dry-run (`prepared`, stopped before Submit). Counted only on the
   confirmation page.

Learned on a live posting (Wise): the `spl-*` web components have open shadow roots (Playwright
pierces them); option and radio text is the element's `label` **property**, not an
attribute; `spl-radio` / `spl-checkbox` are the controls themselves (role + `aria-checked`,
no inner `<input>`); select options live in the menu named by the input's `aria-controls`.
`tests/e2e/test_smartrecruiters_apply_browser.py` runs the flow, submit included, against a
local stand-in with those shapes. **A real submission hasn't been made yet**, so the
confirmation check (a `confirmation` / `success` path, or "Thank you for applying") is
verified only on the stand-in.

## Workday

Workday has the most employer boards of any ATS: about 1,300 distinct boards in the
SimplifyJobs listings alone. Its job list is public:

```
POST https://<tenant>.<wdN>.myworkdayjobs.com/wday/cxs/<tenant>/<site>/jobs
{"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "..."}
```

`tools/ats_workday.py` uses it:

- **Search, not full listing.** It sends the run's keywords as `searchText`, at up to 100
  jobs per term, instead of paging through boards with thousands of jobs. Role shorthand is
  expanded first, as the title filter does (`swe` → "software engineer", …). Keywords are
  taken one alternative each per round, de-duplicated, up to 5 terms. A Workday job link
  searches its requisition id, and is cached per job, not as the whole board.
- **Throttling.** Workday rate-limits **across companies** with HTTP 429 and no
  `Retry-After` header. Pages are paced 1 s apart, and a 429 backs off (5, 10, 20, 40 s).
  403, 404, 410 and 422 mean the board is disabled, unknown or closed, and are never
  waited on.
- **Applying needs an account per employer.** Workday has no guest apply: every
  employer (tenant, e.g. `3m.wd1`) has its own candidate account.
  - **Opt-in creation:** with **Create Workday accounts** (`create_workday_accounts`) on, a
    missing account is created with the run's `email` and a generated 20-character password.
    The e-mail and password are saved to **`data/workday_accounts.json`** *before* the form is
    sent, so the account can't be lost. The file is mode 0600 and under `data/`, which is
    git-ignored (a unit test checks this). **Never commit or push it.** If Workday asks to
    verify the e-mail, the job reports `needs_verification`; click the link, and the next
    run signs in. An e-mail that already has an account there fails with a pointer to store its
    password. This happens in a dry run too: creating the account is the one step a dry
    run doesn't hold back.
  - **Or create it yourself** on the employer's careers site with the run's `email`, then
    store its password:

  ```bash
  uv run python scripts/workday_password.py set 3m.wd1   # prompts; encrypted with APP_SECRET
  ```

  Passwords are looked up in this order: the env var `WORKDAY_PASSWORD_3M_WD1`, the
  encrypted settings store, `data/workday_accounts.json`, then (macOS) a Keychain item with
  service `agent-auto-system workday 3m.wd1` and the e-mail as account. Without opt-in
  creation, jobs at an employer with no stored password go to `manual[]` with their link and
  the reason. No résumé is needed for a
  run whose only matches are such jobs.
- **The questions are read after sign-in.** Workday shows them only inside the wizard, so
  the adapter plans in the browser (`plans_in_browser`), one step at a time: sign in, open
  the job (or **Continue Application** for a saved draft), then on each step read every
  visible `formField-*` (label, kind, options), answer it with the shared rules, and *Save
  and Continue*. Workday's own fields get defaults: Phone Device Type "Mobile"; Country
  and "How did you hear" come from the shared rules. Only a **required** terms /
  acknowledgement box is ticked, and demographic questions get the decline option.
- **Nothing is guessed.** A required question with no confident answer stops the job at
  that step (`skipped`, listed in `unanswered`). Workday keeps the draft, so add the answer
  to `extra_answers` and the next run resumes it. The résumé is uploaded only if the draft
  has none yet.
- **Saved default answers.** Standing answers to personal questions (address, "bonded",
  "academic ranking", salary…) can be kept in **`data/default_answers.txt`**, in the same
  `keyword: answer` lines as `extra_answers` (`#` starts a comment). Every ATS run applies
  them after its own `extra_answers`, so a run's own answer wins. The file is personal data
  under the git-ignored `data/` (override the path with `ATS_DEFAULT_ANSWERS_FILE`); tests
  never read it.
- **Locale specifics (Micron, Taiwan, 2026-11-21).** "Given / Family Name - Western Script"
  is the profile's name (the *Local Script* one is left alone). Next to the *Country Phone
  Code* widget, Phone Number takes the national number: "+886 963335868" is typed as
  "963335868", or Workday rejects it as an invalid format. A required *Upload a file*
  box counts as answered once the résumé is attached.
- **Dry run stops at Review.** Otherwise Submit counts only on Workday's confirmation; an
  unconfirmed submit is recorded `pending`, as on the other platforms.
- **Verified live on 3M** (2026-11-20): sign-in, resuming a draft, all steps, stopping at
  Review (dry run), and a **real submission**. Workday confirmed it; Candidate Home lists it
  *In Process*, and the job page then shows "You applied for this job" with no Apply button.
  On Review, Submit is the same footer button as *Save and Continue* (`pageFooterNextButton`)
  with the label "Submit", so the adapter clicks the footer button whose text is Submit.
  `tests/e2e/test_workday_apply_browser.py` covers the whole flow against a local stand-in
  built from the same hooks.

## Unconfirmed submissions

If Submit is clicked but no confirmation appears, the employer **may still have
received the application**. Sending it again on a later run could duplicate it. So such
a job (`unconfirmed`, or `needs_verification` when the board e-mailed a code):

- is written to the ledger with `"status": "pending"`. Later runs skip it with
  "submitted on an earlier run but never confirmed", and **never resend it
  automatically**. Check your e-mail or the employer's portal, then delete the entry from
  the ledger if you want it retried;
- is **not** counted in `applied` or `submitted_count`; `unconfirmed_count` reports it;
- **does** use up `max_applications`, so an uncertain submission can't push a run past
  its cap.

Confirmation is matched on the platform's own route (a whole URL path segment such as
`thanks`, never a substring that a company slug could contain) or on its confirmation
text.

## Not verified live

Every adapter has been **dry-run against real boards** (Palantir on Lever, OpenAI on
Ashby, Hugging Face on Workable): the forms were opened and filled and the Submit button
was found. The **post-submit confirmation** for Lever, Ashby and Workable has **not**
been observed on a live site, because that would mean sending a real application. The
markers (`/thanks`, the "Thank you for applying" and "Application submitted" texts) come
from the platforms' known flows and are covered by the hermetic e2e fixtures. A wrong
marker makes a real application show up as `unconfirmed`. It is still blocked from being
sent again (see above), so the cost is a manual check, not a duplicate.

## Finding boards by country

`scripts/ats_country_boards.py` is the Lever, Ashby, Workable and Workday counterpart of
`greenhouse_country_boards.py`. It uses the same country presets and engineering-title
filter.

```bash
uv run python scripts/ats_country_boards.py scan                  # all four platforms; data/ats_boards.json
uv run python scripts/ats_country_boards.py scan --platform workday   # paced: 2 workers, 2 s, searches "engineer"
uv run python scripts/ats_country_boards.py scan --platform workable --limit 50
uv run python scripts/ats_country_boards.py report taiwan uk      # data/ats_reports/<country>_<date>.md
```

- **Where candidates come from:** the job-board-aggregator Lever and Ashby lists, plus
  every Lever, Ashby and Workable link in the SimplifyJobs and hiring-without-whiteboards
  repos (resolved by `ats_router`).
- **How each board is checked:** with the platform adapter's own `discover()`, the same
  code `ats_apply` runs.
- **Dead vs. throttled:** an HTTP 404 means the board is dead. Throttling pauses the scan
  and retries, so it never marks a board dead.
- **The report:** its `boards:` line uses `platform:token`, ready to paste into an
  `ats_apply` job.

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
| `tests/unit/test_ats_recruitee.py` / `tests/e2e/test_recruitee_apply_browser.py` | Recruitee routing, discovery and planning against a captured Channable offer; a Chromium run on a stand-in whose question ids differ from the API's, with a late Cookiebot banner |
| `tests/unit/test_ats_teamtailor.py` / `tests/e2e/test_teamtailor_apply_browser.py` | Teamtailor routing, the JSON Feed, form parsing and planning against a captured Anyfin form; a Chromium run on that real form DOM with a shim for its scripts |
| `tests/unit/test_ats_personio_bamboohr.py` | Personio / BambooHR routing and discovery against captured feeds (ottonova, Scribd), and the runner listing them as manual with the reason |
| `tests/unit/test_ats_sources.py` | each source against **real captured responses** (HN thread, Remote OK, Remotive, We Work Remotely RSS, with e-mail addresses scrubbed to `@example.com`), plus how source refs flow through the runner: filtering, de-duplication, leads-only runs |
| `scripts/ats_live_smoke.py` | **operational check, not pytest** (tests stay offline): a dry run against live Greenhouse, Lever, Ashby and Workable boards and every discovery source. Never submits. Exits 1 on API or form drift |
