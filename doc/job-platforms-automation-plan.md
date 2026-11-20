# Job platforms: landscape and auto-apply automation plan

English | [繁體中文](job-platforms-automation-plan.zh-TW.md)

Status: **Phases 0–2 shipped** (#107, #109, #110). The rest is still a proposal. See
[Progress and lessons](#progress-and-lessons-as-of-2026-11-19) for what changed when it met
the real sites.

| Already supported | Kind | Code |
|---|---|---|
| `linkedin_apply` | Job board, Easy Apply (login) | `tools/linkedin_apply_tool.py` |
| `greenhouse_apply` | ATS, public board (no login) | `tools/greenhouse_apply_tool.py`, `scripts/greenhouse_country_boards.py` |
| `tw104_apply` | TW job board (login) | `tools/tw104_apply_tool.py` |
| `tasker_apply` | TW freelance / gig (login, JSON API) | `tools/tasker_apply_tool.py` |

This doc covers:

1. The other major platforms, apart from LinkedIn, Greenhouse and Indeed.
2. How to automate each one: whether it reuses current code, how complex it is, and the
   rough effort.
3. Remote, contract and freelance platforms.
4. A phased roadmap.

Effort is in **developer-days** for one platform, to the bar `greenhouse_apply` meets: the
spec, flow, tool, a dry-run mode, a ledger, a canary, golden evals, and a doc. These are
estimates, not commitments. Anything that depends on a site's live DOM or anti-bot setup
must be re-checked against the site before work starts.

---

## Progress and lessons (as of 2026-11-19)

| Phase | Status | What shipped |
|---|---|---|
| 0. Shared ATS core | ✅ #107 | `tools/ats_common.py`: shared answer rules (`Question` → `answer_question`), matching, ledger and browser helpers. Greenhouse moved onto it with no behaviour change |
| 1. ATS adapters | ✅ #109, #112 | the `ats_apply` job type, `ats_router.py`, and adapters for **Lever, Ashby, Workable** (plus Greenhouse). SmartRecruiters is discovery-only. Country board scanner `scripts/ats_country_boards.py` (#112) |
| 2. Discovery sources | ✅ #110 | `sources`: **HN "Who is hiring"**, Remote OK, Remotive and We Work Remotely feed the adapters; everything else becomes a manual lead |
| 3. Taiwan boards | ⏸ skipped for now | Investigated live, nothing built (findings below) |
| 4 (part). Freelancer.com bids | ✅ #115, #116 | `freelancer_bids` job type: official public search API, USD budget / type / competition filters, LLM-drafted proposals from the user's background (declines misfits), and **opt-in bid placement** with the user's `FREELANCER_OAUTH_TOKEN` (#116). Not yet verified with a real token. PRO360 and Upwork are still to do |
| 5 (part). Seek / JobStreet discovery | ✅ #114 | `seek_au`, `seek_nz`, `jobstreet_sg/my/ph/id` sources via Seek's search API, as manual leads. JobsDB HK/TH answer 403; job pages are behind a bot challenge. **Applying (Quick Apply)** needs the user's Seek account and is still to do |
| 6 (part). Workday discovery | ✅ #113 | Workday jobs found via the public `/wday/cxs` API and listed as manual leads; in the board scanner. **Applying on Workday** (an account per employer) is still to do |
| 4–6 | not started | — |

**What the live sites showed (each checked in a real browser, not assumed):**

- **Forms can be read without a browser** on all four apply-able ATSs: Lever (its
  server-rendered HTML), Ashby (the hosted page's public GraphQL), Workable (a form API)
  and Greenhouse (the Job Board API). The "plan in Python, then fill" model carried over
  unchanged.
- **SmartRecruiters** puts its apply page behind a DataDome bot wall. Headless browsers
  get "Access is temporarily restricted"; headed Chrome passes. The form is built from web
  components, and there's no form API. It's discovery-only for now (§3.1 underestimated
  this).
- **Remote job boards do *not* redirect to the employer's ATS** (§4.1 assumed they did):
  - Remote OK's apply link loops back to Remote OK.
  - Remotive's job page has no employer link.
  - We Work Remotely needs an account behind a Cloudflare check.
  - Only about 2–6 % of listings contain an ATS link at all, so these boards are mostly
    manual leads.
- **HN "Who is hiring" is the source that pays off.** In one month, 36 of 243 posts linked a
  supported ATS directly and 74 gave an e-mail address.
- **Built In** shows the employer link only to signed-in users. **Google Jobs** needs a
  paid SerpAPI key. Neither was built.
- **Boilerplate links matter.** A listing's EEO or AAP policy footer can point at a
  *different* posting on the same ATS, and listings cross-promote other roles. On job
  boards, a link is followed only when it's presented as how to apply.
- **Unconfirmed submissions** (Submit clicked, no confirmation seen) are recorded as
  pending and never resent automatically. The older `greenhouse_apply` job type still
  lacks this.
- **Taiwan boards (Phase 3, skipped for now):**
  - **1111** puts even its search page behind a captcha gate (a Cloudflare challenge plus
    an ALTCHA proof-of-work). Headless and headed Chrome both stayed on "Loading…". The
    plan's "clone of 104, 3–5 days" estimate doesn't hold.
  - **CakeResume** is behind Cloudflare ("請稍候…"), even in headed Chrome.
  - **Yourator** has a public JSON API (`/api/v4/jobs`). 44 % of the jobs sampled (78 of 179)
    apply on an external site: 66 via **Teamdoor** (a Taiwanese ATS), a few via Greenhouse
    or Lever (including `grnh.se` short links). That makes it a cheap discovery source,
    plus a possible Teamdoor adapter.
  - **518** search pages are server-rendered and readable.
  - Applying in-site on all four needs the user's own logged-in account.

---

## 1. Key observation: most platforms are either a *source* or a *form*

The platforms split into two roles, and the split decides how much code each one needs:

- **Applicant tracking systems (ATS)** such as Greenhouse, Lever, Ashby and Workday own the
  actual application **form**. Every job, wherever it was found, ends at one of these.
- **Job boards and aggregators** such as Glassdoor, Google Jobs, RemoteOK and Hacker News
  are mostly **discovery**. Most of their listings link out to the employer's ATS. Only some
  boards have their own in-site apply (Seek Quick Apply, 104, Dice Easy Apply, LinkedIn
  Easy Apply).

So the cheapest way to get wide coverage is:

```
discovery sources ──► normalized job {title, company, location, apply_url}
                          │
                          ▼
                 detect_ats(apply_url)   ──► greenhouse | lever | ashby | workable | smartrecruiters | workday | unknown
                          │
                          ▼
                 ATS apply adapter (shared answer planner + ledger)
```

A new *discovery source* (RemoteOK, Remotive, HN "Who is Hiring", Google Jobs) then costs
only **1–2 days**, because it reuses every ATS adapter already built. A new *ATS adapter*
makes every source more useful. This is the same multi-source and merge pattern that
`email_collect` already uses (`EmailCollectFlow._run_source`).

---

## 2. What the current code gives us to reuse

| Asset | Where | Reusable for |
|---|---|---|
| Answer planner: profile and rule answers, option matching, years-to-range mapping, sponsorship vs. authorization, language and programming-language options, EEOC "decline", single-option acknowledgement | `greenhouse_apply_tool.py` (`plan_answers`, `_profile_answer`, `_rule_answer`, `match_option`, `_years_option`, `_authorized_for`, …) | **Every ATS form**. The logic is platform-neutral, but today it is tied to Greenhouse's question JSON format |
| LLM fallback for required questions (abstains when unsure) | `GreenhouseAnswerCrew` | Every form |
| Relevance gate (`task_filter`) | `GreenhouseRelevanceCrew` | Every source |
| Keyword and location matching, interleaving across boards | `expand_keywords`, `match_job`, `_interleave` | Every source |
| Local "already applied" ledger | `load_ledger`, `record_applied` | Every platform without a server-side "already applied" check |
| Country board scanner | `scripts/greenhouse_country_boards.py` | Lever, Ashby, Workable, SmartRecruiters and Recruitee board scans (same "company token → public JSON" shape) |
| Logged-in session via storage state, `_looks_logged_out`, headed login refresh | `browser_session.py`, `scripts/*_login.py`, the 104 / tasker / LinkedIn tools | Every board that needs a login (1111, CakeResume, Seek, Dice, Wellfound, …) |
| Capture the site's own bearer token, then use its JSON API instead of the DOM | `tasker_apply_tool.py` (`_capture_session`, `_get_json`, `_submit`) | SPAs with an internal JSON API (Yourator, Cake, Freelancer.com) |
| Search page → job cards → apply popup → cover letter | `tw104_apply_tool.py` | 1111 and 518 are near-clones of 104's flow |
| Multi-step modal walker, daily-limit detection | `linkedin_apply_tool.py` (`_wait_for_modal_step`, `_hit_daily_limit`) | Seek / JobStreet Quick Apply, Dice, Indeed-style wizards |
| Canary drift probes, `AutomationSpec` registry, manifest-driven UI, eval harness | `canary.py`, `spec.py`, `evals/` | Every new spec gets these with no extra work |

There is **no generic form filler** yet. `playwright_form_tool.py` is hard-coded to one
Google Form, so any "unknown ATS" fallback would be new code.

**Prerequisite refactor (Phase 0, about 3–4 days):** move the platform-neutral parts of
`greenhouse_apply_tool.py` into `src/automation/tools/ats_common.py`, built around one
normalized question type:

```python
@dataclass
class Question:
    key: str            # DOM / API field id
    label: str
    kind: Literal["text", "textarea", "select", "multiselect", "checkbox", "file", "date"]
    required: bool
    options: list[str]
```

Then `plan_answers(questions, profile, answer_fn)` and the ledger, matching and
relevance-gate helpers become shared. Each adapter only implements
`discover() → jobs`, `fetch_questions(job) → [Question]` and
`fill_and_submit(page, job, answers)`. Greenhouse moves onto the new interface first, as a
pure refactor guarded by its existing unit tests and evals.

---

## 3. Platform-by-platform assessment

Legend. **Reuse:** H means it's mostly the shared core plus a small adapter; M means some
patterns carry over and the DOM work is new; L means it's essentially new.
**Complexity:** L / M / H / XH. **ToS risk:** how strongly the site prohibits automated
applying or scraping, which affects whether we should build it at all. LinkedIn is already
"high" and runs at the user's own risk.

### 3.1 Applicant tracking systems (form owners)

| Platform | Discovery | Apply mechanics | Login | Anti-bot | Reuse | Complexity | Effort | Notes |
|---|---|---|---|---|---|---|---|---|
| **Lever** | Public postings API `api.lever.co/v0/postings/<co>?mode=json` | Single page at `jobs.lever.co/<co>/<id>/apply`. Custom questions are **only in the DOM** (not in the API), so they're scraped from the form | No | hCaptcha on some boards | **H** | **L–M** | 3–5 d | Best next target: many startups use it, and it's the closest shape to Greenhouse |
| **Ashby** | Public job-board API `api.ashbyhq.com/posting-api/job-board/<org>` | React SPA at `jobs.ashbyhq.com/<org>/<id>/application`. The form definition is fetched as JSON by the page, so it can be read before filling | No | reCAPTCHA | **H** | **M** | 4–6 d | Growing fast with tech startups. Same "plan in Python, then fill" model as Greenhouse |
| **SmartRecruiters** | Public API `api.smartrecruiters.com/v1/companies/<co>/postings` | One-click apply UI with screening questions. Optional "apply with LinkedIn" | No (guest apply) | Moderate | **H** | **M** | 5–7 d | Mid-size and enterprise, strong in Europe |
| **Workable** | Public board JSON at `apply.workable.com/api/v3/accounts/<co>/jobs` | The form's fields come from a JSON form definition | No | Cloudflare challenge on some boards | **H** | **M** | 4–6 d | Many European SMBs |
| **Recruitee / Teamtailor / Personio / BambooHR / JazzHR** | Each has a public feed (`<co>.recruitee.com/api/offers`, `<co>.jobs.personio.de/xml`, `<co>.bamboohr.com/careers/list`, …) | Simple one-page forms | No | Low | **H** | **L–M** | 2–4 d each after Phase 0 | Worth covering together as "small-ATS adapters" once the shared core exists |
| **Workday** | Per-tenant JSON `POST <tenant>.wdN.myworkdayjobs.com/wday/cxs/<tenant>/<site>/jobs` | Wizard of 5–7 steps: account → My Info → Experience (résumé parse, then fix-ups) → Questions → Voluntary disclosures → Review. `data-automation-id` attributes are **stable across tenants**, which makes it feasible | **A new account per employer tenant**, plus e-mail verification | Moderate | **M** (planner yes, DOM no) | **XH** | 15–20 d | Has the most jobs of any ATS, and the hardest automation. Needs automated e-mail verification (e.g. the Gmail MCP) and storing a password for each tenant. Build after the cheaper wins |
| **iCIMS** | Per-company portals, varied | Often iframe-embedded multi-step forms, configured differently per employer | Usually an account | Moderate | **L** | **H** | 10–15 d | Low ROI. Use "assist mode" instead (fill what we can and hand off to a human) |
| **Taleo (Oracle) / SuccessFactors (SAP)** | Per-company | Older enterprise wizards with session timeouts and an account per company | Yes | Moderate | **L** | **XH** | 15+ d each | Defer. Only worth it if a target employer list demands them |

### 3.2 General and tech job boards

| Platform | Role | Apply mechanics | Reuse | Complexity | Effort | ToS risk | Recommendation |
|---|---|---|---|---|---|---|---|
| **Glassdoor** | Mostly discovery | Mostly redirects to the employer's ATS. Its own Easy Apply is shared with Indeed now | M | M (Cloudflare) | 3–4 d (discovery only) | High | Discovery only, routed to ATS adapters |
| **ZipRecruiter** | Board | 1-click apply with a stored profile | M (LinkedIn wizard pattern) | M–H | 5–7 d | High | Low priority |
| **Monster / CareerBuilder / SimplyHired** | Board | Mixed in-site and redirect | M | M | 4–6 d | Medium | Skip unless asked: declining traffic |
| **Google Jobs** | Aggregator only | None (no apply). No official API; SerpAPI and similar services are paid | H | L | 1–2 d (via SerpAPI) | Low with an API | Good discovery source feeding the ATS router |
| **Wellfound** (formerly AngelList Talent) | Board | Login, then a short note to the founder plus a stored profile | M (session + LLM cover note) | M (DataDome) | 4–6 d | Medium | Worth it for the startup audience |
| **Dice** | Board (US tech) | Login, Easy Apply wizard | M (LinkedIn wizard) | M | 4–6 d | Medium | Medium priority for US contract tech roles |
| **Welcome to the Jungle** (merged with Otta) | Board | Mostly redirects to the ATS. Some in-site apply | H | M | 3–5 d | Medium | Discovery plus routing |
| **Built In** | Board | Redirects to the ATS | H | L | 1–2 d | Low | Discovery plus routing |
| **YC Work at a Startup** | Board | Login, then message the founder | M | M | 3–4 d | Medium | The message is the "application", so it's LLM-drafted |
| **Hacker News "Who is Hiring"** | Monthly thread | Free-text posts with e-mails or ATS links | **H** (`hn_digest` + `email_collect` already exist) | L | 2–3 d | Low (official Algolia / Firebase APIs) | High value, low cost. Parse posts with an LLM, route ATS links, queue e-mail outreach |
| **Hired** | Reverse marketplace | Profile only (companies reach out), so there's nothing to apply to | — | — | — | — | Skip |

### 3.3 Regional boards

| Region | Platform | Apply mechanics | Reuse | Complexity | Effort | Recommendation |
|---|---|---|---|---|---|---|
| **Taiwan** | **1111人力銀行** | Login, search → card → apply with a stored résumé and cover letter, the same model as 104 | **H** (clone `tw104_apply_tool.py`) | L–M | 3–5 d | **Do first in TW** |
| Taiwan | **CakeResume / Cake** | Login, one-click apply with the Cake profile and résumé. SPA backed by JSON | M–H (tasker bearer-capture pattern) | M | 4–5 d | Do: strong for tech and English-speaking roles |
| Taiwan | **Yourator** | Login, apply with a résumé and optional message | M–H | L–M | 3–4 d | Do: startup audience |
| Taiwan | **518熊班** | Login, similar to 104 / 1111 | H | L–M | 3–4 d | Later: more blue-collar and service jobs |
| **Europe** | StepStone (DE and others) | Mix of in-site "Quick apply" and ATS redirects | M | M–H | 5–7 d | Medium (DE market) |
| Europe | XING (DACH) | Mostly redirects to the ATS | H | M | 2–3 d (discovery) | Discovery plus routing |
| Europe | InfoJobs (ES) | Official developer API (OAuth). Check whether applying is exposed before starting | M | M | 4–6 d | Pairs with the existing Spain Greenhouse board work |
| Europe | Reed / Totaljobs (UK) | Reed has an official search API. Applying is in-site | M | M | 4–5 d | Medium |
| **APAC** | **Seek** (AU/NZ) **+ JobStreet / JobsDB** (SEA) | Login, then a Quick Apply wizard. **JobStreet and JobsDB now run on Seek's platform**, so one adapter covers all three | M (LinkedIn wizard) | M | 6–8 d for all three | **High value**: a single adapter covers AU, NZ, SG, MY, HK, PH, ID and TH |
| APAC | Naukri (IN) | Login, in-site apply. Aggressive anti-bot | M | H | 6–8 d | Only if India is a target |
| APAC | BOSS直聘 / 51job / 智联招聘 (CN) | BOSS is app-first and chat-driven. All three have strong anti-bot and need a CN phone number | L | XH | — | Skip |
| APAC | Rikunabi / doda / BizReach (JP) | Agent-mediated, long Japanese forms, scouting model | L | H–XH | — | Skip; "assist mode" at most |
| APAC | JobKorea / Saramin (KR) | Login, in-site apply. Saramin has an open search API | M | H | 6–8 d | Only if Korea is a target |

---

## 4. Remote, contract and freelance platforms

### 4.1 Remote-job boards (full-time or contract)

> **Corrected after testing (Phase 2):** these boards mostly do **not** expose the
> employer's application link. See [Progress and lessons](#progress-and-lessons-as-of-2026-11-19).
> The "Apply" column below was the original assumption. In practice they're discovery
> sources that produce manual leads.

| Platform | Discovery | Apply | Reuse | Effort | Notes |
|---|---|---|---|---|---|
| **RemoteOK** | Public JSON API `remoteok.com/api` | Redirect to ATS / e-mail | H | 1 d | Easiest source of all |
| **Remotive** | Public API `remotive.com/api/remote-jobs` | Redirect to ATS | H | 1 d | Easy |
| **We Work Remotely** | RSS feeds by category | Redirect to ATS / e-mail | H | 1–2 d | Easy |
| **Working Nomads / Jobspresso / Remote.co / JustRemote / Himalayas** | RSS or JSON, or light scraping | Redirect | H | 1–2 d each | Diminishing returns past the first three |
| **FlexJobs** | Paid membership, login | Redirect | M | 2–3 d | Only if the user subscribes |
| **EU Remote Jobs / Remote Rocketship / Arc.dev job board** | Scrape or RSS | Redirect | H | 1–2 d | Optional |

All of these go through the ATS router (§1), so their value grows with each ATS adapter
built.

### 4.2 Freelance marketplaces (bid / proposal model, the same shape as `tasker_apply`)

| Platform | Model | Automation surface | Reuse | Complexity | Effort | ToS risk | Recommendation |
|---|---|---|---|---|---|---|---|
| **Upwork** | Proposals that cost paid "Connects" | Official GraphQL API, but submitting proposals from third-party tools is restricted. Heavy bot detection. Accounts get banned for automation | M (tasker funnel + LLM proposal) | H | 6–8 d | **Very high** | **Draft-only**: find, rank and draft proposals, and a human clicks submit. Never auto-submit (it costs money and risks the account) |
| **Freelancer.com** | Bids | **Official REST API** with bid endpoints (OAuth) | **H** (tasker funnel: list → check → submit → confirm) | M | 4–5 d | Low via the API | **Best freelance target**: the official API means no DOM fragility |
| **PeoplePerHour** | Proposals | Login, web form | M | M | 4–5 d | Medium | Medium (UK/EU) |
| **Guru** | Quotes (bid credits) | Login, web form | M | M | 4–5 d | Medium | Low priority |
| **Malt** (FR/DE/ES/NL) | Clients contact freelancers; profile-centric | Mostly inbound messages, so there's little to apply to | L | — | — | — | Profile upkeep only; skip auto-apply |
| **Contra** | Commission-free, profile plus some job posts | Login, apply to posts | M | M | 3–4 d | Medium | Optional |
| **Fiverr** | Seller-side gig listings (buyers come to you) | No apply model | — | — | — | — | Skip |
| **Twago / Freelancermap / Gulp** (DACH IT contract) | Project boards | Login, apply to a project | M | M | 4–5 d each | Medium | Only if DACH contract work is a goal |
| **Taiwan: PRO360達人網, 1111外包網, 518外包網** | Gig / quote requests | Login, quote. Similar to tasker | **H** (tasker) | L–M | 3–5 d each | Medium | PRO360 first: the closest twin of tasker |

### 4.3 Vetted talent networks and contract staffing (apply once, they match you)

These are not apply-per-job platforms. Each runs a one-time vetting process (tests,
interviews), then matches the candidate to roles. **Automation value is low**, so list them
for the user's own manual sign-up and don't build adapters:

- **Developer networks:** Toptal, Turing, Andela, Arc.dev (talent side), Lemon.io, Gun.io,
  Braintrust, A.Team, X-Team, Proxify, Gigster
- **Business and consulting:** Catalant, Expert360, BTG (Business Talent Group)
- **Niche:** Kolabtree (science), Codementor (mentoring and freelance), 99designs and Dribbble
  (design)
- **Staffing agencies** (contract roles; their job sites usually sit on Bullhorn or an ATS):
  Robert Half, Hays, Randstad, Adecco, Michael Page, Kforce, TEKsystems. Their job pages can
  feed the ATS router as a discovery source if wanted.
- **AI-training contract work** (remote, task-based): Outlier, DataAnnotation, Alignerr,
  Mercor. These use onboarding tests, not per-job applications.

---

## 5. Roadmap

| Phase | Scope | Effort | Why this order |
|---|---|---|---|
| **0. Shared ATS core** ✅ | Extract `ats_common.py` (`Question`, `plan_answers`, ledger, matching, relevance gate) and move Greenhouse onto it | 3–4 d | Every later adapter becomes "discovery + fill" only. Greenhouse tests and evals guard the refactor |
| **1. Cheap ATS wins** ✅ (scanner still to do) | Lever, Ashby, then Workable and SmartRecruiters. Add `detect_ats(url)` and a generic `ats_apply` spec that routes by URL. Extend the country board scanner to the new ATSs | 15–20 d | Public APIs and no login, so the most new coverage for the least risk |
| **2. Discovery sources** ✅ (HN + 3 boards) | RemoteOK, Remotive, We Work Remotely, HN "Who is Hiring", Built In, Google Jobs (SerpAPI) → ATS router. Unknown-ATS links land in a "manual" list in the run report | 7–10 d | Each source costs 1–2 days and reuses every adapter from Phase 1 |
| **3. Taiwan boards** ⏸ skipped | 1111 (clone of 104), CakeResume, Yourator, then 518 | 13–18 d | Reuses the 104 / tasker patterns directly; strong local value |
| **4. Freelance** | Freelancer.com (API bids), PRO360 (tasker clone), Upwork **draft-only** | 13–18 d | Same funnel as tasker. Upwork stays human-in-the-loop |
| **5. APAC board** | Seek + JobStreet + JobsDB (one adapter) | 6–8 d | One build covers about eight markets |
| **6. Workday** | Tenant discovery, account creation, e-mail verification via Gmail, wizard walker on `data-automation-id` | 15–20 d | Largest job pool but the most expensive. Build once the shared core is proven |
| **Deferred** | iCIMS, Taleo, SuccessFactors, Glassdoor / ZipRecruiter apply, CN / JP / KR boards, vetted networks | — | Low ROI, hard anti-bot, or a non-apply model. Revisit when a specific target needs them |

Total for Phases 0–5 is about **60–80 developer-days**. Adding Phase 6 brings it to about
**75–100 days**.

### Per-adapter checklist (from `CLAUDE.md`)

Each new platform must:

1. Register an `AutomationSpec` in `spec.py`, with a `canary` (read-only probe of the
   search or board endpoint) or a `canary_skip` reason.
2. Add a flow and tool following the **deterministic-funnel** rule: the flow drives the
   tools directly, and the crew is used only for relevance and answer judgement.
3. Add golden evals under `evals/<job_type>/` or a justified `evals/skip.yaml` entry.
4. Add an `_AGENT_DEFS` row, and manifest `fields` (`custom_ui=False`) unless the form
   needs a file upload. The résumé upload already goes through `/api/uploads`.
5. Support dry-run, a ledger, a `warnings` list, and partial results instead of raising.
6. For login sites: a `scripts/<name>_login.py` and an entry in `browser_session.py` so
   Admin → Sessions can refresh the login.

### Cross-cutting risks

- **ToS and account bans.** Applying from the user's own logged-in account carries the same
  risk as `linkedin_apply` today. Keep per-run caps and human-like pacing, and default to
  `dry_run` for new adapters.
- **CAPTCHAs.** Greenhouse already needs headed real Chrome. Expect the same on Lever, Ashby
  and Workable. Don't integrate CAPTCHA-solving services.
- **Answer quality across forms.** The shared planner must keep the Greenhouse rule of
  **skipping a job when a required answer is unknown** rather than guessing. This matters
  more as the form variety grows.
- **Duplicate applications across sources.** The same job can appear on RemoteOK, HN and
  the employer's Lever board. The ledger key should be the ATS canonical job id after
  routing, not the source URL.
