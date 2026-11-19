# 求職平台：全貌與自動應徵規劃

[English](job-platforms-automation-plan.md) | 繁體中文

狀態：**Phase 0–2 已上線**（#107、#109、#110），其餘仍為提案。實際碰到真實網站後的修正見
「進度與實測心得」。

| 已支援 | 類型 | 程式碼 |
|---|---|---|
| `linkedin_apply` | 求職網站，Easy Apply（需登入） | `tools/linkedin_apply_tool.py` |
| `greenhouse_apply` | ATS，公開職缺頁（免登入） | `tools/greenhouse_apply_tool.py`、`scripts/greenhouse_country_boards.py` |
| `tw104_apply` | 台灣求職網站（需登入） | `tools/tw104_apply_tool.py` |
| `tasker_apply` | 台灣接案／零工（需登入，JSON API） | `tools/tasker_apply_tool.py` |

本文件涵蓋：

1. LinkedIn、Greenhouse、Indeed 以外的其他主要平台。
2. 各平台的自動化方式：能否沿用現有程式碼、複雜度，以及大致工作量。
3. 遠端、約聘與接案平台。
4. 分階段路線圖。

工作量以**單一平台的工作天**計，標準比照 `greenhouse_apply`：spec、flow、tool、dry-run 模式、
已應徵紀錄（ledger）、canary、golden eval 與文件。這些都是估計而非承諾。凡是取決於網站目前
DOM 或反機器人機制的內容，開工前都必須再對照實際網站確認。

---

## 進度與實測心得（截至 2026-11-19）

| 階段 | 狀態 | 已上線內容 |
|---|---|---|
| 0. 共用 ATS 核心 | ✅ #107 | `tools/ats_common.py`：共用答題規則（`Question` → `answer_question`）、比對、ledger、瀏覽器 helper。Greenhouse 已搬到上面，行為不變 |
| 1. ATS adapter | ✅ #109、#112 | `ats_apply` 工作類型、`ats_router.py`，以及 **Lever、Ashby、Workable** 的 adapter（另含 Greenhouse）。SmartRecruiters 只做探索。國家職缺頁掃描器 `scripts/ats_country_boards.py`（#112） |
| 2. 探索來源 | ✅ #110 | `sources`：**HN「Who is hiring」**、Remote OK、Remotive、We Work Remotely 接進 adapter；其餘一律成為手動 lead |
| 3. 台灣求職網站 | ⏸ 暫緩 | 已實測調查，未實作（見下方） |
| 5（部分）. Seek／JobStreet 探索 | 🔄 #114 | 透過 Seek 的搜尋 API 提供 `seek_au`、`seek_nz`、`jobstreet_sg/my/ph/id` 來源，列為手動 lead。JobsDB HK／TH 回應 403；職缺頁在機器人驗證之後。**應徵（Quick Apply）**需要使用者的 Seek 帳號，尚未完成 |
| 6（部分）. Workday 探索 | ✅ #113 | 透過公開的 `/wday/cxs` API 找到 Workday 職缺並列為手動 lead；已加入職缺頁掃描器。**在 Workday 上應徵**（每個雇主各需一個帳號）尚未完成 |
| 4–6 | 尚未開始 | — |

**真實網站的實測結果（皆在真實瀏覽器中確認，而非假設）：**

- 四個可自動應徵的 ATS **都能在不開瀏覽器的情況下讀出表單**：Lever（伺服器端渲染的
  HTML）、Ashby（託管頁面的公開 GraphQL）、Workable（表單 API）、Greenhouse（Job Board
  API）。「先用 Python 規劃，再填寫」的模式可直接沿用。
- **SmartRecruiters** 的應徵頁有 DataDome 反機器人牆。無頭瀏覽器會看到「Access is temporarily
  restricted」，有頭的 Chrome 則可通過。表單由 web component 組成，且沒有表單 API。目前只做
  探索（§3.1 低估了這點）。
- **遠端職缺網站*不會*導向雇主的 ATS**（§4.1 原本這樣假設）：
  - Remote OK 的應徵連結會繞回自己的頁面。
  - Remotive 的職缺頁沒有雇主連結。
  - We Work Remotely 需要帳號，且在 Cloudflare 驗證之後。
  - 只有約 2–6 % 的職缺內含 ATS 連結，所以這些網站大多只產生手動 lead。
