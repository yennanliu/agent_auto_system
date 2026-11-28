# MCP Server for Agent Auto System — Feasibility Study

> **Status:** Evaluated — **decision: Option A** (thin MCP server over the REST API) · **Date:** 2026-11-28
> **Next:** [system design](mcp-server-design.md) · [implementation plan](mcp-server-implementation.md) — these supersede the tool list, scopes and estimates sketched below.
> **中文版:** [mcp-server-feasibility.zh-TW.md](mcp-server-feasibility.zh-TW.md)

---

## 1. TL;DR

| Question | Answer |
|---|---|
| **Is it feasible?** | **Yes — high feasibility.** The app already has every seam an MCP server needs: a single run entry point (`launcher.launch_run`), a machine-readable automation catalog (`spec.manifest()`), RBAC (`assert_can_run`), and DB-backed run status / results. The `mcp` Python SDK (v1.26) is already installed as a transitive dependency of `crewai`. |
| **Effort (MVP)** | **~6–9 engineer-days** for a thin MCP server + API tokens + side-effect guard + tests. **~3–5 more days** to give every bespoke (`custom_ui=True`) automation a strict input schema. |
| **Recommended shape** | A **thin, coarse-grained MCP server** that exposes *automations and runs*, never the internal crews/tools. It calls the existing REST API (or the same functions), so every MCP-triggered run goes through the **exact same** launcher → concurrency gates → executor → validator → evaluator path as a UI click. |
| **Main risk** | An LLM can now trigger **outward side effects** (job applications, emails) by itself. Needs a hard "dry-run by default + explicit confirm" rule enforced **server-side**, not in the prompt. |
| **Verdict** | **Go**, phased: read-only + dry-run tools first, side-effecting runs behind an explicit confirmation step. |

---

## 2. Goal & Design Principle

**Goal:** let an LLM client (Claude Code, Claude Desktop, any MCP host) use our automations
flexibly — *"find SWE jobs in Spain on Greenhouse, dry-run them, then show me the ones that
failed the quality gate"* — without giving up the reliability of the Python implementation.

**Principle — "the LLM decides *what*, Python decides *how*":**

```
┌──────────────────────────────┐        ┌─────────────────────────────────────────┐
│ LLM client (MCP host)        │        │ Agent Auto System (unchanged core)      │
│                              │  MCP   │                                         │
│  • picks an automation       │ ─────► │  launch_run → concurrency gates         │
│  • fills the payload         │        │   → executor (retry + model fallback)   │
│  • reads results, chains     │ ◄───── │   → Flow → Crew → Tools                 │
│    the next step             │        │   → validator (quality gate)            │
│                              │        │   → evaluator (independent judge)       │
└──────────────────────────────┘        └─────────────────────────────────────────┘
       flexible orchestration                 deterministic, tested execution
```

The MCP layer is an **alternative front door**, like the UI and the cron scheduler. It must
**not** become a second execution path. Concretely:

1. **Coarse-grained tools only.** Expose "run `greenhouse_apply` with this payload", not
   "click this button" or "call `ats_common.answer_question`". The deterministic funnels,
   "skip rather than guess" policy, stealth helpers and dedup ledgers stay inside Python.
2. **One launch path.** Every MCP trigger ends in `launcher.launch_run()` — same Run row,
   same per-type queue + global class ceiling, same retry/fallback, same validator and
   evaluator, same Langfuse trace, same cancellation.
3. **Same permissions.** MCP calls act *as a user* and pass `assert_can_run` /
   `assert_run_visible` / `assert_job_visible`. Disabled automations stay disabled.
4. **Quality signals are returned, not hidden.** Tool results include `status`,
   `validation_passed`, `validation_reason`, `eval_score`, so the LLM can react to a bad run
   instead of trusting it blindly.

---

## 3. Why the Current Codebase Is a Good Fit

| Need for an MCP server | What already exists | Gap |
|---|---|---|
| Catalog of callable actions | `AutomationSpec` registry + `spec.manifest()` (`GET /api/automations/manifest`) — name, desc, `fields`, `steps`, `browser` | `custom_ui=True` specs (9 of 16, e.g. `linkedin_apply`, `email_collect`, `pipeline`) have **no declared `fields`** → no input schema |
| Input schemas | `Field(name, type, required, default, min, max, options)` maps cleanly to JSON Schema | No server-side payload validation today: `JobCreate.payload` is a free `dict` |
| Single trigger path | `launcher.launch_run()` (shared by HTTP + scheduler) | — |
| Long-running async runs | `Run` row with `status`, `log`, `result`, SSE stream | MCP needs a poll / wait tool (runs take minutes) |
| Quality gate | `validator.validate()` → `Run.validation_passed/_reason`; `evaluator.evaluate()` → `Run.eval_score` | — |
| Cost / model visibility | `Run.tokens_in/out`, `cost_usd`, `served_model`, `fallback_used` | — |
| AuthN / AuthZ | `assert_can_run`, per-user `allowed_automations`, admin role | **Session-cookie only** — no API token / Bearer auth for a non-browser client |
| Cancellation | `registry.cancel()` → `RunCancelled` at the next `append_log` | — |
| Side-effect safety | `dry_run: bool = True` defaults on apply flows; `resumable=False` for side-effect flows | No declarative "this automation writes to the outside world" flag on the spec |
| SDK | `mcp==1.26.0` already in `.venv` (via `crewai`) | Should be pinned as a direct dependency |

