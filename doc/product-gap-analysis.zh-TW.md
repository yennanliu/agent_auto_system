# 產品缺口分析 — 邁向企業級 AI Agent

> 審視日期:2026-10-03 · 範圍:整個 repo(`eb56a06`)· 視角:PM + Tech Lead
> English version: [product-gap-analysis.md](product-gap-analysis.md)
>
> 相關文件:[saas-readiness.md](saas-readiness.md)(多租戶 SaaS)、[self-hosted-readiness.md](self-hosted-readiness.md)(自架部署)、[improvements.md](improvements.md)(2026-05 程式碼稽核,部分已過時)。本文**不重複**上述內容,聚焦在與 Grok Bot、Meta Muse 這類 agent 產品之間的*產品*缺口,以及補齊缺口所需的工程工作。

---

## 1. 摘要

**我們現在是什麼:** 一個扎實、可自架的*自動化控制台*。13 個固定自動化、「填表單 → 執行」的 UI、每次執行都會驗證/重試/評分/計價的 harness、cron 排程、RBAC、多模型、Langfuse/Prometheus。約 780 個測試。台灣市場覆蓋度高(tasker、104、蝦皮、公會、經濟部)。

**市場現在對「商業 agent」的定義**(Grok Bot,2026-08;Meta Muse for Small Business,2026-09):使用者*對話*一個有名字、持續存在、了解你生意的 agent;它在你登入的工具上 24/7 無人值守地工作,只在需要核准時回來找你,會主動通知你,也會主動發現機會。

**缺口一句話:** 我們有*引擎*(harness、flows、排程器、瀏覽器自動化),但沒有*agent 體驗*(對話、記憶、核准、通知、連接器、主動性)。補齊缺口主要是在現有基礎上做產品與黏合層,外加少數基礎建設前置(持久化佇列、逾時、Postgres)。

**我們已經領先的地方:** 使用者可選模型並看到每次執行的成本、*獨立*的 LLM 評審、自架與資料在地化、台灣在地覆蓋、清楚的稽核面。評測文章點名 Grok Bot 的企業採用障礙是「不能選模型、沒有權限範圍、沒有稽核軌跡」——正是我們該放大的優勢。

**前五步(依序):**

| # | 行動 | 為什麼先做 |
|---|---|---|
| 1 | 修補安全漏洞(jobs 的 IDOR、pipeline 繞過權限、共用 SSRF 防護、重試上限) | 便宜,而且任何 agent 產品都不能建在這些洞上 |
| 2 | 通知(Email / LINE / Slack / webhook):完成、失敗、待核准 | 讓排程器真正有用;「無人值守」的前提 |
| 3 | 商業檔案(Business Profile)+ 逐項核准佇列 | 把「表單」變成「助理」的兩個關鍵功能;所有現有 flow 都能重用 |
| 4 | 對話式入口(「Ask」),把自然語言路由到現有 manifest | 最醒目的躍進;風險低,因為只會驅動已存在的 flow |
| 5 | 持久化佇列 + worker + 每次執行逾時 + 真正可用的 Postgres | 持續運作的 24/7 agent、多副本、「取消真的會停」都需要它 |

---

## 2. 現況快照