- **HN「Who is hiring」才是真正有價值的來源。** 一個月內，243 篇貼文中有 36 篇直接連到支援的
  ATS，74 篇提供 e-mail。
- **Built In** 只對已登入使用者顯示雇主連結。**Google Jobs** 需要付費的 SerpAPI key。兩者皆未實作。
- **樣板連結很重要。** 職缺的 EEO／AAP 政策頁尾可能指向同一 ATS 上的*另一個*職缺，職缺也會
  交叉推廣其他職位。在求職網站上，只有被呈現為「如何應徵」的連結才會被採用。
- **未確認的送出**（已按送出、但沒看到確認頁）會記錄為 pending，且不會自動重送。較早的
  `greenhouse_apply` 工作類型仍缺少這項保護。
- **台灣求職網站（Phase 3，暫緩）：**
  - **1111** 連搜尋頁都有驗證關卡（Cloudflare 驗證加上 ALTCHA 工作量證明）。無頭與有頭的
    Chrome 都停在「Loading…」。原規劃「複製 104，3–5 天」的估計不成立。
  - **CakeResume** 在 Cloudflare 之後（「請稍候…」），有頭的 Chrome 也一樣。
  - **Yourator** 有公開 JSON API（`/api/v4/jobs`）。抽樣的職缺中 44 %（179 筆中 78 筆）在
    外部網站應徵：66 筆透過 **Teamdoor**（台灣的 ATS），少數透過 Greenhouse 或 Lever（含
    `grnh.se` 短網址）。因此它是低成本的探索來源，也可能值得做一個 Teamdoor adapter。
  - **518** 的搜尋頁是伺服器端渲染，可直接讀取。
  - 這四個網站的站內應徵都需要使用者自己登入的帳號。

---

## 1. 關鍵觀察：多數平台不是「來源」就是「表單」

平台分成兩種角色，而這個區分決定了每個平台需要寫多少程式：

- **應徵者追蹤系統（ATS）**，例如 Greenhouse、Lever、Ashby、Workday，擁有真正的應徵
  **表單**。不論職缺是從哪裡找到的，最後都會走到其中一個。
- **求職網站與聚合器**，例如 Glassdoor、Google Jobs、RemoteOK、Hacker News，主要負責
  **探索職缺**。大多數職缺會連結到雇主的 ATS，只有部分網站有站內應徵（Seek Quick Apply、
  104、Dice Easy Apply、LinkedIn Easy Apply）。

因此要以最低成本取得最大覆蓋率：

```
探索來源 ──► 標準化職缺 {title, company, location, apply_url}
                │
                ▼
       detect_ats(apply_url)   ──► greenhouse | lever | ashby | workable | smartrecruiters | workday | unknown
                │
                ▼
       ATS 應徵 adapter（共用答題規劃器 + ledger）
```

如此一來，新增一個*探索來源*（RemoteOK、Remotive、HN「Who is Hiring」、Google Jobs）只需
**1–2 天**，因為它能沿用所有已建好的 ATS adapter。每多一個 *ATS adapter*，所有來源的價值都會
跟著提升。這與 `email_collect` 已採用的多來源合併模式相同（`EmailCollectFlow._run_source`）。

---

## 2. 現有程式碼可沿用的部分

