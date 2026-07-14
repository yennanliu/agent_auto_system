# SaaS-Readiness Roadmap

> Audit date: 2026-07-14
> Scope: what has to change to sell `agent_auto_system` as a multi-tenant SaaS to
> external, paying end-users.
> Companion docs: [improvements.md](improvements.md) (2026-05-27 general code audit —
> much of it now done: auth, scheduler, Langfuse, Prometheus, CI, Docker),
> [auth-and-admin-design.md](auth-and-admin-design.md),
> [aws-ecs-fargate-deployment.md](aws-ecs-fargate-deployment.md).

## TL;DR — verdict

Today this is a **well-built single-tenant internal tool**, not a SaaS. The engineering
*inside* each automation is genuinely solid: retry + cross-model fallback, an independent
LLM-as-judge, a validation gate, Langfuse tracing, Prometheus metrics, RBAC allowlists,
Fernet-encrypted keys, ~380 tests, a multi-stage Dockerfile, and a real CI pipeline.

But four things every SaaS needs are **completely absent**, and one thing the product is
built on actively breaks in a multi-user world:

| Pillar | State | Severity |
|---|---|---|
| **Multi-tenancy** (org/workspace, data isolation) | None — flat single-tenant | 🔴 blocker |
| **Self-serve signup + onboarding** | None — admin-seeded/admin-created only | 🔴 blocker |
| **Billing / plans / quotas / rate limits** | None — cost is *tracked* but never *enforced* | 🔴 blocker |
| **Per-user integration credentials** (Shopee/104/Tasker) | Global shared files on disk | 🔴 blocker |
| **Horizontal scale** (SQLite, in-process tasks, non-distributed scheduler) | Single-instance by design | 🟠 major |

Everything else (modularity, docs, UX polish, security hardening) is important but is
"make it good," not "make it possible." The rest of this doc is a prioritized TODO list.

---

## P0 — Blockers (cannot sell without these)

### 1. Multi-tenancy & data isolation

The data model is flat. `User` (`src/models.py:6`) has no `org_id` / `tenant_id` / `team`
— authorization is just `is_admin` + a per-user `allowed_automations` allowlist. Worse,
isolation is only *half* real:

- **Runs are isolated** (row-level, by `user_id`): `runs.py:100-102`, `_assert_run_visible`
  returns 404 for others' runs.
- **Jobs are NOT isolated.** `GET /jobs` (`jobs.py:47-48`) returns *every* job to *any*
  logged-in user; `get_job`/`update_job`/`delete_job` (`jobs.py:72,80,102`) do no ownership
  check. Any user can view, edit, delete, or **run** another user's job. `Job.created_by`
  exists but is used only for scheduled-run attribution.
- `/schedules`, `/jobs/{id}/overview`, and `/stats` are all global.

**TODO**
- [ ] Introduce an `Organization` (tenant) table; add `org_id` FK to `User`, `Job`, `Run`,
      `Setting`, and every future per-tenant row. Make `org_id` the mandatory scope on
      **every** query, not an afterthought.
- [ ] Add a query-scoping dependency/helper so no router can accidentally forget the filter
      (the `jobs.py` gap above is exactly that failure mode). Consider a thin repository
      layer or SQLAlchemy event that injects `WHERE org_id = :current_org`.
- [ ] Roles within a tenant: `owner` / `admin` / `member` (the current global `is_admin`
      becomes tenant-scoped; keep a separate super-admin/staff concept for *you*, the
      SaaS operator).
- [ ] Move `Setting` (LLM keys, enabled automations, eval judge) to be **per-tenant** —
      today it's one global key/value table (`models.py:17`).
- [ ] Add a conformance test that every list/detail endpoint 404s/403s across tenant
      boundaries (mirror the existing run-visibility tests).

### 2. Self-serve authentication & onboarding

There is **no signup**. Users are the env-seeded `admin`/`admin` (`main.py:36`) or
admin-created via `POST /admin/users`. Session cookies work (`SessionMiddleware`,
`main.py:93`) but `https_only=False`, and there are no API keys for programmatic access.

