# MCP Server — Implementation Plan (Option A)

> **Status:** Plan for review · **Date:** 2026-11-28
> **中文版:** [mcp-server-implementation.zh-TW.md](mcp-server-implementation.zh-TW.md)
> **Design:** [mcp-server-design.md](mcp-server-design.md) · **Why:** [feasibility study](mcp-server-feasibility.md)

This plan turns the [system design](mcp-server-design.md) into **5 reviewable PRs**. Each PR
is independently mergeable, keeps the UI working, and ships with tests. Section numbers
like "§4.4" refer to the design doc.

---

## 0. Overview

| PR | Title | Depends on | Est. (days) | User-visible result |
|---|---|---|---|---|
| 1 | API tokens + run audit fields | — | 2 | Users can create tokens; REST works with `Authorization: Bearer` |
| 2 | Payload schema, validation, run summary | — | 1.5 | `/api/automations/{type}/schema`, `/api/runs/{id}/summary`; bad payloads → 422 |
| 3 | Side-effect declaration + guard + confirmations | 1, 2 | 1.5–2 | Token runs of apply/send types are safe-mode-only unless confirmed |
| 4 | `aas-mcp` server package | 1, 2, 3 | 2 | `claude mcp add …` works end-to-end |
| 5 | Schemas for `custom_ui` automations | 2 | 3–5 | The LLM can fill `linkedin_apply`, `email_collect`, `pipeline`, … with strict schemas |
| | **Total** | | **~10–12.5** | MVP usable after PR 4 (~7–7.5 days) |

PRs 1 and 2 can be built in parallel. Work goes in a sibling git worktree per PR
(`../agent_auto_system-mcp-<pr>`).

---

## PR 1 — API tokens and run audit fields

### Files

| File | Change |
|---|---|
| `src/models.py` | `ApiToken` table (§5); `Run.trigger: str = "manual"`, `Run.api_token_id: int \| None` |
| `src/database.py` | `init_db()`: `ALTER TABLE run ADD COLUMN trigger …` / `api_token_id …` in try/except (idempotent) |
| `src/api_tokens.py` *(new)* | `generate() -> (plain, hash, prefix)`, `hash_token()`, `lookup(session, plain) -> ApiToken \| None`, `has_scope(token, scope)`, `SCOPES = ("read", "run", "run:external")` |
| `src/auth.py` | `current_user`: Bearer first (invalid ⇒ 401, no cookie fallback); set `request.state.api_token`; new `require_scope(scope)` dependency + `token_scopes(request)` helper (session ⇒ all scopes) |
| `src/routers/tokens.py` *(new)* | `POST/GET/DELETE /api/tokens`; admin may list/revoke anyone's and grant `run:external`; **requires a session** (reject when `request.state.api_token` is set) |
| `src/main.py` | `include_router(tokens.router, prefix="/api", dependencies=_gated)` |
| `src/automation/launcher.py` | `launch_run(..., trigger, api_token_id=None)` writes both onto the `Run` row (it already takes `trigger`) |
| `src/routers/runs.py` | `trigger_run`: `trigger="api" if token else "manual"`, pass `api_token_id`; `cancel_run`/`delete` need scope `run` for token requests |
| `src/routers/jobs.py` | `create_job`/`update_job`/`delete_job` need scope `run` for token requests |
| `src/logging_setup.py` | Filter that masks `Authorization` / `aas_…` in any log record; bind `token_prefix` in log context |
| `ui/js/tokens.js` *(new)* + `ui/index.html` | "API tokens" panel (Account menu for every user): create (name, scopes, expiry) → show plain token once with copy button; list; revoke. Admin page: all tokens + grant `run:external` |
| `ui/js/main.js` | import `tokens.js` |

### Key code

```python
# src/api_tokens.py
PREFIX = "aas_"

def generate() -> tuple[str, str, str]:
    plain = PREFIX + secrets.token_urlsafe(32)
    return plain, hash_token(plain), plain[:8]

def hash_token(plain: str) -> str:
    return hashlib.sha256(plain.encode()).hexdigest()

def lookup(session: Session, plain: str) -> ApiToken | None:
    if not plain.startswith(PREFIX):
        return None
    tok = session.exec(select(ApiToken).where(ApiToken.token_hash == hash_token(plain))).first()
    now = datetime.now(UTC)
    if tok is None or tok.revoked_at or (tok.expires_at and tok.expires_at < now):
        return None
    return tok
```