| 資產 | 位置 | 可沿用於 |
|---|---|---|
| 答題規劃器：個人資料與規則答案、選項比對、年資對應區間、贊助簽證與工作許可的區分、語言與程式語言選項、EEOC「不願透露」、單一選項的同意勾選 | `greenhouse_apply_tool.py`（`plan_answers`、`_profile_answer`、`_rule_answer`、`match_option`、`_years_option`、`_authorized_for` 等） | **所有 ATS 表單**。邏輯本身與平台無關，但目前綁定 Greenhouse 的題目 JSON 格式 |
| 必填題的 LLM 備援（不確定時放棄作答） | `GreenhouseAnswerCrew` | 所有表單 |
| 相關性過濾（`task_filter`） | `GreenhouseRelevanceCrew` | 所有來源 |
| 關鍵字與地點比對、跨職缺頁交錯排序 | `expand_keywords`、`match_job`、`_interleave` | 所有來源 |
| 本機「已應徵」紀錄 | `load_ledger`、`record_applied` | 所有無法從伺服器端查詢「已應徵」的平台 |
| 依國家掃描職缺頁的腳本 | `scripts/greenhouse_country_boards.py` | Lever、Ashby、Workable、SmartRecruiters、Recruitee 的職缺頁掃描（同樣是「公司代碼 → 公開 JSON」的形式） |
| 以 storage state 保存登入狀態、`_looks_logged_out`、有頭瀏覽器重新登入 | `browser_session.py`、`scripts/*_login.py`、104／tasker／LinkedIn 的 tool | 所有需要登入的網站（1111、CakeResume、Seek、Dice、Wellfound 等） |
| 擷取網站自己的 bearer token，改用其 JSON API 而非操作 DOM | `tasker_apply_tool.py`（`_capture_session`、`_get_json`、`_submit`） | 有內部 JSON API 的 SPA（Yourator、Cake、Freelancer.com） |
| 搜尋頁 → 職缺卡片 → 應徵彈窗 → 自我推薦信 | `tw104_apply_tool.py` | 1111 與 518 的流程幾乎是 104 的翻版 |
| 多步驟彈窗走訪、每日上限偵測 | `linkedin_apply_tool.py`（`_wait_for_modal_step`、`_hit_daily_limit`） | Seek／JobStreet Quick Apply、Dice、Indeed 類的多步驟精靈 |
| Canary 偏移偵測、`AutomationSpec` 註冊表、manifest 驅動的 UI、eval 框架 | `canary.py`、`spec.py`、`evals/` | 每個新 spec 都能直接取得，不需額外工作 |

目前**還沒有通用的表單填寫器**。`playwright_form_tool.py` 寫死了單一 Google 表單，所以任何
「未知 ATS」的備援都會是新程式碼。

**前置重構（Phase 0，約 3–4 天）：**把 `greenhouse_apply_tool.py` 中與平台無關的部分移到
`src/automation/tools/ats_common.py`，並以一個標準化的題目型別為核心：

```python
@dataclass
class Question:
    key: str            # DOM / API 欄位 id
    label: str
    kind: Literal["text", "textarea", "select", "multiselect", "checkbox", "file", "date"]
    required: bool
    options: list[str]
```

之後 `plan_answers(questions, profile, answer_fn)` 以及 ledger、比對、相關性過濾等 helper 都
變成共用。每個 adapter 只需實作 `discover() → jobs`、`fetch_questions(job) → [Question]` 與
`fill_and_submit(page, job, answers)`。Greenhouse 先搬到新介面，這是純重構，由既有的單元
測試與 eval 把關。

---

## 3. 各平台評估

圖例。**沿用度：**H 表示大多是共用核心加上小型 adapter；M 表示部分模式可沿用、DOM 操作需新寫；
L 表示基本上是全新開發。**複雜度：**L／M／H／XH。**ToS 風險：**網站禁止自動應徵或爬取的
程度，會影響是否值得開發。LinkedIn 已屬「高」，風險由使用者自行承擔。

### 3.1 應徵者追蹤系統（表單擁有者）

