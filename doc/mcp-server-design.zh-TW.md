# MCP Server — 系統設計（選項 A：架在 REST API 之上的薄層 server）

> **狀態：** 方向已定，設計待審 · **日期：** 2026-11-28
> **English version:** [mcp-server-design.md](mcp-server-design.md)
> **相關文件：** [可行性評估](mcp-server-feasibility.zh-TW.md) ·
> [實作計畫](mcp-server-implementation.zh-TW.md)

---

## 1. 背景

[可行性評估](mcp-server-feasibility.zh-TW.md)選定**選項 A**：一個獨立的 MCP server 行程，
透過 HTTP 以個人 API token 呼叫既有的 FastAPI app。它**不會** import flow、crew、tool，也不碰資料庫。

本文件定義元件、介面契約、資料模型、安全模型與失敗行為。
[實作計畫](mcp-server-implementation.zh-TW.md)再把它拆成多個 PR。

## 2. 目標與非目標

**目標**

| # | 目標 |
|---|---|
| G1 | MCP host（Claude Code、Claude Desktop 等）可以探索、描述、執行、觀察、取消並檢視自動化。 |
| G2 | 每個由 MCP 觸發的 run 都走與 UI 點擊**完全相同的執行路徑**：`launch_run` → 併發閘門 → executor → validator → evaluator。 |
| G3 | 套用相同權限：每位使用者的 allowlist、全域停用的自動化、run / job 擁有權。 |
| G4 | **透過 API 執行對外副作用（投遞 / 寄送 / 送出）前，必須先有成功的 dry run 和明確確認**，由 REST server 強制。 |
| G5 | LLM 讀到的結果一律帶有品質訊號（`validation_passed`、`eval_score`……），而且精簡到放得進 context window。 |
| G6 | 新增自動化**不需要改 MCP 程式碼**：工具對 `job_type` 是通用的，呼叫時才讀取目錄。 |

**非目標（v1）：** 遠端多使用者 MCP 託管（選項 B）、編輯 cron 排程、admin 設定、檔案上傳
（`profit_health_check`）、瀏覽器 session 登入、自訂自動化編寫、串流 progress 通知。

## 3. 架構

### 3.1 元件圖

```
┌──────────────────────────┐
│ MCP host                 │  Claude Code / Claude Desktop / 任何 MCP 客戶端
│ （LLM + 使用者核准介面） │
└────────────┬─────────────┘
             │ MCP over stdio（JSON-RPC）
┌────────────▼─────────────────────────────────────────────┐
│ aas-mcp（新行程：`python -m src.mcp_server`）            │
│  ├─ server.py   FastMCP app，註冊工具                    │
│  ├─ tools.py    9 個工具；參數檢查；預設 dry-run         │
│  ├─ client.py   httpx.AsyncClient（Bearer、逾時設定）    │
│  └─ format.py   錯誤 → 工具錯誤對應、文字呈現            │
│  無狀態 · 不連 DB · 不 import flow/crew                  │
└────────────┬─────────────────────────────────────────────┘
             │ HTTPS/HTTP + `Authorization: Bearer aas_…`
┌────────────▼─────────────────────────────────────────────┐
│ FastAPI app（既有，擴充）                                │
│  auth.current_user  ← 新增：Bearer token 路徑 + scope    │
│  /api/jobs, /api/jobs/{id}/run  ← 新增：payload 驗證     │
│                                  + 副作用防護            │
│  /api/automations/{type}/schema ← 新增                   │
│  /api/runs/{id}/summary         ← 新增（裁切後的檢視）   │
│  /api/runs/{id}/confirm         ← 新增（確認 token）     │
│  /api/tokens                    ← 新增（建立/列出/撤銷） │
│            │                                             │
│            ▼                                             │
│  launcher.launch_run(trigger="api") → 併發閘門 → executor│
│            → Flow → Crew → Tools → validator → evaluator │
└──────────────────────────────────────────────────────────┘
```

