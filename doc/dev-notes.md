# Dev Notes

Extended reference for topics that don't belong in the day-to-day CLAUDE.md.

---

## PostgreSQL (Production)

Set `DATABASE_URL=postgresql+psycopg2://user:pass@host/db` in `.env`. SQLModel/SQLAlchemy handles the dialect switch automatically.

```bash
uv add psycopg2-binary
```

Schema migrations are dialect-neutral Alembic revisions (next section), so the same
revisions apply to SQLite and Postgres.

---

## Database migrations (Alembic)

`database.init_db()` runs on every startup and calls `src.migrations.run_migrations(engine)`,
which is `alembic upgrade head` plus a one-time adoption step for databases that predate
Alembic. It is idempotent — a database already at head is left untouched.

| Database state | What startup does |
|---|---|
| **Fresh** (none of our tables) | `upgrade head` — the baseline revision builds the whole schema, later revisions apply on top |
| **Legacy** (our tables, no `alembic_version` row) | Create any missing baseline table, run the old hand-written `ALTER TABLE … ADD COLUMN` block **one last time**, verify the live schema against the baseline, then `stamp 0001_baseline` + `upgrade head` |
| **Versioned** (`alembic_version` row) | `upgrade head` (no-op when current) |

If a legacy database does **not** match the baseline, startup stops with
`SchemaMismatchError` listing every difference; nothing is stamped and no revision runs.
The check (`baseline.schema_drift()`) compares tables, column names, column type
families and named indexes. It deliberately does not compare nullability, defaults or
foreign keys: columns added by the legacy `ALTER`s are looser than the baseline's
(e.g. `run.tokens_in` is nullable `INTEGER DEFAULT 0` there) and SQLite cannot change
that in place. A revision that needs those tightened must do it explicitly (batch mode).

Layout:

```
src/migrations/__init__.py            run_migrations(), SchemaMismatchError, in-code Alembic Config
src/migrations/baseline.py            FROZEN: baseline schema, legacy ALTER list, schema_drift()
src/migrations/env.py                 Alembic environment (SQLite uses batch mode)
src/migrations/versions/0001_baseline.py
alembic.ini                           CLI authoring only — the app never reads it
```

Alembic is configured in code and finds its scripts relative to the package, not the
working directory, so it behaves the same under `uvicorn`, in the Docker image (`COPY src/`)
and in the frozen desktop build (`agent_backend.spec` ships `env.py` + `versions/*.py` as
data files, because Alembic loads them by path). A revision that imports a module nothing
else in the app imports needs that module added to `hiddenimports` in the spec.

### Adding a column / table / index

1. Change `src/models.py`.
2. Generate a revision against a throwaway database that is at head (never your real one):
   ```bash
   export DATABASE_URL=sqlite:////tmp/mig.db
   uv run alembic upgrade head
   uv run alembic revision --autogenerate -m "add run.notified_at" --rev-id 0002_run_notified_at
   ```
3. Review the generated file in `src/migrations/versions/`. A `NOT NULL` column on an
   existing table needs a `server_default` (or add nullable → backfill → tighten); keep it
   working on both SQLite and Postgres (no dialect-specific SQL without a guard).
4. `uv run pytest tests/unit/test_migrations.py` — `test_models_match_migration_head`
   fails if the models and the revisions disagree.

Do **not** add `ALTER TABLE` statements to `init_db()` and do **not** edit `baseline.py`:
the baseline is what already-deployed databases were stamped with.

---

## Harness Internals

### provider.py

Two functions:
- `normalize(provider, model)` — resolves `(provider, model)` strings to `(effective_provider, effective_model)` with no API call, no key check. Used by the executor for logging/metrics.
- `resolve(provider, model, temperature)` — creates a `crewai.LLM` instance; raises `OSError` if the API key env var is missing. Called by each flow's `execute_crew`.

`_CATALOG` is the single source of truth for supported providers and models.

### validator.py

`_CHECKS` dict maps job type → validation function. Called after every crew execution. On failure + retries remaining, the executor re-runs the flow and injects `previous_error` into the payload so the LLM can self-correct. This is the **boolean gate** that decides success/failed — distinct from the quality *score* below.

