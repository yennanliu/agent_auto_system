from pathlib import Path

import yaml
from fastapi import APIRouter

router = APIRouter()

_PROJECT_ROOT = Path(__file__).parent.parent.parent


def _read_file(rel: str) -> str:
    try:
        return (_PROJECT_ROOT / rel).read_text(encoding="utf-8")
    except Exception:
        return ""



# Compact per-agent structural facts. Role/goal/backstory are NOT here — they are
# read from the crew's agents.yaml at serve time by _build_agents(), so the System
# catalog can never drift from the real config. See doc/automation-extensibility-design.md.
_AGENT_DEFS: list[dict] = [
    {"id": 'form_agent', "name": 'Form Agent', "tools": ['google_form_inspector', 'google_form_submit'], "crew": 'FormFillerCrew', "task": 'fill_form_task', "job_type": 'google_form_fill', "source_file": 'src/automation/crews/form_crew/config/agents.yaml'},
    {"id": 'web_scraper_agent', "name": 'Web Scraper Agent', "tools": ['web_scraper'], "crew": 'WebScraperCrew', "task": 'scrape_task', "job_type": 'web_scraper', "source_file": 'src/automation/crews/web_scraper_crew/config/agents.yaml'},
    {"id": 'email_sender_agent', "name": 'Email Sender Agent', "tools": ['gmail_send_email'], "crew": 'EmailSenderCrew', "task": 'send_email_task', "job_type": 'email_sender', "source_file": 'src/automation/crews/email_sender_crew/config/agents.yaml'},
    {"id": 'hn_analyst', "name": 'HN Analyst', "tools": ['hn_top_stories'], "crew": 'HNDigestCrew', "task": 'digest_task', "job_type": 'hacker_news_digest', "source_file": 'src/automation/crews/hn_digest_crew/config/agents.yaml'},
    {"id": 'google_sheet_agent', "name": 'Google Sheet Agent', "tools": ['google_sheet_reader'], "crew": 'GoogleSheetCrew', "task": 'sheet_read_task', "job_type": 'google_sheet_reader', "source_file": 'src/automation/crews/google_sheet_crew/config/agents.yaml'},
    {"id": 'x_analyst', "name": 'X Analyst', "tools": ['x_post_scraper'], "crew": 'XScraperCrew', "task": 'x_scrape_task', "job_type": 'x_scraper', "source_file": 'src/automation/crews/x_scraper_crew/config/agents.yaml'},
    {"id": 'shopee_seller_analyst', "name": 'Shopee Seller Analyst', "tools": ['shopee_seller_scraper'], "crew": 'ShopeeSellerCrew', "task": 'shopee_seller_task', "job_type": 'shopee_seller_scraper', "source_file": 'src/automation/crews/shopee_seller_crew/config/agents.yaml'},
    {"id": 'data_validator', "name": '資料驗證員', "tools": [], "crew": 'ProfitHealthCrew', "task": 'validate_task', "job_type": 'profit_health_check', "source_file": 'src/automation/crews/profit_health_crew/config/agents.yaml'},
    {"id": 'data_corrector', "name": '資料修正員', "tools": [], "crew": 'ProfitHealthCrew', "task": 'correct_task', "job_type": 'profit_health_check', "source_file": 'src/automation/crews/profit_health_crew/config/agents.yaml'},
    {"id": 'profit_analyzer', "name": '利潤分析師', "tools": ['profit_calc'], "crew": 'ProfitHealthCrew', "task": 'analyze_task', "job_type": 'profit_health_check', "source_file": 'src/automation/crews/profit_health_crew/config/agents.yaml'},
    {"id": 'action_advisor', "name": '行動建議員', "tools": [], "crew": 'ProfitHealthCrew', "task": 'advise_task', "job_type": 'profit_health_check', "source_file": 'src/automation/crews/profit_health_crew/config/agents.yaml'},
    {"id": 'proposal_writer', "name": 'Proposal Writer', "tools": [], "crew": 'TaskerProposalCrew', "task": 'write_proposal_task', "job_type": 'tasker_apply', "source_file": 'src/automation/crews/tasker_apply_crew/config/agents.yaml'},
    {"id": 'relevance_judge', "name": 'Relevance Judge', "tools": [], "crew": 'TaskerRelevanceCrew', "task": 'judge_relevance_task', "job_type": 'tasker_apply', "source_file": 'src/automation/crews/tasker_relevance_crew/config/agents.yaml'},
    {"id": 'tw104_relevance_judge', "name": '104 Relevance Judge', "yaml_key": 'relevance_judge', "tools": [], "crew": 'TW104RelevanceCrew', "task": 'judge_relevance_task', "job_type": 'tw104_apply', "source_file": 'src/automation/crews/tw104_relevance_crew/config/agents.yaml'},
    {"id": 'tw104_area_resolver', "name": '104 Area Resolver', "yaml_key": 'area_resolver', "tools": [], "crew": 'TW104AreaCrew', "task": 'resolve_area_task', "job_type": 'tw104_apply', "source_file": 'src/automation/crews/tw104_area_crew/config/agents.yaml'},
    {"id": 'lead_qualifier_agent', "name": 'Lead Qualifier', "tools": [], "crew": 'EmailCollectCrew', "task": 'qualify_task', "job_type": 'email_collect', "source_file": 'src/automation/crews/email_collect_crew/config/agents.yaml'},
]