**規則：** 所有安全與權限判斷都放在 **REST server**。MCP server 只提供便利性（安全預設值、
等待、格式化）。用同一個 token 直接 `curl`，得到的保護完全相同。

### 3.2 部署方式

| 情境 | MCP server 執行位置 | `AAS_BASE_URL` | 備註 |
|---|---|---|---|
| 本機開發（主要） | 開發者筆電，由 host 以 stdio 啟動 | `http://127.0.0.1:8000` | 與需要瀏覽器 session 的自動化在同一台機器 |
| 筆電 → 託管中的 app | 開發者筆電 | `https://<host>` | token 只能走 TLS；瀏覽器類自動化仍需在 host 上有 session |

MCP 行程本身不需在伺服器端部署任何東西。FastAPI 的修改隨一般 app 一起上線。

## 4. REST API 變更

### 4.1 個人 API token

- 格式：`aas_` + 32 bytes URL-safe 隨機字串（約 43 字元）。只在建立時顯示**一次**。
- 以 `sha256(token)` 儲存；明文不落地也不寫進 log。（高熵隨機 token 不需要 bcrypt；
  sha256 可以用索引做 O(1) 查詢。）
- `auth.current_user` 的判斷順序：**先看 Bearer header**，再看 session cookie，最後才是
  desktop mode。帶了 Bearer 但無效 → **401**，不能退回改用 cookie。
- 驗證成功後：設定 `request.state.api_token = <ApiToken>`，並更新 `last_used_at`（每分鐘最多一次）。
- 擁有者必須是啟用中的帳號；token 繼承擁有者的角色與 allowlist。**token 的權限永遠不會超過它的使用者。**

**Scope** 可以把 token 的權限縮到比使用者更小：

| Scope | 允許的操作 |
|---|---|
| `read` | 使用者本來就看得到的所有 `GET` 端點 |
| `run` | 建立 job、觸發 run、取消 run；`side_effects="external"` 類型**只能用安全模式** |
| `run:external` | 經確認後，真正執行 `external` 類型（還需要確認 token，見 §4.4）。**只有 admin 能授予。** |

Session（cookie）請求維持現狀：視為擁有所有 scope，也不經過副作用防護。UI 不受影響。

端點：`POST /api/tokens`（name、scopes、可選的 `expires_in_days`）→ 回傳一次明文；
`GET /api/tokens`（自己的 token：前綴、名稱、scope、最後使用時間）；`DELETE /api/tokens/{id}`
（撤銷）。Admin 可以列出、撤銷任何人的 token，並授予 `run:external`。管理 token 本身**需要
session**，token 不能再產生 token。

### 4.2 Payload 驗證與 schema 端點

`spec.payload_schema(job_type) -> dict | None` 產生 JSON Schema（draft 2020-12）：

| `Field.type` | JSON Schema |
|---|---|
| `text`、`textarea` | `string` |
| `url` | `string`，`format: uri` |
| `number` | `number`（依 `min`/`max` 加上 `minimum`/`maximum`） |
| `checkbox` | `boolean` |
| `select` | `string` + 由選項值組成的 `enum` |

`required` 來自 `Field.required`，預設值來自 `Field.default`，說明文字來自 `label` + `help`。
`custom_ui` 的 spec 則從選用的 `AutomationSpec.payload_model`（一個 Pydantic model，通常是
flow state model 的子集）取得 schema。spec 宣告之前，schema 為 `null`，payload 照舊接受。
自訂自動化（`custom:<slug>`）依它宣告的輸入建立 schema。

`POST /api/jobs` 與 `PATCH /api/jobs/{id}` 會用 schema 驗證 payload：

- **Token 請求：嚴格模式。** 型別、必填、範圍、enum 錯誤，**以及未知欄位**，都回 `422`，
  附上 `{path, message}` 清單，讓 LLM 能據此修正。
- **Session 請求：寬鬆模式。** 型別、必填、範圍、enum 錯誤仍回 `422`；未知欄位只記 warning，
  讓會送出額外欄位的既有 UI 表單繼續正常運作。等 UI 測試確認沒問題後，再改成嚴格模式。