**TODO**
- [ ] Public signup flow → creates a new `Organization` + owner `User`, email verification,
      password reset (all currently missing).
- [ ] Set `SessionMiddleware(https_only=True, same_site="lax")` in production; today it's
      `https_only=False` (`main.py:93`).
- [ ] **Programmatic API keys** (per-tenant, hashed at rest, scoped) so customers can call
      the API without a browser session — SaaS customers will want this.
- [ ] Consider SSO / social login (Google is a natural fit given the Google-Form/Sheet
      automations) once the org model exists.
- [ ] Optional: OAuth2/JWT for third-party integrations, but session cookies are fine for
      the first-party UI — don't over-build.

### 3. Per-user integration credentials (the "separate token script" problem)

This is the user's explicit question #5, and it's the sharpest SaaS blocker.

Today, browser integrations (Shopee, 104, Tasker) persist a Playwright `storage_state`
to **one shared file per integration for the whole deployment**:

- Shopee → `data/shopee_state.json` (`scripts/shopee_login.py:25`, consumed by
  `shopee_scraper_tool.py:27`)
- Tasker → `data/tasker_state.json` (`scripts/tasker_login.py:27`)
- 104 → `data/tw104_state.json` (`scripts/104_login.py`)

Each is created **manually** by a headed Chromium window where an operator solves the
captcha/OTP by hand and presses Enter. **If two tenants each connected their own Shopee
account, the second login would overwrite the first.** This design cannot serve more than
one customer.

**TODO**
- [ ] **Namespace session state per tenant/user**: store the `storage_state` blob in the
      DB (or object storage), keyed by `(org_id, integration)`, **Fernet-encrypted** — reuse
      the exact pattern already used for LLM keys (`settings_store.py:91-124`). The tools
      would load/save the blob instead of a fixed path.
- [ ] **Replace the CLI scripts with an in-app "Connect account" flow.** A user clicks
      "Connect Shopee" in the UI → a server-driven browser session (or a hosted headed
      session / remote-browser service) lets them log in once → the encrypted session is
      saved to their tenant. No terminal, no `.env`, no shared file.
- [ ] Handle **session expiry gracefully**: detect the "logged out" state per tenant and
      surface a "reconnect" prompt in the UI instead of silently failing runs.
- [ ] Never pre-fill credentials from global `.env` (`SHOPEE_USERNAME/PASSWORD` etc.) — in
      SaaS there is no single global account.
- [ ] Security/legal: storing customers' third-party credentials/sessions is a serious
      liability. Document data handling, encrypt everything, and confirm each integration's
      ToS permits automation on a customer's behalf (Shopee/104/Tasker/X all have
      anti-automation terms — this is a real go-to-market risk, not just an engineering one).

### 4. Billing, plans, quotas & rate limiting

**None of this exists.** A grep for plan/tier/subscription/stripe/quota/rate-limit finds
nothing. The system *estimates* per-run cost from a static price table
(`harness/costs.py`, fallback `(1.0, 3.0)` per-1M-token for unknown models) and stores it
in `Run.cost_usd`, and `/stats` aggregates it — but **globally, never grouped by user/org**,
and nothing is ever *enforced*. A customer could trigger unlimited runs and burn unlimited
LLM credits.

**TODO**
- [ ] **Plan/tier model**: e.g. Free / Pro / Enterprise with limits on runs/month,
      concurrent runs, enabled automation types, seats.
- [ ] **Usage metering per org**: add `GROUP BY org_id` rollups of runs, tokens, and
      `cost_usd` (the data is already captured per-run — `models.py:47-51`).
- [ ] **Quota enforcement** at trigger time (`assert_can_run` is the natural hook): reject
      with 402/429 when a tenant exceeds its plan.
- [ ] **Rate limiting** on `POST /jobs/{id}/run` and signup/login (e.g. `slowapi`). Nothing
      throttles run creation today.
- [ ] **Billing integration** (Stripe): subscriptions, metered usage, invoices, dunning.
- [ ] Decide the pricing axis carefully — LLM cost is a real COGS you pass through; browser
      automations (Playwright) are CPU/RAM-heavy and are a *separate* cost center to meter.

