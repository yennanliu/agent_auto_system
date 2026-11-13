// Automation catalog: job types, TYPE_META / AUTO_CATALOG, the LLM provider→model map
// and the server manifest (FLOW_STEPS / MANIFEST) that overrides the static tables.

import { _origFetch } from './net.js';

// ── Constants ──────────────────────────────────────────────────────────────────

export let ALL_TYPES = ['google_form_fill', 'web_scraper', 'hacker_news_digest', 'x_scraper', 'email_sender', 'google_sheet_reader', 'shopee_seller_scraper', 'profit_health_check', 'tasker_apply', 'tw104_apply', 'linkedin_apply', 'greenhouse_apply', 'email_collect', 'pipeline'];

export const TYPE_META = {
  google_form_fill:   { chip: 'FORM',  cls: 'chip-form'     },
  web_scraper:        { chip: 'WEB',   cls: 'chip-web'      },
  hacker_news_digest: { chip: 'HN',    cls: 'chip-hn'       },
  x_scraper:          { chip: 'X',     cls: 'chip-x'        },
  email_sender:       { chip: 'EMAIL', cls: 'chip-email'    },
  google_sheet_reader: { chip: 'SHEET', cls: 'chip-sheet'    },
  shopee_seller_scraper: { chip: 'SHOPEE', cls: 'chip-shopee' },
  profit_health_check: { chip: '利潤健檢', cls: 'chip-profit' },
  tasker_apply:        { chip: 'TASKER', cls: 'chip-tasker' },
  tw104_apply:         { chip: '104',    cls: 'chip-104'    },
  linkedin_apply:      { chip: 'LINKEDIN', cls: 'chip-linkedin' },
  greenhouse_apply:    { chip: 'GREENHOUSE', cls: 'chip-greenhouse' },
  email_collect:        { chip: 'EMAILS', cls: 'chip-leads' },
  pipeline:            { chip: 'PIPE',  cls: 'chip-pipeline' },
};

