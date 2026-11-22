# Project Evaluation — PM & EM View

> Date: 2026-11-08 · Baseline: `main` @ `aed26f7`
> Language: English · [繁體中文版](project-evaluation.zh-TW.md)
> Companion audits: [improvements.md](improvements.md) (2026-05, code audit — mostly done),
> [saas-readiness.md](saas-readiness.md) and [self-hosted-readiness.md](self-hosted-readiness.md)
> (2026-07, go-to-market paths). This doc does not repeat them. It re-scores the project as it
> is today and proposes the **next** features and improvements, ranked from a product (PM) and an
> engineering (EM) point of view.

---

## 1. Snapshot — where the project is

| Dimension | Today |
|---|---|
| Automations | 14 built-in types (form fill, scrapers, HN digest, Sheet reader, Shopee sellers, 利潤健檢, Tasker / 104 / LinkedIn / Greenhouse auto-apply, Email Collector, email sender, pipeline) + admin-authored no-code `custom:<slug>` + entry-point plugins |
| Harness | Multi-LLM (OpenAI / Anthropic / Gemini), validate → self-correcting retry → cross-model fallback, independent LLM-as-judge, token + cost accounting, Langfuse traces, Prometheus `/metrics` |
| Operations | Cron scheduler, Airflow-style run grid, SSE live log, run cancellation, stale-run reconciliation, browser-session freshness + headed re-login |
| Platform | Login + Google/GitHub SSO, per-user allowlist RBAC, Fernet-encrypted keys, Docker image, AWS ECS guide, Electron desktop build, CI (ruff, unit, integration, Docker smoke) |
| Extensibility | One `AutomationSpec` drives dispatch, allowlist, validator, rubric, step graph and (Phase 2) a manifest-driven form |
| Size / quality | ~33k lines (Python + `ui/app.js`), ~800 tests, 26 commits since Aug 2026 |