| 平台 | 探索 | 應徵機制 | 登入 | 反機器人 | 沿用度 | 複雜度 | 工作量 | 備註 |
|---|---|---|---|---|---|---|---|---|
| **Lever** | 公開職缺 API `api.lever.co/v0/postings/<co>?mode=json` | 單頁表單 `jobs.lever.co/<co>/<id>/apply`。自訂題目**只存在於 DOM**（API 裡沒有），需從表單擷取 | 否 | 部分職缺頁有 hCaptcha | **H** | **L–M** | 3–5 天 | 最佳下一個目標：很多新創使用，且形態最接近 Greenhouse |
| **Ashby** | 公開職缺 API `api.ashbyhq.com/posting-api/job-board/<org>` | React SPA `jobs.ashbyhq.com/<org>/<id>/application`。頁面以 JSON 取得表單定義，可在填寫前先讀取 | 否 | reCAPTCHA | **H** | **M** | 4–6 天 | 在科技新創間快速成長。與 Greenhouse 相同的「先用 Python 規劃，再填寫」模式 |
| **SmartRecruiters** | 公開 API `api.smartrecruiters.com/v1/companies/<co>/postings` | 一鍵應徵 UI，含篩選題。可選「用 LinkedIn 應徵」 | 否（訪客應徵） | 中等 | **H** | **M** | 5–7 天 | 中大型企業，歐洲比例高 |
| **Workable** | 公開職缺 JSON `apply.workable.com/api/v3/accounts/<co>/jobs` | 表單欄位來自 JSON 表單定義 | 否 | 部分職缺頁有 Cloudflare 驗證 | **H** | **M** | 4–6 天 | 許多歐洲中小企業 |
| **Recruitee / Teamtailor / Personio / BambooHR / JazzHR** | 各有公開 feed（`<co>.recruitee.com/api/offers`、`<co>.jobs.personio.de/xml`、`<co>.bamboohr.com/careers/list` 等） | 簡單單頁表單 | 否 | 低 | **H** | **L–M** | Phase 0 後每個 2–4 天 | 共用核心完成後，適合一起做成「小型 ATS adapter」 |
| **Workday** | 各租戶 JSON `POST <tenant>.wdN.myworkdayjobs.com/wday/cxs/<tenant>/<site>/jobs` | 5–7 步精靈：建帳號 → 個人資料 → 經歷（解析履歷後修正）→ 題目 → 自願揭露 → 確認。`data-automation-id` 屬性**跨租戶穩定**，因此可行 | **每個雇主租戶都要新帳號**，外加 e-mail 驗證 | 中等 | **M**（規劃器可用，DOM 不行） | **XH** | 15–20 天 | 所有 ATS 中職缺最多，也最難自動化。需要自動化 e-mail 驗證（例如 Gmail MCP）並為每個租戶保存密碼。等便宜的目標完成後再做 |
| **iCIMS** | 各公司入口網站，差異大 | 常是 iframe 內嵌的多步驟表單，每個雇主設定不同 | 通常需帳號 | 中等 | **L** | **H** | 10–15 天 | 投報率低。改用「輔助模式」（能填的先填，再交給人處理） |
| **Taleo（Oracle）/ SuccessFactors（SAP）** | 各公司 | 舊式企業精靈，有 session 逾時，每家公司一個帳號 | 是 | 中等 | **L** | **XH** | 各 15 天以上 | 延後。只有目標雇主清單需要時才值得做 |

### 3.2 綜合與科技求職網站

| 平台 | 角色 | 應徵機制 | 沿用度 | 複雜度 | 工作量 | ToS 風險 | 建議 |
|---|---|---|---|---|---|---|---|
| **Glassdoor** | 主要是探索 | 多數轉址到雇主 ATS。其 Easy Apply 現已與 Indeed 共用 | M | M（Cloudflare） | 3–4 天（僅探索） | 高 | 僅用於探索，再導向 ATS adapter |
| **ZipRecruiter** | 求職網站 | 以儲存的個人檔案一鍵應徵 | M（LinkedIn 精靈模式） | M–H | 5–7 天 | 高 | 低優先 |
| **Monster / CareerBuilder / SimplyHired** | 求職網站 | 站內應徵與轉址混合 | M | M | 4–6 天 | 中 | 除非有需求否則略過：流量下滑中 |
| **Google Jobs** | 純聚合器 | 無（無法應徵）。沒有官方 API；SerpAPI 等服務需付費 | H | L | 1–2 天（透過 SerpAPI） | 使用 API 時低 | 很好的探索來源，可接到 ATS router |
| **Wellfound**（原 AngelList Talent） | 求職網站 | 登入後寫一段短訊給創辦人，加上已存的個人檔案 | M（session + LLM 撰寫短訊） | M（DataDome） | 4–6 天 | 中 | 對新創族群值得做 |
| **Dice** | 求職網站（美國科技） | 登入，Easy Apply 精靈 | M（LinkedIn 精靈） | M | 4–6 天 | 中 | 美國科技約聘職缺的中優先 |
| **Welcome to the Jungle**（已併入 Otta） | 求職網站 | 多數轉址到 ATS，部分站內應徵 | H | M | 3–5 天 | 中 | 探索 + 導向 |
| **Built In** | 求職網站 | 轉址到 ATS | H | L | 1–2 天 | 低 | 探索 + 導向 |
| **YC Work at a Startup** | 求職網站 | 登入後傳訊給創辦人 | M | M | 3–4 天 | 中 | 訊息本身就是「應徵」，由 LLM 起草 |
| **Hacker News「Who is Hiring」** | 每月討論串 | 自由格式貼文，附 e-mail 或 ATS 連結 | **H**（已有 `hn_digest` 與 `email_collect`） | L | 2–3 天 | 低（官方 Algolia／Firebase API） | 高價值、低成本。用 LLM 解析貼文，導向 ATS 連結，e-mail 則排入外展佇列 |
| **Hired** | 反向媒合平台 | 只有個人檔案（由公司主動聯繫），沒有可應徵的對象 | — | — | — | — | 略過 |