export const AUTO_CATALOG = {
  google_form_fill: {
    icon: '📋', name: 'Form Fill',
    desc: 'Automatically fill and submit any Google Form using AI agents that inspect the form structure and match your data to the correct fields.',
    inputs: [
      { name: 'company_name', type: 'str', desc: 'Company name to fill in' },
      { name: 'company_size', type: 'select', desc: 'Size tier of the company' },
      { name: 'ai_problem',   type: 'str', desc: 'AI use-case or problem description' },
    ],
    crew: 'FormFillerCrew', flow: 'FormFillFlow',
    agent: 'Form Agent', tools: ['Google Form Inspector', 'Google Form Submit'],
  },
  web_scraper: {
    icon: '🌐', name: 'Web Scraper',
    desc: 'Fetch any public web page and return a comprehensive structured summary — title, key points, headings, word count, and outbound links. No question needed.',
    inputs: [
      { name: 'url', type: 'str', desc: 'Full URL to scrape' },
    ],
    crew: 'WebScraperCrew', flow: 'WebScraperFlow',
    agent: 'Web Content Analyst', tools: ['Web Scraper'],
  },
  hacker_news_digest: {
    icon: '🔶', name: 'HN Digest',
    desc: 'Fetch the top N Hacker News stories, summarize each in one sentence, pick the story of the day, and identify recurring themes.',
    inputs: [
      { name: 'limit', type: 'int (1–10)', desc: 'Number of top stories to include' },
    ],
    crew: 'HNDigestCrew', flow: 'HNDigestFlow',
    agent: 'HN Analyst', tools: ['HN Top Stories'],
  },
  x_scraper: {
    icon: '✕', name: 'X Scraper',
    desc: 'Scrape recent public posts from any X (Twitter) profile, identify top content, themes, and produce a written summary.',
    inputs: [
      { name: 'username', type: 'str', desc: 'X handle (without the @ symbol)' },
      { name: 'limit',    type: 'int (1–10)', desc: 'Number of recent posts to fetch' },
    ],
    crew: 'XScraperCrew', flow: 'XScraperFlow',
    agent: 'X Analyst', tools: ['X Post Scraper'],
  },
  email_sender: {
    icon: '✉️', name: 'Email Sender',
    desc: 'Send an email to one or more recipients via Gmail SMTP. Supports HTML or plain-text bodies, CC, and multiple comma-separated addresses. No LLM — content is sent exactly as provided.',
    inputs: [
      { name: 'to',      type: 'str', desc: 'Recipient address(es), comma-separated' },
      { name: 'subject', type: 'str', desc: 'Email subject line' },
      { name: 'body',    type: 'str', desc: 'Email body — HTML or plain text' },
      { name: 'cc',      type: 'str (optional)', desc: 'CC addresses, comma-separated' },
    ],
    crew: 'EmailSenderCrew', flow: 'EmailSenderFlow',
    agent: 'Email Sender Agent', tools: ['Gmail Send'],
  },
  google_sheet_reader: {
    icon: '📊', name: 'Sheet Reader',
    desc: 'Fetch any public Google Sheet as structured data and get an AI-generated analysis: column overview, row count, key statistics, and notable patterns. Works with any sharing-enabled sheet.',
    inputs: [
      { name: 'url',   type: 'str',         desc: 'Google Sheets URL (any format — share link, edit link, or export URL)' },
      { name: 'limit', type: 'int (1–500)', desc: 'Maximum rows to fetch (default 200)' },
    ],
    crew: 'GoogleSheetCrew', flow: 'GoogleSheetFlow',
    agent: 'Google Sheet Agent', tools: ['Google Sheet Reader'],
  },
  shopee_seller_scraper: {
    icon: '🛒', name: 'Shopee Sellers',
    desc: 'Search shopee.tw for a keyword and collect the sellers behind the top N products — shop name, URL, location, join date, rating, followers, and item count. Reuses a saved login session.',
    inputs: [
      { name: 'keyword', type: 'str', desc: 'Product search keyword (e.g. 無線耳機)' },
      { name: 'limit',   type: 'int (1–100)', desc: 'Number of top products / sellers to collect' },
    ],
    crew: 'ShopeeSellerCrew', flow: 'ShopeeSellerFlow',
    agent: 'Shopee Seller Analyst', tools: ['Shopee Seller Scraper'],
  },
  profit_health_check: {
    icon: '🧾', name: '利潤健檢',
    desc: 'Select all your Shopee CSVs at once — the system auto-classifies each by filename (sales, cost, ads, returns). A 4-agent crew validates → corrects → analyzes → advises, producing a per-SKU profit health report in Traditional Chinese plus a downloadable PDF — most-profitable, fake hits, ad-eats-profit, and return-anomaly SKUs with a next-week action list.',
    inputs: [
      { name: 'files', type: 'file[] (.csv)', desc: '一次選取所有蝦皮 CSV，依檔名自動分類；須含 sales 與 cost' },
    ],
    crew: 'ProfitHealthCrew', flow: 'ProfitHealthFlow',
    agent: '資料驗證員 · 資料修正員 · 利潤分析師 · 行動建議員', tools: ['Profit Calc', 'Report Renderer'],
  },
  tasker_apply: {
    icon: '🧰', name: 'Tasker 自動提案',
    desc: 'Log in to tasker.com.tw and auto-apply (提案) to open cases in a category: fill the 初次估價 min/max charge, write a tailored 提案說明 per case with AI, and submit. Auto-advances through listing pages, skipping cases you already proposed to or can\'t propose to, until it has applied to max_cases eligible ones. A submission is only counted as successful when the site confirms it. Dry-run by default — prepares proposals without clicking 送出提案.',
    inputs: [
      { name: 'category_ids', type: 'str',       desc: 'Category id(s) from selected_categories, e.g. 110 or 110,101001' },
      { name: 'min_charge',   type: 'int',       desc: '初次估價 lower bound (元)' },
      { name: 'max_charge',   type: 'int',       desc: '初次估價 upper bound (元, 須大於 min)' },
      { name: 'max_cases',    type: 'int (1–500)', desc: 'Number of eligible cases to actually apply to (auto-advances pages)' },
      { name: 'task_filter',  type: 'str',       desc: '2nd gate (optional): natural-language filter; AI skips cases that don\'t match before proposing' },
      { name: 'dry_run',      type: 'bool',      desc: 'If checked, fill but do NOT click 送出提案' },
    ],
    crew: 'TaskerProposalCrew + TaskerRelevanceCrew', flow: 'TaskerApplyFlow',
    agent: 'Proposal Writer', tools: ['Tasker Auto-Apply'],
  },
  tw104_apply: {
    icon: '💼', name: '104 自動應徵',
    desc: 'Log in to 104.com.tw (via a saved session) and auto-apply (應徵) to open jobs matching a keyword: click 應徵, pick a saved 推薦信 cover letter, and submit. Auto-advances through search result pages, skipping jobs you already applied to, until it has applied to max_applications ones. An application is only counted as successful when the site confirms it (lands on /job/apply/done/). The LLM (gemini/openai/anthropic) acts as an optional relevance gate — the apply itself is pure browser automation. Dry-run by default — prepares applications without clicking 確認送出.',
    inputs: [
      { name: 'keyword',          type: 'str',         desc: 'Job search keyword, e.g. 軟體工程師 / python' },
      { name: 'area',             type: 'str',         desc: 'Area name OR 104 code — 台北 / taipei / 高雄, or 6001001000 (blank = all Taiwan). AI auto-resolves names & typos to codes.' },
      { name: 'max_applications', type: 'int (1–1000)', desc: 'Number of jobs to actually apply to (auto-advances pages)' },
      { name: 'max_pages',        type: 'int (1–500)',  desc: 'Max search result pages to scan before stopping' },
      { name: 'cover_letter',     type: 'str',         desc: 'Custom 自我推薦信 text typed into the apply form (blank = site default; max 2000). Save & reuse as a named pattern.' },
      { name: 'remote',           type: 'bool',        desc: '只搜完全遠端職缺 (104 remoteWork=1)' },
      { name: 'part_time',        type: 'bool',        desc: '只搜兼職職缺 (104 工作性質 ro=2)' },
      { name: 'task_filter',      type: 'str',         desc: '2nd gate (optional): natural-language filter; AI skips jobs that don\'t match before applying' },
      { name: 'dry_run',          type: 'bool',        desc: 'If checked, prepare but do NOT click 確認送出' },
    ],
    crew: 'TW104RelevanceCrew', flow: 'TW104ApplyFlow',
    agent: '104 Relevance Judge', tools: ['104 Auto-Apply'],
  },
  linkedin_apply: {
    icon: '🔗', name: 'LinkedIn Easy Apply',
    desc: 'Log in to LinkedIn (via a saved session) and auto-apply to "Easy Apply" jobs matching a keyword: open each job, walk the multi-step Easy Apply modal (fill contact info / screening questions → Next/Review → Submit). Auto-advances through search result pages, skipping jobs you already applied to, until it has applied to max_applications ones. An application is only counted as successful when LinkedIn shows the "application was sent" banner. The LLM (gemini/openai/anthropic) acts as an optional relevance gate — the apply itself is pure browser automation. Dry-run by default — fills the form without clicking Submit.',
    inputs: [
      { name: 'keywords',         type: 'str',         desc: 'Job search keywords, e.g. Software Engineer / python' },
      { name: 'location',         type: 'str',         desc: 'Location text — Taipei / United Kingdom (default Taiwan)' },
      { name: 'remote',           type: 'bool',        desc: 'Only search remote jobs (LinkedIn f_WT=2)' },
      { name: 'phone',            type: 'str',         desc: 'Phone number for the Easy Apply contact question (optional)' },
      { name: 'nationality',      type: 'str',         desc: 'Default answer to nationality/citizenship questions (default Taiwanese)' },
      { name: 'years_experience', type: 'int',         desc: 'Default answer to "years of experience" screening questions' },
      { name: 'max_applications', type: 'int (1–1000)', desc: 'Number of jobs to actually apply to (auto-advances pages)' },
      { name: 'max_pages',        type: 'int (1–500)',  desc: 'Max search result pages to scan before stopping' },
      { name: 'task_filter',      type: 'str',         desc: '2nd gate (optional): natural-language filter; AI skips jobs that don\'t match before applying' },
      { name: 'dry_run',          type: 'bool',        desc: 'If checked, fill the form but do NOT click Submit' },
    ],
    crew: 'LinkedInRelevanceCrew', flow: 'LinkedInApplyFlow',
    agent: 'LinkedIn Relevance Judge', tools: ['LinkedIn Easy Apply'],
  },
  greenhouse_apply: {
    icon: '🌱', name: 'Greenhouse Apply',
    desc: 'Auto-apply to jobs on companies\' Greenhouse job boards — no login needed. Lists each board\'s open jobs via the public Job Board API, filters them by title keywords and location, reads every application form\'s questions from the same API and answers them from your profile (plus your "keyword: answer" extra answers). A job with a required question that can\'t be answered truthfully is skipped and listed, never sent with a guess. Then a browser fills the form, uploads your resume and submits — counted as applied only when Greenhouse shows its confirmation page. The LLM is optional: a relevance gate (task_filter) and a screening answerer grounded in your candidate_background. Dry-run by default.',
    inputs: [
      { name: 'boards',               type: 'str',  desc: 'Greenhouse board tokens or URLs, comma-separated (e.g. figma, airbnb)' },
      { name: 'keywords',             type: 'str',  desc: 'Title filter phrases, comma-separated (blank = all jobs)' },
      { name: 'location',             type: 'str',  desc: 'Job-location filter substrings, e.g. Remote, Taipei (blank = any)' },
      { name: 'first_name / last_name / email / phone', type: 'str', desc: 'Applicant contact fields' },
      { name: 'resume_path',          type: 'str',  desc: 'Resume file on the server (default GREENHOUSE_RESUME_PATH)' },
      { name: 'extra_answers',        type: 'str',  desc: '"keyword: answer" lines for recurring custom questions' },
      { name: 'salary / notice / languages / programming / AI experience / reason for leaving', type: 'str', desc: 'Default answers to recurring screening questions, pre-filled and editable on the run form' },
      { name: 'candidate_background', type: 'str',  desc: 'Your own summary; enables LLM answers to required screening questions' },
      { name: 'task_filter',          type: 'str',  desc: '2nd gate (optional): natural-language filter; AI skips jobs that don\'t match' },
      { name: 'max_applications',     type: 'int (1–200)', desc: 'Number of jobs to actually apply to' },
      { name: 'dry_run',              type: 'bool', desc: 'If checked, fill the form but do NOT click Submit' },
    ],
    crew: 'GreenhouseRelevanceCrew + GreenhouseAnswerCrew', flow: 'GreenhouseApplyFlow',
    agent: 'Greenhouse Relevance Judge / Screening Answerer', tools: ['Greenhouse Apply'],
  },
  email_collect: {
    icon: '📧', name: 'Email Collector',
    desc: 'Find businesses and collect their contact emails — no paid database. Discovers companies from Google Maps, Taiwan 公會/工會 member directories, and the 經濟部 商工登記 company registry, scrapes each website for emails, verifies them (MX + SMTP), removes duplicates across sources, optionally enriches each lead with registry facts (統一編號, 資本額, 負責人), then uses AI to score fit and write a personalized opening line for each one.',
    inputs: [
      { name: 'query',    type: 'str', desc: 'What businesses to find (e.g. marketing agency, dental clinic). Also the keyword for 公會 directory and 經濟部 registry search — use Chinese there' },
      { name: 'region',   type: 'str', desc: 'Where (e.g. Taipei / Berlin / Austin, TX). Blank = anywhere. Filters the registered address for the 經濟部 source' },
      { name: 'industry', type: 'str (optional)', desc: 'Extra qualifier folded into the search term' },
      { name: 'offer',    type: 'str (optional)', desc: "What you're pitching — drives ICP scoring & hooks" },
      { name: 'limit',    type: 'int (1–500)', desc: 'Number of businesses to discover (across all sources, after dedupe)' },
      { name: 'sources',  type: 'list', desc: "Discovery sources: 'maps', 'association' (公會 member directories), 'govbiz' (經濟部 商工登記). Default: maps" },
      { name: 'associations', type: 'list', desc: "公會 directories to search — a built-in slug (see GET /api/associations; 'tca' = 台北市電腦商業同業公會) or any member-list URL" },
      { name: 'resolve_missing_websites', type: 'bool', desc: 'Look up a website on Maps for companies discovered without one (registry rows) so they can yield an email' },
      { name: 'gcis_enrich', type: 'bool', desc: 'Attach 統一編號 / 資本額 / 負責人 / 設立日期 from the 經濟部 registry to each lead' },
      { name: 'smtp_check', type: 'bool', desc: 'Run the SMTP RCPT probe during verification' },
      { name: 'include_social', type: 'bool', desc: 'Mine social profiles (Facebook, X, Instagram) for contacts and follow any real site they link' },
    ],
    crew: 'EmailCollectCrew', flow: 'EmailCollectFlow',
    agent: 'Lead Qualifier', tools: ['Google Maps Search', '公會 Member Directory', '經濟部 Company Registry', 'Web Email Extractor', 'Email Verifier', 'Facebook Contact', 'X Profile Contact', 'Instagram Contact'],
  },
  pipeline: {
    icon: '🔗', name: 'Pipeline',
    desc: 'Chain multiple automations in sequence. Each step\'s output is available to later steps via {{steps.N.result}}, {{steps.N.result.field}}, or {{steps.N.result.field[].subfield}} (flattens a list of objects into a comma-joined string, e.g. leads[].email) template variables in any payload field.',
    inputs: [
      { name: 'steps', type: 'list', desc: 'Ordered list of {job_type, payload} step definitions' },
    ],
    crew: 'Per-step crew', flow: 'Pipeline (execute_pipeline)',
    agent: 'Per step', tools: ['All step tools'],
  },
};