`GET /api/automations/{job_type}/schema` → `{job_type, name, desc, side_effects,
safe_overrides, schema, rubric, steps, browser}`。類型不存在、已停用或不在呼叫者的 allowlist
時一律回 `404`，避免透露有哪些類型存在。

### 4.3 副作用宣告（spec 為唯一事實來源）

`AutomationSpec` 新增欄位：

```python
side_effects: Literal["none", "external"] = "none"
# 讓 "external" run 變成無害（dry run）的 payload 值。None = 沒有安全模式。
safe_overrides: dict | None = None
```

| job_type | side_effects | safe_overrides |
|---|---|---|
| `linkedin_apply`、`tw104_apply`、`tasker_apply`、`greenhouse_apply`、`ats_apply` | external | `{"dry_run": true}` |
| `freelancer_bids` | external | `{"submit_bids": false}` |
| `email_sender`、`google_form_fill` | external | `None`（沒有安全模式，一律需要確認） |
| `pipeline` | *每次呼叫時推導*：任一步驟是 external 即為 external | 各步驟 overrides 的組合 |
| 其他所有類型、`custom:*` | none | — |

`test_spec_registry.py` 新增以下斷言：`external` ⇒ `resumable=False`；`safe_overrides` 的 key
存在於 flow 的 state model；任何提到 `dry_run`/`submit` 的 flow 都必須宣告為 `external`。
最後一項是 grep 層級的安全網。

### 4.4 副作用防護與確認機制

防護在 `trigger_run`（`POST /api/jobs/{id}/run`）執行，**只針對 token 驗證的請求**：

```
spec.side_effects == "none"                 → 需要 scope `run`              → 啟動
payload ⊇ safe_overrides（安全模式）        → 需要 scope `run`              → 啟動
其他情況（真正的副作用）                    → 需要 scope `run:external`
                                              且 header X-Confirmation-Token
                                              對 (user, job_type, payload_hash) 有效
                                              → 消耗 token → 啟動
                                              否則 403 {code:"confirmation_required"}
```

「payload ⊇ safe_overrides」指安全值必須**明確出現**在 payload 中。防護不依賴 flow 的預設值，
因為預設值可能會改。

**確認 token** 用來證明先前已有一次被審閱過的 dry run：

- `POST /api/runs/{run_id}/confirm`（需要 scope `run:external`）：只接受同一使用者、以安全模式
  執行、**成功**且 `validation_passed=true`、24 小時內結束的 run。
- 回應：`{confirmation_token, expires_at, job_type, real_payload_preview}`，其中
  `real_payload` = 把 dry run 的 payload 中 `safe_overrides` 的 key 翻轉 / 移除後的結果。
- 以 `sha256(token)` 儲存，綁定 `(user_id, job_type, payload_hash(real_payload))`；
  **只能用一次**，**15 分鐘內有效**。
- `payload_hash` = 標準化 JSON（`sort_keys=True, separators=(",",":")`）的 sha256。
- 預覽和送出之間只要改動任何欄位，雜湊就對不上。LLM 無法先 dry-run payload A，再送出 payload B。

真人把關的那一步，是 MCP host 的核准提示：送出工具標記為 `destructiveHint=true`，描述也寫明
它會執行真實動作。未來可加入更嚴格的 `MCP_CONFIRM_MODE=ui`（改在網頁 UI 核准）。

### 4.5 Run 稽核欄位

`Run` 新增兩個欄位，透過 `init_db()` 的 `ALTER TABLE` 加入（包在 try/except 中）：

| 欄位 | 值 |
|---|---|
| `trigger` | `manual` · `schedule` · `resume` · `api` |
| `api_token_id` | 指向 `ApiToken` 的 FK，或 `NULL` |