| 面向 | 狀態 | 證據 |
|---|---|---|
| 自動化 | 13 個已註冊 spec;1 個多 agent crew(profit_health);12 個單 agent 或工具驅動 | `src/automation/spec.py:247-435` |
| 無程式碼自訂自動化 | 僅限 admin、單 agent、**無工具**、無記憶、不能編輯 | `src/custom_automations.py`、`crews/dynamic_crew.py:30` |
| 互動模式 | 點選卡片 → 填表 → 執行 → 看 JSON/表格/PDF | `ui/index.html:344-358`、`ui/app.js` |
| 聊天 / 自然語言路由 / 記憶 / RAG | 無(grep chat、memory、embedding、knowledge 皆無結果) | — |
| 人機協作 | 3 個投遞 flow 有 `dry_run` 勾選(全有或全無);沒有逐項核准 | `tasker_apply_flow.py:50`、`tw104_apply_flow.py:70`、`linkedin_apply_flow.py:48` |
| 通知 | 無。只有 SSE 與歷史表格 | `src/routers/runs.py:250` |
| 連接器 | Gmail SMTP 寄出(單一營運者帳號);Google Sheets 唯讀;4 個網站的 Playwright session;無 Slack/LINE/Notion/Shopify/MCP | `tools/gmail_send_tool.py:52`、`browser_session.py:56-98` |
| 排程 | Cron(UTC)、行程內、單實例、無重疊防護、無補跑 | `scheduler.py:15-17, 40, 88-106` |
| 執行 | web 行程內 `asyncio.create_task` + 預設 thread pool;無佇列、無逾時、取消不會停掉執行緒 | `launcher.py:49`、`executor.py:114,169` |
| 資料模型 | User / Setting / Job / Run;無組織、無稽核日誌、無 artifact 表;結果與 log 以 JSON 文字存放 | `src/models.py` |
| 可觀測性 | 每次執行的 Langfuse trace、Prometheus `/metrics`、OTel、每次執行的 eval 分數 | `harness/langfuse_tracer.py`、`telemetry.py` |
| 測試 / CI | 約 780 個;ruff + pytest + Docker + smoke;**CI 沒跑 mypy、沒有覆蓋率門檻** | `.github/workflows/ci.yml` |
| 文件漂移 | README 寫 11 個自動化 / 380 個測試 / 「要改 6 個檔案」;實際是 13 / 約 780 / spec 驅動 | `README.md:15, 168, 335` vs `CLAUDE.md` |
| 桌面版 | Electron 外殼,Phase 0–2(後端已凍結;簽章待辦) | `electron/README.md` |

---

## 3. 對標:Grok Bot 與 Muse 實際在做什麼

以下依據 2026-10 的公開報導(來源見 §10)。請當作這個品類的*形狀*,不是要照抄的規格。

| 能力 | Grok Bot(xAI / SpaceXAI,2026-08) | Meta Muse / Muse for Small Business(2026-09) |
|---|---|---|
| 互動 | 聊天串中的**具名、持續存在的 bot**;協調者 bot 在共享串中把工作分派給專家 bot | 聊天優先的個人 agent,有名字、頭像、風格;目標導向(「找新客戶」、「提升營收」) |
| 商業脈絡 | 每個 bot 跨任務保留設定與脈絡 | 學習生意賣什麼、品牌語氣、常見客戶詢問 |
| 執行 | 每個 bot 有**自己的持續雲端電腦**(瀏覽器、檔案系統、終端機、MCP、已登入的網頁 app);你關上筆電後仍 24/7 執行 | 在**每位使用者獨立的 VM** 裡代你執行長時間任務;瀏覽器畫面對使用者可見 |
| 工具觸及 | 「像人一樣登入 app」——不需要 API;MCP 連接器 | Shopify、Stripe、QuickBooks、Slack、Notion、Asana、Canva、Klaviyo、HighLevel、Zoom、Dropbox、Box、Figma、IG/FB 商業帳號 |
| 人機協作 | **需要決策或核准時回來找你** | 「Sentinel」權限系統,區分讀與寫 |
| 主動性 | 在業務、行銷、財務、營運、客服、招募間重複執行工作流 | 提醒老闆客戶連結機會;現金流評估;建立行銷活動;給建議 |
| 通路 | macOS、Windows、Linux、iOS | iOS、Android、web、眼鏡、即時通訊式介面 |
| 治理 | **弱**:使用者不能選模型、無權限範圍、無稽核軌跡(評測者所述) | 分級權限;2026 年底規劃隱私模式 |
| 定價 | 約 $120/席/月,綁在方案內 | 免費層;$20 / $100 每月 |

**共同模式:** *輸入目標 → 有記憶 + 工具 + 排程的 agent → 需要時核准 → 輸出通知*。一切圍繞一個持續存在的 agent 組織,而不是一次性表單。

---

## 4. 缺口矩陣

圖例:✅ 已有 · 🟡 部分 · ❌ 缺少 · ⭐ 我們的優勢