### 3.3 區域性求職網站

| 地區 | 平台 | 應徵機制 | 沿用度 | 複雜度 | 工作量 | 建議 |
|---|---|---|---|---|---|---|
| **台灣** | **1111人力銀行** | 登入，搜尋 → 卡片 → 以已存履歷與自我推薦信應徵，模式與 104 相同 | **H**（複製 `tw104_apply_tool.py`） | L–M | 3–5 天 | **台灣第一優先** |
| 台灣 | **CakeResume / Cake** | 登入，用 Cake 個人檔案與履歷一鍵應徵。以 JSON 為後端的 SPA | M–H（tasker 擷取 bearer token 的模式） | M | 4–5 天 | 建議做：科技與英文職缺強 |
| 台灣 | **Yourator** | 登入，附履歷與選填訊息應徵 | M–H | L–M | 3–4 天 | 建議做：新創族群 |
| 台灣 | **518熊班** | 登入，與 104／1111 類似 | H | L–M | 3–4 天 | 稍後：偏藍領與服務業 |
| **歐洲** | StepStone（德國等） | 站內「Quick apply」與 ATS 轉址混合 | M | M–H | 5–7 天 | 中（德國市場） |
| 歐洲 | XING（德奧瑞） | 多數轉址到 ATS | H | M | 2–3 天（探索） | 探索 + 導向 |
| 歐洲 | InfoJobs（西班牙） | 官方開發者 API（OAuth）。開工前先確認是否開放應徵 | M | M | 4–6 天 | 可搭配既有的西班牙 Greenhouse 職缺頁工作 |
| 歐洲 | Reed / Totaljobs（英國） | Reed 有官方搜尋 API，應徵在站內 | M | M | 4–5 天 | 中 |
| **亞太** | **Seek**（澳紐）**+ JobStreet / JobsDB**（東南亞） | 登入後走 Quick Apply 精靈。**JobStreet 與 JobsDB 現在運行在 Seek 的平台上**，一個 adapter 涵蓋三者 | M（LinkedIn 精靈） | M | 三者合計 6–8 天 | **高價值**：單一 adapter 涵蓋澳、紐、新、馬、港、菲、印尼、泰 |
| 亞太 | Naukri（印度） | 登入，站內應徵，反機器人積極 | M | H | 6–8 天 | 僅在以印度為目標時 |
| 亞太 | BOSS直聘 / 51job / 智聯招聘（中國） | BOSS 以 App 為主、靠聊天驅動。三者反機器人都很強，且需要中國手機號 | L | XH | — | 略過 |
| 亞太 | Rikunabi / doda / BizReach（日本） | 透過仲介、日文長表單、獵才（scout）模式 | L | H–XH | — | 略過；至多「輔助模式」 |
| 亞太 | JobKorea / Saramin（韓國） | 登入，站內應徵。Saramin 有公開搜尋 API | M | H | 6–8 天 | 僅在以韓國為目標時 |

---

## 4. 遠端、約聘與接案平台

### 4.1 遠端職缺網站（全職或約聘）

> **實測後修正（Phase 2）：** 這些網站大多**不會**提供雇主的應徵連結，見「進度與實測心得」。
> 下表的「應徵」欄是原本的假設。實際上它們是探索來源，只會產生手動 lead。

