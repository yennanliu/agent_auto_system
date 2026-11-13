# 專案評估 — PM 與 EM 觀點

> 日期:2026-11-08 · 基準:`main` @ `aed26f7`
> 語言:繁體中文 · [English version](project-evaluation.md)
> 相關文件:[improvements.md](improvements.md)(2026-05 程式碼稽核,大多已完成)、
> [saas-readiness.md](saas-readiness.md)、[self-hosted-readiness.md](self-hosted-readiness.md)
> (2026-07,兩條上市路線)。本文不重複上述內容,而是依**目前**狀態重新評分,
> 並從產品(PM)與工程管理(EM)兩個角度,排出**下一步**該做的功能與改進。

---

## 1. 現況總覽

| 面向 | 現況 |
|---|---|
| 自動化任務 | 14 種內建類型(表單填寫、各類爬蟲、HN 摘要、Sheet 讀取、蝦皮賣家、利潤健檢、Tasker / 104 / LinkedIn / Greenhouse 自動應徵、Email Collector、寄信、Pipeline),加上管理員自建的免程式 `custom:<slug>` 與 entry-point 外掛 |
| Harness | 多 LLM(OpenAI / Anthropic / Gemini)、驗證 → 帶錯誤重試 → 跨模型備援、獨立 LLM 評審、token 與成本計算、Langfuse 追蹤、Prometheus `/metrics` |
| 營運 | Cron 排程、Airflow 式執行格狀圖、SSE 即時日誌、取消執行、啟動時修正卡住的 run、瀏覽器登入狀態檢查與重新登入 |
| 平台 | 登入 + Google/GitHub SSO、per-user 白名單 RBAC、Fernet 加密金鑰、Docker 映像、AWS ECS 指南、Electron 桌面版、CI(ruff、單元、整合、Docker smoke) |
| 擴充性 | 單一 `AutomationSpec` 同時決定派送、白名單、驗證器、評分標準、步驟圖,以及(Phase 2)由 manifest 產生的表單 |
| 規模 / 品質 | 約 3.3 萬行(Python + `ui/app.js`)、約 800 個測試、2026 年 8 月以來 26 個 commit |

**整體結論。** 工程核心(harness、spec registry、排程器)已經成熟,比大多數內部自動化工具好。
但從 5 月到現在,專案增加**廣度**(更多自動化類型)的速度快過增加**深度**(讓每個任務的結果
在 run 結束後真正派上用場)。下一階段應該讓產品從「會跑自動化」進化到「能完成一件事」:
結果能送進其他系統、對外動作有人工把關,並補上多人使用所需的信任與安全基礎。

### 評分表

| 面向 | 分數(1–5) | 說明 |
|---|---|---|
| 核心執行與可靠性 | 4.5 | 重試、備援、評審、修正都很紮實;但沒有併發上限,也沒有持久化佇列 |
| 擴充性 | 4 | Spec registry + manifest + 外掛;但 UI 仍是 3,600 行手寫的 `app.js` |
| 可觀測性 | 4 | Langfuse + Prometheus + 單次成本;沒有告警,也沒有結構化 JSON 日誌 |
| 安全與多使用者隔離 | 2.5 | Run 已隔離;**Job 與 PDF 報表沒有**(見 §3 E1) |
| 產品成果 / 整合 | 2 | 結果只留在系統內(只有 CSV/PDF);沒有 webhook、通知或 CRM 串接 |
| 上手體驗與 UX | 2.5 | 功能強但偏專家導向;沒有範本,也沒有「跟上次比有什麼變化」 |
| 資料生命週期 | 2 | 沒有保存期限,沒有 Alembic,個資(名單、負責人)永久保留 |

---

## 2. PM 觀點 — 建議新增的功能

目前的使用者大致有三類:**(a)** 做開發名單的小型 B2B / 業務團隊(Email Collector、蝦皮賣家),
**(b)** 個人求職者或接案者(104 / LinkedIn / Greenhouse / Tasker 自動應徵),**(c)** 蝦皮賣家
(利潤健檢)。他們要的都是**成果**(約到會議、拿到面試、改善毛利),而不是執行日誌。以下依
「每單位投入能縮小多少差距」排序。

### P0 — 槓桿最高

#### F1. 通知與對外 Webhook
- **問題:** 排程任務失敗了沒人知道,完成了也沒人看,使用者得自己打開系統。
- **功能:** 每個 job 可設定「成功 / 失敗 / 分數低於 X」時,透過 Email、Slack / LINE / Discord
  webhook,或通用的簽章 HTTP webhook 送出 `{run_id, status, eval_score, summary, result_url}`。