| # | 能力 | 我們 | 他們 | 缺口 / 備註 |
|---|---|---|---|---|
| 1 | 對話式、目標導向的入口 | ❌ | ✅ | 使用者得知道要選 13 張卡片中的哪一張,再填 5–15 個欄位 |
| 2 | 持續存在的商業檔案 / 記憶 | ❌ | ✅ | 電話、國籍、offer、品牌語氣每次表單都要重填;prompt 從不知道「使用者是誰」 |
| 3 | 具名、持續存在的 agent(目標 + 排程 + 工具) | 🟡 | ✅ | 我們有 Job + cron;缺的是把身分、記憶、連接器、通知打包成一個物件 |
| 4 | 24/7 無人值守執行 | 🟡 | ✅ | 有 cron 但跑在 web 行程內;無持久化佇列、無逾時、無重疊防護,重啟會讓所有進行中的執行失敗 |
| 5 | 瀏覽器 / 已登入 app 的觸及 | 🟡 | ✅ | tasker/104/LinkedIn/蝦皮有 Playwright;session 只存本機、有頭登入需要桌面、Docker 映像沒有 Chromium |
| 6 | 連接器 / 整合 | ❌ | ✅ | 只有 Gmail SMTP(單一營運者帳號)和 Sheets 唯讀。沒有 Slack、LINE、Notion、Shopify、QuickBooks、Stripe、Drive 寫入、MCP |
| 7 | 逐項核准(人機協作) | 🟡 | ✅ | `dry_run` 是整次執行全有或全無;沒有「審這 8 份提案,核准 5 份」 |
| 8 | 通知(完成 / 失敗 / 需要你) | ❌ | ✅ | 使用者必須開著分頁 |
| 9 | 主動洞察 / 提醒 | ❌ | ✅ | 所有自動化都是 pull;沒有東西在看趨勢(利潤、名單、投遞)並主動開口 |
| 10 | 多 agent 分派 | 🟡 | ✅(Grok) | 一個 4-agent crew;無協調者、無 `allow_delegation`、無 agent 間對話串 |
| 11 | 權限範圍(讀 vs 寫) | 🟡 | 🟡 | 每使用者自動化白名單 ✅;但 jobs 沒有依擁有者隔離(IDOR),pipeline 繞過白名單 |
| 12 | 稽核軌跡 | 🟡 | ❌ | Run 有 user_id、tokens、成本、eval——比 Grok Bot 好——但沒有登入、金鑰變更、核准、admin 動作的紀錄 |
| 13 | 模型選擇 + 每次執行成本 | ⭐ | ❌(Grok) | 每次執行可選 provider/model,有成本、tokens、7 日統計。評測者批評 Grok 路由不透明 |
| 14 | 獨立品質評審 | ⭐ | ❌ | LLM 評審絕不評自己的輸出;有啟發式備援 |
| 15 | 自架 / 資料在地 | ⭐ | ❌ | Docker、Electron、Postgres 路徑。他們的 agent 跑在廠商 VM 上 |
| 16 | 台灣在地覆蓋 | ⭐ | ❌ | tasker、104、蝦皮、公會名錄、經濟部登記、統編補強 |
| 17 | 行動 / 即時通訊通路 | ❌ | ✅ | 只有 web(響應式);沒有 LINE / Telegram / WhatsApp bot |
| 18 | UI 語言 | 🟡 | ✅ | `lang="en"` 的框架混著中文表單;app 內沒有 zh-TW/en 切換(只有行銷站有) |
| 19 | 可分享的結果 | ❌ | 🟡 | 只能下載 PDF/CSV;沒有分享連結,名單沒有輕量 CRM |
| 20 | 公開 API / webhook / SDK | ❌ | 🟡(MCP) | 只有 cookie 認證;沒有 API key、沒有進出站 webhook |
| 21 | 團隊 / 工作區 / 計費 | ❌ | ✅ | 已在 saas-readiness.md 涵蓋;此處不重複 |

---

## 5. PM 視角 — 該新增什麼

優先序假設產品方向是「**給台灣中小企業與個人工作者的、可稽核的自架商業 agent**」。每項都說明它解鎖什麼、重用什麼。

### P0 — 把控制台變成助理