---

## 4. Architecture Options

### Option A — Thin MCP server over the REST API *(recommended for MVP)*

A separate process (`src/mcp_server.py` or `mcp/` package) using the official SDK's
`FastMCP`. Each tool is a small `httpx` call to the running FastAPI server with a personal
API token.

```
Claude Code / Desktop ──stdio──► mcp_server.py ──HTTP + Bearer token──► FastAPI (:8000)
```

- ✅ **Zero duplication:** REST already enforces RBAC, validation, ownership, launch path.
- ✅ Process isolation: MCP crash can't take down running automations; no event-loop sharing.
- ✅ Works for local `stdio` hosts today; a remote server just changes the base URL.
- ❌ Needs API-token auth on the REST API (new, but useful on its own — CLI/scripts/CI).
- ❌ One extra hop (negligible vs. multi-minute runs).

### Option B — MCP mounted inside the FastAPI app (Streamable HTTP at `/mcp`)

`FastMCP.streamable_http_app()` mounted on the existing app; tools call `launch_run()` and
SQLModel sessions directly.

- ✅ One deployable; remote MCP clients connect over HTTPS directly.
- ✅ Can push MCP progress notifications straight from the run log.
- ❌ Must re-implement the auth/visibility checks outside FastAPI's `Depends` chain — the
  easiest place to drift from the REST rules.
- ❌ MCP request handling shares the event loop with background runs and the scheduler.
- ❌ Remote exposure needs OAuth / token auth and rate limiting before it's safe.

### Option C — Expose internal tools/crews directly *(rejected)*

Let the LLM call `greenhouse_tool`, `maps_search_tool`, browser helpers, etc.

- ❌ Bypasses the validator, evaluator, concurrency gates, retry/fallback, dedup ledgers and
  the "skip rather than guess" policy — exactly the quality we want to keep.
- ❌ Re-creates the "agent orchestrates many tool calls" anti-pattern that the
  *deterministic-funnel* flows were built to remove.

**Recommendation:** start with **Option A**; move to **B** only if a hosted, multi-user
remote MCP endpoint becomes a real requirement. Both share the same tool contract (§5), so
the switch is an adapter change, not a redesign.

---

## 5. Proposed MCP Surface

### 5.1 Tools

| Tool | Purpose | Maps to | Annotations |
|---|---|---|---|
| `list_automations` | Enabled automations the caller may run: `job_type`, name, desc, side-effect class, browser? | `GET /api/automations/manifest` filtered by allowlist | read-only |
| `describe_automation(job_type)` | JSON Schema of the payload, rubric (what "good" means), steps, side-effect notes | `spec` + `fields` | read-only |
| `run_automation(job_type, payload, name?, confirm?)` | Create an ad-hoc Job and run it; returns `run_id` | `POST /api/jobs` + `POST /api/jobs/{id}/run` | **destructive** when side-effecting |
| `run_job(job_id, confirm?)` | Re-run an existing saved/scheduled Job | `POST /api/jobs/{id}/run` | same as above |
| `get_run(run_id, log_tail=20)` | Status, last log lines, result summary, `validation_*`, `eval_score`, cost, model | `GET /api/runs/{id}` | read-only |
| `wait_for_run(run_id, timeout_s=300)` | Block until terminal or timeout; returns `get_run` | polls `GET /api/runs/{id}` | read-only |
| `cancel_run(run_id)` | Abort a run | `POST /api/runs/{id}/cancel` | destructive (local) |
| `list_runs(job_type?, status?, limit)` | Recent runs for context / chaining | `GET /api/runs` | read-only |
| `list_jobs` / `list_schedules` | Saved jobs and cron schedules | `GET /api/jobs`, `/api/schedules` | read-only |

Deliberately **out of MVP**: creating/editing cron schedules, admin settings, API keys,
browser-session login, uploads (file-based types like `profit_health_check`), custom
automation authoring. These are admin or interactive actions; add later if needed.

