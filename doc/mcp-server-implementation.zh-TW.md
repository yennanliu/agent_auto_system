# MCP Server — 實作計畫（選項 A）

> **狀態：** 計畫待審 · **日期：** 2026-11-28
> **English version:** [mcp-server-implementation.md](mcp-server-implementation.md)
> **設計：** [mcp-server-design.zh-TW.md](mcp-server-design.zh-TW.md) · **緣由：** [可行性評估](mcp-server-feasibility.zh-TW.md)

本計畫把[系統設計](mcp-server-design.zh-TW.md)拆成 **5 個可獨立審查的 PR**。每個 PR 都能單獨
merge、不影響 UI 運作，並附帶測試。文中「§4.4」之類的編號指的是設計文件的章節。

---

## 0. 總覽

| PR | 標題 | 相依 | 估計（天） | 使用者可見成果 |
|---|---|---|---|---|
| 1 | API token + run 稽核欄位 | — | 2 | 使用者可建立 token；REST 支援 `Authorization: Bearer` |
| 2 | Payload schema、驗證、run 摘要 | — | 1.5 | `/api/automations/{type}/schema`、`/api/runs/{id}/summary`；錯誤 payload → 422 |
| 3 | 副作用宣告 + 防護 + 確認機制 | 1、2 | 1.5–2 | 投遞 / 寄送類型用 token 執行時，未經確認只能跑安全模式 |
| 4 | `aas-mcp` server 套件 | 1、2、3 | 2 | `claude mcp add …` 端對端可用 |
| 5 | `custom_ui` 自動化的 schema | 2 | 3–5 | LLM 能用嚴格 schema 填寫 `linkedin_apply`、`email_collect`、`pipeline` 等 |
| | **合計** | | **約 10–12.5** | PR 4 完成即可使用 MVP（約 7–7.5 天） |

PR 1 和 PR 2 可以平行開發。每個 PR 都在各自的 sibling git worktree
（`../agent_auto_system-mcp-<pr>`）中進行。

---

## PR 1 — API token 與 run 稽核欄位

### 檔案

| 檔案 | 變更 |
|---|---|
| `src/models.py` | `ApiToken` 表（§5）；`Run.trigger: str = "manual"`、`Run.api_token_id: int \| None` |
| `src/database.py` | `init_db()`：`ALTER TABLE run ADD COLUMN trigger …` / `api_token_id …`，包在 try/except 中（可重複執行） |
| `src/api_tokens.py` *（新）* | `generate() -> (plain, hash, prefix)`、`hash_token()`、`lookup(session, plain) -> ApiToken \| None`、`has_scope(token, scope)`、`SCOPES = ("read", "run", "run:external")` |
| `src/auth.py` | `current_user`：先檢查 Bearer（無效 ⇒ 401，不退回 cookie）；設定 `request.state.api_token`；新增 `require_scope(scope)` dependency 與 `token_scopes(request)` helper（session ⇒ 擁有所有 scope） |
| `src/routers/tokens.py` *（新）* | `POST/GET/DELETE /api/tokens`；admin 可列出 / 撤銷任何人的 token 並授予 `run:external`；**需要 session**（`request.state.api_token` 有值時拒絕） |
| `src/main.py` | `include_router(tokens.router, prefix="/api", dependencies=_gated)` |
| `src/automation/launcher.py` | `launch_run(..., trigger, api_token_id=None)` 把兩者寫入 `Run`（原本就有 `trigger` 參數） |
| `src/routers/runs.py` | `trigger_run`：有 token 時 `trigger="api"`，否則 `"manual"`，並傳入 `api_token_id`；token 請求呼叫 `cancel_run` / `delete` 需要 scope `run` |
| `src/routers/jobs.py` | token 請求呼叫 `create_job` / `update_job` / `delete_job` 需要 scope `run` |
| `src/logging_setup.py` | 加 filter，遮蔽任何 log 中的 `Authorization` / `aas_…`；在 log context 綁定 `token_prefix` |
| `ui/js/tokens.js` *（新）* + `ui/index.html` | 「API tokens」面板（每位使用者的 Account 選單）：建立（名稱、scope、期限）→ 顯示一次明文並附複製按鈕；列表；撤銷。Admin 頁面：所有 token + 授予 `run:external` |
| `ui/js/main.js` | import `tokens.js` |