**A1. 商業檔案(記憶 v1)。**
每使用者(之後每工作區)一份檔案:公司名、賣什麼、目標客群(ICP)、地區、品牌語氣、預設語言、預設 LLM、聯絡資料(電話、國籍、年資,供投遞 flow 用)、已存的自我推薦信與提案範本(已存在,但目前是全域)。透過 `{{profile.*}}` 模板注入所有 prompt,並預填表單。
*解鎖:*「它了解我的生意」;免除重複輸入;讓 `email_collect` 的 offer、tasker 提案、104 自我推薦信保持一致。*重用:* `settings_store`(改為每使用者)、manifest `fields`(新增 `default_from_profile`)。

**A2. 核准佇列(人機協作 v2)。**
新增 `PendingAction` 表 + 「收件匣」頁。會產生副作用的 flow(tasker/104/LinkedIn 投遞、email_sender、未來的連接器)先產生草稿;使用者逐項核准 / 編輯 / 拒絕;核准的才執行。取代全有或全無的 `dry_run`。之後再加每個 agent 的 `auto_approve` 政策。
*解鎖:* 安全的無人值守執行、「需要決策時回來找你」的行為、以及「誰核准了什麼」的稽核軌跡。*重用:* 投遞工具已經把「填寫」和「送出」分開。

**A3. 通知。**
事件:執行完成、執行失敗、待核准、排程被跳過。通路:Email(現有 Gmail 工具 → 改每使用者)、**LINE Messaging API**(台灣預設)、Slack webhook、通用 webhook。通路設定放在個人檔案。提供摘要選項(「每天一則」)。
*解鎖:* 排程器成為真正的產品;A5/A6 的前提。

**A4. 「Ask」— 對話式入口(建在現有 flow 之上的 copilot)。**
一個聊天面板:(1) 依自動化 manifest 判斷意圖,(2) 從訊息 + 個人檔案抽出欄位值,(3) 顯示預填好的表單供確認,(4) 執行,(5) 回答針對結果的追問(結果 JSON 在脈絡中)。先只做*路由*;暫不做開放式的工具呼叫 agent。
*解鎖:* 風險最低的招牌體驗——它只能觸發已存在且通過驗證的 flow。*重用:* `GET /api/automations/manifest` 已描述每個欄位。

**A5. 連接器 v1。**
每使用者的 OAuth / token 儲存(加密,如同 LLM 金鑰),涵蓋:Google(以使用者身分寄 Gmail、Sheets 寫入、Drive)、Slack、LINE、Notion。新增寫入端工具:「附加列到 Sheet」、「發到 Slack/LINE」、「建立 Notion 頁」。透過工具白名單開放給 pipeline 與自訂自動化。
*解鎖:* 結果落到生意本來就在用的地方,而不是我們的 JSON 檢視器。取代單一營運者 Gmail 帳號(同時是隱私修補)。

### P1 — 成為 agent 產品

**A6. Agents(具名、持續存在)。**
`Agent = 個人檔案切片 + 目標 + 一或多個自動化 + 排程 + 連接器 + 通知政策 + 核准政策`。Schedules 頁變成「我的 Agents」:「獵客手 — 每週一 09:00,Maps + 公會,LINE 通知,寄信前先核准」。每個 agent 有自己的對話串(執行、核准、訊息)——這就是 Grok Bot / Muse 的物件模型,用我們現有的東西組出來。

**A7. 主動洞察。**
在現有資料上跑排程的「觀察者」自動化:跨次上傳的利潤健檢趨勢(本月毛利掉 8%)、名單漏斗(12 筆新名單,3 筆回覆)、投遞統計(投遞率、回應)。每週摘要送到使用者的通路。重用 `profit_health_check`、`email_collect`、投遞 flow 作為資料來源。

**A8. 可用工具的自訂自動化。**
讓 admin 為 `custom:<slug>` 自動化掛上白名單工具(有 SSRF 防護的網頁抓取、Sheets 讀寫、連接器、MCP server),並設定每個自動化的成本上限與執行配額。這是 extensibility-design §Phase 3 被擋下的項目;A5 的連接器層與共用 SSRF 防護(F3)是它的前提。

**A9. 結果工作區。**
名單成為紀錄(狀態 新 / 已聯絡 / 已回覆 / 成交;備註;負責人),可匯出到 Sheets;PDF/HTML 報告的唯讀分享連結(簽章、會過期);執行結果可搜尋。

