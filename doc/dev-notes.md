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

## Structured logging

`src/logging_setup.py` (stdlib `logging` only, no extra dependency). Configured from env in
`src.main` — at import and again in the lifespan, because `uvicorn.run(app)` (desktop entry)
applies uvicorn's own logging config after the import.

| Variable | Default | Effect |
|---|---|---|
| `LOG_FORMAT` | `text` | `text`: logging is left exactly as uvicorn sets it up (unchanged dev output). `json`: one JSON object per line on stderr for **every** logger — app, uvicorn (error + access) and third-party libraries. |
| `LOG_LEVEL` | unset | Root level. Unset = untouched in text mode, `INFO` in json mode. In text mode, setting it also installs a plain root handler so app `INFO` lines become visible. |

Sample line (json mode, emitted inside a run):

```json
{"ts": "2026-11-13T02:19:14.201+00:00", "level": "INFO", "logger": "src.automation.executor", "message": "Starting run_id=42 job_type=web_scraper", "request_id": "430a5ce549e7490e8a3950adaada852c", "user_id": 1, "run_id": 42, "job_id": 7, "job_type": "web_scraper"}
```

**Correlation fields** live in one `ContextVar` and are stamped onto records by a handler-level
`ContextFilter` (so library records get them too). Unbound fields are omitted.

- `request_id`, `user_id` — bound per HTTP request by `RequestContextMiddleware` (pure ASGI,
  inside `SessionMiddleware`). A well-formed incoming `X-Request-ID` is honoured, otherwise one
  is generated; either way it is echoed on the response. `user_id` is the session's user id.
- `run_id`, `job_id`, `job_type`, `user_id` (the run's owner) — bound by `launcher.launch_run`
  around `asyncio.create_task` (a task copies the context at creation) and by
  `executor.execute_run` inside the task. A run triggered over HTTP keeps the triggering
  `request_id`; scheduled runs have none. Concurrent runs each have their own context.

**Threads.** The context follows `await`, `asyncio.create_task` and `asyncio.to_thread` (how the
executor runs `flow.kickoff`, the evaluator and Langfuse), and CrewAI 1.x copies it into its own
worker threads (flow methods, async tasks, parallel tool calls) — covered by a test that kicks
off a real `Flow`. It does **not** reach a thread started with a bare `threading.Thread`,
`ThreadPoolExecutor.submit` or `loop.run_in_executor` (CrewAI's async `ainvoke` tool path does
this; the app uses sync `kickoff`). Records from such threads are still JSON, just without the
ids — wrap the target in `contextvars.copy_context().run` if you add one.

**Limits / secrets.**
- Only the ids above can be bound (`bind_log_context` rejects anything else) — payloads, API
  keys, passwords and cookies never enter the context. `extra=` values under sensitive-looking
  keys (`password`, `token`, `api_key`, `authorization`, `cookie`, `payload`, …) are redacted and
  access-log lines drop the query string (OAuth `code`/`state`). Message *text* is not scrubbed:
  never format a secret into a log message.
- Only `logging` records are JSON. Raw `print()` / CrewAI `verbose=True` console output and
  interpreter tracebacks written straight to stderr are not (crews run with `verbose=False`).
- The per-run progress log shown in the UI (`progress.append_log` → DB) is separate and unchanged.
- The Docker image and compose file keep the `text` default; set `LOG_FORMAT=json` in the
  environment where a collector (CloudWatch, Loki, …) ingests the output.

---

## Scalability Roadmap

### Task queue

`asyncio.create_task` keeps all automation in-process. For horizontal scaling or crash resilience, migrate to ARQ (Redis-backed) or Dramatiq. The executor interface `execute_run(run_id, job_type, payload)` is already queue-friendly.

### Job scheduling

`Job.schedule` stores a cron expression (e.g. `"0 8 * * *"`) but no scheduler runs yet. Wire up APScheduler in the `lifespan` context (`src/main.py`) to poll for due jobs and call `execute_run`. The `JobCreate.schedule` field already exposes this via the API and UI.