`request.state.api_token` 有值時，`trigger_run` 傳入 `trigger="api"`。UI 歷史可以顯示「API」
標籤，Analytics 也能依觸發來源拆分成本。`get_stats()` 仍維持單次 SQL；除非日後加上依觸發來源的
統計，否則這部分不需要改。

### 4.6 Run 摘要端點

`GET /api/runs/{run_id}/summary?log_tail=20&items=10` 回傳**有上限**的檢視（目標 < 8 KB）：

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
             "items": [/* 主要清單的前 N 筆，長字串截到 300 字元 */],
             "truncated": true, "error": null},
  "safe_mode": true,
  "links": {"full": "/api/runs/812", "csv": "/api/runs/812/leads.csv"}
}
```

裁切規則是通用的：計算結果中每個 list 的筆數，保留最大那個 list 的前 `items` 筆，截斷字串，
並丟掉超過大小預算的 key。日後個別 spec 可以用選用的 `summarize(result) -> dict` 覆寫。
這段邏輯放在伺服器端，方便單元測試，也能共用。

## 5. 資料模型

```
ApiToken                                   ConfirmationToken
─────────────────────────                  ──────────────────────────────
id            int PK                       id            int PK
user_id       int FK user.id（索引）       user_id       int FK user.id
name          str                          token_hash    str UNIQUE
token_hash    str UNIQUE（索引）           job_type      str
prefix        str（"aas_AbC1"）            payload_hash  str
scopes        str JSON list                source_run_id int FK run.id
created_at    datetime                     expires_at    datetime
last_used_at  datetime | None              used_at       datetime | None
expires_at    datetime | None
revoked_at    datetime | None

