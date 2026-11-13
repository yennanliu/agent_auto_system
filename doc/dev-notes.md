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

### Prompt caching

`estimate_cost` also takes `cached_tokens` and `cache_write_tokens`: the parts of `tokens_in` billed at the model's cache-read and cache-write rates (`_CACHE_PRICING`). A model with no verified cache rate is charged full input for reads and 1.25x input for writes, so the estimate can run high but not low. `prompt_tokens` always means the whole prompt: OpenAI and Gemini report it that way, and `provider._AnthropicLLM` folds Anthropic's separately reported cache counts back in. The counts reach the run log (`Prompt cache: N tokens read, M written`) and the Langfuse generation's `usage_details`; they are not stored on the `Run` row.

- **Anthropic** caches only what a request marks. `provider._mark_cache_breakpoint` puts one 5-minute `cache_control` breakpoint on the system prompt, which also covers the tool definitions rendered before it. On by default; `PROMPT_CACHE_ENABLED=0` turns it off. crewai 1.14 has no caching option of its own (it calls the Anthropic SDK directly, no litellm), so the marker is added in the `_prepare_completion_params` override.
- **OpenAI and Gemini** cache a repeated prompt prefix on their own once it is long enough (around 1,024 tokens on OpenAI). Nothing is sent differently; the task templates already put the stable text (filter, offer, instructions) before the per-item text, and no crew puts a date or run id in its prompt. Keep it that way.

**What it saves today: nothing for the built-in crews.** Their stable prefix (tools + system prompt) is about 140-300 tokens by an offline `tiktoken` count, perhaps 400 on Claude's newer tokenizer. Anthropic's minimum is 512 tokens on the 5.x models, 1,024 on Sonnet 4.6 and 4,096 on Haiku 4.5 (the default), and a shorter prefix is silently sent uncached. A cache entry also lives 5 minutes, so an hourly or daily schedule would miss it even with a long prompt. Caching starts to pay when a custom automation has long instructions (they become the system prompt) and runs many times within minutes, or when a funnel flow judges dozens of items in a row with a long shared prefix. The task message is deliberately not marked: most crews make two LLM calls per run, and a cache write that is read at most once costs 25% extra for nothing.

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