### 關鍵程式碼

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
# src/auth.py — current_user（草稿）
auth = request.headers.get("authorization", "")
if auth.lower().startswith("bearer "):
    if os.getenv("API_TOKENS_ENABLED", "1") != "1":
        raise HTTPException(401, "API tokens are disabled")
    tok = api_tokens.lookup(session, auth[7:].strip())
    user = session.get(User, tok.user_id) if tok else None
    if not user or not user.is_active:
        raise HTTPException(401, "Invalid or revoked API token")
    request.state.api_token = tok
    api_tokens.touch(session, tok)          # 更新 last_used_at，每分鐘最多一次
    return user
# … 既有的 session cookie / DESKTOP_MODE 路徑不變
```

`require_user` / `require_admin` 建立在 `current_user` 之上，所以不用改。

### 測試

- `tests/unit/test_api_tokens.py`：generate / hash / prefix；lookup 拒絕已撤銷、已過期、
  前綴錯誤的 token；`has_scope`。
- `tests/integration/test_api_token_auth.py`：Bearer 呼叫 `GET /api/runs` ✓；Bearer 無效
  ⇒ 401（即使 cookie 有效）；停用的使用者 ⇒ 401；只有 `read` 的 token 呼叫 `POST /api/jobs`
  ⇒ 403；token 不能呼叫 `/api/tokens`；非 admin 不能授予 `run:external`；觸發的 run 帶有
  `trigger="api"` 和 `api_token_id`。
- `tests/integration/test_authz_consistency.py`：擴充成每個受保護路由也用 token 檢查一次
  （允許 / 拒絕結果與 session 相同）。
- `tests/ui/tokens.test.js`：建立後只顯示一次 token；撤銷會呼叫 DELETE（`coverage.test.js`
  要求必須有這個檔案）。
- Logging 測試：帶 `Authorization: Bearer aas_x…` 的請求，log 中絕不出現未遮蔽的 token。

---

## PR 2 — Payload schema、驗證、run 摘要

### 檔案

| 檔案 | 變更 |
|---|---|
| `src/automation/spec.py` | `payload_schema(job_type) -> dict \| None`（Field → JSON Schema 對照表，見 §4.2）；`AutomationSpec.payload_model: type[BaseModel] \| None = None`（PR 5 使用） |
| `src/automation/payload_validation.py` *（新）* | `validate_payload(job_type, payload, *, strict) -> list[{path, message}]`，使用 `jsonschema`（Draft 2020-12）；嚴格模式 ⇒ `additionalProperties: false` |
| `src/custom_automations.py` | `payload_schema_for(slug)`，依宣告的輸入產生 schema |
| `src/routers/jobs.py` | `create_job` / `update_job`：驗證 payload；`strict = request.state.api_token is not None`；錯誤 ⇒ `422 {"detail": {"code": "invalid_payload", "errors": [...]}}`；寬鬆模式下的未知欄位 ⇒ `logger.warning` |
| `src/routers/system.py` | `GET /api/automations/{job_type}/schema`（不存在 / 已停用 / 未被允許 ⇒ 404） |
| `src/run_summary.py` *（新）* | `summarize(run, job, *, log_tail, items) -> dict`，套用通用裁切規則（§4.6），位元組預算 `SUMMARY_MAX_BYTES = 8000` |
| `src/routers/runs.py` | `GET /api/runs/{run_id}/summary` |
| `pyproject.toml` | 加入 `jsonschema>=4`（目前已是間接相依，仍明確鎖定） |

### 裁切規則（`run_summary.summarize`）

1. `result = _parse_result(run.result)`；若有 `error`，原樣保留（截到 1000 字元）。
2. `counts` = 每個頂層 list 的 `{key: len(v)}`，加上每個頂層整數。
3. `items` = **最大**頂層 list 的前 `items` 筆；每個字串值截到 300 字元；巢狀 list 以長度取代。
4. `log_tail` = `run.log` 的最後 `log_tail` 筆。
5. JSON 仍超過預算時，先把 `items` 減半，再拿掉 `log_tail`，直到符合為止；並設 `truncated: true`。

### 測試

- `tests/unit/test_payload_schema.py`：每種 `Field.type`；必填 / 預設 / 最小 / 最大 / enum；
  每個已註冊的非 `custom_ui` spec 都產生合法的 Draft 2020-12 schema
  （`jsonschema.Draft202012Validator.check_schema`）；每個 spec 的**預設值**都能通過自己的 schema。
- `tests/unit/test_payload_validation.py`：嚴格模式拒絕未知欄位；寬鬆模式接受；錯誤路徑清楚可讀。
- `tests/integration/test_jobs_api.py`：token + 錯誤 payload ⇒ 422 並附錯誤清單；session + 額外欄位
  ⇒ 201（寬鬆）；既有的 UI payload fixture 仍然通過。
- `tests/unit/test_run_summary.py`：大型結果（5 000 筆 lead）維持在 8 KB 以下；counts 正確；
  失敗的 run 保留 `error`；驗證失敗時顯示 `validation_reason`。
- `tests/ui/*`：預期不需修改，但仍跑完整 UI 測試確認。

---

## PR 3 — 副作用宣告、防護、確認機制

### 檔案

| 檔案 | 變更 |
|---|---|
| `src/automation/spec.py` | `side_effects: Literal["none","external"] = "none"`、`safe_overrides: dict \| None = None`；依 §4.3 表設定；兩者都加進 `manifest()` 和 schema 端點；`effective_side_effects(job_type, payload)` 依步驟推導 `pipeline` 的副作用 |
| `src/models.py` | `ConfirmationToken` 表（§5） |
| `src/side_effects.py` *（新）* | `canonical_hash(payload)`、`is_safe(spec, payload)`（只認明確的值）、`real_payload(spec, payload)`、`decide(request, user, job_type, payload) -> Decision(allowed, code, reason)`（§4.4 決策表）、`issue_confirmation(...)`、`consume_confirmation(...)`（只能用一次，在單一 transaction 內完成） |
| `src/routers/runs.py` | `trigger_run`：請求帶 token 時呼叫 `side_effects.decide`；拒絕 ⇒ `403 {"code": "confirmation_required" \| "scope_required", ...}`；讀取 `X-Confirmation-Token`。新增 `POST /api/runs/{run_id}/confirm` |
| `src/rate_limit.py` *（新）* | 針對 `POST /jobs/{id}/run`，依 `api_token_id` 做 in-memory 滑動視窗限流（`API_RUN_RATE_LIMIT`，預設 `30/h`）⇒ 429 + `Retry-After`。目前單一行程即可；需註明 Postgres / 多 worker 的限制 |
| `tests/unit/test_spec_registry.py` | `external ⇒ not resumable`；`safe_overrides` 的 key ⊆ flow state 欄位；啟發式檢查：flow state 有 `dry_run` / `submit_*` ⇒ spec 必須是 `external` |

### 防護決策表（參數化測試）

| 認證 | side_effects | payload | scopes | 確認 token | ⇒ |
|---|---|---|---|---|---|
| session | 任意 | 任意 | — | — | 允許（UI 不變） |
| token | none | 任意 | run | — | 允許 |
| token | external | 安全值有明確寫出 | run | — | 允許 |
| token | external | 缺少安全 key | run | — | 403 `confirmation_required` |
| token | external | 真實 | run | — | 403 `scope_required` |
| token | external | 真實 | run:external | 無 / 錯誤 / 已使用 / 已過期 / 雜湊不符 | 403 `confirmation_required` |
| token | external | 真實 | run:external | 有效 | 允許，token 被消耗 |
| token | external、無安全模式 | 任意 | run:external | 無 | 403 `confirmation_required` |

### 測試

- `tests/unit/test_side_effects.py`：上表，加上 `canonical_hash` 不受 key 順序影響、`real_payload`
  正確翻轉 `dry_run` 和 `submit_bids`、`pipeline` 推導。
- `tests/integration/test_confirmation_flow.py`：dry run（假 flow）→ `/confirm` → 真實 run 允許一次
  → 第二次使用 403 → 過期（凍結時間）403 → 改動 payload 403；`/confirm` 拒絕失敗的 run、
  `validation_passed=false`、非安全模式的 run、別人的 run、超過 24 小時的 run。
- 速率限制：一小時內第 31 次觸發 ⇒ 429。

---

## PR 4 — `aas-mcp` server

### 目錄結構

```
src/mcp_server/
  __init__.py
  __main__.py     # `python -m src.mcp_server` → server.main()
  server.py       # FastMCP("agent-auto-system")，註冊工具，stdio transport
  client.py       # AasClient：httpx.AsyncClient(base_url, Bearer, 逾時)；每個端點一個型別化方法
  tools.py        # 9 個工具（設計 §6）；只依賴 AasClient 的純邏輯，容易單元測試
  errors.py       # HTTP 狀態 / body → MCP 工具錯誤文字（設計 §6.1）
```

這個套件**不得 import** `src.automation.flows`、`crews`、`tools`、`database`、`models`，只能
import 純函式 helper（目前沒有）。用單元測試強制：在全新的直譯器中 import `src.mcp_server`，
再檢查 `sys.modules`。

### 關鍵程式碼

```python
# src/mcp_server/server.py
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

mcp = FastMCP("agent-auto-system")
client = AasClient.from_env()          # AAS_BASE_URL、AAS_API_TOKEN（必填）、AAS_TIMEOUT_S

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

# … describe_automation、run_job、get_run、wait_for_run、cancel_run、list_runs

def main() -> None:
    mcp.run()          # stdio
```

> 工具描述（docstring）是給 LLM 看的，維持英文。

```python
# src/mcp_server/tools.py — 客戶端的安全模式強制（只是便利性；
# 真正的控制在 REST 防護）
async def run_automation(c, job_type, payload, name):
    meta = await c.schema(job_type)                       # 404 ⇒「未知或無權限」
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

注意事項：

- **stdout 屬於 MCP 協定。** 在任何輸出發生前，先把 `logging` 設成寫到 stderr；套件內禁止 `print()`。
- 工具名稱、描述與回傳的 JSON，就是 LLM 看到的「prompt」。保持簡短精確，並用 snapshot 測試涵蓋，
  確保每次修改都是刻意的。
- `ACTIVE = {"pending", "queued", "running"}` 直接以字面值複製 `concurrency.ACTIVE_STATUSES`，
  避免 import app；另用測試斷言兩者相同。

### 相依與進入點

```toml
# pyproject.toml
dependencies = [ …, "mcp>=1.26,<2" ]        # 目前只是經由 crewai 的間接相依

[project.scripts]
aas-mcp = "src.mcp_server.__main__:main"
```

### 客戶端設定

Claude Code：

```bash
claude mcp add agent-auto-system \
  -e AAS_BASE_URL=http://127.0.0.1:8000 \
  -e AAS_API_TOKEN=aas_xxxxxxxx \
  -- uv --directory /path/to/agent_auto_system run python -m src.mcp_server
```

Claude Desktop（`claude_desktop_config.json`）：

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

### 測試

- `tests/unit/test_mcp_tools.py`：用假的 `AasClient` 測 `tools.*`（安全 override 有合併；
  無安全模式的類型被拒；`wait_for_run` 逾時回傳 `timed_out: true`；§6.1 的每種錯誤對應）。
- `tests/unit/test_mcp_isolation.py`：import `src.mcp_server` 不會載入任何
  `src.automation.*` / `src.database` / `crewai`。
- `tests/integration/test_mcp_e2e.py`：in-memory MCP client session ↔ server，`AasClient` 使用
  `httpx.ASGITransport(app=src.main.app)`，`launch_run` 換成寫入固定結果的假 flow。涵蓋
  list → describe → run（安全）→ wait → confirm_and_submit → get_run，以及 401/403/422 路徑。
- `scripts/mcp_smoke.py`：對執行中的 server 做實際冒煙測試（一次 `hacker_news_digest`、
  一次 `greenhouse_apply` dry run）。和 `smoke_test.py` 一樣支援 `--no-run`。
- `tools/list` 回應的 snapshot（名稱、描述、輸入 schema）。

### 文件

- `doc/mcp-server.md`（使用者指南）：建立 token、`claude mcp add`、範例 prompt、五行講完安全模型。
- `CLAUDE.md`：在分層表加上 `MCP` 一列、新增指令（`uv run python -m src.mcp_server`、
  `scripts/mcp_smoke.py`），並加一條不變式：
  *「MCP server 只是 REST 客戶端，安全與權限都在 REST server。」*
- `README.md`：加一行連結。

---

## PR 5 — `custom_ui` 自動化的 schema

9 個 `custom_ui` spec 沒有 `fields`。每個都要宣告一個只含輸入欄位的 Pydantic model，並設定
`AutomationSpec.payload_model`；之後 `payload_schema()` 會回傳 `payload_model.model_json_schema()`。

| Spec | Model 的事實來源 | 備註 |
|---|---|---|
| `linkedin_apply`、`tw104_apply`、`tasker_apply` | flow `State` 的輸入欄位（排除結果欄位與 `llm_*`） | 必須包含 `dry_run` 欄位 |
| `email_collect` | `EmailCollectState` 的輸入，含 `sources` enum（`maps`/`association`/`govbiz`） | |
| `x_scraper`、`google_form_fill`、`email_sender` | flow state 的輸入 | 後兩者沒有安全模式，v1 在 MCP 上只能 describe |
| `pipeline` | `steps: list[{job_type, payload}]`，每一步遞迴驗證 | 副作用逐步推導 |
| `profit_health_check` | `{upload_id}` | 上傳不在 MCP 範圍內，只提供 schema |

做法：在 flow 模組中定義 `XInput(BaseModel)`，再讓 `State` 繼承它（`class XState(XInput): …`）。
這樣輸入欄位只有**一份**定義，schema 不會和 flow 實際讀取的欄位脫節。

測試：`test_payload_schema.py` 新增「每個啟用中的 spec 都有 schema」（`xfail` 清單逐步清空）；
每個客製表單的 UI payload fixture 都必須通過嚴格驗證，才能取消寬鬆模式。

---

## 上線步驟

1. 以 `API_TOKENS_ENABLED=1` merge PR 1–3。UI 使用者完全感受不到變化。觀察 log 中寬鬆模式的
   「unknown payload key」warning。
2. Merge PR 4。為自己建立一個 `read`+`run` token，連上 Claude Code，執行 `scripts/mcp_smoke.py`。
3. 先只用唯讀和安全模式 1–2 週，在 History 和 Analytics 檢視 `trigger="api"` 的 run。
4. 需要時才為自己的 token 授予 `run:external`。
5. 完成 PR 5，之後把 session 請求也切成嚴格驗證。

**緊急開關：** `API_TOKENS_ENABLED=0` 可在不部署的情況下讓所有 token 請求回 401，等於讓 MCP
server 失效。撤銷單一 token 則在下一個請求就生效。

## 驗收清單（MVP = PR 1–4）

- [ ] 在 Claude Code 說「列出我的自動化」→ 只出現已啟用且被允許的類型。
- [ ] 「幫我 dry-run Greenhouse 投遞……」→ 建立的 run 帶 `dry_run: true`、`trigger="api"`，
      並在 UI 歷史中以 API 標籤顯示。
- [ ] `wait_for_run` 回傳品質欄位；1 000 筆 lead 的 run，結果摘要 < 8 KB。
- [ ] 直接用 token `curl` 並帶 `dry_run:false` ⇒ 403 `confirmation_required`。
- [ ] `confirm_and_submit` 會觸發 host 的核准提示；成功一次；同一個 dry run 第二次呼叫 ⇒ 403。
- [ ] 已停用的自動化 / 未被允許的使用者 ⇒「未知或無權限」。
- [ ] `API_TOKENS_ENABLED=0` ⇒ 每個 MCP 工具都回報認證錯誤。
- [ ] `uv run pytest tests/unit tests/integration -v` 與 `cd tests/ui && npm test` 全部通過。