**Overall verdict.** The engineering core (harness, spec registry, scheduler) is mature, better than
most internal automation tools. Since May the project has added **breadth** (more automations) faster
than **depth** (making each automation's output useful once the run ends). The next phase should move
the product from "runs automations" to "gets an outcome done": outputs that reach other systems,
human-in-the-loop control over actions that touch the outside world, and the trust and safety basics
that multi-user use needs.

### Scorecard

| Area | Score (1–5) | Note |
|---|---|---|
| Core execution & reliability | 4.5 | Retry/fallback/judge/reconcile is solid; no concurrency limit or durable queue |
| Extensibility | 4 | Spec registry + manifest + plugins; the UI is still a 3.6k-line hand-written `app.js` |
| Observability | 4 | Langfuse + Prometheus + per-run cost; no alerting, no structured JSON logs |
| Security & multi-user isolation | 2.5 | Runs are scoped; **jobs and PDF reports are not** (see §3, E1) |
| Product outcome / integrations | 2 | Results stay inside the app (CSV/PDF only); no webhooks, notifications or CRM push |
| Onboarding & UX | 2.5 | Powerful but expert-oriented; no templates, run diff or "what changed" view |
| Data lifecycle | 2 | No retention, no Alembic, PII (leads, 負責人) kept forever |

---

## 2. PM view — features to add

Who the product serves today: **(a)** a small B2B / sales team running lead-gen (Email Collector,
Shopee sellers), **(b)** an individual job-seeker or freelancer (104 / LinkedIn / Greenhouse / Tasker
auto-apply), **(c)** a Shopee seller (利潤健檢). Each of these users wants an **outcome** (meetings
booked, interviews, a healthier margin), not a run log. The features below are ranked by how much
they close that gap per unit of effort.

### P0 — highest leverage

#### F1. Notifications & outbound webhooks
- **Problem:** scheduled runs fail silently or finish unseen; the user has to open the app.
- **Feature:** per-job "on success / on failure / on score < X" → email, Slack/LINE/Discord
  webhook, or a generic signed HTTP webhook carrying `{run_id, status, eval_score, summary, result_url}`.
- **Why now:** the cron scheduler exists, but a schedule nobody watches is only half a feature.
  LINE matters for a TW audience.
- **Success metric:** % of scheduled runs whose failure is acknowledged within 24 h.
- **Effort:** S–M. A `notify` section in the Job row and one `notify_run(run_id)` helper, called
  after **every** terminal `_update_run`, not once at the end of `executor.execute_run`, which
  returns early. The call sites are: validation failure after retries (`executor.py:263`), success
  (`executor.py:269`, before the early `return`), exception failure (`executor.py:293`) and,
  optionally, cancellation. Runs failed by `reconcile_stale_runs` after a restart never reach the
  executor, so reconciliation must also return the IDs of the runs it failed (select them before the
  bulk `UPDATE`), and startup then sends their failure notifications. The same rule as Langfuse
  applies: `notify_run` catches and logs its own errors and never raises into the executor or startup.

#### F2. Approval queue (human-in-the-loop) for outward-facing actions
- **Problem:** the auto-apply and email automations act on the real world (submit applications,
  send proposals and emails). `dry_run` is all-or-nothing.
- **Feature:** a third mode, **"review"**: the flow prepares the actions (the proposal text, the
  cover letter, the email) and parks them as `pending_approval` items. The user approves, edits or
  rejects them in an inbox, and only approved items run.
- **Why:** it is the biggest trust unlock. Users who would never enable fully automatic applying
  will use "draft for me, I click send". It also lowers ToS and reputation risk.
- **Success metric:** weekly active users of the apply/outreach automations; approve rate.
- **Effort:** M. New `ActionItem` table and a split of the deterministic funnels into
  prepare → execute (they already separate them internally).

#### F3. Lead & application tracker (results as entities, not blobs)
- **Problem:** Email Collector leads and job applications live inside `run.result` JSON. There is
  no dedup across runs, no status, and no "already contacted / applied" memory.
- **Feature:** persist leads and applications as rows with a status pipeline
  (`new → contacted → replied → won/lost`, `applied → interview → offer/rejected`), dedup across
  runs, and skip anything already applied to or contacted.
- **Why:** it turns one-off runs into a compounding asset, and it is the basis for F4 and for
  real ROI reporting.
- **Effort:** M.

### P1 — strong value

| # | Feature | Notes |
|---|---|---|
| F4 | **Integrations out**: Google Sheets append, HubSpot / Notion / Airtable push, CSV to S3/Drive | Many users want the leads in their existing tool. Implement as a pipeline step type (`sink`), so it composes with every automation. |
| F5 | **Run-to-run diff / "what's new"** | For scheduled scrapers (HN, X, Shopee, Sheets): show only new items since the last successful run, and notify only on change. Cuts noise far more than F1 alone. |
| F6 | **Template gallery & guided first run** | Ship ready-made jobs ("Weekly Taipei café leads", "Daily HN AI digest", "Apply to 5 backend jobs/day, review mode"). Shortens time-to-first-value for new users. |
| F7 | **Pipeline v2**: conditional steps, `for_each` fan-out, and continue-on-error | Today pipelines are strictly linear and stop on the first failure. `for_each` (e.g. per lead → draft email) is the most-asked shape. |
| F8 | **Cost budgets & guardrails** | Per-user / per-job monthly USD cap, estimated cost before running, and auto-pause when the cap is reached. Cost is already tracked, so this is cheap to add. |
| F9 | **Outcome analytics** | Funnel stats on top of F3: leads → replies, applications → interviews, cost per qualified lead. This is the number a buyer cares about. |

### P2 — later / bets

- **F10 Natural-language job builder**: "every Monday find 20 bubble-tea shops in Taichung and draft
  an intro email" → a proposed job + schedule for the user to confirm, built on the manifest and
  custom automations.
- **F11 Custom automations with tools**: today `custom:<slug>` is a no-tools agent. Let admins
  whitelist safe tools (web fetch, sheet read) so no-code automations can do real work.
- **F12 Mobile / LINE bot front-end** to approve F2 items and receive F1 alerts on the go.
- **F13 Public read-only share links** for a run report (the 利潤健檢 PDF, a digest), signed and expiring.

### What NOT to build now
- More scraper or auto-apply targets before F2/F3 exist: each new one adds maintenance (DOM drift,
  as the LinkedIn and Greenhouse history shows) without improving the outcome of the existing ones.
- Full multi-tenant SaaS (see saas-readiness.md) before deciding the go-to-market path. The
  self-hosted path is cheaper and fits the scraping and credential model better.

---

## 3. EM view — engineering improvements

### P0 — correctness & safety (do first, mostly small)

#### E1. Close the authorization gaps (security bug)
Runs are user-scoped (`_assert_run_visible`), but:
- `GET /api/jobs`, `GET/DELETE /api/jobs/{id}` (`src/routers/jobs.py`) have **no ownership check**.
  Any logged-in user can list, read (payloads can contain emails, salary ranges and offers) and
  **delete** other users' jobs. `PATCH` only checks the automation allowlist, not ownership.
- `GET /api/runs/{id}/report.pdf` (`src/routers/runs.py:194`) takes no user at all. Any
  authenticated user can fetch any 利潤健檢 financial report by iterating run IDs.
- `/api/stats`, `/api/schedules` and `/jobs/{id}/overview` are global.

**Fix:**
- A single `_assert_job_visible(job, user)` mirroring the run helper, called by every `{job_id}` route.
- `GET /api/jobs` filters to `Job.created_by == user.id` for non-admins; admins still see all jobs.
- `report.pdf` takes `require_user`, loads the `Run` (404 if missing) and calls
  `_assert_run_visible(run, user)` **before** serving the file. A user dependency alone is not enough.
- `/stats`, `/schedules` and `/overview` take the user and scope to their own rows for non-admins.
- Tests: a parametrized conformance test that *every* `{id}` route returns 404 for another non-admin
  user, plus a multi-user collection test that user A's `GET /api/jobs` never contains user B's jobs
  (and an admin's does). These tests are what prevent a regression the next time a router is added.

#### E2. Bounded concurrency & backpressure
`launch_run` calls `asyncio.create_task` without a limit. A cron burst or a user clicking "run" ten
times starts ten headed or headless Chromium instances plus LLM calls in one process.
**Fix:** a global plus per-job-type semaphore (browser jobs ≈ 2, LLM-only ≈ 8), a `queued` run status
shown in the UI, and a rule that skips a scheduled tick when the previous run of the same job is still active.

#### E3. Real migrations
`init_db()` has more than 20 hand-written `ALTER TABLE … ADD COLUMN` statements in try/except blocks.
That cannot rename, backfill, add indexes safely or downgrade, and F2/F3 need new tables with foreign keys.
**Fix:** adopt Alembic with a baseline revision that matches the current schema, and keep
`init_db()` calling `alembic upgrade head` on startup so the "idempotent on startup" contract still holds.
Fresh and existing databases need different first steps:
- **Fresh database** (no tables): `upgrade head` runs the baseline migration, which creates the schema.
- **Existing database** (tables but no `alembic_version`): first run the legacy `ALTER TABLE` pass
  one last time, then check that the live schema matches the baseline (for example, an
  `alembic check` / autogenerate diff with no changes, or a test that compares the inspected
  columns and indexes). Only then run `alembic stamp <baseline>`, and after that `upgrade head`. If
  the check finds a mismatch, stop with a clear error instead of stamping a database that the
  baseline does not describe.

#### E4. Data retention & PII handling
Run logs, `result` JSON, leads (including `responsible` / 負責人, a natural person), uploads and
PDFs are kept forever. **Fix:** a configurable retention policy (for example runs 90 d and uploads
30 d), a daily purge task (the scheduler can host it), and an "export & delete my data" admin action.
This is a PDPA/GDPR basic once Email Collector is used for real.

### P1 — scale & maintainability

| # | Improvement | Why |
|---|---|---|
| E5 | **Split `ui/app.js` (3.6k lines)** into ES modules per area (runs, jobs, admin, forms), with no build step required, and continue migrating `custom_ui=True` forms to manifest `fields` | The single file is now the main merge-conflict and regression hotspot; Phase 2 of the RFC only covers simple forms. |
| E6 | **Durable execution** — persist step checkpoints so a restart resumes instead of failing the run (`reconcile_stale_runs` marks it `failed` today) | Long browser funnels (Email Collector, auto-apply) lose all their progress on a deploy. |
| E7 | **Scraper drift detection** — nightly canary runs (`dry_run`) per browser automation, plus alerts on selector or validation failure-rate spikes | LinkedIn and Greenhouse history shows that DOM drift is the #1 source of breakage, and today users find it before we do. |
| E8 | **Structured JSON logging** with `run_id` / `user_id` / `job_type` correlation | Needed for any hosted or self-hosted support; pairs with the existing Prometheus metrics. |
| E9 | **SSRF guard** in `WebScraperTool` and the custom-automation fetchers: block private, link-local and metadata IPs after DNS resolution | Already flagged in saas-readiness.md, still open; it becomes urgent if F11 lets admins grant fetch tools. |
| E10 | **Secure defaults** — refuse to start in non-desktop mode with `admin`/`admin` or the dev `APP_SECRET`; `https_only=True` on the session cookie behind TLS | Today these only log a warning. |
| E11 | **Rate limiting** on login and run trigger (`slowapi`) | Prevents brute force and runaway costs; small. |

### P2 — quality of engineering life

- **E12 LLM model catalog refresh**: `provider.py` defaults (`gpt-4o-mini`, `claude-haiku-4-5`,
  `claude-sonnet-4-6`) are a generation behind. Add the current models and keep the old ones in
  `costs.py` for historical pricing. Consider structured outputs (JSON schema) to replace
  parse-from-text in the crews.
- **E13 Prompt caching** for the crews' static system prompts (Anthropic `cache_control`, OpenAI
  automatic caching). This cuts cost on scheduled repeats.
- **E14 Coverage gate** in CI (`pytest-cov`, a ratchet threshold) and a `make new-automation`
  scaffold that stamps out spec + flow + crew + test.
- **E15 Eval regression set**: a frozen set of inputs and expected outputs per automation, scored by
  the existing judge in CI (nightly). Prompt changes then have a measurable quality bar.

---

## 4. Suggested roadmap

| Milestone | Scope | Outcome |
|---|---|---|
| **M1 — Trust (≈2 wks)** | E1, E2, E10, E11, F1 | Safe for a team to share; nothing fails silently |
| **M2 — Outcome (≈4 wks)** | E3, F2, F3, F5, E4 | Review-before-act; leads/applications become tracked entities |
| **M3 — Reach (≈4 wks)** | F4, F7, F8, F9, E7 | Results land in the user's tools; ROI is measurable; drift is caught early |
| **M4 — Scale & polish** | E5, E6, E8, E12–E15, F6, F10–F13 | Faster to extend, cheaper to run, easier to onboard |

### PM/EM trade-off notes
- **E1 is a bug, not a roadmap item.** It ships first, regardless of priorities.
- F2 (approval queue) and F3 (tracker) both need new tables. Doing **E3 (Alembic) first** avoids
  piling two more ad-hoc migrations onto `init_db()`.
- F1 and F5 share one primitive, "compare to the last successful run". Build it once.
- Hold new automation types until M2 lands. Every new scraper raises E7's maintenance load.

---

## 5. KPIs to track

| KPI | Source | Target direction |
|---|---|---|
| Run success rate (per type, 7-day) | `/api/stats` | ↑ ≥ 95 % for non-browser jobs |
| Median eval score | `run.eval_score` | ↑, with no drop after prompt changes (E15) |
| Cost per successful run / per qualified lead | `run.cost_usd` + F3 | ↓ |
| Time-to-detect a broken scraper | E7 canary vs first user report | ↓ < 24 h |
| Scheduled-run failures acknowledged in 24 h | F1 adds `notified_at` and `acknowledged_at` to `Run`; `acknowledged_at` is set by an "Acknowledge" action on the failed run (UI button or the link in the notification). KPI = failed scheduled runs with `acknowledged_at − notified_at ≤ 24 h` ÷ all failed scheduled runs that were notified | ↑ |
| Weekly active users of review mode | F2 | ↑ |