def _build_agents() -> list[dict]:
    """Merge structural defs with role/goal/backstory read live from crew YAML.

    Runs at import time (when _CATALOG is defined), so it must never raise: a
    malformed or missing agents.yaml degrades to empty prose rather than crashing
    app startup.
    """
    agents = []
    for d in _AGENT_DEFS:
        key = d.get("yaml_key", d["id"])
        cfg: dict = {}
        try:
            parsed = yaml.safe_load(_read_file(d["source_file"]))
            if isinstance(parsed, dict) and isinstance(parsed.get(key), dict):
                cfg = parsed[key]
        except Exception:  # noqa: BLE001 — bad YAML must not break the catalog/startup
            cfg = {}
        agents.append({
            "id": d["id"], "name": d["name"],
            "role": (cfg.get("role") or "").strip(),
            "goal": (cfg.get("goal") or "").strip(),
            "backstory": (cfg.get("backstory") or "").strip(),
            "tools": d["tools"], "crew": d["crew"], "task": d["task"],
            "job_type": d["job_type"], "source_file": d["source_file"],
        })
    return agents


_CATALOG: dict = {
    "agents": _build_agents(),
    "tools": [
        {
            "id": "google_form_inspector",
            "name": "Google Form Inspector",
            "class": "GoogleFormInspectorTool",
            "description": "Fetch a Google Form's structure. Returns the form_id and for each question: title, entry_id, type, and options. Call this FIRST before submitting.",
            "inputs": [
                {"name": "url", "type": "str", "description": "The Google Form URL"},
            ],
            "used_by": ["FormFillerCrew"],
            "source_file": "src/automation/tools/google_form_tools.py",
        },
        {
            "id": "google_form_submit",
            "name": "Google Form Submit",
            "class": "GoogleFormSubmitTool",
            "description": "Submit a Google Form via HTTP POST with session cookies and CSRF token. Handles the full GET→POST flow to avoid silent field discards.",
            "inputs": [
                {"name": "form_id", "type": "str", "description": "Form ID from URL"},
                {"name": "responses", "type": "dict", "description": "Mapping of entry_id → answer value"},
            ],
            "used_by": ["FormFillerCrew"],
            "source_file": "src/automation/tools/google_form_tools.py",
        },
        {
            "id": "web_scraper",
            "name": "Web Scraper",
            "class": "WebScraperTool",
            "description": "Fetch a web page and return full structured content: title, meta description, h1-h3 headings, main text (up to 8 000 chars), outbound links, and word count.",
            "inputs": [
                {"name": "url", "type": "str", "description": "URL to fetch"},
            ],
            "used_by": ["WebScraperCrew"],
            "source_file": "src/automation/tools/web_scraper_tool.py",
        },
        {
            "id": "gmail_send_email",
            "name": "Gmail Send",
            "class": "GmailSendTool",
            "description": "Send an email via Gmail SMTP using an app password. Supports multiple recipients (comma-separated), CC, and HTML or plain-text bodies.",
            "inputs": [
                {"name": "to",      "type": "str",           "description": "Recipient(s), comma-separated"},
                {"name": "subject", "type": "str",           "description": "Email subject line"},
                {"name": "body",    "type": "str",           "description": "HTML or plain-text email body"},
                {"name": "cc",      "type": "str (optional)","description": "CC recipients, comma-separated"},
            ],
            "used_by": ["EmailSenderCrew"],
            "source_file": "src/automation/tools/gmail_send_tool.py",
        },
        {
            "id": "hn_top_stories",
            "name": "HN Top Stories",
            "class": "HNTopStoriesTool",
            "description": "Fetch the top stories from Hacker News via Firebase API. Returns title, url, score, comments, and author for each story.",
            "inputs": [
                {"name": "limit", "type": "int", "description": "Number of stories to fetch (1–10)"},
            ],
            "used_by": ["HNDigestCrew"],
            "source_file": "src/automation/tools/hn_tool.py",
        },
        {
            "id": "google_sheet_reader",
            "name": "Google Sheet Reader",
            "class": "GoogleSheetTool",
            "description": "Fetch a Google Sheet as CSV and return structured data: column names, row count, all data rows (up to limit), and a 5-row preview. Accepts a standard Google Sheets URL or a direct CSV export URL — auto-converts to the export format.",
            "inputs": [
                {"name": "url",   "type": "str",          "description": "Google Sheets URL or CSV export URL"},
                {"name": "limit", "type": "int (1–500)",  "description": "Maximum rows to return (default 200)"},
            ],
            "used_by": ["GoogleSheetCrew"],
            "source_file": "src/automation/tools/google_sheet_tool.py",
        },
        {
            "id": "x_post_scraper",
            "name": "X Post Scraper",
            "class": "XScraperTool",
            "description": "Fetch recent posts from a public X profile. Tries multiple nitter instances first, falls back to Playwright on x.com.",
            "inputs": [
                {"name": "username", "type": "str", "description": "X handle (without @)"},
                {"name": "limit", "type": "int", "description": "Number of posts to fetch"},
            ],
            "used_by": ["XScraperCrew"],
            "source_file": "src/automation/tools/x_scraper_tool.py",
        },
        {
            "id": "shopee_seller_scraper",
            "name": "Shopee Seller Scraper",
            "class": "ShopeeSellerScraperTool",
            "description": "Search shopee.tw for a keyword, open the top N products, and collect the seller behind each: shop name, URL, location, join date, rating, rating count, follower count, item count, response rate. Reuses a persisted login session (SHOPEE_STORAGE_STATE) — prefers Shopee's internal JSON API, falls back to DOM scraping.",
            "inputs": [
                {"name": "keyword", "type": "str", "description": "Product search keyword"},
                {"name": "limit",   "type": "int", "description": "Number of top products / sellers to collect"},
            ],
            "used_by": ["ShopeeSellerCrew"],
            "source_file": "src/automation/tools/shopee_scraper_tool.py",
        },
        {
            "id": "profit_calc",
            "name": "Profit Calc",
            "class": "ProfitCalcTool",
            "description": "Compute deterministic per-SKU profit metrics for an uploaded data set. Given an upload_id, reads the 4 Shopee CSVs and returns each SKU's revenue, cost, ad_spend, refunds, net_profit, margin_pct, units, roas, return_count/rate, plus grouped flags (最賺錢/假爆品/廣告吃利潤/退貨異常). All arithmetic is done in Python — never by the LLM.",
            "inputs": [
                {"name": "upload_id", "type": "str", "description": "Upload id returned by POST /api/uploads"},
            ],
            "used_by": ["ProfitHealthCrew"],
            "source_file": "src/automation/tools/profit_calc_tool.py",
        },
        {
            "id": "report_renderer",
            "name": "Report Renderer",
            "class": "render_report_pdf",
            "description": "Deterministically render a 利潤健檢 ProfitReport (JSON) into a styled HTML report and print it to PDF via headless Chromium (json → html → pdf). Produces summary cards, a per-SKU table with margin bars + AI 判斷 badges, and a prioritised next-week action list. Pure presentation — no LLM. Invoked by ProfitHealthFlow.render_pdf; the PDF is served at GET /api/runs/{id}/report.pdf.",
            "inputs": [
                {"name": "report", "type": "dict", "description": "ProfitReport-shaped JSON from the crew"},
                {"name": "out_path", "type": "Path", "description": "Destination PDF path (reports/<run_id>.pdf)"},
            ],
            "used_by": ["ProfitHealthFlow"],
            "source_file": "src/automation/report_render.py",
        },
        {
            "id": "tasker_apply",
            "name": "Tasker Auto-Apply",
            "class": "TaskerApplyTool",
            "description": (
                "Log in to tasker.com.tw (persisted session or .env credentials) and "
                "auto-apply (提案) to open cases in a category. Collects open case links "
                "from /cases?selected_categories=<ids>, opens each, clicks 我要提案, fills "
                "the 初次估價 min/max charge and 提案說明, skips already-applied cases, and "
                "clicks 送出提案 only when dry_run is false. Called directly by the flow "
                "(with an LLM proposal_fn); this BaseTool wrapper uses a static template."
            ),
            "inputs": [
                {"name": "category_ids",      "type": "str",  "description": "Category id(s), e.g. '110' or '110,101001'"},
                {"name": "min_charge",        "type": "int",  "description": "初次估價 lower bound"},
                {"name": "max_charge",        "type": "int",  "description": "初次估價 upper bound"},
                {"name": "proposal_template", "type": "str",  "description": "Base/fallback 提案說明 text"},
                {"name": "max_cases",         "type": "int (1–500)",  "description": "Number of eligible cases to actually apply to; auto-advances pages (default 5)"},
                {"name": "dry_run",           "type": "bool", "description": "If true (default), prepare but do NOT click 送出提案"},
            ],
            "used_by": ["TaskerApplyFlow"],
            "source_file": "src/automation/tools/tasker_apply_tool.py",
        },
        {
            "id": "tw104_apply",
            "name": "104 Auto-Apply",
            "class": "TW104ApplyTool",
            "description": (
                "Log in to 104.com.tw (persisted session) and auto-apply (應徵) to open "
                "jobs matching a keyword. Pages through /jobs/search/, opens each job, "
                "clicks 應徵, selects a saved 推薦信 cover letter, skips already-applied "
                "jobs (已應徵), and clicks 確認送出 only when dry_run is false — counting "
                "success only when the site lands on /job/apply/done/. Called directly "
                "by the flow (with an optional LLM relevance_fn); the apply itself is "
                "pure DOM automation."
            ),
            "inputs": [
                {"name": "keyword",          "type": "str",  "description": "Job search keyword, e.g. '軟體工程師'"},
                {"name": "area",             "type": "str",  "description": "Area name or 104 code — '台北'/'taipei'/'6001001000' (blank = all Taiwan); the flow resolves names to codes, LLM-enriching misses"},
                {"name": "order",            "type": "str",  "description": "Listing sort order (default '1')"},
                {"name": "max_applications", "type": "int (1–1000)", "description": "Number of jobs to actually apply to; auto-advances pages (default 5)"},
                {"name": "max_pages",        "type": "int (1–500)", "description": "Max search result pages to scan before stopping (default 10)"},
                {"name": "cover_letter",     "type": "str",  "description": "Custom 自我推薦信 free text typed into the apply form (blank = site default 系統預設; max 2000 chars)"},
                {"name": "remote",           "type": "bool", "description": "Only search 完全遠端 jobs (104 remoteWork=1)"},
                {"name": "part_time",        "type": "bool", "description": "Only search 兼職 jobs (104 工作性質 ro=2)"},
                {"name": "dry_run",          "type": "bool", "description": "If true (default), prepare but do NOT click 確認送出"},
            ],
            "used_by": ["TW104ApplyFlow"],
            "source_file": "src/automation/tools/tw104_apply_tool.py",
        },
        {
            "id": "maps_search",
            "name": "Google Maps Search",
            "class": "MapsSearchTool",
            "description": "Search Google Maps for businesses matching a query in a region (headless Chromium): scroll the results feed and open each place panel to read name, website, phone, address, and category. The website feeds the email-extraction stage. Returns partial results + warnings instead of failing on markup shifts.",
            "inputs": [
                {"name": "query",  "type": "str", "description": "What to search, e.g. 'marketing agency'"},
                {"name": "region", "type": "str", "description": "Where, e.g. 'Taipei' / 'Berlin' / 'Austin, TX'"},
                {"name": "limit",  "type": "int (1–500)", "description": "Number of listings to collect"},
            ],
            "used_by": ["EmailCollectFlow"],
            "source_file": "src/automation/tools/maps_search_tool.py",
        },
        {
            "id": "web_email_extract",
            "name": "Web Email Extractor",
            "class": "WebEmailExtractTool",
            "description": "Fetch a business website (homepage + common contact/about/impressum pages) and extract contact emails from mailto: links and page text, filtering tracking/CDN/placeholder junk and ranking role addresses (info@, contact@…) first. Guesses role addresses from the domain if none are published (flagged guessed).",
            "inputs": [
                {"name": "website", "type": "str", "description": "Business website URL"},
            ],
            "used_by": ["EmailCollectFlow"],
            "source_file": "src/automation/tools/email_extract_tool.py",
        },
        {
            "id": "email_verify",
            "name": "Email Verifier",
            "class": "EmailVerifyTool",
            "description": "Verify emails for free — syntax, MX-record lookup (dnspython, A-record fallback), and a best-effort SMTP RCPT probe that never sends mail. Returns per-email deliverability signals and a high/medium/low confidence label. Port-25-blocked / greylisted probes report 'unknown', never a failure.",
            "inputs": [
                {"name": "emails",     "type": "list[str]", "description": "Addresses to verify"},
                {"name": "smtp_check", "type": "bool",      "description": "Run the SMTP RCPT probe (default true)"},
            ],
            "used_by": ["EmailCollectFlow"],
            "source_file": "src/automation/tools/email_verify_tool.py",
        },
    ],
    "crews": [
        {
            "id": "form_filler_crew",
            "name": "FormFillerCrew",
            "process": "sequential",
            "agents": ["form_agent"],
            "job_type": "google_form_fill",
            "flow": "FormFillFlow",
            "tasks": [
                {
                    "name": "fill_form_task",
                    "description": "Inspect the Google Form structure then submit with provided company details.",
                    "expected_output": '{"submitted": true, "confirmation": "..."}',
                    "config_file": "src/automation/crews/form_crew/config/tasks.yaml",
                }
            ],
            "source_file": "src/automation/crews/form_crew/crew.py",
        },
        {
            "id": "web_scraper_crew",
            "name": "WebScraperCrew",
            "process": "sequential",
            "agents": ["web_scraper_agent"],
            "job_type": "web_scraper",
            "flow": "WebScraperFlow",
            "tasks": [
                {
                    "name": "scrape_task",
                    "description": "Fetch the URL and extract a comprehensive structured summary of all page content.",
                    "expected_output": '{"url": "...", "title": "...", "summary": "...", "key_points": [...], "headings": [...], "word_count": N, "links": [...], "content": "..."}',
                    "config_file": "src/automation/crews/web_scraper_crew/config/tasks.yaml",
                }
            ],
            "source_file": "src/automation/crews/web_scraper_crew/crew.py",
        },
        {
            "id": "email_sender_crew",
            "name": "EmailSenderCrew",
            "process": "sequential",
            "agents": ["email_sender_agent"],
            "job_type": "email_sender",
            "flow": "EmailSenderFlow",
            "tasks": [
                {
                    "name": "send_email_task",
                    "description": "Call gmail_send_email tool with the exact provided parameters without modification.",
                    "expected_output": '{"sent": true, "to": "...", "subject": "...", "confirmation": "..."}',
                    "config_file": "src/automation/crews/email_sender_crew/config/tasks.yaml",
                }
            ],
            "source_file": "src/automation/crews/email_sender_crew/crew.py",
        },
        {
            "id": "hn_digest_crew",
            "name": "HNDigestCrew",
            "process": "sequential",
            "agents": ["hn_analyst"],
            "job_type": "hacker_news_digest",
            "flow": "HNDigestFlow",
            "tasks": [
                {
                    "name": "digest_task",
                    "description": "Fetch top N HN stories, summarize each, pick story of the day, identify 2–3 themes.",
                    "expected_output": '{"story_of_the_day": {...}, "stories": [...], "themes": [...]}',
                    "config_file": "src/automation/crews/hn_digest_crew/config/tasks.yaml",
                }
            ],
            "source_file": "src/automation/crews/hn_digest_crew/crew.py",
        },
        {
            "id": "google_sheet_crew",
            "name": "GoogleSheetCrew",
            "process": "sequential",
            "agents": ["google_sheet_agent"],
            "job_type": "google_sheet_reader",
            "flow": "GoogleSheetFlow",
            "tasks": [
                {
                    "name": "sheet_read_task",
                    "description": "Fetch the Google Sheet with the reader tool, then analyze columns, statistics, and patterns.",
                    "expected_output": '{"url": "...", "columns": [...], "row_count": N, "summary": "...", "insights": [...], "data": [...], "preview": [...]}',
                    "config_file": "src/automation/crews/google_sheet_crew/config/tasks.yaml",
                }
            ],
            "source_file": "src/automation/crews/google_sheet_crew/crew.py",
        },
        {
            "id": "x_scraper_crew",
            "name": "XScraperCrew",
            "process": "sequential",
            "agents": ["x_analyst"],
            "job_type": "x_scraper",
            "flow": "XScraperFlow",
            "tasks": [
                {
                    "name": "x_scrape_task",
                    "description": "Fetch N posts from a public X profile, find top post, identify themes, write summary.",
                    "expected_output": '{"username": "...", "post_count": N, "top_post": {...}, "themes": [...], "summary": "...", "posts": [...]}',
                    "config_file": "src/automation/crews/x_scraper_crew/config/tasks.yaml",
                }
            ],
            "source_file": "src/automation/crews/x_scraper_crew/crew.py",
        },
        {
            "id": "shopee_seller_crew",
            "name": "ShopeeSellerCrew",
            "process": "sequential",
            "agents": ["shopee_seller_analyst"],
            "job_type": "shopee_seller_scraper",
            "flow": "ShopeeSellerFlow",
            "tasks": [
                {
                    "name": "shopee_seller_task",
                    "description": "Search Shopee for the keyword, collect sellers behind the top N products, and summarize.",
                    "expected_output": '{"keyword": "...", "seller_count": N, "sellers": [...], "summary": "..."}',
                    "config_file": "src/automation/crews/shopee_seller_crew/config/tasks.yaml",
                }
            ],
            "source_file": "src/automation/crews/shopee_seller_crew/crew.py",
        },
        {
            "id": "profit_health_crew",
            "name": "ProfitHealthCrew",
            "process": "sequential",
            "agents": ["data_validator", "data_corrector", "profit_analyzer", "action_advisor"],
            "job_type": "profit_health_check",
            "flow": "ProfitHealthFlow",
            "tasks": [
                {
                    "name": "validate_task",
                    "description": "驗證四份 CSV 的欄位與內容，列出資料問題。",
                    "expected_output": '{"ok": bool, "issues": [...]}',
                    "config_file": "src/automation/crews/profit_health_crew/config/tasks.yaml",
                },
                {
                    "name": "correct_task",
                    "description": "正規化與修補資料，記錄已套用的修正與被剔除的列。",
                    "expected_output": '{"applied": [...], "dropped": [...]}',
                    "config_file": "src/automation/crews/profit_health_crew/config/tasks.yaml",
                },
                {
                    "name": "analyze_task",
                    "description": "呼叫 profit_calc(upload_id) 取得精確利潤數字，歸納四類旗標商品。",
                    "expected_output": '{"skus": [...], "flags": {...}}',
                    "config_file": "src/automation/crews/profit_health_crew/config/tasks.yaml",
                },
                {
                    "name": "advise_task",
                    "description": "綜合前述結果，產出繁中健檢報告：建議、下週行動清單、總結。",
                    "expected_output": '{"summary": "...", "skus": [...], "flags": {...}, "recommendations": [...], "action_items": [...], "validation": {...}}',
                    "config_file": "src/automation/crews/profit_health_crew/config/tasks.yaml",
                },
            ],
            "source_file": "src/automation/crews/profit_health_crew/crew.py",
        },
        {
            "id": "tasker_apply_crew",
            "name": "TaskerProposalCrew",
            "process": "sequential",
            "agents": ["proposal_writer"],
            "job_type": "tasker_apply",
            "flow": "TaskerApplyFlow",
            "tasks": [
                {
                    "name": "write_proposal_task",
                    "description": "Write a tailored Traditional-Chinese 提案說明 for a single case (one kickoff per case). Browser automation is handled directly by TaskerApplyFlow via the tasker_apply tool.",
                    "expected_output": "A ready-to-send 提案說明 plain-text string (no title/quotes/markdown).",
                    "config_file": "src/automation/crews/tasker_apply_crew/config/tasks.yaml",
                }
            ],
            "source_file": "src/automation/crews/tasker_apply_crew/crew.py",
        },
        {
            "id": "tasker_relevance_crew",
            "name": "TaskerRelevanceCrew",
            "process": "sequential",
            "agents": ["relevance_judge"],
            "job_type": "tasker_apply",
            "flow": "TaskerApplyFlow",
            "tasks": [
                {
                    "name": "judge_relevance_task",
                    "description": "Second gate: judge whether a single case matches the user's natural-language task_filter (one kickoff per scanned case), skipping irrelevant cases before a proposal is written. Only runs when task_filter is set.",
                    "expected_output": "A strict JSON verdict {\"relevant\": bool, \"reason\": str}.",
                    "config_file": "src/automation/crews/tasker_relevance_crew/config/tasks.yaml",
                }
            ],
            "source_file": "src/automation/crews/tasker_relevance_crew/crew.py",
        },
        {
            "id": "email_collect_crew",
            "name": "EmailCollectCrew",
            "process": "sequential",
            "agents": ["lead_qualifier_agent"],
            "job_type": "email_collect",
            "flow": "EmailCollectFlow",
            "tasks": [
                {
                    "name": "qualify_task",
                    "description": "Score ICP fit (1–5) and write one personalization hook per discovered business. Discovery, email extraction, and verification are done deterministically in EmailCollectFlow via the maps_search / web_email_extract / email_verify tools.",
                    "expected_output": '[{"i": 0, "icp_fit": 4, "reason": "...", "hook": "..."}]',
                    "config_file": "src/automation/crews/email_collect_crew/config/tasks.yaml",
                }
            ],
            "source_file": "src/automation/crews/email_collect_crew/crew.py",
        },
    ],
    "workflows": [
        {
            "id": "form_fill_flow",
            "name": "FormFillFlow",
            "job_type": "google_form_fill",
            "crew": "FormFillerCrew",
            "state_fields": [
                {"name": "company_name", "type": "str", "default": ""},
                {"name": "company_size", "type": "str", "default": ""},
                {"name": "ai_problem", "type": "str", "default": ""},
                {"name": "run_id", "type": "int", "default": 0},
            ],
            "steps": [
                {
                    "name": "validate_payload",
                    "decorator": "@start()",
                    "description": "Validates all required fields (company_name, company_size, ai_problem) are present. Raises ValueError if any are missing.",
                },
                {
                    "name": "execute_crew",
                    "decorator": "@listen(validate_payload)",
                    "description": "Kicks off FormFillerCrew with the validated payload. Returns the raw crew output.",
                },
            ],
            "source_file": "src/automation/flows/form_fill_flow.py",
        },
        {
            "id": "web_scraper_flow",
            "name": "WebScraperFlow",
            "job_type": "web_scraper",
            "crew": "WebScraperCrew",
            "state_fields": [
                {"name": "url", "type": "str", "default": ""},
                {"name": "run_id", "type": "int", "default": 0},
            ],
            "steps": [
                {
                    "name": "validate_payload",
                    "decorator": "@start()",
                    "description": "Validates that url is present. Raises ValueError if missing.",
                },
                {
                    "name": "execute_crew",
                    "decorator": "@listen(validate_payload)",
                    "description": "Kicks off WebScraperCrew with the url. Returns structured page summary JSON.",
                },
            ],
            "source_file": "src/automation/flows/web_scraper_flow.py",
        },
        {
            "id": "email_sender_flow",
            "name": "EmailSenderFlow",
            "job_type": "email_sender",
            "crew": "EmailSenderCrew (direct tool call — no LLM)",
            "state_fields": [
                {"name": "to",      "type": "str", "default": ""},
                {"name": "subject", "type": "str", "default": ""},
                {"name": "body",    "type": "str", "default": ""},
                {"name": "cc",      "type": "str", "default": ""},
                {"name": "run_id",  "type": "int", "default": 0},
            ],
            "steps": [
                {
                    "name": "validate_payload",
                    "decorator": "@start()",
                    "description": "Validates to, subject, and body are present. Logs recipient count.",
                },
                {
                    "name": "send_email",
                    "decorator": "@listen(validate_payload)",
                    "description": "Calls GmailSendTool directly via SMTP — no LLM involved. Returns send confirmation JSON.",
                },
            ],
            "source_file": "src/automation/flows/email_sender_flow.py",
        },
        {
            "id": "hn_digest_flow",
            "name": "HNDigestFlow",
            "job_type": "hacker_news_digest",
            "crew": "HNDigestCrew",
            "state_fields": [
                {"name": "limit", "type": "int", "default": 5},
                {"name": "run_id", "type": "int", "default": 0},
            ],
            "steps": [
                {
                    "name": "validate_payload",
                    "decorator": "@start()",
                    "description": "Validates that limit is between 1 and 10.",
                },
                {
                    "name": "execute_crew",
                    "decorator": "@listen(validate_payload)",
                    "description": "Kicks off HNDigestCrew with the limit. Returns the digest JSON.",
                },
            ],
            "source_file": "src/automation/flows/hn_digest_flow.py",
        },
        {
            "id": "x_scraper_flow",
            "name": "XScraperFlow",
            "job_type": "x_scraper",
            "crew": "XScraperCrew",
            "state_fields": [
                {"name": "username", "type": "str", "default": ""},
                {"name": "limit", "type": "int", "default": 5},
                {"name": "run_id", "type": "int", "default": 0},
            ],
            "steps": [
                {
                    "name": "validate_payload",
                    "decorator": "@start()",
                    "description": "Validates that username is present. Raises ValueError if missing.",
                },
                {
                    "name": "execute_crew",
                    "decorator": "@listen(validate_payload)",
                    "description": "Kicks off XScraperCrew with username and limit. Returns the social media analysis JSON.",
                },
            ],
            "source_file": "src/automation/flows/x_scraper_flow.py",
        },
        {
            "id": "shopee_seller_flow",
            "name": "ShopeeSellerFlow",
            "job_type": "shopee_seller_scraper",
            "crew": "ShopeeSellerCrew",
            "state_fields": [
                {"name": "keyword", "type": "str", "default": ""},
                {"name": "limit",   "type": "int", "default": 5},
                {"name": "run_id",  "type": "int", "default": 0},
            ],
            "steps": [
                {
                    "name": "validate_payload",
                    "decorator": "@start()",
                    "description": "Validates that keyword is present. Raises ValueError if missing.",
                },
                {
                    "name": "execute_crew",
                    "decorator": "@listen(validate_payload)",
                    "description": "Loads the persisted Shopee session, kicks off ShopeeSellerCrew with keyword and limit, returns the seller analysis JSON.",
                },
            ],
            "source_file": "src/automation/flows/shopee_seller_flow.py",
        },
        {
            "id": "google_sheet_flow",
            "name": "GoogleSheetFlow",
            "job_type": "google_sheet_reader",
            "crew": "GoogleSheetCrew",
            "state_fields": [
                {"name": "url",   "type": "str", "default": ""},
                {"name": "limit", "type": "int", "default": 200},
                {"name": "run_id","type": "int", "default": 0},
            ],
            "steps": [
                {
                    "name": "validate_payload",
                    "decorator": "@start()",
                    "description": "Validates that url is present. Raises ValueError if missing.",
                },
                {
                    "name": "execute_crew",
                    "decorator": "@listen(validate_payload)",
                    "description": "Kicks off GoogleSheetCrew with the URL and limit. The agent fetches the CSV with the google_sheet_reader tool, analyzes it, and returns structured JSON.",
                },
            ],
            "source_file": "src/automation/flows/google_sheet_flow.py",
        },
        {
            "id": "profit_health_flow",
            "name": "ProfitHealthFlow",
            "job_type": "profit_health_check",
            "crew": "ProfitHealthCrew",
            "state_fields": [
                {"name": "upload_id", "type": "str", "default": ""},
                {"name": "sales_csv", "type": "str", "default": ""},
                {"name": "cost_csv", "type": "str", "default": ""},
                {"name": "ads_csv", "type": "str", "default": ""},
                {"name": "returns_csv", "type": "str", "default": ""},
                {"name": "run_id", "type": "int", "default": 0},
            ],
            "steps": [
                {
                    "name": "validate_payload",
                    "decorator": "@start()",
                    "description": "Resolves upload_id, reads the CSVs from uploads/<id>/, and validates that required sales+cost files are present.",
                },
                {
                    "name": "execute_crew",
                    "decorator": "@listen(validate_payload)",
                    "description": "Resolves the LLM and runs ProfitHealthCrew (驗證→修正→分析→建議). Returns the Traditional-Chinese profit report JSON.",
                },
                {
                    "name": "render_pdf",
                    "decorator": "@listen(execute_crew)",
                    "description": "Renders the report JSON → HTML → PDF (reports/<run_id>.pdf) and injects pdf_url into the result. Fail-soft: rendering errors never fail the run.",
                },
            ],
            "source_file": "src/automation/flows/profit_health_flow.py",
        },
        {
            "id": "pipeline",
            "name": "Pipeline",
            "job_type": "pipeline",
            "crew": "(Orchestrates multiple sub-flows)",
            "state_fields": [
                {"name": "steps", "type": "list[{job_type, payload}]", "default": []},
            ],
            "steps": [
                {
                    "name": "interpolate_and_dispatch",
                    "decorator": "@step (sequential loop)",
                    "description": (
                        "For each step: substitute {{steps.N.result}} and "
                        "{{steps.N.result.field}} templates in payload, dispatch to the "
                        "appropriate sub-flow, collect result. The final pipeline result "
                        "contains all step results and the last step's result."
                    ),
                },
            ],
            "source_file": "src/automation/pipeline.py",
        },
        {
            "id": "tasker_apply_flow",
            "name": "TaskerApplyFlow",
            "job_type": "tasker_apply",
            "crew": "TaskerProposalCrew (per-case 提案說明) + TaskerRelevanceCrew (2nd-gate filter) + tasker_apply tool (browser)",
            "state_fields": [
                {"name": "category_ids",      "type": "str",  "default": ""},
                {"name": "min_charge",        "type": "int",  "default": 0},
                {"name": "max_charge",        "type": "int",  "default": 0},
                {"name": "proposal_template", "type": "str",  "default": ""},
                {"name": "task_filter",       "type": "str",  "default": ""},
                {"name": "max_cases",         "type": "int",  "default": 5},
                {"name": "dry_run",           "type": "bool", "default": True},
                {"name": "run_id",            "type": "int",  "default": 0},
            ],
            "steps": [
                {
                    "name": "validate_payload",
                    "decorator": "@start()",
                    "description": "Validates category_ids, min_charge, max_charge (min <= max).",
                },
                {
                    "name": "execute_apply",
                    "decorator": "@listen(validate_payload)",
                    "description": (
                        "Resolves the LLM, logs in to tasker.com.tw, scans open cases in "
                        "the category, and for each: (optional 2nd gate) if task_filter is "
                        "set, TaskerRelevanceCrew judges relevance and skips non-matching "
                        "cases before proposing; then writes a tailored 提案說明 via "
                        "TaskerProposalCrew, fills 初次估價 min/max + 提案說明, skips "
                        "already-applied cases, and clicks 送出提案 unless dry_run. "
                        "Returns an applied/skipped summary JSON (incl. filtered_count)."
                    ),
                },
            ],
            "source_file": "src/automation/flows/tasker_apply_flow.py",
        },
        {
            "id": "email_collect_flow",
            "name": "EmailCollectFlow",
            "job_type": "email_collect",
            "crew": "EmailCollectCrew (ICP fit + hook) + maps_search / web_email_extract / email_verify tools",
            "state_fields": [
                {"name": "query",      "type": "str",  "default": ""},
                {"name": "region",     "type": "str",  "default": ""},
                {"name": "industry",   "type": "str",  "default": ""},
                {"name": "offer",      "type": "str",  "default": ""},
                {"name": "limit",      "type": "int",  "default": 15},
                {"name": "smtp_check", "type": "bool", "default": True},
                {"name": "run_id",     "type": "int",  "default": 0},
            ],
            "steps": [
                {
                    "name": "validate_payload",
                    "decorator": "@start()",
                    "description": "Requires query; folds industry into the search term.",
                },
                {
                    "name": "run_funnel",
                    "decorator": "@listen(validate_payload)",
                    "description": (
                        "Runs the funnel deterministically: DISCOVER businesses on Google "
                        "Maps (maps_search) → EXTRACT emails from each website "
                        "(web_email_extract) → VERIFY (email_verify: syntax/MX/SMTP) → "
                        "dedupe & rank. Then QUALIFIES leads with EmailCollectCrew (ICP fit "
                        "+ personalization hook). Returns discovered/lead counts + leads[]."
                    ),
                },
            ],
            "source_file": "src/automation/flows/email_collect_flow.py",
        },
    ],
}


@router.get("/system")
def get_system():
    result: dict = {}
    seen_files: dict[str, str] = {}

    for category, items in _CATALOG.items():
        enriched = []
        for item in items:
            item = dict(item)
            sf = item.get("source_file")
            if sf:
                if sf not in seen_files:
                    seen_files[sf] = _read_file(sf)
                item["source_code"] = seen_files[sf]
            if category == "crews":
                tasks = []
                for t in item.get("tasks", []):
                    t = dict(t)
                    cf = t.get("config_file")
                    if cf:
                        if cf not in seen_files:
                            seen_files[cf] = _read_file(cf)
                        t["config_code"] = seen_files[cf]
                    tasks.append(t)
                item["tasks"] = tasks
            enriched.append(item)
        result[category] = enriched

    return result


@router.get("/automations/manifest")
def get_automations_manifest():
    """Declarative manifest of every automation, for the browser to render the
    picker, run forms, and step graph from — see src/automation/spec.py. Includes
    admin-authored custom automations (Phase 3G) so they render with no extra UI."""
    from src import custom_automations
    from src.automation import spec

    return {"automations": spec.manifest() + custom_automations.manifest_entries()}