**A10. 擋住採用的產品打磨。**
App 內 **zh-TW / en 切換**(行銷站有,app 沒有);修正目錄數字漂移(README 寫 11、首頁寫「八種」、程式碼是 13);首次啟動精靈(也在 self-hosted-readiness P0-2);有時區概念的排程編輯器;「為什麼失敗?」—— 由 validator 原因 + eval 備註生成說明。

**A11. 公開 API + webhook。**
個人 API key(雜湊儲存)、用 key 呼叫 `POST /api/jobs/{id}/run`、每個 job 的進站 webhook 觸發、執行事件的出站 webhook(與 A3 共用事件匯流排)。讓 Zapier/Make/n8n 使用者接得上,也為未來 SDK/MCP server 鋪路。

**A12. 即時通訊 bot。**
用 LINE(之後 Telegram/Slack)bot 當 A4 的前端:「幫我找台中的室內設計公司 email」→ 路由 → 核准 → 結果回到聊天。不用做 app 就有行動體驗。

### P2 — 擴大模型

**A13. 多 agent 編排。** 一個協調者把目標拆解成現有自動化(pipeline v2,支援分支/條件)、在有用處使用 `allow_delegation`、agent 間對話串。
**A14. 範本 / 市集。** 把 agent 定義(A6)與自訂自動化當成可安裝範本分享;plugin registry 已存在於 `AUTOMATION_PLUGINS_ENABLED` 之後。
**A15. 團隊、工作區、計費或授權。** 依選定的商業模式遵循 saas-readiness.md 或 self-hosted-readiness.md。

### 不該照抄的

- 開放式的「agent 自己決定呼叫哪些工具」執行。我們的確定性漏斗原則(CLAUDE.md)是個特色:更便宜、可測試、可稽核。把自主性加在*路由*層(A4/A13),flow 保持確定性。
- 每位使用者一台廠商託管的 VM。自架與模型選擇是我們的差異化;保留它們。

---

## 6. Tech Lead 視角 — 該改善與修補什麼

依急迫度分組。檔案參照對應目前的程式樹。

### 6.1 立刻修(安全性,都很小)

| # | 問題 | 位置 | 修法 |
|---|---|---|---|
| F1 | **jobs 的 IDOR**:列表/取得/更新/刪除/觸發都不檢查 `created_by`;`/overview*`、`/runs/{id}/steps`、`/jobs/{id}/overview`、`/runs/{id}/report.pdf`、`/stats` 洩漏其他使用者資料 | `routers/jobs.py:46-110, 202-290`、`routers/runs.py:30-45, 194-200, 345` | 重用 `runs.py:24-27` 的擁有者隔離模式(`test_run_scoping.py` 已測);為 jobs 補 IDOR 測試 |
| F2 | **pipeline 繞過白名單**:只檢查頂層的 `"pipeline"` 型別;各步驟型別沒檢查 | `routers/jobs.py:57`、`pipeline.py:75` | 建立*與*執行時對每個步驟型別呼叫 `assert_can_run` |
| F3 | **SSRF**:`web_scraper_tool.py:31` 把任何 URL 丟給 `urlopen`(`file://`、metadata IP);`email_extract_tool.py:293`、`google_sheet_tool.py:24-52`、`google_form_tools.py:129`、`playwright_form_tool.py:43` 同樣 | tools/ | 把 `tw_association_tool.py:603-660` 會檢查每次 redirect 的防護抽成 `tools/_net.py`;全部改用 |
| F4 | **成本無上限**:payload 的 `max_retries` 沒有上限;每次重試最多 5 次模型嘗試;`hn_tool.py:43` 開 `limit` 條執行緒 | `executor.py:185`、`provider.py:8` | 限制 `max_retries ≤ 3`、限制執行緒數、加每使用者並行執行配額 |
| F5 | **不安全預設只警告**:admin/admin、`APP_SECRET=dev-insecure-change-me`、`https_only=False`、登入沒限流、沒 CSRF token | `main.py:32-47, 103`、`routers/auth.py:49` | 非 `DESKTOP_MODE` 且非 `DEBUG` 時直接啟動失敗;`https_only` 由環境變數控制;登入加 slowapi;SameSite=strict 或 CSRF token |
| F6 | **SSO 自動連結**:任何已驗證 email 相符就連到既有帳號,包含 admin | `oauth.py:130-145` | 要求在已登入 session 中明確連結,或把 admin 排除在自動連結之外 |
| F7 | **權限層級錯誤**:`POST /sessions/{name}/login` 讓任何使用者在伺服器上開有頭瀏覽器;自我推薦信是全域且人人可寫;`/metrics`、`/docs`、`/openapi.json` 公開 | `routers/sessions.py:30`、`routers/cover_letters.py`、`telemetry.py:46`、`main.py:96` | sessions 限 admin;自我推薦信改每使用者(併入 A1);metrics/docs 加認證或網路限制 |
| F8 | **上傳**:`upload_id` 沒綁使用者;`profit_calc_tool.py:193` 未驗證就拼進路徑;沒有保留期限 | `routers/uploads.py`、`profit_calc_tool.py:193` | 驗證 UUID 格式、存 `user_id`、TTL 清理 |
| F9 | **金鑰衍生**:Fernet key 是 `sha256(APP_SECRET)`;輪替會讓所有已存金鑰無聲失效 | `settings_store.py:83-97` | 有 salt 的 HKDF + key 版本欄位;明確的重新加密指令 |

