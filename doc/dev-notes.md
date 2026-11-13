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

## Rate limiting

`src/rate_limit.py` — a small in-process limiter built on the `limits` library (the engine
underneath `slowapi`; slowapi's own decorators can't key on the username in the login body or
count only failed attempts). Over-budget requests get **429** with a `Retry-After` header and a
readable `detail`, which the UI shows as-is.

| Env var | Default | Counts | Keyed by |
|---|---|---|---|
| `RATE_LIMIT_ENABLED` | `1` | `0` turns every limit off | — |
| `RATE_LIMIT_LOGIN_IP` | `10/minute` | **failed** `POST /api/auth/login` | client IP |
| `RATE_LIMIT_LOGIN_USERNAME` | `10/5minutes` | **failed** `POST /api/auth/login` | submitted username (lower-cased) |
| `RATE_LIMIT_RUN` | `20/minute;200/hour` | `POST /api/jobs/{id}/run` | authenticated user id |

Values use the `limits` syntax (`N/second|minute|hour|day`, `N/5minutes`); `;` combines several
windows; an empty value disables that one limit; an invalid value logs a warning and falls back to
the default. Moving-window strategy.

- **Login** — successful sign-ins are never counted, so a shared office IP isn't penalised for
  normal use. Once a budget is exhausted the endpoint answers 429 *before* checking the password
  (even a correct one), so a locked-out guesser learns nothing. Trade-off: someone can lock a known
  username out for the window by failing on purpose — the usual price of a per-account limit; raise
  or empty `RATE_LIMIT_LOGIN_USERNAME` if that matters more than distributed guessing.
- **Run trigger** — the check lives in the HTTP route, not in `launcher.launch_run()`, so the cron
  scheduler is **not** rate limited. It caps how *often* a user starts runs, not how many run at
  once.
- **Client IP behind a proxy** — the limiter uses `request.client.host` and never reads
  `X-Forwarded-For` itself (a client-supplied header would make the key spoofable). Behind a
  reverse proxy / load balancer, set uvicorn's `FORWARDED_ALLOW_IPS` (or `--forwarded-allow-ips`)
  to the proxy's address/CIDR — uvicorn then rewrites the client address from the header for
  trusted peers only. Left unset, every client behind the proxy shares one IP bucket: coarse, but
  not bypassable. Do not set it to `*` on a directly reachable server.
- **Single process only** — counters are in memory: they reset on restart and are not shared
  between workers or replicas (N workers ⇒ up to N× each budget). Move the `limits` storage to
  Redis before running more than one worker.
- **Not covered** — SSO callbacks (the provider does its own throttling), `POST /api/auth/password`
  and the admin endpoints (both already require a session).

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