Run（+ trigger str 預設 "manual"、+ api_token_id int | None）
```

兩張新表由 `init_db()` 裡的 `SQLModel.metadata.create_all` 建立，只有 `Run` 的兩個新欄位需要
`ALTER TABLE`。SQLite 與 PostgreSQL 都適用。

## 6. MCP 工具契約

所有工具都回傳**結構化內容**（JSON），外加一段簡短文字。每次 REST 呼叫都帶使用者的 token，
所以所有權限都由 REST server 強制。

| 工具 | 輸入 | 輸出 | 標註 |
|---|---|---|---|
| `list_automations` | — | `[{job_type, name, desc, side_effects, has_safe_mode, browser, has_schema}]`，只列出已啟用且被允許的類型 | readOnly |
| `describe_automation` | `job_type` | `/schema` 的回應 | readOnly |
| `run_automation` | `job_type`、`payload`、`name?` | `{run_id, job_id, status, safe_mode}` | —（僅安全模式） |
| `run_job` | `job_id` | 同上 | —（僅安全模式） |
| `confirm_and_submit` | `dry_run_run_id` | `{run_id, job_id, status, safe_mode:false}` | **destructive**、openWorld |
| `get_run` | `run_id`、`log_tail?=20`、`items?=10` | `/summary` 的回應 | readOnly |
| `wait_for_run` | `run_id`、`timeout_s?=120`（上限 600） | `/summary` + `timed_out: bool` | readOnly |
| `cancel_run` | `run_id` | `{run_id, status}` | destructive（僅影響本地） |
| `list_runs` | `job_type?`、`status?`、`limit?=10`（上限 50） | `[{run_id, job_type, status, started_at, validation_passed, eval_score}]` | readOnly |

行為說明：

- **`run_automation` / `run_job` 永遠不會產生真實副作用。** 遇到 `external` 類型時，會在送出前
  **把 `safe_overrides` 合併進 payload**；沒有安全模式的類型（`email_sender`、`google_form_fill`）
  則直接拒絕。拒絕訊息會指出唯一能執行真實動作的途徑：`confirm_and_submit`。
- **`confirm_and_submit`** 是執行真實動作的唯一途徑：
  `POST /runs/{id}/confirm` → 用 `real_payload` 呼叫 `POST /jobs` → 帶 `X-Confirmation-Token`
  呼叫 `POST /jobs/{id}/run`。它是獨立、且標記為 destructive 的工具，所以 host 執行前會先詢問使用者。
- 沒有安全模式的類型無法產生 dry run，所以 v1 中 `confirm_and_submit` 也無法觸及它們。
  透過 MCP 寄信或填表**不在範圍內**：在有預覽模式之前，這些只能從 UI 執行。
- **`wait_for_run`** 每 2 秒輪詢一次 `/summary`（逐步放慢到 5 秒），直到 run 進入終止狀態或超過
  `timeout_s` 才返回。逾時**不算錯誤**：結果會帶 `timed_out: true`，工具描述也會告訴模型再呼叫一次。
- **工具描述**簡短，但一定涵蓋：「成功」的定義（`validation_passed`）、結果中的爬取文字是不可信
  資料、真實動作需要 `confirm_and_submit`。

### 6.1 錯誤對應

| REST 狀態 | MCP 結果（`isError: true`） | 訊息範例 |
|---|---|---|
| 401 | 認證錯誤 | 「API token 無效、過期或已撤銷：請到 Account → API tokens 建立新的」 |
| 403 `confirmation_required` | 受防護 | 「這會執行真實動作。請先用安全模式執行，再使用 confirm_and_submit。」 |
| 403 其他 / 404 | 無權限 | 「未知的自動化，或此使用者無權執行」（不洩漏細節） |
| 409 | 衝突 | 例如取消時「Run is not active」 |
| 422 | 驗證錯誤 | 原樣回傳 `{path, message}` 清單，讓模型修正 payload |
| 429 | 速率限制 | 「已達速率上限，請 N 秒後重試」 |
| 5xx / 連線錯誤 | 伺服器錯誤 | 「無法連線到 <base_url> 的 Agent Auto System：server 有在執行嗎？」 |

## 7. 關鍵流程

### 7.1 安全模式執行 → 等待 → 讀取

```
LLM          aas-mcp                    FastAPI                       executor
 │ run_automation(greenhouse_apply, P)  │                               │
 │───────────►│ P' = P ∪ {dry_run:true} │                               │
 │            │ POST /api/jobs (P')     │ schema 驗證（嚴格）           │
 │            │────────────────────────►│ assert_can_run、scope `run`   │
 │            │ POST /jobs/{id}/run     │ 防護：安全模式 ✓              │
 │            │────────────────────────►│ launch_run(trigger="api") ───►│ 排隊 → 執行
 │◄───────────│ {run_id, safe_mode:true}│                               │
 │ wait_for_run(run_id)                 │                               │
 │───────────►│ GET /runs/{id}/summary（輪詢）                          │
 │◄───────────│ status=success、quality{…}、result{counts, items}       │
```

### 7.2 經確認的真實執行

```
LLM ── confirm_and_submit(dry_run_run_id=812) ──► host 詢問使用者「允許？」── 是
aas-mcp ── POST /runs/812/confirm ─────► 檢查：本人的 run、成功、validation_passed、
                                          安全模式、<24h、scope run:external
        ◄── {confirmation_token, real_payload}（P 改為 dry_run:false）
aas-mcp ── POST /jobs (real_payload) ──► 嚴格驗證
aas-mcp ── POST /jobs/{id}/run + X-Confirmation-Token
                                     ──► 防護：hash(real_payload) 相符、未使用、未過期
                                         → 標記已使用 → launch_run(trigger="api")
        ◄── {run_id: 813}
```

## 8. 安全模型

| 威脅 | 控制措施 |
|---|---|
| Token 外洩（shell 歷史、設定檔） | 有 scope、可撤銷、可設過期；`run:external` 只能由 admin 授予；明文只顯示一次；雜湊儲存；永不寫入 log（logging filter 遮蔽 `Authorization`） |
| 爬取內容中的 prompt injection 引導 LLM 投遞 / 寄信 | 真實動作需要 `run:external` + 綁定在**同一** payload 成功 dry run 上的確認 token + host 對 destructive 工具的核准。注入的文字無法產生 token。 |
| LLM 陷入迴圈、大量觸發 run | 對 `POST /jobs/*/run` 做每個 token 的速率限制（預設每小時 30 次，`API_RUN_RATE_LIMIT`），加上既有的 per-type 佇列與全域上限；schema 的 `max` 限制 `limit` 類欄位 |
| 透過 token 提權 | token ⊆ 使用者：相同的 `assert_can_run` / 可見性檢查；token 不能建立 token；使用者停用 ⇒ 其所有 token 失效 |
| 新端點的 CSRF | 瀏覽器不會自動送出 Bearer；token 管理需要 session，並沿用既有的 same-site cookie |
| 列舉自動化 | 不存在、已停用、未被允許的類型一律回 404 |
| 確認 token 重放 | 只能用一次、15 分鐘有效、綁定使用者 + job_type + payload 雜湊 |

## 9. 可靠度與維運

- **MCP server 是無狀態的。** host 重啟不會遺失任何東西，run 都存在 app 裡。若 MCP 行程在
  `wait_for_run` 途中掛掉，run 仍會繼續，之後用 `get_run` 就能接著查。
- **App 重啟**會中斷執行中的 run，與現在相同（`resume.py` 只會重新啟動 `resumable` 類型）。
  MCP 工具如實回報 `/summary` 的內容。
- **逾時：** httpx 連線 5 秒，每次讀取 30 秒；`wait_for_run` 整體 ≤ 600 秒。
- **設定（MCP 行程的環境變數）：** `AAS_BASE_URL`（預設 `http://127.0.0.1:8000`）、
  `AAS_API_TOKEN`（必填）、`AAS_TIMEOUT_S`、`AAS_WAIT_MAX_S`。
- **設定（app 的環境變數）：** `API_TOKENS_ENABLED`（預設 `1`）、`API_RUN_RATE_LIMIT`、
  `CONFIRMATION_TTL_S`（預設 `900`）。
- **可觀測性：** run 帶有 `trigger="api"` 與 `api_token_id`，會出現在 History、Overview、
  Analytics 與 Langfuse。token 請求的每一行 app log 都帶 `token_prefix`。MCP server 只寫
  **stderr**，因為 stdout 是 MCP 通道。

## 10. 測試策略

| 層級 | 測試內容 |
|---|---|
| 單元 | token 雜湊 / 查詢 / scope；各 `Field` 型別的 `payload_schema`；嚴格 vs 寬鬆驗證；防護決策表（§4.4）做成參數化測試；payload 雜湊標準化；摘要裁切預算；spec registry 的副作用不變式 |
| 整合（FastAPI TestClient） | Bearer 401/403 路徑；token ⊆ 使用者；確認 token 生命週期（發出 → 使用 → 重用被拒 → 過期 → 雜湊不符）；記錄 `Run.trigger` / `api_token_id`；速率限制 |
| MCP 端對端（同行程） | SDK 的 in-memory client session ↔ `aas-mcp`，透過 `httpx.ASGITransport` 指向 FastAPI app，並把 `launch_run` 換成假 flow；涵蓋 list → describe → run（安全）→ wait → confirm_and_submit，以及每種錯誤對應 |
| 手動冒煙測試 | `scripts/mcp_smoke.py` 對執行中的 server 跑：一次 `hacker_news_digest`、一次 `greenhouse_apply` dry run |

## 11. 考慮過的替代方案

- **選項 B（MCP 掛在 FastAPI 內）：** 延後。若之後需要遠端託管，§6 的工具契約與 §4 的 REST
  變更都能原封不動沿用。
- **只在 MCP server 做防護：** 不採用。任何持有 token 的人都能用 `curl` 繞過。
- **以 flow 預設值當安全模式（`dry_run` 預設 `True`）：** 不採用。防護要求安全值必須明確寫出，
  這樣預設值改動時，API run 不會悄悄變成真實執行。
- **透過 MCP 開放排程：** v1 不採用。錯誤的排程會不斷重複副作用。