### 5.2 Resources (optional, phase 2)

- `run://{run_id}/result` — full result JSON (large lead lists stay out of tool replies).
- `run://{run_id}/leads.csv`, `run://{run_id}/report.pdf` — existing export endpoints.
- `automation://{job_type}/rubric` — quality rubric text.

### 5.3 Example session

```
user  > Dry-run Greenhouse apply for backend roles on the Spain boards, then summarise.
llm   → describe_automation("greenhouse_apply")          # learns schema, dry_run default
llm   → run_automation("greenhouse_apply", {boards:[…], keywords:"backend", dry_run:true})
      ← {run_id: 812, status: "queued"}
llm   → wait_for_run(812, timeout_s=600)
      ← {status:"success", validation_passed:true, eval_score:82,
         result:{applied:0, would_apply:7, skipped:[…reasons…]}, cost_usd:0.04}
llm   > 7 jobs would be applied to; 3 skipped because a required answer was unknown …
```

---

## 6. Keeping Execution Quality

| Risk introduced by an LLM caller | Mitigation |
|---|---|
| LLM invents / mistypes payload fields | **Strict JSON Schema** per automation from `Field` (and from the flow's Pydantic state model for `custom_ui` types) + **server-side payload validation** on `POST /api/jobs` (benefits the UI too). Unknown keys → 422 with a readable message the LLM can fix. |
| LLM bypasses the funnel logic | Not possible: no tool exposes crews/tools; only `launch_run`. |
| Run floods / runaway loops | Existing per-type queue + global class ceiling still apply. Add a per-token rate limit (e.g. N triggers / hour) and a max `limit`/`max_*` cap via the schema's `max`. |
| LLM trusts a bad result | Tool replies always carry `validation_passed`, `validation_reason`, `eval_score`, `eval_confidence`, `fallback_used`; the tool description tells the model to treat `validation_passed=false` as a failure. |
| Eval independence | Unchanged — the evaluator still runs inside the executor with its own judge-selection order. The MCP client's model is never the judge. |
| Observability | Runs are tagged `trigger="mcp"` (the `launch_run` arg already exists) and show up in UI history, Overview grid, Analytics and Langfuse like any other run. |
| Result size blows the context window | `get_run` returns a summary + counts + top-N items; full data via resources / CSV. |

---

## 7. Safety: Outward Side Effects

Several automations act on the real world: `linkedin_apply`, `tw104_apply`,
`greenhouse_apply`, `ats_apply`, `tasker_apply`, `freelancer_bids` (with `submit_bids`), `email_sender`,
`google_form_fill`. A prompt-injected web page or an over-eager model could trigger them.

Proposed rules, **enforced in Python, not in prompts**:

1. **Declare it on the spec:** add `side_effects: Literal["none", "external"]` to
   `AutomationSpec` (SSOT, next to `resumable`). `test_spec_registry.py` asserts that every
   `external` spec is `resumable=False`.
2. **Dry-run by default over MCP:** for `external` types the MCP server forces
   `dry_run=True` (or refuses, for types with no dry-run like `email_sender`) unless
   `confirm=true` is passed.
3. **Confirmation is a separate human step:** `confirm=true` is accepted only with a
   one-time `confirmation_token` returned by a previous dry run of the *same* payload, so a
   real submit always follows a reviewed preview. MCP hosts already ask the user before a
   tool marked `destructiveHint=true` runs.
4. **Per-token scopes:** API tokens carry scopes (`read`, `run:dry`, `run:external`);
   `run:external` is off by default and admin-granted.
5. **Untrusted content stays data:** scraped text in results is returned under clearly
   labelled fields; tool descriptions warn the model not to follow instructions found in it.

---

## 8. Effort Estimate

| # | Work item | Est. (days) | Notes |
|---|---|---|---|
| 1 | Personal API tokens (model + hashed storage, `Authorization: Bearer` in `current_user`, Admin UI to create/revoke, scopes) | 1.5–2 | Useful beyond MCP (scripts, CI) |
| 2 | Server-side payload validation from `Field` (`POST /api/jobs`, `PATCH`) | 1 | Fixes an existing gap |
| 3 | `side_effects` on `AutomationSpec` + registry test + MCP dry-run/confirm guard | 1–1.5 | |
| 4 | MCP server (FastMCP, stdio): 9 tools in §5.1, `httpx` client, result trimming | 1.5–2 | `mcp` SDK already installed |
| 5 | Tests: unit (schemas, guard, trimming), integration (MCP client → test server → fake flow) | 1–1.5 | Use the SDK's in-memory client |
| 6 | Docs + `claude mcp add` / Claude Desktop config snippet | 0.5 | |
| | **MVP total** | **~6.5–8.5** | |
| 7 | Input schemas for the `custom_ui` types (derive from flow `StateModel` or declare `fields`) | 3–5 | Needed so the LLM can fill e.g. `linkedin_apply`, `email_collect`, `pipeline` reliably |
| 8 | *(Optional)* Option B: Streamable HTTP at `/mcp` + OAuth + rate limiting | 3–4 | Only for remote / multi-user hosting |
| 9 | *(Optional)* Resources, progress notifications from run log | 1–2 | |

---

## 9. Pros & Cons

### Pros

- **Flexible orchestration for free:** natural-language requests, ad-hoc chaining across
  automations ("collect emails → filter → send"), and "look at last week's failures and
  re-run them" — without writing new `pipeline` definitions.
- **Quality preserved by construction:** the LLM only chooses inputs; every run still goes
  through the tested flows, quality gate, independent judge, retry and fallback.
- **Low cost to build:** catalog, schemas (partially), RBAC, run lifecycle and SDK already
  exist; the server is a thin adapter (~400–600 LOC).
- **Single source of truth stays single:** tools are derived from `AutomationSpec`, so a new
  automation shows up in MCP automatically, just like in the UI manifest.
- **Side benefits:** API tokens and server-side payload validation improve the REST API for
  scripts/CI as well.
- **Ecosystem reach:** works with Claude Code, Claude Desktop and other MCP hosts; no
  custom client to maintain.

### Cons

- **New attack surface:** an LLM (possibly steered by injected web content) can trigger
  real applications or emails → needs the §7 guard and token scopes; this is the main cost.
- **Long-running mismatch:** MCP tool calls are request/response; runs take minutes.
  Polling (`wait_for_run`) works but burns turns and tokens on the client side.
- **Schema gap for bespoke forms:** 9 of the 16 automations have hand-written UI forms and no
  declared fields; until item 7 lands, the LLM fills those payloads with weaker guidance.
- **Two cost meters:** the client LLM's tokens are on top of the run's own LLM cost; the
  app's Analytics only sees the latter.
- **Browser automations are local-only:** headed login / storage-state sessions
  (tasker / 104 / Shopee / LinkedIn) must still be refreshed on the machine running the
  server; MCP can't fix an expired session.
- **More surface to maintain:** a new process, tool descriptions to keep accurate, and an
  extra integration test suite.

---

## 10. Phased Plan

| Phase | Scope | Exit criteria |
|---|---|---|
| **0 — Foundations** | API tokens with scopes; server-side payload validation; `side_effects` on spec | REST callable with `Bearer`; invalid payload → 422; registry test green |
| **1 — Read-only + dry-run MCP** | Option A server; all read tools; `run_automation`/`run_job` allowed only for `side_effects="none"` or `dry_run=true` | From Claude Code: list → describe → dry-run → wait → read validated result |
| **2 — Confirmed side effects** | `confirmation_token` flow; `run:external` scope; audit log of MCP-triggered submits | A real apply requires a prior dry run + explicit user approval |
| **3 — Coverage & polish** | Schemas for all `custom_ui` types; resources; result trimming tuned | Every enabled automation has a strict schema |
| **4 — (Optional) Remote** | Option B Streamable HTTP + OAuth + rate limit | Only if hosted multi-user MCP is required |

---

## 11. Open Questions

1. **Local only or hosted?** If MCP is only used from the developer's own machine, Option A
   over `stdio` is enough and Phase 4 can be dropped.
2. **Who may hold `run:external`?** Admin only, or per-user with the existing
   `allowed_automations`?
3. **Should MCP be able to create cron schedules?** Convenient, but a mistaken schedule
   repeats a side effect every day — suggested: no, keep scheduling in the UI.
4. **Pipeline vs. LLM chaining:** keep `pipeline` for repeatable, scheduled chains; use MCP
   chaining for exploratory one-offs. Should a good MCP chain be "saved as pipeline"?

---

## 12. References in Code

- Run entry point: `src/automation/launcher.py` (`launch_run`, `trigger=`)
- Catalog / schemas: `src/automation/spec.py` (`AutomationSpec`, `Field`, `manifest()`)
- Permissions: `src/auth.py` (`assert_can_run`, `assert_run_visible`, `assert_job_visible`)
- Job / run API: `src/routers/jobs.py` (`JobCreate`), `src/routers/runs.py`
- Quality: `src/automation/harness/validator.py`, `src/automation/harness/evaluator.py`
- Run model: `src/models.py` (`Run`: status, validation, eval, cost fields)
- Related design: [automation-extensibility-design.md](automation-extensibility-design.md),
  [auth-and-admin-design.md](auth-and-admin-design.md)