### 6.2 可靠性(「無人值守」之前必須做)

| # | 問題 | 位置 | 修法 |
|---|---|---|---|
| R1 | **沒有每次執行逾時** —— 卡住的瀏覽器或 LLM 呼叫會永遠跑下去 | `executor.py`(沒有 `wait_for`) | 每次嘗試加 `asyncio.wait_for` + flow state 中的協作式 `cancel_event`,在步驟間檢查 |
| R2 | **取消不會停止工作** —— 只取消了等待中的 coroutine;執行緒(含它的瀏覽器、郵件、投遞)繼續跑 | `executor.py:114`、`registry.py` | 同一個 `cancel_event`;取消時關閉 Playwright context;標記 `cancelled` 而非 `failed` |
| R3 | **重試會重複副作用** —— 任何錯誤訊息含 "timeout" 子字串就重跑整個 flow,包含 `email_sender` 與投遞 flow | `executor.py:215-279`、`provider.py:12-24` | `AutomationSpec` 加 `idempotent: bool`;非冪等 flow 過了 LLM 步驟就不自動重試;`POST /run` 加冪等 key;排程器加重疊防護 |
| R4 | **成功後 DB 失敗會重跑 flow** —— `_update_run` 在重試的 `try` 裡 | `executor.py:263-279` | 把持久化與執行分開;持久化用自己的 try |
| R5 | **成本記在請求的模型而非實際服務的模型**(fallback 後) | `executor.py:247` | 從 `_run_flow` 回傳實際服務的模型 |
| R6 | **沒有並行控制** —— 只有預設 thread pool(約 CPU+4)限制並行;排隊中的執行已顯示 "running" | `executor.py:179` | Semaphore + `queued` 狀態;每使用者配額 |
| R7 | `append_log` 吞掉所有錯誤;每行 log 一次 DB 寫入 | `progress.py:20-27` | 以 WARNING 記錄;批次寫入或改用 log 表 |

### 6.3 架構(Agents / 多副本的前提)