- **為何現在做:** 排程已經有了,但沒人看的排程只算完成一半。台灣使用者特別需要 LINE。
- **成功指標:** 排程失敗在 24 小時內被處理的比例。
- **工作量:** 小~中。Job 加一個 `notify` 設定,並寫一個 `notify_run(run_id)` 函式。它要在**每一個**
  結束狀態的 `_update_run` 之後呼叫,而不是只掛在 `executor.execute_run` 結尾,因為這個函式會提早
  return。呼叫點包括:重試用完後驗證失敗(`executor.py:263`)、成功(`executor.py:269`,
  在提早 `return` 之前)、例外失敗(`executor.py:293`),也可以視需要包含取消。重啟後被
  `reconcile_stale_runs` 標成失敗的 run 不會經過 executor,所以修正流程也要回傳它標成失敗的
  run ID(在批次 `UPDATE` 之前先查出來),啟動時再發送這些 run 的失敗通知。和 Langfuse 一樣,
  `notify_run` 自己攔截並記錄錯誤,絕不讓錯誤影響 executor 或啟動流程。

#### F2. 對外動作的審核佇列(Human-in-the-loop)
- **問題:** 自動應徵和寄信類任務會直接對外產生影響(送出應徵、提案、寄信)。`dry_run`
  只有全有或全無兩種選擇。
- **功能:** 新增第三種模式 **「review」**:流程先把要做的動作(提案內容、求職信、Email)
  準備好,存成 `pending_approval` 項目;使用者在收件匣中核准、修改或拒絕,只有核准的才會執行。
- **為何:** 這是建立信任最關鍵的一步。很多人不敢開全自動應徵,但很願意用「幫我擬好,我按送出」。
  同時也降低違反平台條款與商譽受損的風險。
- **成功指標:** 應徵 / 開發類任務的週活躍使用者數、核准率。
- **工作量:** 中。新增 `ActionItem` 資料表,並把確定性漏斗拆成「準備 → 執行」兩段
  (內部其實已經分開了)。

#### F3. 名單與應徵追蹤(把結果變成實體,而不是一包 JSON)
- **問題:** Email Collector 的名單和應徵紀錄都塞在 `run.result` JSON 裡。跨次執行不會去重,
  沒有狀態,也不記得「已聯絡 / 已應徵」。
- **功能:** 把名單與應徵存成資料列,並加上狀態流程
  (`new → contacted → replied → won/lost`、`applied → interview → offer/rejected`);
  跨 run 去重,已應徵或已聯絡過的自動跳過。
- **為何:** 讓一次性的執行變成會累積的資產,也是 F4 與真正 ROI 報表的基礎。
- **工作量:** 中。

### P1 — 價值高

| # | 功能 | 說明 |
|---|---|---|
| F4 | **對外整合**:寫入 Google Sheets、推送到 HubSpot / Notion / Airtable、CSV 存到 S3/Drive | 使用者希望名單直接出現在他們已經在用的工具裡。做成 pipeline 的 `sink` 步驟,就能和所有自動化組合。 |
| F5 | **執行差異 /「有什麼新的」** | 適用於排程爬蟲(HN、X、蝦皮、Sheets):只顯示自上次成功執行後新增的項目,有變化才通知。降低雜訊的效果比單做 F1 大得多。 |
| F6 | **範本庫與首次使用引導** | 內建現成任務(「每週台北咖啡廳名單」「每日 HN AI 摘要」「每天應徵 5 個後端職缺(review 模式)」),縮短新使用者看到價值的時間。 |
| F7 | **Pipeline v2**:條件步驟、`for_each` 展開、出錯繼續 | 目前 pipeline 是純線性,第一步失敗就停止。`for_each`(例如每筆名單 → 擬一封信)是最常被需要的形式。 |
| F8 | **成本預算與防護** | 每個使用者 / 每個 job 的月度美元上限、執行前預估成本、超過上限自動暫停。成本已經有在記錄,實作成本低。 |
| F9 | **成果分析** | 建立在 F3 之上的漏斗統計:名單 → 回覆、應徵 → 面試、每個合格名單的成本。這才是付費者在意的數字。 |

### P2 — 之後再做 / 探索性

- **F10 自然語言建立任務**:「每週一找台中 20 家手搖飲店,並擬一封自我介紹信」→ 產生建議的
  job 與排程,由使用者確認。可建立在 manifest 與自訂自動化之上。
- **F11 可使用工具的自訂自動化**:目前 `custom:<slug>` 是沒有工具的 agent。讓管理員把安全的工具
  (網頁抓取、Sheet 讀取)加入白名單,免程式自動化就能做真正的事。
- **F12 手機 / LINE Bot 介面**:隨時核准 F2 項目、接收 F1 告警。
- **F13 公開唯讀分享連結**:分享單次報表(利潤健檢 PDF、摘要),連結有簽章並會過期。

