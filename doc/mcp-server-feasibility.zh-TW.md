# Agent Auto System 的 MCP Server — 可行性評估

> **狀態：** 提案 / 評估（尚未實作）· **日期：** 2026-11-28
> **English version:** [mcp-server-feasibility.md](mcp-server-feasibility.md)

---

## 1. 摘要（TL;DR）

| 問題 | 結論 |
|---|---|
| **可行嗎？** | **可行，且可行性高。** 現有架構已具備 MCP server 需要的所有接點：單一執行入口（`launcher.launch_run`）、機器可讀的自動化目錄（`spec.manifest()`）、權限控管（`assert_can_run`），以及存在 DB 的 run 狀態與結果。`mcp` Python SDK（v1.26）已透過 `crewai` 間接安裝。 |
| **工作量（MVP）** | **約 6–9 人天**：薄層 MCP server + API token + 副作用防護 + 測試。若要讓所有客製表單（`custom_ui=True`）的自動化都有嚴格輸入 schema，**另需約 3–5 天**。 |
| **建議做法** | **薄層、粗粒度的 MCP server**：只暴露「自動化」與「run」，**絕不**暴露內部的 crew / tool。它呼叫既有 REST API（或同一組函式），讓每個由 MCP 觸發的 run 都走與 UI 點擊**完全相同**的路徑：launcher → 併發閘門 → executor → validator → evaluator。 |
| **主要風險** | LLM 可以自行觸發**對外副作用**（投遞履歷、寄信）。必須在**伺服器端**強制「預設 dry-run + 明確確認」，不能只靠 prompt。 |
| **結論** | **建議執行**，分階段：先做唯讀 + dry-run 工具，有副作用的 run 放在明確確認步驟之後。 |

---

## 2. 目標與設計原則

**目標：** 讓 LLM 客戶端（Claude Code、Claude Desktop 或任何 MCP host）能彈性使用我們的自動化
——例如「幫我在西班牙的 Greenhouse 看板找後端職缺，先 dry-run，再列出沒通過品質檢查的」——
同時**不犧牲** Python 實作的可靠度。

**原則 —「LLM 決定*做什麼*，Python 決定*怎麼做*」：**

```
┌──────────────────────────────┐        ┌─────────────────────────────────────────┐
│ LLM 客戶端（MCP host）       │        │ Agent Auto System（核心不變）           │
│                              │  MCP   │                                         │
│  • 選擇要跑的自動化          │ ─────► │  launch_run → 併發閘門                  │
│  • 填寫 payload              │        │   → executor（重試 + 跨模型 fallback）  │
│  • 讀取結果、串接下一步      │ ◄───── │   → Flow → Crew → Tools                 │
│                              │        │   → validator（品質閘門）               │
│                              │        │   → evaluator（獨立評審模型）           │
└──────────────────────────────┘        └─────────────────────────────────────────┘
          彈性編排                              確定性、經過測試的執行
```

MCP 層是**另一個入口**，地位等同 UI 與 cron 排程器；它**不能**變成第二條執行路徑。具體而言：

1. **只提供粗粒度工具。** 暴露「用這個 payload 跑 `greenhouse_apply`」，而不是「點這個按鈕」或
   「呼叫 `ats_common.answer_question`」。確定性漏斗（deterministic funnel）、「寧可跳過也不亂猜」
   政策、stealth 瀏覽器輔助、去重 ledger，全部留在 Python 內。
2. **單一啟動路徑。** 所有 MCP 觸發最終都進入 `launcher.launch_run()`——同樣的 Run 紀錄、
   同樣的 per-type 佇列 + 全域類別上限、同樣的重試 / fallback、同樣的 validator 與 evaluator、
   同樣的 Langfuse trace、同樣的取消機制。
3. **相同權限。** MCP 呼叫*以某位使用者身分*執行，通過 `assert_can_run` /
   `assert_run_visible` / `assert_job_visible`。被停用的自動化依然停用。
4. **品質訊號要回傳，不隱藏。** 工具回應包含 `status`、`validation_passed`、
   `validation_reason`、`eval_score`，讓 LLM 能對不良結果做出反應，而不是盲目相信。

---

## 3. 為什麼現有程式碼很適合