| # | 問題 | 修法 |
|---|---|---|
| S1 | **行程內執行**(`launcher.py:49`):無持久化佇列、只能在起始副本取消、重啟會把*所有*執行中標為失敗(`database.py:75-88`)、N 個副本會讓排程觸發 N 次 | 持久化佇列 + worker:Postgres `SELECT … FOR UPDATE SKIP LOCKED`(不需新基礎設施)或 Redis/arq。排程器 leader lock。用 heartbeat 欄位取代開機時的 reconcile |
| S2 | **Postgres 其實跑不起來**,與文件相反:`check_same_thread` 無條件傳入(`database.py:10`)、`ALTER TABLE user`(保留字,`:46-49`)、`json_insert`(`progress.py:20`)與 `julianday()`(`runs.py:368,442,507`)皆為 SQLite 專屬,錯誤被吞掉;psycopg 不在相依中 | 依方言的 SQL 或 SQLAlchemy JSON 函式;Alembic;CI 加 Postgres job |
| S3 | **SSE = 輪詢 DB**,每個 client 每 0.5 秒一次並重新解析整個 log JSON;無 heartbeat、無最長時間 | 把執行事件發到行程內匯流排(或隨 S1 用 Redis pub/sub);SSE heartbeat;限制串流壽命 |
| S4 | **Run 列既薄又重**:沒有 `job_type`、payload 快照、觸發來源欄位,卻把結果 + 無上限 log 存成 JSON 文字 | 新增 `job_type`、`payload_snapshot`、`trigger`、`cancel_requested`;`RunLog` 與 `Artifact` 表;`PendingAction`(A2);`AuditEvent`(缺口 #12) |
| S5 | **本機磁碟狀態**:上傳、PDF、瀏覽器 `*_state.json` 不跨副本共享;cookie 明文 | 儲存抽象層(本機 / S3);用 F9 的金鑰服務加密 session 檔 |
| S6 | **瀏覽器策略**:runtime 映像沒有 Chromium;`linkedin_apply_tool.py:67,71` 與 `tw104_apply_tool.py:415` 寫死 `headless=False`;17 處各自的 `sync_playwright` 啟動、13 份 UA 字串複本 | `tools/_browser.py` 共用啟動器(stealth 參數、UA、storage state、有 SSRF 防護的路由);含 Chromium + xvfb 的 `browser` 映像變體,或遠端瀏覽器(CDP) |
| S7 | `routers/system.py` 的 `_AGENT_DEFS` 手動維護(889 行),違反 SSOT 原則 | 由 `spec.py` + `agents.yaml` 衍生 |

### 6.4 程式碼品質與漂移

| # | 問題 | 位置 | 修法 |
|---|---|---|---|
| Q1 | **價格表錯誤/過時**:Haiku 4.5 標 $0.25/$1.25(實際 $1/$5,少算 4 倍);未知模型無聲地以 $1/$3 計價;Gemini 各列未驗證 | `harness/costs.py:7, 26` | 更新;未知模型標 `cost_estimated=false` 而不是猜 |
| Q2 | **模型目錄過時**且在 UI 重複:只有 gpt-4o(-mini)、Haiku 4.5、Sonnet 4.6;沒有 `claude-sonnet-5-5`($2/$10)或 `claude-opus-5-5`($4/$20) | `harness/provider.py:26-48`、`ui/app.js` `LLM_MODELS` | 由 `GET /api/system`(或 manifest)提供目錄;UI 讀取 |
| Q3 | CI 沒跑 mypy、設定寬鬆;ruff 沒開 `B`/`S` 規則;沒有覆蓋率門檻 | `pyproject.toml`、`ci.yml` | 加 mypy + `--cov-fail-under`、ruff `B`、`S`(bandit) |
| Q4 | **README 漂移**:「11 種自動化」(13)、「380 個測試」(約 780)、「新增 job type 要改 6 個檔案」(現已 spec 驅動);首頁寫「八種」 | `README.md:15,168,335`、`ui/index.html:142` | CI 產生數字,或移除寫死的數字 |
| Q5 | **沒測到**:linkedin flow/tool、shopee flow/tool、`x_scraper_tool`、`hn_tool`、`telemetry.py`、自我推薦信 router、`registry.py` 取消語意、OAuth callback 路由、SSRF 案例、jobs 的 IDOR | `tests/` | 補針對性測試;IDOR + SSRF 的同時是 F1/F3 的回歸測試 |
| Q6 | 大檔案:`linkedin_apply_tool.py` 898、`system.py` 889、`tw_association_tool.py` 711、`runs.py` 648 行 | — | S6/S7 落地後依職責拆分 |
| Q7 | `improvements.md`(2026-05)沒有狀態標記;很多項目已出貨 | `doc/improvements.md` | 加狀態欄或歸檔 |

### 6.5 做得好的(保留)

單一來源的 `AutomationSpec` registry 與會大聲失敗的一致性測試 · HTTP 與 cron 共用一條 `launch_run` 路徑 · 純函式、有單元測試的排程核心 · run 層級的擁有者隔離(已測)· 處理 72-byte 的 bcrypt 與最後一位 admin 的保護 · Fernet 加密金鑰只顯示遮罩 · `tw_association_tool` 中會檢查 redirect 的 SSRF 防護 · 有大小上限且經驗證的上傳 · 獨立的 eval 評審與絕不拋錯的 tracing · 單趟 SQL 的統計 · Docker test stage + 容器內測試 · opt-in 的 plugins · 確定性漏斗原則。

---

## 7. 路線圖

| 階段 | 週數 | 交付物 | 關閉的缺口列 |
|---|---|---|---|
| **0 — 加固** | 1–2 | F1–F9 · R3(冪等旗標)· Q1、Q2、Q4 · A3 通知 MVP(email + webhook + LINE) | 8、11、部分 12 |
| **1 — 助理** | 3–6 | A1 商業檔案 · A2 核准佇列 + 收件匣 · A4「Ask」路由 · A10 zh-TW/en 切換 · R1、R2 逾時與真正的取消 | 1、2、7、18 |
| **2 — Agent** | 6–10 | S1 佇列/worker · S2 Postgres + Alembic · S3 事件匯流排 · S4 資料表 · A5 連接器 v1 · A6 Agents · A7 主動摘要 | 3、4、6、9 |
| **3 — 平台** | 10+ | A8 可用工具的自訂自動化 · A9 結果工作區 · A11 API + webhook · A12 LINE bot · S5、S6 · A13–A15 | 5、10、17、19、20、21 |

階段 0–1 不需要新基礎設施。階段 2 是持久化佇列與 Postgres 變成必要的時候;階段 1 的所有東西都應該設計成讓 Agents(A6)是組合、而不是重寫。

---

## 8. 定位

> **「給台灣中小企業的、可稽核的自架商業 agent。」**
> Grok Bot 與 Muse 跑在廠商的電腦上、用廠商的模型。我們跑在你的機器上、用你選的模型,每個動作都顯示成本與第二意見的品質分數,送出前先問你,而且已經會講 tasker、104、蝦皮、公會與經濟部。

保持差異化的銳利:模型選擇 + 成本(缺口 13)、獨立評審(14)、自架(15)、在地覆蓋(16)、稽核(12)。先補體驗缺口(1、2、7、8、9),再追連接器的廣度。

---

## 9. 留給團隊的開放問題

1. **先定商業模式**:自架授權(self-hosted-readiness.md)還是託管 SaaS(saas-readiness.md)?它決定階段 2 需要的是租戶隔離還是封裝。
2. **主要通路**:web app、LINE bot,還是 Electron 桌面版?LINE 符合台灣中小企業人設;桌面版符合需要有頭瀏覽器的投遞 flow。
3. **自主性上限**:我們是否允許 agent 不經核准就*送出*(A2 的 `auto_approve`)?建議:v1 對投遞/郵件永不允許;唯讀與寫入自己 Sheet 的動作可允許。
4. **先做哪個連接器**:LINE(通知 + 通路)還是 Google(Sheets 寫入 + 以使用者身分寄 Gmail)?兩者都在階段 0–2;依第一個設計夥伴的工作流決定順序。

---

## 10. 來源

- TechCrunch,2026-09-29 — [Meta is expanding its AI agent Muse to small businesses](https://techcrunch.com/2026/09/29/meta-is-expanding-its-ai-agent-muse-to-small-businesses/)
- CNBC,2026-09-29 — [Meta launches Muse for Small Business](https://www.cnbc.com/2026/09/29/meta-launches-muse-for-small-business-zuckerberg-pushes-enterprise-ai.html)
- Wikipedia — [Muse (AI agent)](https://en.wikipedia.org/wiki/Muse_(AI_agent))
- MediaPost,2026-10-01 — [Meta Launches More Business-Facing Muse AI Offerings](https://www.mediapost.com/publications/article/418408/meta-launches-more-business-facing-muse-ai-offerin.html)
- Beam AI — [Grok Bot for Enterprise AI Agents: The 2026 Reality](https://beam.ai/agentic-insights/grok-bot-enterprise-ai-agents)
- Netalith — [What Is Grok Bot? xAI's New AI Agent Explained](https://netalith.com/blogs/ai-tools/what-is-grok-bot)
- Anthropic 模型價格(Haiku 4.5 $1/$5、Sonnet 5.5 $2/$10、Opus 5.5 $4/$20,每百萬 tokens)— [platform.claude.com/docs](https://platform.claude.com/docs/en/about-claude/models)