| 平台 | 探索 | 應徵 | 沿用度 | 工作量 | 備註 |
|---|---|---|---|---|---|
| **RemoteOK** | 公開 JSON API `remoteok.com/api` | 轉址到 ATS／e-mail | H | 1 天 | 所有來源中最簡單 |
| **Remotive** | 公開 API `remotive.com/api/remote-jobs` | 轉址到 ATS | H | 1 天 | 簡單 |
| **We Work Remotely** | 依類別的 RSS feed | 轉址到 ATS／e-mail | H | 1–2 天 | 簡單 |
| **Working Nomads / Jobspresso / Remote.co / JustRemote / Himalayas** | RSS 或 JSON，或輕量爬取 | 轉址 | H | 各 1–2 天 | 前三個之後效益遞減 |
| **FlexJobs** | 付費會員，需登入 | 轉址 | M | 2–3 天 | 僅在使用者有訂閱時 |
| **EU Remote Jobs / Remote Rocketship / Arc.dev 職缺頁** | 爬取或 RSS | 轉址 | H | 1–2 天 | 選用 |

這些全部經由 ATS router（§1），因此每多建一個 ATS adapter，它們的價值就跟著提升。

### 4.2 接案平台（投標／提案模式，與 `tasker_apply` 同形態）

| 平台 | 模式 | 自動化介面 | 沿用度 | 複雜度 | 工作量 | ToS 風險 | 建議 |
|---|---|---|---|---|---|---|---|
| **Upwork** | 提案需花費付費的「Connects」 | 有官方 GraphQL API，但第三方工具送出提案受限制。機器人偵測嚴格，帳號會因自動化被停權 | M（tasker 流程 + LLM 提案） | H | 6–8 天 | **非常高** | **只起草**：搜尋、排序並起草提案，由人按下送出。絕不自動送出（要花錢且有帳號風險） |
| **Freelancer.com** | 投標 | **官方 REST API** 提供投標端點（OAuth） | **H**（tasker 流程：列表 → 檢查 → 送出 → 確認） | M | 4–5 天 | 透過 API 時低 | **最佳接案目標**：官方 API 意味著不受 DOM 變動影響 |
| **PeoplePerHour** | 提案 | 登入，網頁表單 | M | M | 4–5 天 | 中 | 中（英國／歐洲） |
| **Guru** | 報價（消耗投標點數） | 登入，網頁表單 | M | M | 4–5 天 | 中 | 低優先 |
| **Malt**（法／德／西／荷） | 客戶主動聯繫接案者；以個人檔案為主 | 多為收到的訊息，幾乎沒有可應徵的對象 | L | — | — | — | 只維護個人檔案；不做自動應徵 |
| **Contra** | 免抽成，個人檔案加部分職缺貼文 | 登入，應徵貼文 | M | M | 3–4 天 | 中 | 選用 |
| **Fiverr** | 賣家端的服務上架（買家主動找上門） | 沒有應徵模式 | — | — | — | — | 略過 |
| **Twago / Freelancermap / Gulp**（德奧瑞 IT 約聘） | 專案看板 | 登入，應徵專案 | M | M | 各 4–5 天 | 中 | 僅在以德奧瑞約聘為目標時 |
| **台灣：PRO360達人網、1111外包網、518外包網** | 接案／報價需求 | 登入，報價。與 tasker 類似 | **H**（tasker） | L–M | 各 3–5 天 | 中 | 先做 PRO360：與 tasker 最像 |

### 4.3 審核制人才網路與約聘派遣（申請一次，由對方媒合）

這些不是逐一應徵職缺的平台。每個平台有一次性的審核流程（測驗、面試），之後再媒合職缺。
**自動化價值低**，因此列出來供使用者自行手動註冊，不開發 adapter：

- **開發者網路：**Toptal、Turing、Andela、Arc.dev（人才端）、Lemon.io、Gun.io、
  Braintrust、A.Team、X-Team、Proxify、Gigster
- **商業與顧問：**Catalant、Expert360、BTG（Business Talent Group）
- **利基領域：**Kolabtree（科學）、Codementor（指導與接案）、99designs 與 Dribbble（設計）
- **人力派遣公司**（約聘職缺；其職缺網站通常架在 Bullhorn 或某個 ATS 上）：
  Robert Half、Hays、Randstad、Adecco、Michael Page、Kforce、TEKsystems。如有需要，其職缺頁
  可作為 ATS router 的探索來源。
- **AI 訓練約聘工作**（遠端、以任務計）：Outlier、DataAnnotation、Alignerr、Mercor。
  採用入門測驗，而非逐一應徵職缺。

---

## 5. 路線圖