---

## P1 — Major (needed to scale & operate reliably)

### 5. Infrastructure & horizontal scalability

The stack is explicitly single-instance (see `aws-ecs-fargate-deployment.md`, which mandates
`desiredCount = 1`). Blockers, in order:

1. **SQLite** (`database.py:9`, `sqlite:///./data/auto.db`). Two replicas = two DB files =
   split-brain; local disk is wiped on redeploy. → **Move to PostgreSQL** (SQLModel already
   supports the dialect switch; documented in `dev-notes.md:9`).
2. **In-process `asyncio.create_task`** (`launcher.py:49`) with an in-memory task registry
   (`registry.py:5`). Jobs are lost on crash (only marked `failed` by
   `reconcile_stale_runs`, `database.py:70` — never resumed). Run **cancel** and SSE
   **stream** only work if the request hits the same process. → **Move to a real queue**
   (ARQ/Dramatiq on Redis, per `dev-notes.md:65`); the `execute_run(run_id, job_type,
   payload)` interface is already queue-friendly.
3. **Scheduler double-fires under replicas.** `CronScheduler._next_due` is memory-only
   (`scheduler.py:40`) with no DB lock or leader election — every replica fires every cron
   job. → Add a distributed lock / `SELECT … FOR UPDATE` claim, or run the scheduler as a
   single dedicated worker.
4. **`APP_SECRET`-derived encryption** must be identical & stable across replicas; rotating
   it logs everyone out *and* makes stored keys undecryptable (`main.py:21`). → Move to a
   proper KMS / secrets manager with key-versioning before you have real customer data.

**TODO**
- [ ] Postgres as the default for any hosted deployment; keep SQLite only for local dev.
- [ ] **Alembic** for migrations. Today migrations are hand-rolled `ALTER TABLE … ADD COLUMN`
      wrapped in `try/except: pass` inside `init_db()` (`database.py:22-55`), plus a
      SQLite-only `julianday()` backfill (`database.py:60`) that breaks on Postgres. No
      versioning, no rollback — untenable once you can't just delete the DB.
- [ ] Redis-backed job queue + separate worker process; persist/resume or explicitly
      re-queue in-flight runs on restart.
- [ ] Single-owner scheduler (distributed lock or dedicated deployment).
- [ ] Secrets manager (AWS Secrets Manager / SSM) instead of plaintext env vars — the AWS
      doc currently chooses plaintext task-def env vars (`aws-ecs-fargate-deployment.md:5`).
- [ ] Object storage (S3) for `uploads/` and generated `reports/` PDFs instead of local
      volumes.

### 6. Modularity — make adding an automation cheap & safe