| MCP server 的需求 | 現有能力 | 缺口 |
|---|---|---|
| 可呼叫動作的目錄 | `AutomationSpec` registry + `spec.manifest()`（`GET /api/automations/manifest`）——名稱、描述、`fields`、`steps`、`browser` | `custom_ui=True` 的 spec（16 個中有 9 個，例如 `linkedin_apply`、`email_collect`、`pipeline`）**沒有宣告 `fields`** → 沒有輸入 schema |
| 輸入 schema | `Field(name, type, required, default, min, max, options)` 可直接對應 JSON Schema | 目前沒有伺服器端 payload 驗證：`JobCreate.payload` 是任意 `dict` |
| 單一觸發路徑 | `launcher.launch_run()`（HTTP 與排程器共用） | — |
| 長時間非同步 run | `Run` 紀錄含 `status`、`log`、`result`，另有 SSE 串流 | MCP 需要輪詢 / 等待工具（run 動輒數分鐘） |
| 品質閘門 | `validator.validate()` → `Run.validation_passed/_reason`；`evaluator.evaluate()` → `Run.eval_score` | — |
| 成本 / 模型可見度 | `Run.tokens_in/out`、`cost_usd`、`served_model`、`fallback_used` | — |
| 認證 / 授權 | `assert_can_run`、每位使用者的 `allowed_automations`、admin 角色 | **只支援 session cookie**——非瀏覽器客戶端沒有 API token / Bearer 認證 |
| 取消 | `registry.cancel()` → 在下一次 `append_log` 時拋出 `RunCancelled` | — |
| 副作用安全 | 投遞類 flow 預設 `dry_run: bool = True`；有副作用的 flow 設 `resumable=False` | spec 上沒有宣告式的「此自動化會對外寫入」旗標 |
| SDK | `.venv` 已有 `mcp==1.26.0`（經由 `crewai`） | 應改為直接相依並鎖版本 |

---

## 4. 架構選項

### 選項 A — 架在 REST API 之上的薄層 MCP server *（MVP 建議）*

獨立行程（`src/mcp_server.py` 或 `mcp/` 套件），使用官方 SDK 的 `FastMCP`。每個工具都是一個
小型 `httpx` 呼叫，帶個人 API token 打到執行中的 FastAPI server。

```
Claude Code / Desktop ──stdio──► mcp_server.py ──HTTP + Bearer token──► FastAPI (:8000)
```

- ✅ **零重複：** REST 已強制執行 RBAC、驗證、擁有權檢查與啟動路徑。
- ✅ 行程隔離：MCP 當掉不會拖垮執行中的自動化；不共用 event loop。
- ✅ 今天就能給本機 `stdio` host 使用；改成遠端只需換 base URL。
- ❌ REST API 需新增 API token 認證（新工作，但本身就有價值——CLI / 腳本 / CI 都能用）。
- ❌ 多一跳網路（相較數分鐘的 run 可忽略）。

### 選項 B — 把 MCP 掛進 FastAPI app（Streamable HTTP，路徑 `/mcp`）

將 `FastMCP.streamable_http_app()` 掛載到現有 app；工具直接呼叫 `launch_run()` 與 SQLModel session。

- ✅ 單一部署單元；遠端 MCP 客戶端可直接透過 HTTPS 連線。
- ✅ 可直接把 run log 轉成 MCP progress 通知推送。
- ❌ 必須在 FastAPI 的 `Depends` 鏈之外重新實作認證 / 可見性檢查——最容易與 REST 規則不一致的地方。
- ❌ MCP 請求與背景 run、排程器共用 event loop。
- ❌ 對外開放前需要 OAuth / token 認證與速率限制。

### 選項 C — 直接暴露內部 tool / crew *（不採用）*

讓 LLM 直接呼叫 `greenhouse_tool`、`maps_search_tool`、瀏覽器輔助函式等。

- ❌ 繞過 validator、evaluator、併發閘門、重試 / fallback、去重 ledger，以及「寧可跳過不亂猜」
  政策——正好是我們想保留的品質。
- ❌ 重新引入「讓 agent 編排大量 tool call」的反模式，而 *deterministic-funnel* flow 正是為了消除它。

**建議：** 先做**選項 A**；只有在真的需要「託管、多使用者的遠端 MCP 端點」時才轉向 **B**。
兩者共用同一份工具契約（§5），切換只是換 adapter，不需重新設計。