### 現在「不要」做的事
- 在 F2/F3 之前繼續增加新的爬蟲或自動應徵目標:每多一個就多一份維護成本(DOM 改版,
  LinkedIn 與 Greenhouse 的歷史就是例子),卻沒有提升既有任務的成果。
- 在決定上市路線之前做完整的多租戶 SaaS(見 saas-readiness.md)。自架路線成本較低,
  也更適合目前的爬蟲與帳密模式。

---

## 3. EM 觀點 — 工程改進

### P0 — 正確性與安全(優先做,大多很小)

#### E1. 修補授權漏洞(安全 bug)
Run 已經依使用者隔離(`_assert_run_visible`),但:
- `GET /api/jobs`、`GET/DELETE /api/jobs/{id}`(`src/routers/jobs.py`)**沒有檢查擁有者**。
  任何已登入的使用者都能列出、讀取(payload 可能含 Email、薪資範圍、報價)甚至**刪除**別人的 job。
  `PATCH` 只檢查自動化白名單,不檢查擁有者。
- `GET /api/runs/{id}/report.pdf`(`src/routers/runs.py:194`)完全不看是哪個使用者。任何已登入
  的人只要遞增 run ID,就能下載別人的利潤健檢財務報表。
- `/api/stats`、`/api/schedules`、`/jobs/{id}/overview` 都是全域資料。

**修法:**
- 新增一個和 run 版本對應的 `_assert_job_visible(job, user)`,每個 `{job_id}` 路由都要呼叫。
- `GET /api/jobs` 對非管理員只回傳 `Job.created_by == user.id` 的資料;管理員仍可看到全部。
- `report.pdf` 加上 `require_user`,先讀出 `Run`(不存在就回 404),並在回傳檔案**之前**呼叫
  `_assert_run_visible(run, user)`。只加使用者依賴是不夠的。
- `/stats`、`/schedules`、`/overview` 帶入使用者,非管理員只看到自己的資料。
- 測試:一個參數化的一致性測試,確認*每個* `{id}` 路由對其他非管理員使用者都回 404;再加一個
  多使用者的列表測試,確認使用者 A 的 `GET /api/jobs` 絕不包含使用者 B 的 job(管理員則看得到)。
  這些測試能防止下次新增 router 時又出現同樣的漏洞。

#### E2. 併發上限與背壓
`launch_run` 直接 `asyncio.create_task`,沒有任何上限。排程同時觸發,或使用者連按十次「執行」,
就會在同一個程序裡開十個 Chromium 加上大量 LLM 呼叫。
**修法:** 全域加上依任務類型的 semaphore(瀏覽器類約 2、純 LLM 約 8);UI 顯示 `queued` 狀態;
同一個 job 上一次還在跑時,排程跳過這次觸發。

#### E3. 正式的資料庫遷移
`init_db()` 裡已經有 20 多條包在 try/except 裡的手寫 `ALTER TABLE … ADD COLUMN`。
這種做法沒辦法改名、回填資料、安全地加索引或降版,而 F2/F3 需要帶外鍵的新資料表。
**修法:** 導入 Alembic,建立對應目前 schema 的 baseline revision;`init_db()` 在啟動時呼叫
`alembic upgrade head`,維持「啟動時執行且可重複執行」的約定。新資料庫和既有資料庫的第一步不同:
- **新資料庫**(沒有任何資料表):`upgrade head` 會執行 baseline 遷移,建立整個 schema。
- **既有資料庫**(有資料表但沒有 `alembic_version`):先最後一次執行舊的 `ALTER TABLE`,再確認
  實際 schema 和 baseline 一致(例如 `alembic check` / autogenerate 比對沒有差異,或用測試比對
  inspect 出來的欄位與索引)。確認一致後才執行 `alembic stamp <baseline>`,接著 `upgrade head`。
  如果不一致,就停止並回報清楚的錯誤,不要把 baseline 描述不了的資料庫標記成 baseline。

#### E4. 資料保存期限與個資處理
執行日誌、`result` JSON、名單(包含 `responsible` / 負責人,屬於自然人個資)、上傳檔與 PDF
都永久保存。**修法:** 可設定的保存政策(例如 run 保留 90 天、上傳檔 30 天)、每日清除任務
(可以由排程器執行),以及管理員的「匯出並刪除我的資料」功能。Email Collector 一旦正式使用,
這就是個資法 / GDPR 的基本要求。

### P1 — 規模與可維護性