```python
# src/auth.py — current_user (sketch)
auth = request.headers.get("authorization", "")
if auth.lower().startswith("bearer "):
    if os.getenv("API_TOKENS_ENABLED", "1") != "1":
        raise HTTPException(401, "API tokens are disabled")
    tok = api_tokens.lookup(session, auth[7:].strip())
    user = session.get(User, tok.user_id) if tok else None
    if not user or not user.is_active:
        raise HTTPException(401, "Invalid or revoked API token")
    request.state.api_token = tok
    api_tokens.touch(session, tok)          # last_used_at, at most once a minute
    return user
# … existing session-cookie / DESKTOP_MODE path unchanged
```

`require_user`/`require_admin` keep working unchanged, since they build on `current_user`.

### Tests

- `tests/unit/test_api_tokens.py`: generate/hash/prefix; lookup rejects revoked, expired,
  wrong prefix; `has_scope`.
- `tests/integration/test_api_token_auth.py`: Bearer on `GET /api/runs` ✓; invalid Bearer
  ⇒ 401 even with a valid cookie; inactive user ⇒ 401; `read`-only token ⇒ 403 on
  `POST /api/jobs`; token cannot call `/api/tokens`; non-admin cannot grant `run:external`;
  triggered run has `trigger="api"` + `api_token_id`.
- `tests/integration/test_authz_consistency.py`: extend so every gated route is also checked
  with a token (same allow/deny as the session).
- `tests/ui/tokens.test.js`: create shows the token once; revoke calls DELETE
  (`coverage.test.js` requires the file).
- Logging test: a request with `Authorization: Bearer aas_x…` never appears unmasked.

---

## PR 2 — Payload schema, validation, run summary

### Files

| File | Change |
|---|---|
| `src/automation/spec.py` | `payload_schema(job_type) -> dict \| None` (Field → JSON Schema table, §4.2); `AutomationSpec.payload_model: type[BaseModel] \| None = None` (used by PR 5) |
| `src/automation/payload_validation.py` *(new)* | `validate_payload(job_type, payload, *, strict) -> list[{path, message}]` using `jsonschema` (Draft 2020-12); strict ⇒ `additionalProperties: false` |
| `src/custom_automations.py` | `payload_schema_for(slug)` from the declared inputs |
| `src/routers/jobs.py` | `create_job` / `update_job`: validate; `strict = request.state.api_token is not None`; errors ⇒ `422 {"detail": {"code": "invalid_payload", "errors": [...]}}`; lenient unknown keys ⇒ `logger.warning` |
| `src/routers/system.py` | `GET /api/automations/{job_type}/schema` (404 when unknown / disabled / not allowed) |
| `src/run_summary.py` *(new)* | `summarize(run, job, *, log_tail, items) -> dict` with the generic trimming rules (§4.6), byte budget `SUMMARY_MAX_BYTES = 8000` |
| `src/routers/runs.py` | `GET /api/runs/{run_id}/summary` |
| `pyproject.toml` | add `jsonschema>=4` (check whether it is already transitive; pin it explicitly anyway) |

### Trimming rules (`run_summary.summarize`)

1. `result = _parse_result(run.result)`; if it has `error`, keep it verbatim (cut to 1000 chars).
2. `counts` = `{key: len(v)}` for every top-level list + every top-level int.
3. `items` = first `items` elements of the **largest** top-level list; each string value
   cut to 300 chars; nested lists replaced by their length.
4. `log_tail` = last `log_tail` entries of `run.log`.
5. If the JSON is still over budget, halve `items`, then drop `log_tail`, until it fits;
   set `truncated: true`.

### Tests

- `tests/unit/test_payload_schema.py`: each `Field.type`; required/default/min/max/enum;
  every registered non-`custom_ui` spec yields a valid Draft 2020-12 schema
  (`jsonschema.Draft202012Validator.check_schema`); each spec's **default** values pass
  its own schema.