---

## 5. 建議的 MCP 介面

### 5.1 工具（Tools）

| 工具 | 用途 | 對應 | 標註 |
|---|---|---|---|
| `list_automations` | 呼叫者可執行且已啟用的自動化：`job_type`、名稱、描述、副作用類別、是否需瀏覽器 | `GET /api/automations/manifest`，依 allowlist 過濾 | 唯讀 |
| `describe_automation(job_type)` | payload 的 JSON Schema、rubric（什麼叫「好結果」）、步驟、副作用說明 | `spec` + `fields` | 唯讀 |
| `run_automation(job_type, payload, name?, confirm?)` | 建立臨時 Job 並執行；回傳 `run_id` | `POST /api/jobs` + `POST /api/jobs/{id}/run` | 有副作用時標為 **destructive** |
| `run_job(job_id, confirm?)` | 重跑既有（已儲存 / 已排程）的 Job | `POST /api/jobs/{id}/run` | 同上 |
| `get_run(run_id, log_tail=20)` | 狀態、最後幾行 log、結果摘要、`validation_*`、`eval_score`、成本、模型 | `GET /api/runs/{id}` | 唯讀 |
| `wait_for_run(run_id, timeout_s=300)` | 阻塞直到結束或逾時；回傳同 `get_run` | 輪詢 `GET /api/runs/{id}` | 唯讀 |
| `cancel_run(run_id)` | 中止 run | `POST /api/runs/{id}/cancel` | destructive（僅本地） |
| `list_runs(job_type?, status?, limit)` | 近期 run，用於提供上下文 / 串接 | `GET /api/runs` | 唯讀 |
| `list_jobs` / `list_schedules` | 已儲存的 job 與 cron 排程 | `GET /api/jobs`、`/api/schedules` | 唯讀 |

**刻意不放進 MVP：** 建立 / 修改 cron 排程、admin 設定、API key、瀏覽器 session 登入、
檔案上傳類（如 `profit_health_check`）、自訂自動化編寫。這些屬於管理者或互動式操作，有需要再加。

### 5.2 資源（Resources，選用，第 2 階段）

- `run://{run_id}/result` — 完整結果 JSON（大量名單不塞進工具回應）。
- `run://{run_id}/leads.csv`、`run://{run_id}/report.pdf` — 既有的匯出端點。
- `automation://{job_type}/rubric` — 品質 rubric 文字。

### 5.3 範例對話

```
user  > 幫我對西班牙的 Greenhouse 看板 dry-run 後端職缺投遞，然後整理結果。
llm   → describe_automation("greenhouse_apply")          # 取得 schema，得知 dry_run 預設
llm   → run_automation("greenhouse_apply", {boards:[…], keywords:"backend", dry_run:true})
      ← {run_id: 812, status: "queued"}
llm   → wait_for_run(812, timeout_s=600)
      ← {status:"success", validation_passed:true, eval_score:82,
         result:{applied:0, would_apply:7, skipped:[…原因…]}, cost_usd:0.04}
llm   > 有 7 個職缺可投遞；3 個因必填題目無法確定答案而跳過……
```

---

## 6. 如何維持執行品質

| LLM 呼叫端帶來的風險 | 對策 |
|---|---|
| LLM 捏造 / 打錯 payload 欄位 | 每個自動化提供**嚴格 JSON Schema**（來自 `Field`；`custom_ui` 類型則來自 flow 的 Pydantic state model）+ 在 `POST /api/jobs` 做**伺服器端 payload 驗證**（UI 也受惠）。未知欄位 → 回 422 與可讀訊息，讓 LLM 自行修正。 |
| LLM 繞過漏斗邏輯 | 不可能：沒有任何工具暴露 crew / tool，只有 `launch_run`。 |
| 大量觸發 / 失控迴圈 | 既有 per-type 佇列 + 全域類別上限照樣生效。另加每個 token 的速率限制（例如每小時 N 次），並透過 schema 的 `max` 限制 `limit` / `max_*`。 |
| LLM 相信了壞結果 | 工具回應一律附上 `validation_passed`、`validation_reason`、`eval_score`、`eval_confidence`、`fallback_used`；工具描述明確要求模型把 `validation_passed=false` 視為失敗。 |
| 評審獨立性 | 不變——evaluator 仍在 executor 內、依原本的評審模型選擇順序執行。MCP 客戶端的模型永遠不是評審。 |
| 可觀測性 | run 標記 `trigger="mcp"`（`launch_run` 已有此參數），會像其他 run 一樣出現在 UI 歷史、Overview 格狀圖、Analytics 與 Langfuse。 |
| 結果太大塞爆 context | `get_run` 回傳摘要 + 數量 + 前 N 筆；完整資料走 resources / CSV。 |

