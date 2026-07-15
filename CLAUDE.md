# CLAUDE.md

## Commands

```bash
uv sync                                              # install deps
uv run playwright install chromium                   # browser jobs: form_fill, shopee, x, tasker, email_collect

uv run uvicorn src.main:app --reload --port 8000     # dev server
kill -9 $(lsof -ti:8000)                             # kill port 8000

uv run pytest tests/unit tests/integration -v        # all tests (380)
uv run pytest tests/unit/test_flow.py::test_name -v  # single test
uv run pytest tests/unit tests/integration -v -m "not e2e"  # skip e2e

uv run python scripts/shopee_login.py                # persist a Shopee session (once)
uv run python scripts/tasker_login.py                # persist a tasker.com.tw session (once)
```

## Architecture

```
POST /api/jobs           → create Job row (stores payload JSON, optional cron `schedule`)
POST /api/jobs/{id}/run  → launcher.launch_run() → Run (pending), asyncio.create_task → 202
Background task          → executor.execute_run() → Flow → Crew → Tool(s)
GET /api/runs/{id}/stream → SSE, polls DB every 0.5 s until terminal status
Cron scheduler           → ticks every SCHEDULER_INTERVAL s, fires due schedules via launch_run
GET /api/schedules        → scheduled jobs + next_run_at + last-run summary
GET /api/jobs/{id}/overview → Airflow-style grid: recent runs × per-step status
```

| Layer | File(s) | Role |
|---|---|---|
| Launcher | `src/automation/launcher.py` | `launch_run()` — the single create-Run → task → register path shared by the HTTP trigger and the scheduler |
| Scheduler | `src/automation/scheduler.py` | `CronScheduler` (started in `main.lifespan`); `_sync_and_collect()` is the pure, unit-tested due-detection core. `cron_utils.py` wraps croniter (macros, validation, next-fire). Disable with `SCHEDULER_ENABLED=0` |
| Overview | `src/automation/flow_steps.py` | Canonical flow-step definitions + `infer_step_states()`; source of truth for the `/jobs/{id}/overview` grid (mirrors `ui/app.js` `FLOW_STEPS`) |
| Executor | `src/automation/executor.py` | `_FLOW_MAP` dispatch, retry loop + cross-model fallback, validate → evaluate → Langfuse trace, `_update_run()` |
| Harness | `src/automation/harness/` | `provider.py` (LLM + `fallback_sequence`), `validator.py` (quality gate), `evaluator.py` (independent LLM-as-judge), `costs.py` (pricing), `langfuse_tracer.py` (per-run trace, no-op unless keyed) |
| Flows | `src/automation/flows/*_flow.py` | `crewai.Flow[StateModel]`; each calls `harness.provider.resolve()` at a job-specific `temperature` |
| Crews | `src/automation/crews/*/crew.py` | Plain Python classes — **no `@CrewBase`** (see below) |
| Pipeline | `src/automation/pipeline.py` | Chains steps; later steps read earlier output via `{{steps.N.result}}` |
| Registry | `src/automation/registry.py` | asyncio task dict for run cancellation |
| Auth / RBAC | `src/auth.py`, `src/settings_store.py` | login, per-user `allowed_automations`, global `ALL_AUTOMATIONS`/enabled set, Fernet-encrypted API keys, eval-judge choice |
| SSO / OAuth | `src/oauth.py`, `src/routers/oauth.py` | Google/GitHub sign-in (Authlib); env-gated provider registration, find-link-provision. Setup: [doc/sso-setup.md](doc/sso-setup.md) |
| Routers | `src/routers/` | `auth` · `admin` · `jobs` · `runs` (trigger/cancel/SSE/stats/`report.pdf`/`leads.csv`) · `system` · `sessions` · `uploads` |
| Browser sessions | `src/automation/browser_session.py`, `src/routers/sessions.py` | On-demand headed-browser login refresh for the storage-state automations (tasker/104/Shopee), decoupled from runs. `GET /api/sessions` (freshness), `POST /api/sessions/{name}/login` (background headed login). **Local server only** — gate with `BROWSER_LOGIN_ENABLED=0` on remote hosts. UI: Admin → Sessions |
| UI | `ui/app.js` | `LLM_MODELS` dict drives the provider→model dropdown |

## Key Invariants

**No `@CrewBase` decorators.** `@CrewBase`, `@agent`, `@task`, `@crew` use a module-level memoize cache keyed by `id(self)`. CPython reuses addresses after GC → stale LLM instances on subsequent runs. Each crew is a plain class:

```python
class MyCrew:
    def __init__(self, llm=None): self._llm = llm
    def crew(self) -> Crew: ...  # build Agent/Task/Crew fresh each call
```

