# Dev Notes

Extended reference for topics that don't belong in the day-to-day CLAUDE.md.

---

## PostgreSQL (Production)

Set `DATABASE_URL=postgresql+psycopg2://user:pass@host/db` in `.env`. SQLModel/SQLAlchemy handles the dialect switch automatically.

```bash
uv add psycopg2-binary
```

The `ALTER TABLE` migrations in `init_db()` are SQLite-specific. For Postgres use `ADD COLUMN IF NOT EXISTS` instead.

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

## Resumable runs

A restart no longer fails every in-flight run. `main.lifespan` calls `resume.recover_stale_runs()`, which re-launches a stale run (same run id, log and checkpoint) when **all** of these hold, and otherwise marks it failed exactly as before:

- its `AutomationSpec` declares `resumable=True` (`spec.py` — the only place this is decided);
- it saved at least one checkpoint (`run.checkpoint`, a JSON `{step_key: data}` object written through `checkpoint.save()` / read with `checkpoint.load()`);
- `run.resume_count` is below `RUN_RESUME_MAX_ATTEMPTS` (default 2), so a run that keeps killing the process cannot restart forever.

`RUN_RESUME_ENABLED=0` turns resuming off. The checkpoint is cleared when a run reaches a terminal status and before an in-process validation retry (a retry is a fresh attempt).

**Checkpoints are written after the work, so the unit that was in flight is repeated on resume.** Only declare a flow `resumable` when repeating any unit is harmless. Currently resumable: `email_collect` (read-only funnel: discovery is reused, already-crawled businesses are skipped) and `pipeline` (completed steps are replayed from their stored result; the in-flight step is re-run only if its own job type is resumable, and an edited pipeline is not resumed). Deliberately **not** resumable: `tasker_apply`, `tw104_apply`, `linkedin_apply`, `greenhouse_apply`, `email_sender`, `google_form_fill` — a crash between "submitted" and "checkpoint written" would submit twice. Making one of them resumable needs an idempotency check against the remote site (e.g. "already applied" detection) first.

Flows should store an input signature (`checkpoint.signature(...)`) with their checkpoint and ignore a mismatching one: a resumed run reads its payload from the Job row, which may have been edited meanwhile.

---

## Scalability Roadmap

### Task queue

`asyncio.create_task` keeps all automation in-process. For horizontal scaling or crash resilience, migrate to ARQ (Redis-backed) or Dramatiq. The executor interface `execute_run(run_id, job_type, payload)` is already queue-friendly.

### Job scheduling

`Job.schedule` stores a cron expression (e.g. `"0 8 * * *"`) but no scheduler runs yet. Wire up APScheduler in the `lifespan` context (`src/main.py`) to poll for due jobs and call `execute_run`. The `JobCreate.schedule` field already exposes this via the API and UI.