---

## 7. 安全：對外副作用

部分自動化會影響真實世界：`linkedin_apply`、`tw104_apply`、`greenhouse_apply`、`ats_apply`、
`tasker_apply`、`freelancer_bids`（開啟 `submit_bids` 時）、`email_sender`、`google_form_fill`。
被 prompt injection 污染的網頁或過度積極的模型都可能觸發它們。

建議規則——**由 Python 強制，而非 prompt**：

1. **在 spec 上宣告：** 為 `AutomationSpec` 新增 `side_effects: Literal["none", "external"]`
   （SSOT，與 `resumable` 並列）。`test_spec_registry.py` 斷言所有 `external` spec 都是 `resumable=False`。
2. **MCP 預設 dry-run：** 對 `external` 類型，MCP server 強制 `dry_run=True`（沒有 dry-run 的類型如
   `email_sender` 則直接拒絕），除非傳入 `confirm=true`。
3. **確認是獨立的人為步驟：** 只有在附上「同一 payload 先前 dry run 所回傳的一次性
   `confirmation_token`」時才接受 `confirm=true`，確保真正送出前一定有被審閱過的預覽。
   MCP host 本身也會在執行標記 `destructiveHint=true` 的工具前詢問使用者。
4. **Token 範圍（scope）：** API token 帶 scope（`read`、`run:dry`、`run:external`）；
   `run:external` 預設關閉，由 admin 授權。
5. **不可信內容只當資料：** 結果中的爬取文字放在明確標示的欄位；工具描述提醒模型不要遵循其中的指令。

---

## 8. 工作量估算

| # | 工作項目 | 估計（天） | 備註 |
|---|---|---|---|
| 1 | 個人 API token（model + 雜湊儲存、`current_user` 支援 `Authorization: Bearer`、Admin UI 建立 / 撤銷、scope） | 1.5–2 | MCP 以外也有用（腳本、CI） |
| 2 | 依 `Field` 做伺服器端 payload 驗證（`POST /api/jobs`、`PATCH`） | 1 | 順便補上既有缺口 |
| 3 | `AutomationSpec.side_effects` + registry 測試 + MCP dry-run / 確認防護 | 1–1.5 | |
| 4 | MCP server（FastMCP、stdio）：§5.1 的 9 個工具、`httpx` 客戶端、結果裁切 | 1.5–2 | `mcp` SDK 已安裝 |
| 5 | 測試：單元（schema、防護、裁切）、整合（MCP client → 測試 server → 假 flow） | 1–1.5 | 使用 SDK 的 in-memory client |
| 6 | 文件 + `claude mcp add` / Claude Desktop 設定範例 | 0.5 | |
| | **MVP 合計** | **約 6.5–8.5** | |
| 7 | 為 `custom_ui` 類型補輸入 schema（從 flow `StateModel` 推導或宣告 `fields`） | 3–5 | 讓 LLM 能可靠填寫 `linkedin_apply`、`email_collect`、`pipeline` 等 |
| 8 | *（選用）* 選項 B：`/mcp` Streamable HTTP + OAuth + 速率限制 | 3–4 | 僅在遠端 / 多使用者託管時需要 |
| 9 | *（選用）* Resources、由 run log 推送 progress 通知 | 1–2 | |

---

## 9. 優缺點

### 優點

- **免費獲得彈性編排：** 自然語言請求、跨自動化的臨時串接（「收集 email → 篩選 → 寄信」）、
  「看看上週失敗的 run 並重跑」——不必另寫 `pipeline` 定義。