### evaluator.py

LLM-as-judge quality scoring (0-100 score + 0-1 confidence), purely informational — never changes run status. Key properties:

- **Independent judge.** The judge is never the model that produced the output (a model grading its own homework inflates scores). The preferred judge is resolved in precedence order — **admin UI setting** (`settings_store.get_eval_judge()`) → `EVAL_JUDGE_PROVIDER`/`EVAL_JUDGE_MODEL` env → code default (`gemini/gemini-2.5-flash`). Candidate order is then: preferred → a *different* model in the run's provider → (last resort) the run's own model. Candidates whose provider has no API key are skipped via `provider.has_api_key()` (no error-log spam). If it falls all the way to self-grading, confidence is halved and the notes are flagged `[self-graded: ...]`.
- **Configurable at runtime.** Admins pick the judge provider/model under **Admin → Verification LLM** (`GET`/`PUT /api/admin/eval-judge`), stored via `settings_store.set_eval_judge()`. "Auto" (unset) uses the env/default with automatic key-based fallback.
- **Per-job-type rubric** (`_RUBRICS`) injected into the judge prompt so scoring is grounded in each job's contract, not one generic yardstick.
- **Robust parsing** (`_parse_json`): raw JSON → fenced block → first `{...}` object anywhere in the reply.
- **Heuristic fallback** (completeness-based, confidence 0.4) when no judge is available or the output is unparseable. Errored results skip the LLM call entirely.
- Returns `EvalResult(score, confidence, notes, method, judge_model)`; the executor forwards all of these into `record_run(...)`.

### costs.py

Pricing table keyed by bare model name (strips `gemini/` prefix). `estimate_cost(model, tokens_in, tokens_out)` returns USD float.

### tracker.py (removed)

`update_run_metrics()` was merged into `executor._update_run()` via `**metrics` kwargs so metrics and status are written in a single DB session.

### langfuse_tracer.py

Langfuse LLM observability. CrewAI 1.x calls native provider SDKs (not litellm), so we don't rely on a version-specific auto-instrumentor. Instead the executor emits **one Langfuse trace per run** at each terminal branch (success / validation-fail / exception) via `record_run(...)` — a root span (input=payload, output=result, job metadata) with a nested `generation` observation carrying model, `usage_details` (tokens) and `cost_details`, plus `eval_score` (NUMERIC), `eval_confidence` (NUMERIC) and `eval_quality` (CATEGORICAL bucket) as typed trace scores. `record_run` runs off the event loop (`asyncio.to_thread`, it flushes over the network) and **never raises** — observability must not fail a run.

- Enabled when `LANGFUSE_PUBLIC_KEY` + `LANGFUSE_SECRET_KEY` are set (and `LANGFUSE_ENABLED != "false"`); a no-op otherwise. See `.env.example`.
- The client is a memoized singleton (`get_client()`); `reset()` is a test hook. `main.lifespan` initialises it at startup and `flush()`es on shutdown. `/health` reports `langfuse: <bool>`.
- Uses langfuse SDK v4 (OTel-based): `start_observation(as_type=...)`, `score_trace(data_type=...)`, `propagate_attributes` for trace tags. Trace input/output is inherited from the root span (no deprecated `set_trace_io`).
- `ensure_score_configs()` (called once at startup, idempotent) registers the `eval_score` / `eval_confidence` / `eval_quality` **score configs** so scores are typed, bounds-defined and consistently aggregated in the UI. Scores link to configs by **name + data_type** — the OTel `score_trace` path does not persist `config_id`, so we don't pass one.

---

## Scalability Roadmap

### Task queue

`asyncio.create_task` keeps all automation in-process. For horizontal scaling or crash resilience, migrate to ARQ (Redis-backed) or Dramatiq. The executor interface `execute_run(run_id, job_type, payload)` is already queue-friendly.

### Job scheduling

`Job.schedule` stores a cron expression (e.g. `"0 8 * * *"`) but no scheduler runs yet. Wire up APScheduler in the `lifespan` context (`src/main.py`) to poll for due jobs and call `execute_run`. The `JobCreate.schedule` field already exposes this via the API and UI.