- `tests/unit/test_payload_validation.py`: strict rejects unknown keys; lenient accepts
  them; error paths are readable.
- `tests/integration/test_jobs_api.py`: token + bad payload ⇒ 422 with errors; session +
  extra key ⇒ 201 (lenient); the existing UI payload fixtures still pass.
- `tests/unit/test_run_summary.py`: large result (5 000 leads) stays < 8 KB; counts are
  correct; error runs keep `error`; failed validation surfaces `validation_reason`.
- `tests/ui/*`: no change expected. Run the full UI suite to confirm.

---

## PR 3 — Side-effect declaration, guard, confirmations

### Files

| File | Change |
|---|---|
| `src/automation/spec.py` | `side_effects: Literal["none","external"] = "none"`, `safe_overrides: dict \| None = None`; set per the §4.3 table; include both in `manifest()` and the schema endpoint; `effective_side_effects(job_type, payload)` resolves `pipeline` from its steps |
| `src/models.py` | `ConfirmationToken` table (§5) |
| `src/side_effects.py` *(new)* | `canonical_hash(payload)`, `is_safe(spec, payload)` (explicit values only), `real_payload(spec, payload)`, `decide(request, user, job_type, payload) -> Decision(allowed, code, reason)` (the §4.4 table), `issue_confirmation(...)`, `consume_confirmation(...)` (single-use, done in one transaction) |
| `src/routers/runs.py` | `trigger_run`: when the request carries a token, call `side_effects.decide`; on deny ⇒ `403 {"code": "confirmation_required" \| "scope_required", ...}`; read `X-Confirmation-Token`. New `POST /api/runs/{run_id}/confirm` |
| `src/rate_limit.py` *(new)* | In-memory sliding window per `api_token_id` for `POST /jobs/{id}/run` (`API_RUN_RATE_LIMIT`, default `30/h`) ⇒ 429 + `Retry-After`. Single-process is fine for now; note the Postgres/multi-worker caveat |
| `tests/unit/test_spec_registry.py` | `external ⇒ not resumable`; `safe_overrides` keys ⊆ flow state fields; heuristic: a flow state with `dry_run`/`submit_*` ⇒ spec is `external` |

### Guard decision table (parametrized test)

| auth | side_effects | payload | scopes | confirmation | ⇒ |
|---|---|---|---|---|---|
| session | any | any | — | — | allow (unchanged UI) |
| token | none | any | run | — | allow |
| token | external | safe values explicit | run | — | allow |
| token | external | safe key missing | run | — | 403 `confirmation_required` |
| token | external | real | run | — | 403 `scope_required` |
| token | external | real | run:external | none / bad / used / expired / hash mismatch | 403 `confirmation_required` |
| token | external | real | run:external | valid | allow, token consumed |
| token | external, no safe mode | any | run:external | none | 403 `confirmation_required` |

### Tests

- `tests/unit/test_side_effects.py`: the table above, plus `canonical_hash` order-independence,
  `real_payload` flips `dry_run` and `submit_bids` correctly, and `pipeline` resolution.
- `tests/integration/test_confirmation_flow.py`: dry run (fake flow) → `/confirm` →
  real run allowed once → second use 403 → expiry (freeze time) 403 → payload edited 403;
  `/confirm` refuses failed runs, `validation_passed=false`, runs that weren't in safe mode,
  another user's run, runs older than 24 h.
- Rate limit: the 31st trigger in an hour ⇒ 429.

---

## PR 4 — The `aas-mcp` server

### Layout

```
src/mcp_server/
  __init__.py
  __main__.py     # `python -m src.mcp_server` → server.main()
  server.py       # FastMCP("agent-auto-system"), registers tools, stdio transport
  client.py       # AasClient: httpx.AsyncClient(base_url, Bearer, timeouts); typed methods per endpoint
  tools.py        # the 9 tools (design §6); pure logic over AasClient — easy to unit-test
  errors.py       # HTTP status / body → MCP tool error text (design §6.1)
```

The package **must not import** `src.automation.flows`, `crews`, `tools`, `database` or
`models`. It may import pure helpers (nothing today). A unit test enforces this by importing
`src.mcp_server` in a fresh interpreter and checking `sys.modules`.