| 階段 | 範圍 | 工作量 | 排序理由 |
|---|---|---|---|
| **0. 共用 ATS 核心** ✅ | 抽出 `ats_common.py`（`Question`、`plan_answers`、ledger、比對、相關性過濾），並把 Greenhouse 搬到上面 | 3–4 天 | 之後每個 adapter 只剩「探索 + 填寫」。Greenhouse 的測試與 eval 為重構把關 |
| **1. 便宜的 ATS** ✅（掃描器尚未完成） | Lever、Ashby，接著 Workable 與 SmartRecruiters。新增 `detect_ats(url)` 與依 URL 導向的通用 `ats_apply` spec。把依國家掃描職缺頁的腳本擴展到新 ATS | 15–20 天 | 公開 API、免登入，以最低風險換到最多新覆蓋 |
| **2. 探索來源** ✅（HN + 3 個網站） | RemoteOK、Remotive、We Work Remotely、HN「Who is Hiring」、Built In、Google Jobs（SerpAPI）→ ATS router。未知 ATS 的連結列入執行報告中的「手動」清單 | 7–10 天 | 每個來源 1–2 天，並沿用 Phase 1 的所有 adapter |
| **3. 台灣求職網站** ⏸ 暫緩 | 1111（104 的翻版）、CakeResume、Yourator，接著 518 | 13–18 天 | 直接沿用 104／tasker 模式；在地價值高 |
| **4. 接案** | Freelancer.com（API 投標）、PRO360（tasker 翻版）、Upwork **只起草** | 13–18 天 | 與 tasker 同一套流程。Upwork 保持人工把關 |
| **5. 亞太求職網站** | Seek + JobStreet + JobsDB（一個 adapter） | 6–8 天 | 一次開發涵蓋約八個市場 |
| **6. Workday** | 租戶探索、建立帳號、透過 Gmail 做 e-mail 驗證、以 `data-automation-id` 走訪精靈 | 15–20 天 | 職缺量最大，但成本最高。等共用核心驗證過後再做 |
| **延後** | iCIMS、Taleo、SuccessFactors、Glassdoor／ZipRecruiter 應徵、中日韓求職網站、審核制人才網路 | — | 投報率低、反機器人強，或非應徵模式。有特定目標需要時再評估 |

Phase 0–5 合計約 **60–80 個工作天**。加上 Phase 6 約 **75–100 天**。

### 每個 adapter 的檢查清單（依 `CLAUDE.md`）

每個新平台都必須：

1. 在 `spec.py` 註冊 `AutomationSpec`，附上 `canary`（對搜尋或職缺頁端點的唯讀探測）或
   `canary_skip` 理由。
2. 依**確定性漏斗（deterministic-funnel）**原則撰寫 flow 與 tool：flow 直接驅動 tool，
   crew 只用於相關性與答題判斷。
3. 在 `evals/<job_type>/` 新增 golden eval，或在 `evals/skip.yaml` 加上有理由的項目。
4. 新增 `_AGENT_DEFS` 列，以及 manifest `fields`（`custom_ui=False`），除非表單需要檔案上傳。
   履歷上傳已經走 `/api/uploads`。
5. 支援 dry-run、ledger、`warnings` 清單，以及回傳部分結果而非拋出例外。
6. 需要登入的網站：提供 `scripts/<name>_login.py`，並在 `browser_session.py` 加入項目，讓
   Admin → Sessions 能重新登入。

### 跨領域風險

- **ToS 與帳號停權。**以使用者自己的登入帳號應徵，風險與現在的 `linkedin_apply` 相同。保留
  每次執行的上限與擬人化節奏，新 adapter 預設 `dry_run`。
- **CAPTCHA。**Greenhouse 已需要有頭的真實 Chrome。Lever、Ashby、Workable 預期也是如此。
  不要整合 CAPTCHA 破解服務。
- **跨表單的答題品質。**共用規劃器必須保留 Greenhouse 的規則：**必填題答案未知時跳過該職缺**，
  而不是猜測。表單種類越多，這點越重要。
- **跨來源重複應徵。**同一職缺可能同時出現在 RemoteOK、HN 與雇主的 Lever 職缺頁。ledger 的 key
  應該是導向後的 ATS 標準職缺 id，而不是來源 URL。