(User questions #1 & #2.) Adding a job type today is **copy-paste into ~8 files / ~11
hand-synced hardcoded registries** — CLAUDE.md's "6 files" undercounts. The full list:

| # | Location | What you edit |
|---|---|---|
| 1 | `executor.py:69` `_FLOW_MAP` | dispatch dict |
| 2 | `flows/<name>_flow.py` | `Flow[StateModel]` subclass |
| 3 | `crews/<name>_crew/` | `crew.py` + 2 YAML configs |
| 4 | `routers/system.py` `_CATALOG` | **4 sub-lists**: agents/tools/crews/workflows |
| 5 | `settings_store.py:31` `ALL_AUTOMATIONS` | allowlist |
| 6 | `ui/app.js` | `ALL_TYPES`, `TYPE_META`, `AUTO_CATALOG`, `FLOW_STEPS`, + 2 `switch(jobType)` blocks |
| 7 | `ui/index.html` | a `data-type` card + a `fields-<name>` block |
| 8 | `flow_steps.py:15` `FLOW_STEPS` | **overview-grid source of truth — omitted from CLAUDE.md's list** |

This design **already caused a live drift bug**: `tw104_apply` is missing its `workflows`
entry in `system.py` `_CATALOG` — the maintainers missed one of the 11 sync points. And
`flow_steps.py` carries a comment admitting its `FLOW_STEPS` must be hand-mirrored with
`ui/app.js`'s copy of the same data. There is **no plugin/registry/entry-point system** —
all dispatch is static dicts + a dynamic `importlib` of a string path.

For a SaaS whose value grows with its automation catalog, this is the #1 internal-velocity
tax.

**TODO**
- [ ] **Single per-automation manifest** (one Python object or YAML per job type) declaring:
      job_type, display metadata, input schema (fields + types + validation), flow/crew
      class, flow-step definitions, validator rule, eval rubric. Dispatch, the catalog, the
      allowlist, the overview grid, and validation all *derive* from it — one source of truth.
- [ ] **Registration by discovery** (decorator or entry-point / package scan) so a new
      automation self-registers by existing, instead of being enumerated in 11 places.
- [ ] **Schema-driven UI forms.** Today `ui/app.js` (3,083 lines) hand-codes per-type DOM
      ids and two `switch(jobType)` blocks (`:1343` field toggling, `:1467` payload build),
      reading each field by id. Generate the form from the manifest's input schema instead.
      (This is also the lever that lets non-engineers or customers define automations later.)
- [ ] **Conformance test** asserting `_FLOW_MAP.keys() == ALL_AUTOMATIONS ==
      FLOW_STEPS.keys() == _CATALOG entries == UI ALL_TYPES`. This single test would have
      caught the `tw104` drift. Nothing cross-checks the registries today.
- [ ] Reduce flow boilerplate: 8 of 13 flows are near-identical ~45-line scaffolds
      (`grep` finds 64 copies of the `validate_payload`/`resolve_llm`/`extract_usage`
      pattern). Push more into `FlowMixin`/`BaseFlow` or a factory for the standard
      "single-crew LLM" shape; keep bespoke flows (email_collect, tasker, tw104) as-is.
- [ ] **Fix the `pipeline` unpack bug:** `pipeline.py:55` unpacks `result, usage = …` but
      `_run_flow` returns a 3-tuple `(result, usage, serve)` (`executor.py:143`) — the
      `pipeline` job type looks broken for the flow path and has no test catching it.

### 7. Testing & scaffolding for new automations

(User question #2, testing half.) Coverage is real and deep *per automation* (~498 test
functions; e.g. tw104 has 27, tasker 26), with good fixtures (`conftest.py`: `seed_admin`,
`seed_job`, autouse `_stub_evaluate`). But there is **no scaffold/generator and no
parametrized conformance suite** — every automation's tests are bespoke, and nothing
structurally forces a new type to be tested or fully registered.

**TODO**
- [ ] A `create-automation` scaffold (cookiecutter or a small script) that stamps out the
      flow, crew, YAML, manifest entry, and a starter test file.
- [ ] A parametrized "for each job_type" conformance test (registries agree; a minimal
      dry-run smoke works with a stubbed LLM).
- [ ] Coverage tooling in CI (`pytest-cov` with a threshold) — still not configured.

### 8. Security hardening (multi-tenant raises the stakes)

Carrying over from `improvements.md` §5, now higher-severity because untrusted external
users will hit these:

**TODO**
- [ ] **SSRF** in `WebScraperTool` — scrapes any URL, including `169.254.169.254`
      (cloud metadata) and RFC-1918 internal hosts. Add an allowlist/blocklist + response
      size cap. Critical once *customers* control the URL.
- [ ] **No insecure defaults in production**: `admin`/`admin` seed and
      `APP_SECRET=dev-insecure-change-me` are shipped in `.env.example` and are the fallback
      defaults (`main.py:22,36`). Fail-fast (refuse to boot) when these are unset/default in
      a prod env.
- [ ] Rate-limit auth endpoints (brute-force) and run creation (already noted in §4).
- [ ] Per-tenant secrets isolation — one tenant must never be able to read another's LLM
      keys or integration sessions (falls out of the §1 org model if done right).
- [ ] Audit log of security-relevant actions (login, key changes, runs) per tenant.
- [ ] A formal security review / pen-test before GA; a documented data-retention & deletion
      policy (GDPR "delete my account and all runs").

---

## P2 — Polish (raises conversion & retention, not gating)

### 9. Onboarding & product UX

(User question #4.) The UI is a functional 3,083-line vanilla-JS SPA, but it's an operator
console, not a self-serve product.

**TODO**
- [ ] First-run onboarding: guided "create your first automation" wizard, sample/templated
      jobs, empty-state guidance.
- [ ] In-app "Connect account" flows (ties to §3) replacing the CLI login scripts.
- [ ] Usage/billing dashboard for the customer: runs used vs. plan quota, cost, upgrade CTA.
- [ ] Carry over the still-open UX items from `improvements.md` §9: auto-open the run stream
      after triggering, a **Retry** button on failed runs, runs-list pagination, and a
      confirmation before bulk-delete.
- [ ] Notifications (email/webhook/Slack) on run completion/failure — table stakes for an
      automation product people don't watch live.
- [ ] Consider a component framework + build step if the UI keeps growing; a 3k-line single
      file with hand-synced per-type switches won't scale with the catalog.

### 10. Documentation

(User question #3.) Internal docs are genuinely good (CLAUDE.md, dev-notes, design docs).
What's missing is **customer-facing and contributor-facing** docs.

**TODO**
- [ ] End-user docs: what each automation does, required inputs, how to connect accounts,
      limits, pricing, FAQ.
- [ ] Public **API reference** (FastAPI already serves `/docs`; publish a curated version +
      auth/API-key guide + quickstart).
- [ ] "How to add an automation" contributor guide — **but fix the code first** (§6): the
      current CLAUDE.md list is already wrong (6 vs. 8 files, omits `flow_steps.py`). Docs
      that must be hand-synced with 11 registries will always drift; a manifest makes the
      doc short and correct.
- [ ] Operational runbook: deploy, migrations, backups, incident response, on-call.
- [ ] Legal/trust: Terms of Service, Privacy Policy, DPA, sub-processor list, status page.

### 11. Observability for a multi-tenant operator

Good foundation (Langfuse per-run traces, Prometheus `/metrics`, `/health` with DB + key
checks). Gaps for running it *as a service*:

**TODO**
- [ ] **Structured JSON logging** with request-id + `org_id` correlation — today logs are
      unstructured stdout text (no `dictConfig`, no structlog).
- [ ] Per-tenant metrics/dashboards (label Prometheus series by plan/tenant class, not PII).
- [ ] Error tracking (Sentry) and alerting (SLOs on run success rate, latency, queue depth).
- [ ] Ground `costs.py` in real provider pricing / actual billing where possible; the static
      table with a `(1.0, 3.0)` fallback will misprice if you bill on it.

---

## Suggested sequencing

1. **Foundation (P0):** Postgres + Alembic → org/tenant model + data isolation → self-serve
   signup. Nothing else is safe to build until data is tenant-scoped.
2. **Monetization (P0):** usage metering per org → plans/quotas → rate limiting → Stripe.
3. **Integrations (P0):** per-tenant encrypted session store + in-app connect flow (kills the
   shared-file blocker and the CLI scripts in one move).
4. **Scale & reliability (P1):** Redis job queue + worker; single-owner scheduler; secrets
   manager; S3 for uploads/reports.
5. **Velocity (P1):** automation manifest + registry + schema-driven forms + conformance
   test + scaffold — so the catalog can grow without the 11-registry tax.
6. **Polish (P2):** onboarding, notifications, customer dashboard, docs, legal, Sentry.

## Quick wins (small, high-value, do anytime)

- [ ] Add the registry-conformance test (§6) — catches the `tw104` drift class of bug today.
- [ ] Fix the `pipeline.py:55` 3-tuple unpack bug (§6).
- [ ] Scope `GET /jobs` and job detail/edit/delete by owner (§1) — a real isolation hole now.
- [ ] Fail-fast on default `APP_SECRET` / `admin` password in prod (§8).
- [ ] SSRF blocklist in `WebScraperTool` (§8).
- [ ] `https_only=True` + `same_site` on the session cookie in prod (§2).
- [ ] Confirmation dialog before bulk-delete; Retry button on failed runs (§9).
</content>
</invoke>