### Key code

```python
# src/mcp_server/server.py
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

mcp = FastMCP("agent-auto-system")
client = AasClient.from_env()          # AAS_BASE_URL, AAS_API_TOKEN (required), AAS_TIMEOUT_S

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_automations() -> list[dict]:
    """Automations you may run. side_effects='external' ones run in safe (dry-run) mode
    unless you call confirm_and_submit after a successful dry run."""
    return await tools.list_automations(client)

@mcp.tool()
async def run_automation(job_type: str, payload: dict, name: str | None = None) -> dict:
    """Start a run. Always SAFE: for external automations the safe values (e.g. dry_run=true)
    are forced. Returns run_id; then call wait_for_run. A run only counts as good when
    quality.validation_passed is true."""
    return await tools.run_automation(client, job_type, payload, name)

@mcp.tool(annotations=ToolAnnotations(destructiveHint=True, openWorldHint=True))
async def confirm_and_submit(dry_run_run_id: int) -> dict:
    """PERFORMS A REAL ACTION (submits applications / bids). Re-runs the exact payload of a
    successful, validated dry run with the safe values turned off. Ask the user first."""
    return await tools.confirm_and_submit(client, dry_run_run_id)

# … describe_automation, run_job, get_run, wait_for_run, cancel_run, list_runs

def main() -> None:
    mcp.run()          # stdio
```

```python
# src/mcp_server/tools.py — safe-mode enforcement on the client side (convenience;
# the REST guard is the real control)
async def run_automation(c, job_type, payload, name):
    meta = await c.schema(job_type)                       # 404 ⇒ "unknown or not permitted"
    if meta["side_effects"] == "external":
        if meta["safe_overrides"] is None:
            raise ToolError(f"{job_type} has no safe mode and cannot be run over MCP in v1.")
        payload = {**payload, **meta["safe_overrides"]}
    job = await c.create_job(name or f"mcp: {job_type}", job_type, payload)
    run = await c.trigger(job["id"])
    return {"run_id": run["run_id"], "job_id": job["id"], "status": run["status"],
            "safe_mode": meta["side_effects"] == "external"}

async def wait_for_run(c, run_id, timeout_s=120):
    deadline = time.monotonic() + min(timeout_s, WAIT_MAX_S)
    delay = 2.0
    while True:
        s = await c.summary(run_id)
        if s["status"] not in ACTIVE or time.monotonic() >= deadline:
            return {**s, "timed_out": s["status"] in ACTIVE}
        await asyncio.sleep(delay); delay = min(delay * 1.5, 5.0)
```

Notes:

- **stdout belongs to the MCP protocol.** Configure `logging` to stderr before anything
  prints, and never use `print()` in the package.
- Tool names, descriptions and the JSON returned are the "prompt" the LLM sees. Keep them
  short and precise, and cover them with snapshot tests so edits are deliberate.
- `ACTIVE = {"pending", "queued", "running"}` is duplicated from `concurrency.ACTIVE_STATUSES`
  as a literal to avoid importing the app. A test asserts the two are equal.

### Dependencies and entry point

```toml
# pyproject.toml
dependencies = [ …, "mcp>=1.26,<2" ]        # currently only transitive via crewai

[project.scripts]
aas-mcp = "src.mcp_server.__main__:main"
```

### Client configuration

Claude Code:

```bash
claude mcp add agent-auto-system \
  -e AAS_BASE_URL=http://127.0.0.1:8000 \
  -e AAS_API_TOKEN=aas_xxxxxxxx \
  -- uv --directory /path/to/agent_auto_system run python -m src.mcp_server
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "agent-auto-system": {
      "command": "uv",
      "args": ["--directory", "/path/to/agent_auto_system", "run", "python", "-m", "src.mcp_server"],
      "env": { "AAS_BASE_URL": "http://127.0.0.1:8000", "AAS_API_TOKEN": "aas_xxxxxxxx" }
    }
  }
}
```

### Tests

- `tests/unit/test_mcp_tools.py`: `tools.*` against a fake `AasClient` (safe overrides merged;
  no-safe-mode type refused; `wait_for_run` timeout returns `timed_out: true`; every §6.1
  error mapping).
