# MCP Server — System Design (Option A: thin server over the REST API)

> **Status:** Approved direction, design for review · **Date:** 2026-11-28
> **中文版:** [mcp-server-design.zh-TW.md](mcp-server-design.zh-TW.md)
> **Related:** [feasibility study](mcp-server-feasibility.md) ·
> [implementation plan](mcp-server-implementation.md)

---

## 1. Context

The [feasibility study](mcp-server-feasibility.md) chose **Option A**: a separate MCP server
process that talks to the existing FastAPI app over HTTP with a personal API token. It does
**not** import flows, crews, tools, or the database.

This document covers the components, contracts, data model, security model and failure
behaviour. The [implementation plan](mcp-server-implementation.md) breaks it into PRs.

## 2. Goals and Non-Goals

**Goals**

| # | Goal |
|---|---|
| G1 | An MCP host (Claude Code, Claude Desktop, …) can discover, describe, run, watch, cancel and inspect automations. |
| G2 | Every MCP-triggered run uses the **same execution path** as a UI click: `launch_run` → concurrency gates → executor → validator → evaluator. |
| G3 | The same permissions apply: per-user allowlist, globally disabled automations, run/job ownership. |
| G4 | **Outward side effects (apply / send / submit) cannot happen over the API without a successful dry run and an explicit confirmation**, enforced by the REST server. |
| G5 | Results the LLM reads always carry the quality signals (`validation_passed`, `eval_score`, …) and stay small enough for a context window. |
| G6 | Adding an automation needs **no MCP code change**: tools are generic over `job_type` and read the catalog at call time. |

**Non-goals (v1):** remote multi-user MCP hosting (Option B), cron schedule editing, admin
settings, uploads (`profit_health_check`), browser-session login, custom automation
authoring, streaming progress notifications.

## 3. Architecture

### 3.1 Component view

```
┌──────────────────────────┐
│ MCP host                 │  Claude Code / Claude Desktop / any MCP client
│ (LLM + user approval UI) │
└────────────┬─────────────┘
             │ MCP over stdio (JSON-RPC)
┌────────────▼─────────────────────────────────────────────┐
│ aas-mcp  (new process: `python -m src.mcp_server`)       │
│  ├─ server.py   FastMCP app, tool registration           │
│  ├─ tools.py    9 tools; argument checks; dry-run default│
│  ├─ client.py   httpx.AsyncClient (Bearer, timeouts)     │
│  └─ format.py   error → tool-error mapping, text render  │
│  stateless · no DB · no flow/crew imports                │
└────────────┬─────────────────────────────────────────────┘
             │ HTTPS/HTTP + `Authorization: Bearer aas_…`
┌────────────▼─────────────────────────────────────────────┐
│ FastAPI app (existing, extended)                         │
│  auth.current_user  ← NEW: Bearer token path + scopes    │
│  /api/jobs, /api/jobs/{id}/run  ← NEW: payload validation│
│                                  + side-effect guard     │
│  /api/automations/{type}/schema ← NEW                    │
│  /api/runs/{id}/summary         ← NEW (trimmed view)     │
│  /api/runs/{id}/confirm         ← NEW (confirmation tok) │
│  /api/tokens                    ← NEW (create/list/revoke)│
│            │                                             │
│            ▼                                             │
│  launcher.launch_run(trigger="api") → concurrency → executor
│            → Flow → Crew → Tools → validator → evaluator │
└──────────────────────────────────────────────────────────┘
```

**Rule:** every safety and permission decision lives in the **REST server**. The MCP server
only adds conveniences (safe defaults, waiting, formatting). A raw `curl` with the same
token gets exactly the same protections.

### 3.2 Deployment

| Setup | MCP server runs on | `AAS_BASE_URL` | Notes |
|---|---|---|---|
| Local dev (primary) | developer laptop, spawned by the host over stdio | `http://127.0.0.1:8000` | Same machine as the browser-session automations |
| Laptop → hosted app | developer laptop | `https://<host>` | Token over TLS only; browser automations still need sessions on the host |

There is nothing to deploy server-side for the MCP process itself. The FastAPI changes ship
with the normal app.

## 4. REST API Changes

### 4.1 Personal API tokens