// ── LLM provider → model map ──────────────────────────────────────────────────

export const LLM_MODELS = {
  openai: [
    ['gpt-4o-mini', 'gpt-4o-mini (fast)'],
    ['gpt-4o',      'gpt-4o (smart)'],
    ['gpt-6-luna',  'GPT-6 Luna (fast, newest)'],
    ['gpt-6-sol',   'GPT-6 Sol (smart, newest)'],
  ],
  anthropic: [
    ['claude-haiku-4-5-20251001', 'Haiku 4.5 (fast)'],
    ['claude-sonnet-4-6',         'Sonnet 4.6 (smart)'],
    ['claude-haiku-5-5',          'Haiku 5.5 (fast, newest)'],
    ['claude-sonnet-5-5',         'Sonnet 5.5 (smart, newest)'],
    ['claude-opus-5-5',           'Opus 5.5 (most capable)'],
  ],
  gemini: [
    ['gemini/gemini-3.5-flash',       'Gemini 3.5 Flash'],
    ['gemini/gemini-3.1-flash-lite',   'Gemini 3.1 Flash-Lite'],
    ['gemini/gemini-2.5-pro',          'Gemini 2.5 Pro (smart)'],
    ['gemini/gemini-2.5-flash',        'Gemini 2.5 Flash'],
    ['gemini/gemini-2.5-flash-lite',   'Gemini 2.5 Flash-Lite (fast)'],
    ['gemini/gemini-3.5-flash-lite',   'Gemini 3.5 Flash-Lite (fast, newest)'],
    ['gemini/gemini-3.8-flash',        'Gemini 3.8 Flash (newest)'],
    ['gemini/gemini-4-argon',          'Gemini 4 Argon (paid tier only)'],
    ['gemini/gemini-4-carbon',         'Gemini 4 Carbon (paid tier only)'],
  ],
};

// ── Flow step definitions ─────────────────────────────────────────────────────
// job_type → [{label, trigger}]. Populated from the automation manifest at boot
// (GET /api/automations/manifest) so the client can never drift from the server's
// spec.py step definitions. Stays empty until the manifest loads.
export let FLOW_STEPS = {};
export let MANIFEST = [];                 // raw automation manifest (array)
export const MANIFEST_BY_TYPE = {};       // job_type → manifest entry

export async function loadManifest() {
  try {
    const resp = await _origFetch('/api/automations/manifest');
    if (!resp.ok) return;
    const data = await resp.json();
    MANIFEST = data.automations || [];
    MANIFEST.forEach(a => {
      MANIFEST_BY_TYPE[a.job_type] = a;
      if (a.steps && a.steps.length) {
        FLOW_STEPS[a.job_type] = a.steps.map(([label, trigger]) => ({ label, trigger }));
      }
    });
    if (MANIFEST.length) ALL_TYPES = MANIFEST.map(a => a.job_type);
  } catch (_) { /* keep app usable if the manifest fails to load */ }
}
