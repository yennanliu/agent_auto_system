# Workday and SmartRecruiters apply: design

Status: **design, based on read-only probes of live sites (2026-11-20).** Discovery for
both already ships: SmartRecruiters in #109, Workday in #113. Their jobs are listed under
`manual[]`. This doc covers turning them into `ats_apply` adapters that can apply, and the
decisions that need the user first. Order agreed with the user: **Workday first, then
SmartRecruiters.**

## What the probes found

| | Workday | SmartRecruiters |
|---|---|---|
| Account | **Required at every employer.** There is no guest path | **None**: guest apply |
| Entry | job page `[data-automation-id=adventureButton]` "Apply" → a popup with `autofillWithResume` / `applyManually` / `useMyLastApplication` | `applyUrl` → `jobs.smartrecruiters.com/oneclick-ui/company/<id>/publication/<uuid>` |
| First screen | `/apply/applyManually` → **Create Account** (`email`, `password`, `verifyPassword`, `createAccountSubmitButton`), with a "Sign In" link; a hidden captcha (`noCaptchaWrapper`); password rules: upper, lower, number, special, 8+ | one page of standard fields: `first-name-input`, `last-name-input`, `email-input`, `confirm-email-input`, phone (with a country-code picker), a city autocomplete (`placeOfResidence`), `linkedin-input` / `website-input` / `facebook-input` / `twitter-input`, résumé drop zone, `hiring-manager-message-input` |
| Steps | 7 (progress bar): Create Account/Sign In → My Information → My Experience → Application Questions 1 of 2 → 2 of 2 → Voluntary Disclosures → Review | the standard page, then (per job) screening questions, then submit |
| Read the form before filling? | **No.** Questions are only visible after signing in | **Yes, from inside the browser.** The page loads `oneclick-ui/api/company/<id>/publication/<uuid>` → `fieldSets` (each section with `required` / `visible`). Plain HTTP gets 403 (DataDome) |
| Field hooks | `data-automation-id`, the same across employers | stable element ids inside web components (Playwright locators reach into them) |
| Bot protection | hidden captcha on account creation | DataDome: headless is blocked, **headed real Chrome passes** |

## Workday: proposed design

The answer rules are shared (`ats_common.answer_question`). What's different is that
planning happens **inside the browser, one step at a time**, because the questions only
exist after sign-in.

1. **Credentials per employer.** Store an e-mail and password per Workday tenant
   (`<tenant>.<wdN>`), encrypted with the existing Fernet store in `settings_store`. Use one
   e-mail for every tenant and a generated, strong, unique password per tenant.
2. **Sign in or create.** If credentials exist for the tenant, sign in. If not, create the
   account **only when the run has `create_accounts` on** (off by default). Workday then
   e-mails a verification link. The first version reports `needs_verification` and stops for
   that tenant; the user clicks the link and the next run signs in. A later version could
   read the code from the inbox (e.g. via the Gmail API with the user's consent).
3. **Walk the wizard by `data-automation-id`.**
   - *My Information:* name, address, phone, and "how did you hear" (the careers site).
   - *My Experience:* upload the résumé. Workday parses it into work-history and education
     rows, so keep the parsed rows rather than retyping them. Fill the required fields it
     left empty from the profile; if one can't be filled, skip the job.
   - *Application Questions:* read each `formField-*`'s label, type and options, and answer
     with `answer_question`. A required question with no confident answer stops the
     application at that step (`skipped`, listed in `unanswered`). Nothing is guessed.
   - *Voluntary Disclosures:* the "decline to self-identify" options, as on Greenhouse.
   - *Review:* a dry run stops here (status `prepared`). Otherwise submit, and count it only
     on Workday's confirmation (the "submitted" candidate-home state). An unconfirmed
     submission is recorded as `pending`, as on the other adapters.
4. **Throttling and safety.** The 429 handling from discovery stays. At most a few
   applications per tenant per run. Headed Chrome by default (the account form has a captcha).

**Needs the user before it can be built and verified:**
- **consent to create real Workday accounts in their name**, and which e-mail address to
  use;
- **one employer and one job** to build against, with a dry run up to the Review step and
  no submission without a separate OK;
- **how verification e-mails are handled**: clicked by hand at first, or read from the
  inbox later.

## SmartRecruiters: proposed design

No account is needed, so this can be built and verified (dry run) without the user's
access.

1. **Open the apply page** in headed Chrome, and capture the `publication` JSON the page
   loads. Plain HTTP is blocked, so `load_form` runs in the browser: the adapter opens the
   page in `apply()`, plans the answers from `fieldSets`, then fills.
2. **Standard fields** come from the profile via `standard_value`: name, e-mail (twice),
   phone (country code from the number), city through the autocomplete, LinkedIn / website,
   the résumé upload, and the cover letter as the message to the hiring manager.
   Experience and education are optional, and are left to the résumé parser.
3. **Screening questions** (a later step, when the job has them) are read from the page and
   answered with `answer_question`. A required unknown question means `skipped`, never a
   guess.
4. **Submit and confirm.** A dry run stops before the final submit. A confirmed submission
   is recorded in the ledger. If DataDome shows "Access is temporarily restricted", that
   job gets the status `blocked` with a clear reason, and no retry loop.

**Still to verify on a live job:** the screening-question step and the confirmation page.
The probe stayed on the first screen (it didn't click *Next*).

## Testing approach (both)

The same as the Lever / Ashby / Workable adapters:
- unit tests against captured real responses (the `publication` JSON; Workday's per-step DOM
  once an account exists);
- hermetic browser e2e tests on captured real form DOMs with small stand-in scripts;
- the live dry-run smoke check (`scripts/ats_live_smoke.py`).