- Format: `aas_` + 32 bytes URL-safe random (≈43 chars). Shown **once** at creation.
- Stored as `sha256(token)`; plain text is never persisted or logged. (High-entropy random
  tokens don't need bcrypt; sha256 allows an indexed O(1) lookup.)
- `auth.current_user` order: **Bearer header first**, then session cookie, then desktop mode.
  A Bearer header that is present but invalid → **401**. It must not fall back to the cookie.
- On success: `request.state.api_token = <ApiToken>`; `last_used_at` is updated (throttled to
  once per minute).
- Owner must be active; the token gets the owner's role and allowlist. **A token never has
  more rights than its user.**

**Scopes** narrow a token below its user:

| Scope | Allows |
|---|---|
| `read` | All `GET` endpoints the user can already see |
| `run` | Create jobs, trigger runs and cancel runs, **only in safe mode** for `side_effects="external"` types |
| `run:external` | Confirmed real runs of `external` types (needs a confirmation token too, §4.4). **Admin-granted only.** |

Session (cookie) requests keep their current behaviour: they implicitly hold every scope and
skip the side-effect guard. The UI is unchanged.

Endpoints: `POST /api/tokens` (name, scopes, optional `expires_in_days`) → plain token once;
`GET /api/tokens` (own tokens: prefix, name, scopes, last used); `DELETE /api/tokens/{id}`
(revoke). Admins can list and revoke anyone's tokens and grant `run:external`. Token
management itself requires a **session** (a token cannot mint tokens).

### 4.2 Payload validation and schema endpoint

`spec.payload_schema(job_type) -> dict | None` builds JSON Schema (draft 2020-12):

| `Field.type` | JSON Schema |
|---|---|
| `text`, `textarea` | `string` |
| `url` | `string`, `format: uri` |
| `number` | `number` (+ `minimum`/`maximum` from `min`/`max`) |
| `checkbox` | `boolean` |
| `select` | `string` + `enum` of option values |

`required` comes from `Field.required`, defaults from `Field.default`, and descriptions from
`label` + `help`. For `custom_ui` specs, the schema comes from an opt-in
`AutomationSpec.payload_model` (a Pydantic model, usually a subset of the flow's state
model). Until a spec declares one, the schema is `null` and the payload is accepted as
before. Custom automations (`custom:<slug>`) build a schema from their declared inputs.

`POST /api/jobs` and `PATCH /api/jobs/{id}` validate the payload against the schema:

- **Token requests: strict.** Type, required, range and enum errors **and unknown keys** →
  `422` with a list of `{path, message}` the LLM can act on.
- **Session requests: lenient.** Type, required, range and enum errors are `422`. Unknown keys
  only log a warning, so existing UI forms that send extra keys keep working. They switch to
  strict once the UI tests confirm that is safe.

`GET /api/automations/{job_type}/schema` → `{job_type, name, desc, side_effects,
safe_overrides, schema, rubric, steps, browser}`. Returns `404` when the type is unknown,
disabled, or not in the caller's allowlist. Hiding it this way avoids revealing which types
exist.

### 4.3 Side-effect declaration (spec SSOT)

New `AutomationSpec` fields:

```python
side_effects: Literal["none", "external"] = "none"
# Payload values that make an "external" run harmless (a dry run). None = no safe mode.
safe_overrides: dict | None = None
```

| job_type | side_effects | safe_overrides |
|---|---|---|
| `linkedin_apply`, `tw104_apply`, `tasker_apply`, `greenhouse_apply`, `ats_apply` | external | `{"dry_run": true}` |
| `freelancer_bids` | external | `{"submit_bids": false}` |
| `email_sender`, `google_form_fill` | external | `None` (no safe mode, so it always needs confirmation) |
| `pipeline` | *derived per call*: external if any step is | the steps' overrides combined |
| all others, `custom:*` | none | — |

`test_spec_registry.py` gains these assertions: `external` ⇒ `resumable=False`;
`safe_overrides` keys exist in the flow's state model; every flow that mentions
`dry_run`/`submit` is declared `external`. The last one is a grep-level safety net.

### 4.4 Side-effect guard and confirmation

The guard runs in `trigger_run` (`POST /api/jobs/{id}/run`) **only for token-authenticated
requests**:

```
spec.side_effects == "none"                 → needs scope `run`              → launch
payload ⊇ safe_overrides  (safe mode)       → needs scope `run`              → launch
otherwise (real side effect)                → needs scope `run:external`
                                              AND header X-Confirmation-Token
                                              valid for (user, job_type, payload_hash)
                                              → consume token → launch
                                              else 403 {code:"confirmation_required"}
```

"Payload ⊇ safe_overrides" means **explicitly present** with the safe value. The guard does
not rely on a flow default, because defaults can change.

**Confirmation tokens** prove that a reviewed dry run came first:

- `POST /api/runs/{run_id}/confirm` (scope `run:external`): only for a **successful** run with
  `validation_passed=true`, made in safe mode, by the same user, finished < 24 h ago.
- Response: `{confirmation_token, expires_at, job_type, real_payload_preview}`, where
  `real_payload` = the dry-run payload with the `safe_overrides` keys flipped/removed.
- Stored as `sha256(token)` bound to `(user_id, job_type, payload_hash(real_payload))`,
  **single use**, **15-minute TTL**.
- `payload_hash` = sha256 of canonical JSON (`sort_keys=True, separators=(",",":")`).
- Changing any field between preview and submit breaks the hash. The LLM cannot dry-run
  payload A and then submit payload B.

The human gate is the MCP host's approval prompt. The submit tool is marked
`destructiveHint=true` and its description says it performs a real action. A stronger
`MCP_CONFIRM_MODE=ui` (approve in the web UI) is a possible future extension.

### 4.5 Run audit fields

Two new `Run` columns, added with `init_db()` `ALTER TABLE` in try/except:

| Column | Values |
|---|---|
| `trigger` | `manual` · `schedule` · `resume` · `api` |
| `api_token_id` | FK to `ApiToken` or `NULL` |

`trigger_run` passes `trigger="api"` when `request.state.api_token` is set. The UI history
can show an "API" badge, and Analytics can split cost by trigger. `get_stats()` stays a
single SQL pass. No change is needed for this unless a per-trigger breakdown is added later.

### 4.6 Run summary endpoint

`GET /api/runs/{run_id}/summary?log_tail=20&items=10` returns a **bounded** view
(target < 8 KB):

```json
{
  "run_id": 812, "job_id": 301, "job_type": "greenhouse_apply", "status": "success",
  "trigger": "api", "started_at": "…", "finished_at": "…", "duration_secs": 412.3,
  "quality": {"validation_passed": true, "validation_reason": null,
              "eval_score": 82, "eval_confidence": 0.8},
  "model": {"requested": "…", "served": "…", "fallback_used": false},
  "cost": {"usd": 0.04, "tokens_in": 12034, "tokens_out": 2210},
  "log_tail": [{"ts": "…", "msg": "…"}],
  "result": {"counts": {"applied": 0, "would_apply": 7, "skipped": 3},
             "items": [/* first N of the main list, long strings cut to 300 chars */],
             "truncated": true, "error": null},
  "safe_mode": true,
  "links": {"full": "/api/runs/812", "csv": "/api/runs/812/leads.csv"}
}
```

The trimming is generic: count every list in the result; keep the first `items` of the
largest one; cut strings; drop keys over a size budget. A spec can override it later with an
optional `summarize(result) -> dict`. It lives server-side so it is unit-testable and shared.

## 5. Data Model

```
ApiToken                                   ConfirmationToken
─────────────────────────                  ──────────────────────────────
id            int PK                       id            int PK
user_id       int FK user.id  (index)      user_id       int FK user.id
name          str                          token_hash    str UNIQUE
token_hash    str UNIQUE (index)           job_type      str
prefix        str  ("aas_AbC1")            payload_hash  str
scopes        str  JSON list               source_run_id int FK run.id
created_at    datetime                     expires_at    datetime
last_used_at  datetime | None              used_at       datetime | None
expires_at    datetime | None
revoked_at    datetime | None

Run (+ trigger str default "manual", + api_token_id int | None)
```

Both new tables are created by `SQLModel.metadata.create_all` in `init_db()`. Only the two
`Run` columns need `ALTER TABLE`. Works on SQLite and PostgreSQL.

## 6. MCP Tool Contract

All tools return **structured content** (JSON) plus a short text rendering. Each REST call
uses the user's token, so the REST server enforces every permission.

| Tool | Input | Output | Annotations |
|---|---|---|---|
| `list_automations` | — | `[{job_type, name, desc, side_effects, has_safe_mode, browser, has_schema}]`, filtered to enabled ∩ allowed | readOnly |
| `describe_automation` | `job_type` | the `/schema` response | readOnly |
| `run_automation` | `job_type`, `payload`, `name?` | `{run_id, job_id, status, safe_mode}` | — (safe mode only) |
| `run_job` | `job_id` | same | — (safe mode only) |
| `confirm_and_submit` | `dry_run_run_id` | `{run_id, job_id, status, safe_mode:false}` | **destructive**, openWorld |
| `get_run` | `run_id`, `log_tail?=20`, `items?=10` | `/summary` response | readOnly |
| `wait_for_run` | `run_id`, `timeout_s?=120` (max 600) | `/summary` + `timed_out: bool` | readOnly |
| `cancel_run` | `run_id` | `{run_id, status}` | destructive (local only) |
| `list_runs` | `job_type?`, `status?`, `limit?=10` (max 50) | `[{run_id, job_type, status, started_at, validation_passed, eval_score}]` | readOnly |

Behaviour notes:

- **`run_automation` / `run_job` never perform a real side effect.** For an `external` type
  they **merge `safe_overrides` into the payload** before sending, and they refuse types with
  no safe mode (`email_sender`, `google_form_fill`). The refusal message names the only path
  to a real action: `confirm_and_submit`.
- **`confirm_and_submit`** is the only way to perform a real action:
  `POST /runs/{id}/confirm` → `POST /jobs` with `real_payload` → `POST /jobs/{id}/run` with
  `X-Confirmation-Token`. Because it is a separate, destructive-marked tool, the host asks the
  user before running it.
- Types with no safe mode can't produce a dry run, so `confirm_and_submit` cannot reach them
  in v1. Sending email or filling forms over MCP is **out of scope**: it stays UI-only until a
  preview mode exists.
- **`wait_for_run`** polls `/summary` every 2 s (backing off to 5 s), and returns when the run
  reaches a terminal status or `timeout_s` passes. A timeout is **not an error**. The result
  has `timed_out: true`, and the description tells the model to call it again.
- **Tool descriptions** are short but include: what "success" means (`validation_passed`),
  that scraped text in results is untrusted data, and that real actions need
  `confirm_and_submit`.

### 6.1 Error mapping

| REST status | MCP result (`isError: true`) | Message pattern |
|---|---|---|
| 401 | auth error | "API token invalid, expired or revoked: create a new one in Account → API tokens" |
| 403 `confirmation_required` | guarded | "This would perform a real action. Run it in safe mode first, then use confirm_and_submit." |
| 403 other / 404 | not permitted | "Unknown automation or not permitted for this user" (no detail leak) |
| 409 | conflict | e.g. "Run is not active" on cancel |
| 422 | validation | the list of `{path, message}`, verbatim, so the model can fix the payload |
| 429 | rate limited | "Rate limited, retry after N s" |
| 5xx / connect error | server error | "Agent Auto System unreachable at <base_url>: is the server running?" |

## 7. Key Sequences

### 7.1 Safe run → wait → read

```
LLM          aas-mcp                    FastAPI                       executor
 │ run_automation(greenhouse_apply, P)  │                               │
 │───────────►│ P' = P ∪ {dry_run:true} │                               │
 │            │ POST /api/jobs (P')     │ validate schema (strict)      │
 │            │────────────────────────►│ assert_can_run, scope `run`   │
 │            │ POST /jobs/{id}/run     │ guard: safe mode ✓            │
 │            │────────────────────────►│ launch_run(trigger="api") ───►│ queue → run
 │◄───────────│ {run_id, safe_mode:true}│                               │
 │ wait_for_run(run_id)                 │                               │
 │───────────►│ GET /runs/{id}/summary  (poll)                          │
 │◄───────────│ status=success, quality{…}, result{counts, items}       │
```

### 7.2 Confirmed real run

```
LLM ── confirm_and_submit(dry_run_run_id=812) ──► host asks user "Allow?" ── yes
aas-mcp ── POST /runs/812/confirm ─────► checks: own run, success, validation_passed,
                                          safe mode, <24h, scope run:external
        ◄── {confirmation_token, real_payload} (P with dry_run:false)
aas-mcp ── POST /jobs (real_payload) ──► strict validation
aas-mcp ── POST /jobs/{id}/run + X-Confirmation-Token
                                     ──► guard: hash(real_payload) matches, unused, unexpired
                                         → mark used → launch_run(trigger="api")
        ◄── {run_id: 813}
```

## 8. Security Model

| Threat | Control |
|---|---|
| Token leak (shell history, config file) | Scoped, revocable, optional expiry; `run:external` admin-granted; plain text shown once; hashed at rest; never logged (logging filter masks `Authorization`) |
| Prompt injection in scraped content steers the LLM to apply/send | Real actions need `run:external` + a confirmation token bound to a successful dry run of the **same** payload + host approval of a destructive tool. Injected text cannot mint a token. |
| LLM loops and floods runs | Per-token rate limit on `POST /jobs/*/run` (default 30/h, `API_RUN_RATE_LIMIT`), plus the existing per-type queues and the global ceiling; schema `max` caps the `limit` fields |
| Privilege escalation via token | Token ⊆ user: same `assert_can_run`/visibility checks; tokens cannot create tokens; inactive user ⇒ all their tokens fail |
| CSRF on the new endpoints | Bearer auth is not sent automatically by browsers; token management needs a session plus the existing same-site cookie |
| Enumerating automations | 404 for unknown, disabled and not-allowed types alike |
| Replay of a confirmation | Single use, 15-minute TTL, bound to user + job_type + payload hash |

## 9. Reliability and Operations

- **MCP server is stateless.** A host restart loses nothing; runs live in the app. If the
  MCP process dies mid-`wait_for_run`, the run continues and `get_run` picks it up later.
- **App restart** kills active runs, the same as today (`resume.py` relaunches only
  `resumable` types). The MCP tools report what `/summary` says.
- **Timeouts:** httpx connect 5 s, read 30 s per call; `wait_for_run` ≤ 600 s overall.
- **Config (MCP process env):** `AAS_BASE_URL` (default `http://127.0.0.1:8000`),
  `AAS_API_TOKEN` (required), `AAS_TIMEOUT_S`, `AAS_WAIT_MAX_S`.
- **Config (app env):** `API_TOKENS_ENABLED` (default `1`), `API_RUN_RATE_LIMIT`,
  `CONFIRMATION_TTL_S` (default `900`).
- **Observability:** runs carry `trigger="api"` and `api_token_id` and are visible in
  History, Overview, Analytics and Langfuse. Each app log line for a token request has
  `token_prefix` in its context. The MCP server logs to **stderr** only, because stdout is the
  MCP channel.

## 10. Testing Strategy

| Layer | Tests |
|---|---|
| Unit | token hashing/lookup/scopes; `payload_schema` per `Field` type; strict vs lenient validation; guard decision table (§4.4) as a parametrized test; payload hash canonicalisation; summary trimming budget; spec registry side-effect invariants |
| Integration (FastAPI TestClient) | Bearer 401/403 paths; token ⊆ user; confirmation lifecycle (issue → use → reuse rejected → expiry → hash mismatch); `Run.trigger`/`api_token_id` recorded; rate limit |
| MCP end-to-end (in-process) | the SDK's in-memory client session ↔ `aas-mcp` with an `httpx.ASGITransport` pointed at the FastAPI app, `launch_run` patched to a fake flow; covers list → describe → run (safe) → wait → confirm_and_submit, plus every error mapping |
| Manual smoke | `scripts/mcp_smoke.py` against a running server: one `hacker_news_digest` run and one `greenhouse_apply` dry run |

## 11. Alternatives Considered

- **Option B (MCP mounted in FastAPI):** deferred. The §6 tool contract and §4 REST changes
  carry over unchanged if remote hosting is needed later.
- **Guard in the MCP server only:** rejected. Any token holder could bypass it with `curl`.
- **Flow defaults as the safe mode (`dry_run` defaults to `True`):** rejected. The guard
  requires the safe value to be explicit, so a change to a default can't silently make
  API runs real.
- **Exposing scheduling over MCP:** rejected for v1. A wrong schedule repeats a side effect.