- `tests/unit/test_mcp_isolation.py`: importing `src.mcp_server` pulls in no
  `src.automation.*` / `src.database` / `crewai`.
- `tests/integration/test_mcp_e2e.py`: in-memory MCP client session ↔ the server, with
  `AasClient` using `httpx.ASGITransport(app=src.main.app)` and `launch_run` patched to a
  fake flow that writes a canned result. Covers list → describe → run (safe) → wait →
  confirm_and_submit → get_run, plus 401/403/422 paths.
- `scripts/mcp_smoke.py`: live smoke against a running server (one `hacker_news_digest`,
  one `greenhouse_apply` dry run). Has a `--no-run` flag like `smoke_test.py`.
- Snapshot of the `tools/list` response (names, descriptions, input schemas).

### Docs

- `doc/mcp-server.md` (user guide): create a token, `claude mcp add`, example prompts,
  safety model in 5 lines.
- `CLAUDE.md`: add an `MCP` row to the layer table, the commands
  (`uv run python -m src.mcp_server`, `scripts/mcp_smoke.py`), and an invariant:
  *"MCP server is a REST client only — safety and permissions live in the REST server."*
- `README.md`: one-line pointer.

---

## PR 5 — Schemas for `custom_ui` automations

The 9 `custom_ui` specs have no `fields`. For each, declare an input-only Pydantic model and
set `AutomationSpec.payload_model`. `payload_schema()` then returns
`payload_model.model_json_schema()`.

| Spec | Source of truth for the model | Notes |
|---|---|---|
| `linkedin_apply`, `tw104_apply`, `tasker_apply` | input fields of the flow `State` (exclude results, `llm_*`) | must include the `dry_run` field |
| `email_collect` | `EmailCollectState` inputs incl. `sources` enum (`maps`/`association`/`govbiz`) | |
| `x_scraper`, `google_form_fill`, `email_sender` | flow state inputs | the last two have no safe mode, so describe-only over MCP in v1 |
| `pipeline` | `steps: list[{job_type, payload}]`, each step validated recursively | side effects resolved per step |
| `profit_health_check` | `{upload_id}` | upload is out of MCP scope, so schema only |

Pattern: define `XInput(BaseModel)` in the flow module and make the `State` inherit from it
(`class XState(XInput): …`). Inputs then have **one** definition, and the schema can't drift
from what the flow reads.

Tests: `test_payload_schema.py` gains "every enabled spec has a schema" (the `xfail` list
shrinks to empty); the UI payload fixtures for each bespoke form must pass strict validation
before lenient mode can be retired.

---

## Rollout

1. Merge PR 1–3 with `API_TOKENS_ENABLED=1`. Nothing changes for UI users. Watch logs for
   lenient-mode "unknown payload key" warnings.
2. Merge PR 4. Create a `read`+`run` token for yourself, connect Claude Code, run
   `scripts/mcp_smoke.py`.
3. Use read and safe-mode for 1–2 weeks. Review `trigger="api"` runs in History and Analytics.
4. Grant `run:external` to your own token only when needed.
5. PR 5, then flip session requests to strict validation.

**Kill switch:** `API_TOKENS_ENABLED=0` disables every token request (401), which makes the MCP
server inert, without a deploy. Revoking a single token takes effect on the next request.

## Acceptance checklist (MVP = PR 1–4)

- [ ] From Claude Code: "list my automations" → only enabled ∩ allowed types.
- [ ] "Dry-run Greenhouse apply for …" → run created with `dry_run: true`, `trigger="api"`,
      visible in the UI history with an API badge.
- [ ] `wait_for_run` returns quality fields; result summary < 8 KB for a 1 000-lead run.
- [ ] A raw `curl` with the token and `dry_run:false` ⇒ 403 `confirmation_required`.
- [ ] `confirm_and_submit` triggers the host approval prompt; succeeds once; a second call
      with the same dry run ⇒ 403.
- [ ] Disabled automation / not-allowed user ⇒ "unknown or not permitted".
- [ ] `API_TOKENS_ENABLED=0` ⇒ every MCP tool reports the auth error.
- [ ] `uv run pytest tests/unit tests/integration -v` and `cd tests/ui && npm test` green.