- **品質由架構保證：** LLM 只負責選擇輸入；每個 run 仍走經過測試的 flow、品質閘門、獨立評審、重試與 fallback。
- **建置成本低：** 目錄、schema（部分）、RBAC、run 生命週期與 SDK 都已存在；server 只是薄層 adapter（約 400–600 行）。
- **單一事實來源維持單一：** 工具由 `AutomationSpec` 推導，新增自動化會自動出現在 MCP，如同 UI manifest。
- **附帶好處：** API token 與伺服器端 payload 驗證同時改善了 REST API，腳本 / CI 也受惠。
- **生態系觸及：** 可用於 Claude Code、Claude Desktop 及其他 MCP host，無需維護自製客戶端。

### 缺點

- **新的攻擊面：** LLM（可能被注入的網頁內容操控）可觸發真實投遞或寄信 → 需要 §7 的防護與 token scope；這是最主要的成本。
- **長時間執行的不匹配：** MCP 工具呼叫是請求 / 回應模式，run 卻需數分鐘。輪詢（`wait_for_run`）可行，但會消耗客戶端的回合與 token。
- **客製表單的 schema 缺口：** 16 個自動化中有 9 個使用手寫 UI 表單、沒有宣告欄位；在第 7 項完成前，LLM 填寫這些 payload 的指引較弱。
- **兩套成本計量：** 客戶端 LLM 的 token 費用疊加在 run 本身的 LLM 成本之上；app 的 Analytics 只看得到後者。
- **瀏覽器自動化只能在本機：** 有頭登入 / storage-state session（tasker / 104 / Shopee / LinkedIn）仍須在執行 server 的機器上更新；MCP 無法修復過期的 session。
- **更多維護面：** 多一個行程、需保持準確的工具描述，以及額外的整合測試。

---

## 10. 分階段計畫

| 階段 | 範圍 | 完成條件 |
|---|---|---|
| **0 — 基礎** | 含 scope 的 API token；伺服器端 payload 驗證；spec 加上 `side_effects` | REST 可用 `Bearer` 呼叫；無效 payload → 422；registry 測試通過 |
| **1 — 唯讀 + dry-run MCP** | 選項 A server；所有唯讀工具；`run_automation` / `run_job` 只允許 `side_effects="none"` 或 `dry_run=true` | 在 Claude Code 中：list → describe → dry-run → wait → 讀取已驗證的結果 |
| **2 — 經確認的副作用** | `confirmation_token` 流程；`run:external` scope；MCP 觸發送出的稽核紀錄 | 真正投遞必須先有 dry run + 使用者明確核准 |
| **3 — 覆蓋與打磨** | 所有 `custom_ui` 類型的 schema；resources；調整結果裁切 | 每個啟用的自動化都有嚴格 schema |
| **4 —（選用）遠端** | 選項 B Streamable HTTP + OAuth + 速率限制 | 僅在需要託管多使用者 MCP 時 |

---

## 11. 待決問題

1. **只在本機，還是要託管？** 若 MCP 只在開發者自己的機器使用，選項 A + `stdio` 就夠了，第 4 階段可省略。
2. **誰可以持有 `run:external`？** 只有 admin，還是依既有 `allowed_automations` 開放給個別使用者？
3. **MCP 是否可建立 cron 排程？** 很方便，但錯誤的排程會每天重複產生副作用——建議：不行，排程留在 UI。
4. **Pipeline 與 LLM 串接的分工：** 可重複、需排程的串接用 `pipeline`；探索性的一次性任務用 MCP 串接。是否要支援把好用的 MCP 串接「另存為 pipeline」？

---

## 12. 程式碼參考

- 執行入口：`src/automation/launcher.py`（`launch_run`、`trigger=`）
- 目錄 / schema：`src/automation/spec.py`（`AutomationSpec`、`Field`、`manifest()`）
- 權限：`src/auth.py`（`assert_can_run`、`assert_run_visible`、`assert_job_visible`）
- Job / run API：`src/routers/jobs.py`（`JobCreate`）、`src/routers/runs.py`
- 品質：`src/automation/harness/validator.py`、`src/automation/harness/evaluator.py`
- Run 資料模型：`src/models.py`（`Run`：狀態、驗證、評分、成本欄位）
- 相關設計：[automation-extensibility-design.md](automation-extensibility-design.md)、
  [auth-and-admin-design.md](auth-and-admin-design.md)