| # | 改進 | 原因 |
|---|---|---|
| E5 | **拆分 `ui/app.js`(3,600 行)**:依區塊(runs、jobs、admin、forms)拆成 ES modules,不需要 build step;並持續把 `custom_ui=True` 的表單改成 manifest `fields` | 這個單一檔案已經是合併衝突與回歸問題的主要來源;RFC 的 Phase 2 只涵蓋簡單表單。 |
| E6 | **可續跑的執行**:保存步驟檢查點,重啟後從中斷處繼續,而不是直接標成失敗(目前 `reconcile_stale_runs` 會把它設為 `failed`) | 長時間的瀏覽器漏斗(Email Collector、自動應徵)一遇到部署就會失去全部進度。 |
| E7 | **爬蟲失效偵測**:每晚對每個瀏覽器自動化跑一次 canary(`dry_run`),selector 或驗證失敗率突然升高時發出告警 | LinkedIn 與 Greenhouse 的歷史顯示,DOM 改版是最主要的故障來源,而現在往往是使用者先發現。 |
| E8 | **結構化 JSON 日誌**,帶 `run_id` / `user_id` / `job_type` 關聯欄位 | 不論託管或自架,支援都需要;可搭配現有的 Prometheus 指標。 |
| E9 | 在 `WebScraperTool` 與自訂自動化的抓取工具加上 **SSRF 防護**:DNS 解析後封鎖私有、link-local 與 metadata IP | saas-readiness.md 已經提過,但至今未修;如果 F11 讓管理員開放抓取工具,就會變得很急。 |
| E10 | **安全預設值**:非桌面模式下,若還是 `admin`/`admin` 或開發用的 `APP_SECRET`,就拒絕啟動;在 TLS 後方時 session cookie 設 `https_only=True` | 目前只會印出警告。 |
| E11 | 登入與觸發執行加上**流量限制**(`slowapi`) | 防止暴力破解與成本失控;工作量小。 |

### P2 — 工程體驗

- **E12 更新 LLM 模型清單**:`provider.py` 的預設(`gpt-4o-mini`、`claude-haiku-4-5`、
  `claude-sonnet-4-6`)已經落後一代。加入目前的模型,舊模型留在 `costs.py` 供計算歷史成本。
  也可以考慮用結構化輸出(JSON schema)取代從文字中解析結果。
- **E13 Prompt caching**:對 crew 固定不變的 system prompt 使用快取(Anthropic `cache_control`、
  OpenAI 自動快取),降低排程重複執行的成本。
- **E14 CI 覆蓋率門檻**(`pytest-cov`,只升不降的門檻),以及 `make new-automation` 樣板,
  一次產生 spec + flow + crew + 測試。
- **E15 評估回歸集**:每個自動化準備一組固定的輸入與預期輸出,在 CI(每晚)用現有的評審打分。
  修改 prompt 時就有可量測的品質門檻。

---

## 4. 建議路線圖

| 里程碑 | 範圍 | 成果 |
|---|---|---|
| **M1 — 信任(約 2 週)** | E1、E2、E10、E11、F1 | 可以安全地給團隊共用;不再有無聲的失敗 |
| **M2 — 成果(約 4 週)** | E3、F2、F3、F5、E4 | 先審核再執行;名單與應徵成為可追蹤的資料 |
| **M3 — 觸及(約 4 週)** | F4、F7、F8、F9、E7 | 結果進入使用者的工具;ROI 可量測;爬蟲失效能及早發現 |
| **M4 — 規模與打磨** | E5、E6、E8、E12–E15、F6、F10–F13 | 更容易擴充、更便宜、更容易上手 |

### PM/EM 取捨說明
- **E1 是 bug,不是路線圖項目。** 不管優先順序怎麼排,都要先修。
- F2(審核佇列)和 F3(追蹤)都需要新資料表。**先做 E3(Alembic)**,就不用再往 `init_db()`
  多塞兩組臨時的遷移。
- F1 和 F5 共用同一個基礎能力:「和上一次成功的執行比較」。只要做一次。
- M2 完成前先暫停新增自動化類型,每多一個爬蟲都會加重 E7 的維護負擔。

---

## 5. 追蹤指標(KPI)

| KPI | 資料來源 | 目標方向 |
|---|---|---|
| 執行成功率(依類型,7 日) | `/api/stats` | ↑ 非瀏覽器類 ≥ 95 % |
| 評分中位數 | `run.eval_score` | ↑,修改 prompt 後不下降(E15) |
| 每次成功執行 / 每個合格名單的成本 | `run.cost_usd` + F3 | ↓ |
| 發現爬蟲失效所需時間 | E7 canary 對比第一次使用者回報 | ↓ < 24 小時 |
| 24 小時內被處理的排程失敗 | F1 在 `Run` 加上 `notified_at` 與 `acknowledged_at`;使用者在失敗的 run 上按「已處理」(UI 按鈕或通知中的連結)時寫入 `acknowledged_at`。KPI = `acknowledged_at − notified_at ≤ 24 小時` 的失敗排程 run ÷ 所有已發通知的失敗排程 run | ↑ |
| review 模式週活躍使用者 | F2 | ↑ |