**LLM injection** — pass via constructor `MyCrew(llm=llm)`, never post-init.

**Flow state** — `llm_provider` and `llm_model` must be declared as Pydantic fields in the state model to survive `kickoff(inputs=...)`.

**DB migrations** — add new columns in `database.init_db()` as `ALTER TABLE ADD COLUMN` wrapped in try/except. Runs on startup, idempotent.

**Stats** — `get_stats()` does a single SQL pass; keep it that way.

**Automation registry (SSOT)** — every job type is declared once as an `AutomationSpec` in `src/automation/spec.py`. The allowlist (`settings_store.ALL_AUTOMATIONS`), dispatch (`executor._FLOW_MAP`), validator (`_CHECKS`), rubrics (`_RUBRICS`), and step graph (`flow_steps.FLOW_STEPS`) are all **derived** from it — do not hand-edit those tables. `test_spec_registry.py` fails loud if a spec is inconsistent or its flow can't import. The allowlist still gates both UI visibility (`enabled_automations`) and server-side running (`assert_can_run` → `is_automation_enabled`).

**Eval judge independence** — `evaluator.py` must never score with the same model that produced the output (self-grading inflates scores). Preserve the fallback order: configured/independent judge → a sibling model in the run's provider → the run's own model only as a last resort.

**Tracing/eval never break a run** — `evaluator.evaluate()` and `langfuse_tracer.record_run()` must degrade gracefully (heuristic score / no-op) and never raise into the executor.

## Adding a New Job Type

Since Phase 1 of the [extensibility RFC](doc/automation-extensibility-design.md), the
per-job-type tables (`_FLOW_MAP`, `ALL_AUTOMATIONS`, `_CHECKS`, `_RUBRICS`, `FLOW_STEPS`)
are **derived from one declaration**. To add a job type:

1. `src/automation/spec.py` — add one `register(AutomationSpec(...))` (name, icon, flow
   module/class, `start_log`, `temperature`, `steps`, `validate`, `rubric`). This single
   entry drives dispatch, the allowlist, the validator, the rubric, and the step graph.
2. `src/automation/flows/<name>_flow.py` — `Flow[StateModel]` subclass. For a single-crew
   flow, `execute_crew` is one call to `self._run_crew(Crew, temperature=…, inputs=…,
   working_log=…, done_log=…)` (see `hn_digest_flow.py`).
3. `src/automation/crews/<name>_crew/` — YAML configs + `crew.py`.
4. `src/routers/system.py` — add a row to `_AGENT_DEFS` (structural facts only; role/goal/
   backstory are read from your `agents.yaml` automatically).
5. `ui/app.js` (+ `ui/index.html` fields) — add to the UI form. *(Phase 2 of the RFC will
   derive this from a manifest; until then the form is still hand-written.)*

`_CHECKS`/`_RUBRICS`/`FLOW_STEPS` are no longer edited directly — they come from the spec.
Run `pytest tests/unit/test_spec_registry.py` to confirm the new spec is consistent and its
flow imports.

**Manifest-driven UI (Phase 2).** `GET /api/automations/manifest` (`spec.manifest()`) drives
the browser's picker, run form, and step graph. A spec with `custom_ui=False` + declared
`fields` renders its form generically — **no UI edits needed**. Set `custom_ui=True` to keep a
bespoke hand-written `#fields-<type>` form (file upload, pipeline, multi-field flows).

**Custom & plugin automations (Phase 3).** Admins author no-code automations (`custom:<slug>`)
in Admin → Custom; they are DB-backed (`src/custom_automations.py`), run as a single **no-tools**
LLM agent (`DynamicCrew`) through the harness, and appear in the picker via the manifest.
External packages can register specs via entry points — `spec.load_plugins()`, opt-in with
`AUTOMATION_PLUGINS_ENABLED=1`. See [doc/automation-extensibility-design.md](doc/automation-extensibility-design.md).

**Deterministic-funnel flows** (e.g. `email_collect`, `tasker_apply`) — drive the tools directly in the flow (fast/cheap/reliable) and use the crew only for the LLM-judgement step; don't make the agent orchestrate many tool calls. Return partial results + a `warnings` list from scraper tools instead of raising.

**File-upload job types** (e.g. `profit_health_check`) — the UI POSTs files to `POST /api/uploads` (multipart, saved under `uploads/<uuid>/`), then creates the job with a small `{upload_id}` payload; the flow reads the files from disk. Keeps the payload JSON-only and re-runnable. See [doc/profit-health-check-design.md](doc/profit-health-check-design.md).

---

See [doc/dev-notes.md](doc/dev-notes.md) for PostgreSQL deployment, scalability roadmap, and deeper harness internals.
