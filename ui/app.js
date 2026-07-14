// ── Constants ──────────────────────────────────────────────────────────────────

const ALL_TYPES = ['google_form_fill', 'web_scraper', 'hacker_news_digest', 'x_scraper', 'email_sender', 'google_sheet_reader', 'shopee_seller_scraper', 'profit_health_check', 'tasker_apply', 'tw104_apply', 'email_collect', 'pipeline'];

const TYPE_META = {
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
  email_collect:        { chip: 'EMAILS', cls: 'chip-leads' },
  pipeline:            { chip: 'PIPE',  cls: 'chip-pipeline' },
};

const AUTO_CATALOG = {
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
  email_collect: {
    icon: '📧', name: 'Email Collector',
    desc: 'Find businesses on Google Maps and collect their contact emails — no paid database. For a search + region it discovers businesses, scrapes each website for emails, verifies them (MX + SMTP), removes duplicates, then uses AI to score fit and write a personalized opening line for each one, ready for your outreach.',
    inputs: [
      { name: 'query',    type: 'str', desc: 'What businesses to find (e.g. marketing agency, dental clinic)' },
      { name: 'region',   type: 'str', desc: 'Where (e.g. Taipei / Berlin / Austin, TX). Blank = anywhere' },
      { name: 'industry', type: 'str (optional)', desc: 'Extra qualifier folded into the search term' },
      { name: 'offer',    type: 'str (optional)', desc: "What you're pitching — drives ICP scoring & hooks" },
      { name: 'limit',    type: 'int (1–40)', desc: 'Number of businesses to discover' },
      { name: 'smtp_check', type: 'bool', desc: 'Run the SMTP RCPT probe during verification' },
    ],
    crew: 'EmailCollectCrew', flow: 'EmailCollectFlow',
    agent: 'Lead Qualifier', tools: ['Google Maps Search', 'Web Email Extractor', 'Email Verifier'],
  },
  pipeline: {
    icon: '🔗', name: 'Pipeline',
    desc: 'Chain multiple automations in sequence. Each step\'s output is available to later steps via {{steps.N.result}} or {{steps.N.result.field}} template variables in any payload field.',
    inputs: [
      { name: 'steps', type: 'list', desc: 'Ordered list of {job_type, payload} step definitions' },
    ],
    crew: 'Per-step crew', flow: 'Pipeline (execute_pipeline)',
    agent: 'Per step', tools: ['All step tools'],
  },
};

// ── LLM provider → model map ──────────────────────────────────────────────────

const LLM_MODELS = {
  openai:    [['gpt-4o-mini', 'gpt-4o-mini (fast)'], ['gpt-4o', 'gpt-4o (smart)']],
  anthropic: [['claude-haiku-4-5-20251001', 'Haiku (fast)'], ['claude-sonnet-4-6', 'Sonnet (smart)']],
  gemini: [
    ['gemini/gemini-3.5-flash',       'Gemini 3.5 Flash'],
    ['gemini/gemini-3.1-flash-lite',   'Gemini 3.1 Flash-Lite'],
    ['gemini/gemini-2.5-pro',          'Gemini 2.5 Pro (smart)'],
    ['gemini/gemini-2.5-flash',        'Gemini 2.5 Flash'],
    ['gemini/gemini-2.5-flash-lite',   'Gemini 2.5 Flash-Lite (fast)'],
  ],
};

// ── Flow step definitions (label + log trigger substring) ─────────────────────

// The Verify (result validation) and Evaluate (LLM-as-judge score) nodes run
// centrally in the executor after every job, so they're appended to each flow.
const _QA_STEPS = [
  { label: 'Verify',   trigger: 'Validating result' },
  { label: 'Evaluate', trigger: 'Evaluation complete' },
];
const FLOW_STEPS = {
  google_form_fill: [
    { label: 'Start',        trigger: 'Starting' },
    { label: 'Validate',     trigger: 'Payload validated' },
    { label: 'Inspect Form', trigger: 'Inspecting Google Form' },
    { label: 'Submit',       trigger: 'Form submission attempted' },
    ..._QA_STEPS,
    { label: 'Done',         trigger: 'completed successfully' },
  ],
  web_scraper: [
    { label: 'Start',    trigger: 'Starting' },
    { label: 'Validate', trigger: 'Payload validated' },
    { label: 'Scrape',   trigger: 'scraper agent reading' },
    { label: 'Analyze',  trigger: 'generated summary' },
    ..._QA_STEPS,
    { label: 'Done',     trigger: 'completed successfully' },
  ],
  hacker_news_digest: [
    { label: 'Start',     trigger: 'Starting' },
    { label: 'Validate',  trigger: 'Fetching top' },
    { label: 'Fetch',     trigger: 'analyst agent reading' },
    { label: 'Digest',    trigger: 'Digest generated' },
    ..._QA_STEPS,
    { label: 'Done',      trigger: 'completed successfully' },
  ],
  x_scraper: [
    { label: 'Start',     trigger: 'Starting' },
    { label: 'Validate',  trigger: 'Validated payload' },
    { label: 'Fetch',     trigger: 'Fetching posts' },
    { label: 'Analyze',   trigger: 'Analysis complete' },
    ..._QA_STEPS,
    { label: 'Done',      trigger: 'completed successfully' },
  ],
  email_sender: [
    { label: 'Start',    trigger: 'Starting' },
    { label: 'Validate', trigger: 'Sending to' },
    { label: 'Send',     trigger: 'Connecting to Gmail' },
    ..._QA_STEPS,
    { label: 'Done',     trigger: 'completed successfully' },
  ],
  google_sheet_reader: [
    { label: 'Start',    trigger: 'Starting' },
    { label: 'Validate', trigger: 'Validated sheet URL' },
    { label: 'Fetch',    trigger: 'Fetching Google Sheet' },
    { label: 'Analyze',  trigger: 'Analyzing sheet data' },
    ..._QA_STEPS,
    { label: 'Done',     trigger: 'completed successfully' },
  ],
  shopee_seller_scraper: [
    { label: 'Start',    trigger: 'Starting' },
    { label: 'Validate', trigger: 'Validated payload for keyword' },
    { label: 'Search',   trigger: 'Loading Shopee session' },
    { label: 'Collect',  trigger: 'Seller collection complete' },
    ..._QA_STEPS,
    { label: 'Done',     trigger: 'completed successfully' },
  ],
  profit_health_check: [
    { label: 'Start',    trigger: 'Starting' },
    { label: 'Load CSV', trigger: 'Loaded CSVs' },
    { label: '驗證',     trigger: '蝦皮資料驗證員' },
    { label: '修正',     trigger: '蝦皮資料修正員' },
    { label: '分析',     trigger: '蝦皮利潤分析師' },
    { label: '建議',     trigger: '蝦皮營運行動建議員' },
    { label: 'PDF',      trigger: 'PDF 報告' },
    ..._QA_STEPS,
    { label: 'Done',     trigger: 'completed successfully' },
  ],
  tasker_apply: [
    { label: 'Start',    trigger: 'Starting' },
    { label: 'Validate', trigger: 'Payload validated' },
    { label: 'Login',    trigger: 'Loading tasker.com.tw session' },
    { label: 'Apply',    trigger: 'run complete' },
    ..._QA_STEPS,
    { label: 'Done',     trigger: 'completed successfully' },
  ],
  tw104_apply: [
    { label: 'Start',    trigger: 'Starting' },
    { label: 'Validate', trigger: 'Payload validated' },
    { label: 'Login',    trigger: 'Loading 104.com.tw session' },
    { label: 'Apply',    trigger: 'run complete' },
    ..._QA_STEPS,
    { label: 'Done',     trigger: 'completed successfully' },
  ],
  email_collect: [
    { label: 'Start',     trigger: 'Starting' },
    { label: 'Validate',  trigger: 'Payload validated' },
    { label: 'Discover',  trigger: 'Discovering businesses' },
    { label: 'Extract',   trigger: 'Extracting email' },
    { label: 'Collect',   trigger: 'Collected' },
    { label: 'Qualify',   trigger: 'Qualifying' },
    ..._QA_STEPS,
    { label: 'Done',      trigger: 'completed successfully' },
  ],
};

function inferStepStates(jobType, logs, finalStatus) {
  const steps = FLOW_STEPS[jobType];
  if (!steps) return null;
  const msgs = (logs || []).map(e => e.msg || '');
  let reached = -1;
  steps.forEach((s, i) => {
    if (msgs.some(m => m.includes(s.trigger))) reached = i;
  });
  return steps.map((_, i) => {
    if (i < reached) return 'done';
    if (i === reached) {
      if (finalStatus === 'failed')  return 'failed';
      if (finalStatus === 'success') return 'done';
      return 'running';
    }
    return 'pending';
  });
}

function parseLogTs(ts) {
  if (!ts) return null;
  const p = ts.split(':').map(Number);
  if (p.length !== 3 || p.some(isNaN)) return null;
  return p[0] * 3600 + p[1] * 60 + p[2];
}

function fmtDur(secs) {
  if (secs === null || secs === undefined) return null;
  return secs < 1 ? '<1s' : secs + 's';
}

function computeStepDurations(steps, logs, finalStatus) {
  const entries = logs || [];
  const triggerTs = steps.map(s => {
    const e = entries.find(e => (e.msg || '').includes(s.trigger));
    return e ? parseLogTs(e.ts) : null;
  });
  return triggerTs.map((ts, i) => {
    if (ts === null) return null;
    for (let j = i + 1; j < triggerTs.length; j++) {
      if (triggerTs[j] !== null) return Math.max(0, triggerTs[j] - ts);
    }
    if (finalStatus === 'success' || finalStatus === 'failed') {
      const last = entries[entries.length - 1];
      if (last) { const lt = parseLogTs(last.ts); if (lt !== null) return Math.max(0, lt - ts); }
    }
    return null;
  });
}

function renderStepGraph(jobType, logs, finalStatus) {
  if (jobType === 'pipeline') return renderPipelineStepGraph(logs, finalStatus);
  const steps = FLOW_STEPS[jobType];
  if (!steps) return '';
  const states = inferStepStates(jobType, logs, finalStatus) || steps.map(() => 'pending');
  const durations = computeStepDurations(steps, logs, finalStatus);
  const icons = { done: '✓', running: '…', failed: '✕', pending: '' };
  const parts = [];
  steps.forEach((s, i) => {
    const st = states[i];
    const dur = durations[i] !== null ? `<div class="step-metric">${fmtDur(durations[i])}</div>` : '';
    parts.push(`<div class="step-node sn-${st}"><div class="step-dot">${icons[st] || (i + 1)}</div><div class="step-label">${escHtml(s.label)}</div>${dur}</div>`);
    if (i < steps.length - 1) {
      let connCls = '';
      if (states[i] === 'done') connCls = states[i + 1] === 'running' ? 'sc-partial' : 'sc-done';
      parts.push(`<div class="step-conn ${connCls}"></div>`);
    }
  });
  return `<div class="step-graph">${parts.join('')}</div>`;
}

function renderPipelineStepGraph(logs, finalStatus) {
  const entries = logs || [];
  const stepMap = new Map(); // 0-based idx → {type, n, startTs, endTs}
  for (const e of entries) {
    const mS = (e.msg || '').match(/\[Step (\d+)\/(\d+)\] Starting (.+?)\.\.\./);
    if (mS) {
      const idx = parseInt(mS[1]) - 1;
      if (!stepMap.has(idx)) stepMap.set(idx, { type: mS[3], n: parseInt(mS[2]), startTs: parseLogTs(e.ts), endTs: null });
    }
    const mE = (e.msg || '').match(/\[Step (\d+)\/(\d+)\] Completed .+/);
    if (mE) {
      const idx = parseInt(mE[1]) - 1;
      const info = stepMap.get(idx);
      if (info && info.endTs === null) info.endTs = parseLogTs(e.ts);
    }
  }
  const totalSteps = stepMap.size > 0 ? (stepMap.values().next().value?.n || stepMap.size) : 1;
  const icons = { done: '✓', running: '…', failed: '✕', pending: '' };
  const parts = [];
  for (let i = 0; i < totalSteps; i++) {
    const info = stepMap.get(i);
    let state = info ? (info.endTs !== null ? 'done' : 'running') : 'pending';
    if (i === totalSteps - 1 && state !== 'done') {
      if (finalStatus === 'failed') state = 'failed';
      else if (finalStatus === 'success') state = 'done';
    }
    const label = info ? info.type.replace(/_/g, ' ') : `step ${i + 1}`;
    let dur = null;
    if (info?.startTs !== null && info?.startTs !== undefined) {
      const endT = info.endTs ?? ((finalStatus === 'success' || finalStatus === 'failed')
        ? parseLogTs(entries[entries.length - 1]?.ts) : null);
      if (endT !== null && endT !== undefined) dur = fmtDur(Math.max(0, endT - info.startTs));
    }
    const durHtml = dur ? `<div class="step-metric">${dur}</div>` : '';
    parts.push(`<div class="step-node sn-${state}"><div class="step-dot">${icons[state] || (i + 1)}</div><div class="step-label">${escHtml(label)}</div>${durHtml}</div>`);
    if (i < totalSteps - 1) {
      const connCls = info?.endTs !== null ? 'sc-done' : (info ? 'sc-partial' : '');
      parts.push(`<div class="step-conn ${connCls}"></div>`);
    }
  }
  return `<div class="step-graph">${parts.join('')}</div>`;
}

function updateModelOptions() {
  const provider = document.getElementById('llm-provider').value;
  const modelSel = document.getElementById('llm-model');
  const models = LLM_MODELS[provider] || LLM_MODELS.openai;
  modelSel.innerHTML = models.map(([v, l]) => `<option value="${v}">${l}</option>`).join('');
}

// ══════════════════════════════════════ TASK OVERVIEW ════════════════════════════
// Airflow-style grid grouped by AUTOMATION TYPE: one entry per automation, and its
// grid shows ALL runs of that automation together (columns = runs, rows = steps),
// so runs of the same automation can be compared side by side.

let ovTypes = [];
let ovSelectedType = null;

async function loadOverviewPage() {
  const list = document.getElementById('ov-joblist');
  list.innerHTML = '<div class="loading-state">Loading…</div>';
  try {
    const resp = await fetch('/api/overview');
    if (!resp.ok) throw new Error('Failed to load overview');
    ovTypes = await resp.json();
  } catch (err) {
    list.innerHTML = `<div class="empty-state">${escHtml(err.message)}</div>`;
    return;
  }
  renderOverviewTypeList();
  if (ovTypes.length) {
    const keep = ovTypes.find(t => t.job_type === ovSelectedType);
    selectOverviewType((keep || ovTypes[0]).job_type);
  } else {
    document.getElementById('ov-grid-wrap').innerHTML =
      '<div class="empty-state">No runs yet. Run an automation first.</div>';
  }
}

function renderOverviewTypeList() {
  const list = document.getElementById('ov-joblist');
  if (!ovTypes.length) { list.innerHTML = '<div class="empty-state">No runs</div>'; return; }
  list.innerHTML = ovTypes.map(t => {
    const meta = TYPE_META[t.job_type] || { chip: (t.job_type || '?').toUpperCase(), cls: 'chip-unknown' };
    const name = AUTO_CATALOG[t.job_type]?.name || t.job_type;
    const active = t.job_type === ovSelectedType ? ' active' : '';
    return `<button class="ov-job${active}" data-job-type="${escHtml(t.job_type)}">
      <span class="type-chip ${meta.cls}">${escHtml(meta.chip)}</span>
      <span class="ov-job-name">${escHtml(name)}</span>
      <span class="ov-job-count">${t.run_count}</span>
    </button>`;
  }).join('');
}

async function selectOverviewType(jobType) {
  ovSelectedType = jobType;
  renderOverviewTypeList();
  const wrap = document.getElementById('ov-grid-wrap');
  wrap.innerHTML = '<div class="loading-state">Loading…</div>';
  try {
    const resp = await fetch(`/api/overview/${encodeURIComponent(jobType)}?limit=40`);
    if (!resp.ok) throw new Error('Failed to load overview');
    wrap.innerHTML = renderOverviewGrid(await resp.json());
  } catch (err) {
    wrap.innerHTML = `<div class="empty-state">${escHtml(err.message)}</div>`;
  }
}

const OV_ICON = { done: '✓', success: '✓', running: '', failed: '✕', pending: '' };

function renderOverviewGrid(data) {
  const { job_type, task_names, runs } = data;
  const name = AUTO_CATALOG[job_type]?.name || job_type;
  if (!runs || !runs.length) {
    return `<div class="ov-grid-head"><h2>${escHtml(name)}</h2></div>
      <div class="empty-state">No runs for this automation yet.</div>`;
  }
  const durs = runs.map(r => r.duration_secs || 0);
  const maxDur = Math.max(...durs, 1);
  const bars = runs.map(r => {
    const d = r.duration_secs || 0;
    const h = Math.max(3, Math.round((d / maxDur) * 46));
    return `<div class="ov-bar ov-fill-${r.status}" style="height:${h}px" title="${escHtml(r.job_name)} · run #${r.run_id} · ${r.status} · ${fmtDur(d) || '—'}"></div>`;
  }).join('');

  const tip = r => `${escHtml(r.job_name)} · run #${r.run_id} · ${r.status} · ${new Date(r.started_at).toLocaleString()}`;
  const runHead = runs.map(r =>
    `<div class="ov-cell ov-fill-${r.status}" title="${tip(r)}">${OV_ICON[r.status] || ''}</div>`
  ).join('');

  const rows = task_names.map((tname, ri) => {
    const cells = runs.map(r => {
      const status = r.steps[ri] ? r.steps[ri].status : 'pending';
      return `<div class="ov-cell ov-fill-${status}" title="${escHtml(tname)} · ${escHtml(r.job_name)} · run #${r.run_id} · ${status}">${OV_ICON[status] || ''}</div>`;
    }).join('');
    return `<div class="ov-row"><div class="ov-row-label" title="${escHtml(tname)}">${escHtml(tname)}</div><div class="ov-cells">${cells}</div></div>`;
  }).join('');

  const nSuccess = runs.filter(r => r.status === 'success').length;
  const nFailed  = runs.filter(r => r.status === 'failed').length;

  return `
    <div class="ov-grid-head">
      <h2>${escHtml(name)} <span class="muted" style="font-weight:400;font-size:0.8rem">${escHtml(job_type)}</span></h2>
      <div class="ov-summary">
        <span class="ov-stat"><b>${runs.length}</b> runs</span>
        <span class="ov-stat ov-stat-ok"><b>${nSuccess}</b> ok</span>
        <span class="ov-stat ov-stat-fail"><b>${nFailed}</b> failed</span>
      </div>
    </div>
    <div class="ov-duration-chart" title="Run duration (oldest → newest)">${bars}</div>
    <div class="ov-grid-scroll"><div class="ov-grid">
      <div class="ov-row ov-row-head"><div class="ov-row-label">Task \\ Run →</div><div class="ov-cells">${runHead}</div></div>
      ${rows}
    </div></div>
    <div class="ov-legend">
      <span><i class="ov-cell ov-fill-success"></i> success</span>
      <span><i class="ov-cell ov-fill-failed"></i> failed</span>
      <span><i class="ov-cell ov-fill-running"></i> running</span>
      <span><i class="ov-cell ov-fill-pending"></i> pending</span>
    </div>`;
}

document.getElementById('ov-joblist').addEventListener('click', (e) => {
  const btn = e.target.closest('[data-job-type]');
  if (btn) selectOverviewType(btn.dataset.jobType);
});

// ══════════════════════════════════════ SCHEDULES ════════════════════════════════

async function loadSchedulesPage() {
  const tbody = document.getElementById('schedules-tbody');
  tbody.innerHTML = '<tr><td colspan="7" class="empty-state">Loading…</td></tr>';
  try {
    const resp = await fetch('/api/schedules');
    if (!resp.ok) throw new Error('Failed to load schedules');
    renderSchedules(await resp.json());
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty-state">${escHtml(err.message)}</td></tr>`;
  }
}

function renderSchedules(rows) {
  const tbody = document.getElementById('schedules-tbody');
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty-state">No scheduled jobs. Add a Schedule in the New Run dialog.</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map(r => {
    const meta = TYPE_META[r.job_type] || { chip: (r.job_type || '?').toUpperCase(), cls: 'chip-unknown' };
    const next = r.next_run_at ? new Date(r.next_run_at).toLocaleString() : '<span class="muted">—</span>';
    const cronStyle = r.valid ? '' : ' style="color:var(--red)"';
    const last = r.last_run
      ? `<span class="badge badge-${r.last_run.status}">${r.last_run.status}</span> <span class="muted">${new Date(r.last_run.started_at).toLocaleString()}</span>`
      : '<span class="muted">never</span>';
    return `<tr>
      <td><span class="job-name-text" title="${escHtml(r.name)}">${escHtml(r.name)}</span></td>
      <td><span class="type-chip ${meta.cls}">${escHtml(meta.chip)}</span></td>
      <td><code${cronStyle}>${escHtml(r.schedule)}</code>${r.valid ? '' : ' <span class="muted">(invalid)</span>'}</td>
      <td>${next}</td>
      <td>${r.run_count}</td>
      <td>${last}</td>
      <td class="th-actions">
        <button class="btn btn-ghost btn-sm" data-sched-run="${r.job_id}">Run now</button>
        <button class="btn btn-ghost btn-sm" data-sched-edit="${r.job_id}" data-cron="${escHtml(r.schedule)}">Edit</button>
        <button class="btn btn-ghost btn-sm" data-sched-unset="${r.job_id}" data-name="${escHtml(r.name)}">Unschedule</button>
      </td>
    </tr>`;
  }).join('');
}

async function editSchedule(jobId, currentCron) {
  const next = prompt('New cron expression (min hour day month weekday, UTC):', currentCron || '');
  if (next === null) return;  // cancelled
  const cron = next.trim();
  if (!cron) { showToast('Empty — use Unschedule to remove the schedule', 'error'); return; }
  try {
    const resp = await fetch(`/api/jobs/${jobId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ schedule: cron }),
    });
    if (resp.status === 400) {
      const e = await resp.json().catch(() => ({}));
      showToast(e.detail || 'Invalid cron expression', 'error');
      return;
    }
    if (!resp.ok) throw new Error('Update failed');
    showToast('Schedule updated', 'success');
    loadSchedulesPage();
  } catch (err) { showToast(err.message, 'error'); }
}

async function unscheduleJob(jobId, name) {
  const ok = await confirmDialog('Remove schedule', `Stop running "${name}" on a schedule? The job itself is kept.`);
  if (!ok) return;
  try {
    const resp = await fetch(`/api/jobs/${jobId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ schedule: null }),
    });
    if (!resp.ok) throw new Error('Update failed');
    showToast('Schedule removed', 'success');
    loadSchedulesPage();
  } catch (err) { showToast(err.message, 'error'); }
}

document.getElementById('schedules-refresh-btn').addEventListener('click', loadSchedulesPage);
document.getElementById('schedules-tbody').addEventListener('click', (e) => {
  const runBtn = e.target.closest('[data-sched-run]');
  if (runBtn) { navigate('activity'); rerun(parseInt(runBtn.dataset.schedRun, 10)); return; }
  const editBtn = e.target.closest('[data-sched-edit]');
  if (editBtn) { editSchedule(parseInt(editBtn.dataset.schedEdit, 10), editBtn.dataset.cron); return; }
  const unsetBtn = e.target.closest('[data-sched-unset]');
  if (unsetBtn) { unscheduleJob(parseInt(unsetBtn.dataset.schedUnset, 10), unsetBtn.dataset.name); return; }
});

// ── State ─────────────────────────────────────────────────────────────────────

let activeEventSource  = null;
let sseStartTime       = null;
let cachedRuns         = [];
const elapsedTimers    = new Map();
let toastTimer         = null;
let selectedRunIds     = new Set();
let systemData         = null;
let systemCategory     = 'agents';
let confirmResolve     = null;
let runsPage           = 0;    // 0-indexed current page
let runsTotal          = 0;    // total rows matching current filters
const RUNS_PAGE_SIZE   = 50;
const historyFilters   = { job_type: '', status: '', started_after: '', started_before: '' };
let activeJobType      = null;
let activeLogs         = [];

// ── DOM refs ──────────────────────────────────────────────────────────────────

const modal           = document.getElementById('modal');
const runForm         = document.getElementById('run-form');
const progressPanel   = document.getElementById('progress-panel');
const progressLog     = document.getElementById('progress-log');
const progressJobName = document.getElementById('progress-job-name');
const progressPulse   = document.getElementById('progress-pulse');
const confirmModal    = document.getElementById('confirm-modal');

// ── Page routing ──────────────────────────────────────────────────────────────

function navigate(page) {
  if (page === 'admin' && !(CURRENT_USER && CURRENT_USER.is_admin)) page = 'landing';
  document.querySelectorAll('.nav-tab').forEach(t => t.classList.toggle('active', t.dataset.page === page));
  document.querySelectorAll('.page').forEach(p => {
    p.classList.toggle('active', p.id === `page-${page}`);
  });
  history.replaceState(null, '', `#${page}`);

  if (page === 'landing')   renderLandingAutos();
  if (page === 'system')    loadSystemPage();
  if (page === 'run')       renderAutomationsPage();
  if (page === 'analytics') loadPerformancePage();
  if (page === 'overview')  loadOverviewPage();
  if (page === 'schedules') loadSchedulesPage();
  if (page === 'admin')     loadAdminPage();
  if (page === 'landing')   window.scrollTo({ top: 0 });
}

// Delegated navigation: nav tabs, logo, footer links — anything with [data-page]
document.addEventListener('click', (e) => {
  const el = e.target.closest('[data-page]');
  if (el) { e.preventDefault(); navigate(el.dataset.page); }
});

// ── Landing page ────────────────────────────────────────────────────────────
function renderLandingAutos() {
  const grid = document.getElementById('lp-autos');
  if (!grid || grid.dataset.rendered) return;
  const visible = visibleTypeSet();
  grid.innerHTML = ALL_TYPES.filter(t => visible.has(t)).map(t => {
    const a = AUTO_CATALOG[t]; if (!a) return '';
    const m = TYPE_META[t] || {};
    return `<button class="lp-auto" data-type="${t}">
      <div class="lp-auto-top">
        <span class="lp-auto-icon">${a.icon}</span>
        <span class="type-chip ${m.cls || ''}">${m.chip || ''}</span>
      </div>
      <h4>${a.name}</h4>
      <p>${a.desc}</p>
    </button>`;
  }).join('');
  grid.dataset.rendered = '1';
  grid.querySelectorAll('.lp-auto').forEach(card =>
    card.addEventListener('click', () => openModal(card.dataset.type)));
}

document.getElementById('lp-launch').addEventListener('click', () => navigate('run'));
document.getElementById('lp-run').addEventListener('click', () => openModal());
document.getElementById('lp-cta-run').addEventListener('click', () => openModal());
document.getElementById('lp-cta-dash').addEventListener('click', () => navigate('activity'));

// Navigate to the page in the hash on load, defaulting to the landing page
const VALID_PAGES = ['landing','run','activity','overview','schedules','system','analytics','admin'];

// Automations the current user may see/launch (admins see all; others get the
// intersection of globally-enabled and their personal allowlist).
function visibleTypeSet() {
  if (!CURRENT_USER) return new Set();
  // The global enabled set applies to everyone (disabled = blocked for all,
  // including admins). Admins bypass only the per-user allowlist.
  const enabled = new Set(CURRENT_USER.enabled_automations || ALL_TYPES);
  if (CURRENT_USER.is_admin) return new Set(ALL_TYPES.filter(t => enabled.has(t)));
  const allowed = CURRENT_USER.allowed_automations;
  const allowAll = allowed === '*';
  return new Set(ALL_TYPES.filter(t =>
    enabled.has(t) && (allowAll || (Array.isArray(allowed) && allowed.includes(t)))));
}

// ── Auth gate ───────────────────────────────────────────────────────────────
// The whole app sits behind a login. We check /api/auth/me on boot; a 401 (here
// or on any later request) drops a full-screen login overlay back over the app.
let CURRENT_USER = null;

const loginOverlay = document.getElementById('login-overlay');
const loginForm    = document.getElementById('login-form');
const loginError   = document.getElementById('login-error');
const userMenu     = document.getElementById('user-menu');
const userChip     = document.getElementById('user-chip');

function showLogin() {
  CURRENT_USER = null;
  if (userMenu) userMenu.hidden = true;
  loginOverlay.classList.add('show');
  const u = document.getElementById('login-username');
  if (u) u.focus();
}

function onAuthenticated(user) {
  CURRENT_USER = user;
  loginOverlay.classList.remove('show');
  if (userMenu) userMenu.hidden = false;
  if (userChip) userChip.textContent = user.username + (user.is_admin ? ' · admin' : '');
  const navAdmin = document.getElementById('nav-admin');
  if (navAdmin) navAdmin.hidden = !user.is_admin;
  loginError.hidden = true;
  if (loginForm) loginForm.reset();
  loadHistory();  // populate the activity panel now that we're authenticated
}

function bootApp() {
  const initialPage = (location.hash.slice(1) || 'landing');
  navigate(VALID_PAGES.includes(initialPage) ? initialPage : 'landing');
}

// Any 401 from an /api/ call (e.g. session expiry) re-shows the login overlay.
// The login request itself is exempt so a bad password shows an inline error.
const _origFetch = window.fetch.bind(window);
window.fetch = async (input, init) => {
  const resp = await _origFetch(input, init);
  const url = typeof input === 'string' ? input : (input && (input.url || input.href)) || '';
  if (resp.status === 401 && url.includes('/api/') && !url.includes('/api/auth/login')) {
    showLogin();
  }
  return resp;
};

if (loginForm) {
  loginForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    loginError.hidden = true;
    const submitBtn = document.getElementById('login-submit');
    submitBtn.disabled = true;
    try {
      const resp = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: document.getElementById('login-username').value,
          password: document.getElementById('login-password').value,
        }),
      });
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        loginError.textContent = body.detail || 'Sign in failed';
        loginError.hidden = false;
        return;
      }
      onAuthenticated(await resp.json());
      bootApp();
    } finally {
      submitBtn.disabled = false;
    }
  });
}

const logoutBtn = document.getElementById('logout-btn');
if (logoutBtn) {
  logoutBtn.addEventListener('click', async () => {
    await fetch('/api/auth/logout', { method: 'POST' });
    showLogin();
  });
}

// ── Admin page ────────────────────────────────────────────────────────────
// All endpoints are admin-only server-side; the UI just renders/edits.
let ADMIN_TAB = 'users';

function loadAdminPage() {
  const tabs = document.getElementById('admin-tabs');
  if (tabs && !tabs.dataset.wired) {
    tabs.dataset.wired = '1';
    tabs.addEventListener('click', (e) => {
      const b = e.target.closest('.cat-tab');
      if (b) adminSwitch(b.dataset.adm);
    });
  }
  adminSwitch(ADMIN_TAB);
}

function adminSwitch(tab) {
  ADMIN_TAB = tab;
  document.querySelectorAll('#admin-tabs .cat-tab')
    .forEach(b => b.classList.toggle('active', b.dataset.adm === tab));
  document.getElementById('admin-users').hidden = tab !== 'users';
  document.getElementById('admin-keys').hidden  = tab !== 'keys';
  document.getElementById('admin-judge').hidden = tab !== 'judge';
  document.getElementById('admin-autos').hidden = tab !== 'autos';
  if (tab === 'users') renderAdminUsers();
  if (tab === 'keys')  renderAdminKeys();
  if (tab === 'judge') renderAdminJudge();
  if (tab === 'autos') renderAdminAutos();
}

async function renderAdminUsers() {
  const el = document.getElementById('admin-users');
  el.innerHTML = '<div class="loading-state">Loading…</div>';
  const resp = await fetch('/api/admin/users');
  if (!resp.ok) { el.innerHTML = '<div class="loading-state">Failed to load users.</div>'; return; }
  const users = await resp.json();
  const rows = users.map(u => {
    const allow = u.allowed_automations === '*'
      ? '<span class="badge badge-admin">all</span>'
      : (u.allowed_automations.length
          ? u.allowed_automations.map(t => `<span class="badge badge-off">${escHtml(t)}</span>`).join(' ')
          : '<span class="muted">none</span>');
    return `<tr>
      <td>${escHtml(u.username)}</td>
      <td>${u.is_admin ? '<span class="badge badge-admin">admin</span>' : '<span class="muted">user</span>'}</td>
      <td>${u.is_active ? '<span class="badge badge-on">active</span>' : '<span class="badge badge-off">disabled</span>'}</td>
      <td>${u.is_admin ? '<span class="muted">—</span>' : allow}</td>
      <td><div class="admin-actions">
        <button class="btn-sm" data-act="edit" data-id="${u.id}">Edit</button>
        <button class="btn-sm" data-act="toggle" data-id="${u.id}">${u.is_active ? 'Disable' : 'Enable'}</button>
        <button class="btn-sm" data-act="pw" data-id="${u.id}">Reset PW</button>
        <button class="btn-sm danger" data-act="del" data-id="${u.id}">Delete</button>
      </div></td></tr>`;
  }).join('');
  el.innerHTML = `
    <div class="admin-bar">
      <div class="muted">${users.length} user(s)</div>
      <button class="btn btn-primary btn-sm" id="admin-add-user">+ New User</button>
    </div>
    <div class="admin-card" style="padding:0;overflow-x:auto">
      <table class="admin-table">
        <thead><tr><th>Username</th><th>Role</th><th>Status</th><th>Allowed automations</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
  document.getElementById('admin-add-user').onclick = () => openUserModal(null);
  el.querySelectorAll('[data-act]').forEach(b =>
    b.onclick = () => adminUserAction(b.dataset.act, +b.dataset.id, users));
}

async function adminUserAction(act, id, users) {
  const u = users.find(x => x.id === id);
  if (act === 'edit') return openUserModal(u);
  if (act === 'toggle') {
    const r = await fetch(`/api/admin/users/${id}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ is_active: !u.is_active }),
    });
    if (!r.ok) alert((await r.json().catch(() => ({}))).detail || 'Failed');
    return renderAdminUsers();
  }
  if (act === 'pw') {
    const pw = prompt(`New password for "${u.username}" (min 8 characters):`);
    if (!pw) return;
    const r = await fetch(`/api/admin/users/${id}/password`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ new_password: pw }),
    });
    alert(r.ok ? 'Password updated.' : ((await r.json().catch(() => ({}))).detail || 'Failed'));
    return;
  }
  if (act === 'del') {
    if (!confirm(`Delete user "${u.username}"? This cannot be undone.`)) return;
    const r = await fetch(`/api/admin/users/${id}`, { method: 'DELETE' });
    if (!r.ok) alert((await r.json().catch(() => ({}))).detail || 'Failed');
    return renderAdminUsers();
  }
}

function openUserModal(user) {
  const m = document.getElementById('admin-user-modal');
  const isEdit = !!user;
  document.getElementById('auser-title').textContent = isEdit ? `Edit ${user.username}` : 'New User';
  document.getElementById('auser-id').value = isEdit ? user.id : '';
  const un = document.getElementById('auser-username');
  un.value = isEdit ? user.username : '';
  un.disabled = isEdit;
  // Password is set at creation; for existing users use the "Reset PW" action.
  document.getElementById('auser-password-field').hidden = isEdit;
  document.getElementById('auser-password').value = '';
  document.getElementById('auser-admin').checked = isEdit ? user.is_admin : false;

  const grid = document.getElementById('auser-allow-grid');
  grid.innerHTML = ALL_TYPES.map(t =>
    `<label><input type="checkbox" class="auser-allow" value="${t}"> ${escHtml(AUTO_CATALOG[t]?.name || t)}</label>`
  ).join('');
  const wild = isEdit && user.allowed_automations === '*';
  document.getElementById('auser-allow-all').checked = wild;
  const allowedList = (isEdit && Array.isArray(user.allowed_automations)) ? user.allowed_automations : [];
  grid.querySelectorAll('.auser-allow').forEach(cb => cb.checked = allowedList.includes(cb.value));

  syncAllowDisabled();
  document.getElementById('auser-error').hidden = true;
  m.classList.remove('hidden');
}

function syncAllowDisabled() {
  const isAdmin  = document.getElementById('auser-admin').checked;
  const allowAll = document.getElementById('auser-allow-all').checked;
  document.getElementById('auser-allow-field').style.opacity = isAdmin ? '0.5' : '1';
  document.getElementById('auser-allow-all').disabled = isAdmin;
  document.querySelectorAll('.auser-allow').forEach(cb => cb.disabled = isAdmin || allowAll);
}

async function saveUser(e) {
  e.preventDefault();
  const errEl = document.getElementById('auser-error');
  errEl.hidden = true;
  const id = document.getElementById('auser-id').value;
  const isAdmin  = document.getElementById('auser-admin').checked;
  const allowAll = document.getElementById('auser-allow-all').checked;
  let allowed;
  if (isAdmin || allowAll) allowed = ['*'];
  else allowed = [...document.querySelectorAll('.auser-allow:checked')].map(c => c.value);

  let resp;
  if (id) {
    resp = await fetch(`/api/admin/users/${id}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ is_admin: isAdmin, allowed_automations: allowed }),
    });
  } else {
    resp = await fetch('/api/admin/users', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        username: document.getElementById('auser-username').value.trim(),
        password: document.getElementById('auser-password').value,
        is_admin: isAdmin,
        allowed_automations: allowed,
      }),
    });
  }
  if (!resp.ok) {
    errEl.textContent = (await resp.json().catch(() => ({}))).detail || 'Save failed';
    errEl.hidden = false;
    return;
  }
  document.getElementById('admin-user-modal').classList.add('hidden');
  renderAdminUsers();
}

async function renderAdminKeys() {
  const el = document.getElementById('admin-keys');
  el.innerHTML = '<div class="loading-state">Loading…</div>';
  const resp = await fetch('/api/admin/llm-keys');
  if (!resp.ok) { el.innerHTML = '<div class="loading-state">Failed to load keys.</div>'; return; }
  const keys = await resp.json();
  el.innerHTML = keys.map(k => `
    <div class="admin-card">
      <div class="admin-card-row">
        <div>
          <strong>${escHtml(k.provider)}</strong>
          ${k.configured
            ? `<span class="badge badge-on">configured</span> <span class="badge badge-src">${escHtml(k.source)}</span> <span class="muted">${escHtml(k.masked || '')}</span>`
            : '<span class="badge badge-off">not set</span>'}
          <div class="muted" style="font-size:0.75rem;margin-top:0.25rem">env var: ${escHtml(k.env)} · models: ${k.models.map(escHtml).join(', ')}</div>
        </div>
      </div>
      <div class="key-input" style="margin-top:0.7rem">
        <input type="password" placeholder="Paste new API key to override…" data-key-input="${k.provider}" />
        <button class="btn-sm" data-key-save="${k.provider}">Save</button>
        ${k.source === 'db' ? `<button class="btn-sm danger" data-key-clear="${k.provider}">Clear</button>` : ''}
      </div>
    </div>`).join('');
  el.querySelectorAll('[data-key-save]').forEach(b => b.onclick = async () => {
    const p = b.dataset.keySave;
    const val = el.querySelector(`[data-key-input="${p}"]`).value;
    const r = await fetch(`/api/admin/llm-keys/${p}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ api_key: val }),
    });
    if (!r.ok) alert((await r.json().catch(() => ({}))).detail || 'Failed');
    renderAdminKeys();
  });
  el.querySelectorAll('[data-key-clear]').forEach(b => b.onclick = async () => {
    const p = b.dataset.keyClear;
    if (!confirm(`Clear the stored ${p} key and revert to the environment variable?`)) return;
    await fetch(`/api/admin/llm-keys/${p}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ api_key: '' }),
    });
    renderAdminKeys();
  });
}

async function renderAdminAutos() {
  const el = document.getElementById('admin-autos');
  el.innerHTML = '<div class="loading-state">Loading…</div>';
  const resp = await fetch('/api/admin/automations');
  if (!resp.ok) { el.innerHTML = '<div class="loading-state">Failed to load.</div>'; return; }
  const data = await resp.json();
  const enabled = new Set(data.enabled);
  el.innerHTML = `<div class="admin-card">
    <div class="muted" style="margin-bottom:0.6rem">Disabled automations cannot be created or run by anyone (including admins). Re-enable to use them.</div>
    ${data.all.map(t => `
      <div class="toggle-row">
        <div><strong>${escHtml(AUTO_CATALOG[t]?.name || t)}</strong> <span class="muted" style="font-size:0.78rem">${escHtml(t)}</span></div>
        <label class="switch"><input type="checkbox" data-auto="${t}" ${enabled.has(t) ? 'checked' : ''}><span class="track"></span></label>
      </div>`).join('')}
  </div>`;
  el.querySelectorAll('[data-auto]').forEach(cb => cb.onchange = async () => {
    const next = [...el.querySelectorAll('[data-auto]:checked')].map(c => c.dataset.auto);
    const r = await fetch('/api/admin/automations', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: next }),
    });
    if (!r.ok) { alert('Failed to update'); return renderAdminAutos(); }
    // Keep the run/landing filters in sync without a reload.
    if (CURRENT_USER) CURRENT_USER.enabled_automations = (await r.json()).enabled;
    const lp = document.getElementById('lp-autos');
    if (lp) delete lp.dataset.rendered;  // force the landing grid to re-filter
  });
}

async function renderAdminJudge() {
  const el = document.getElementById('admin-judge');
  el.innerHTML = '<div class="loading-state">Loading…</div>';
  const resp = await fetch('/api/admin/eval-judge');
  if (!resp.ok) { el.innerHTML = '<div class="loading-state">Failed to load.</div>'; return; }
  const d = await resp.json();
  const providers = d.providers || {};
  const curP = d.provider || '';
  const provOpts = [`<option value="">Auto — default (${escHtml(d.default.provider)} / ${escHtml(d.default.model)})</option>`]
    .concat(Object.keys(providers).map(p =>
      `<option value="${escHtml(p)}" ${p === curP ? 'selected' : ''}>${escHtml(p)}</option>`)).join('');

  el.innerHTML = `<div class="admin-card">
    <div class="muted" style="margin-bottom:0.6rem">
      The <strong>Evaluate</strong> node scores every result with an LLM-as-judge (0–100 quality).
      The judge always runs on a model <em>independent</em> of the one that ran the job.
      Leave on <strong>Auto</strong> to use the default, with automatic fallback to any provider that has an API key.
    </div>
    <div class="key-input">
      <select id="judge-provider">${provOpts}</select>
      <select id="judge-model"></select>
      <button class="btn-sm" id="judge-save">Save</button>
    </div>
    <div class="muted" style="font-size:0.75rem;margin-top:0.5rem">Set the provider's API key under the <strong>LLM Keys</strong> tab first.</div>
  </div>`;

  const provSel = el.querySelector('#judge-provider');
  const modelSel = el.querySelector('#judge-model');
  function fillModels() {
    const p = provSel.value;
    if (!p) { modelSel.innerHTML = '<option value="">—</option>'; modelSel.disabled = true; return; }
    modelSel.disabled = false;
    const ms = providers[p] || [];
    modelSel.innerHTML = `<option value="">${escHtml(p)} default</option>` +
      ms.map(m => `<option value="${escHtml(m)}" ${m === d.model ? 'selected' : ''}>${escHtml(m)}</option>`).join('');
  }
  fillModels();
  provSel.onchange = fillModels;
  el.querySelector('#judge-save').onclick = async () => {
    const r = await fetch('/api/admin/eval-judge', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider: provSel.value || null, model: modelSel.value || null }),
    });
    if (!r.ok) { alert((await r.json().catch(() => ({}))).detail || 'Failed'); return; }
    renderAdminJudge();
  };
}

// Wire the user create/edit modal once (its markup is static).
(function wireAdminUserModal() {
  const m = document.getElementById('admin-user-modal');
  if (!m) return;
  const close = () => m.classList.add('hidden');
  document.getElementById('auser-close').onclick = close;
  document.getElementById('auser-cancel').onclick = close;
  m.addEventListener('click', (e) => { if (e.target === m) close(); });
  document.getElementById('auser-form').addEventListener('submit', saveUser);
  document.getElementById('auser-admin').addEventListener('change', syncAllowDisabled);
  document.getElementById('auser-allow-all').addEventListener('change', syncAllowDisabled);
})();

// Boot: only enter the app if there's a live session.
(async () => {
  try {
    const resp = await _origFetch('/api/auth/me');
    if (resp.ok) {
      onAuthenticated(await resp.json());
      bootApp();
    } else {
      showLogin();
    }
  } catch {
    showLogin();
  }
})();

// ── Modal open/close ──────────────────────────────────────────────────────────

function openModal(preselect) {
  // Hide automation types this user may not run (server enforces too).
  const visible = visibleTypeSet();
  let firstVisible = null;
  document.querySelectorAll('#type-grid .type-card').forEach(card => {
    const ok = visible.has(card.dataset.type);
    card.classList.toggle('hidden', !ok);
    if (ok && !firstVisible) firstVisible = card.dataset.type;
  });
  if (preselect && visible.has(preselect)) selectJobType(preselect);
  else if (firstVisible) selectJobType(firstVisible);
  modal.classList.remove('hidden');
}
function closeModalFn() { modal.classList.add('hidden'); }

document.getElementById('new-run-btn').addEventListener('click', () => openModal());
document.getElementById('run-new-btn').addEventListener('click', () => openModal());
document.getElementById('modal-close').addEventListener('click', closeModalFn);
document.getElementById('cancel-btn').addEventListener('click', closeModalFn);
document.getElementById('llm-provider').addEventListener('change', updateModelOptions);
document.getElementById('job-schedule-preset').addEventListener('change', (e) => {
  if (e.target.value) document.getElementById('job-schedule').value = e.target.value;
  e.target.value = '';  // reset the picker; the text field is the source of truth
});
document.getElementById('ph-files').addEventListener('change', renderPhClassified);
modal.addEventListener('click', (e) => { if (e.target === modal) closeModalFn(); });

// ── Job type card selection ───────────────────────────────────────────────────

document.getElementById('type-grid').addEventListener('click', (e) => {
  const card = e.target.closest('.type-card');
  if (card) selectJobType(card.dataset.type);
});

function selectJobType(type) {
  if (type !== 'pipeline') {
    document.getElementById('pipeline-steps-list').innerHTML = '';
    document.querySelector('#modal .modal').classList.remove('modal-wide');
  }
  document.querySelectorAll('.type-card')
    .forEach(c => c.classList.toggle('active', c.dataset.type === type));
  document.getElementById('job-type').value = type;
  ALL_TYPES.forEach(t =>
    document.getElementById(`fields-${t}`).classList.toggle('hidden', t !== type));
  if (type === 'pipeline') {
    document.querySelector('#modal .modal').classList.add('modal-wide');
    const list = document.getElementById('pipeline-steps-list');
    if (!list.querySelector('.pipeline-step-card')) {
      addPipelineStep('x_scraper');
      addPipelineStep('email_sender');
    }
  }
  if (type === 'tw104_apply') loadTw104CoverLetters();
}

// ── 104 saved cover-letter (自我推薦信) patterns ──────────────────────────────
let _tw104Covers = [];

async function loadTw104CoverLetters(selectName) {
  const sel = document.getElementById('tw104-cover-saved');
  if (!sel) return;
  try {
    const resp = await fetch('/api/cover-letters');
    _tw104Covers = resp.ok ? await resp.json() : [];
  } catch { _tw104Covers = []; }
  sel.innerHTML = '<option value="">— 載入已存推薦信範本 —</option>' +
    _tw104Covers.map(c => `<option value="${encodeURIComponent(c.name)}">${c.name}</option>`).join('');
  if (selectName) sel.value = encodeURIComponent(selectName);
}

function _tw104CoverCount() {
  const ta = document.getElementById('tw104-cover');
  const el = document.getElementById('tw104-cover-count');
  if (ta && el) el.textContent = String(ta.value.length);
}

function wireTw104CoverLetters() {
  const ta = document.getElementById('tw104-cover');
  const sel = document.getElementById('tw104-cover-saved');
  const saveBtn = document.getElementById('tw104-cover-save');
  const delBtn = document.getElementById('tw104-cover-delete');
  if (!ta || !sel || !saveBtn || !delBtn) return;

  ta.addEventListener('input', _tw104CoverCount);

  sel.addEventListener('change', () => {
    const name = decodeURIComponent(sel.value || '');
    const found = _tw104Covers.find(c => c.name === name);
    if (found) { ta.value = found.text || ''; _tw104CoverCount(); }
  });

  saveBtn.addEventListener('click', async () => {
    const current = sel.value ? decodeURIComponent(sel.value) : '';
    const name = (prompt('儲存為推薦信範本，請輸入名稱：', current) || '').trim();
    if (!name) return;
    const resp = await fetch('/api/cover-letters', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, text: ta.value }),
    });
    if (resp.ok) { await loadTw104CoverLetters(name); showToast(`已儲存範本「${name}」`, 'success'); }
    else { const b = await resp.json().catch(() => ({})); showToast(b.detail || '儲存失敗', 'error'); }
  });

  delBtn.addEventListener('click', async () => {
    const name = sel.value ? decodeURIComponent(sel.value) : '';
    if (!name) { showToast('請先從下拉選單選擇要刪除的範本', 'error'); return; }
    if (!confirm(`刪除推薦信範本「${name}」？`)) return;
    const resp = await fetch(`/api/cover-letters/${encodeURIComponent(name)}`, { method: 'DELETE' });
    if (resp.ok) { await loadTw104CoverLetters(); showToast(`已刪除範本「${name}」`, 'success'); }
    else showToast('刪除失敗', 'error');
  });
}
wireTw104CoverLetters();

// ── Profit health check: filename → role classification (mirrors uploads.py) ──

const _PH_ROLE_KEYWORDS = [
  ['returns', ['return', 'refund', '退貨', '退款']],
  ['cost',    ['cost', '成本']],
  ['ads',     ['ads', 'advert', '廣告', 'discount', '折扣']],
  ['sales',   ['sales', 'sale', 'order', '銷售', '訂單']],
];
const _PH_ROLE_LABEL = { sales: '銷售', cost: '成本', ads: '廣告', returns: '退貨' };

// Mirrors _classify in src/routers/uploads.py: match keywords against whole tokens
// (not raw substrings) so a short keyword can't hide inside a word (e.g. "ad" in
// "download"). CJK keywords have no token separators, so fall back to substring.
function classifyCsv(filename) {
  const name = (filename || '').toLowerCase();
  const tokens = name.match(/[a-z0-9]+/g) || [];
  for (const [role, kws] of _PH_ROLE_KEYWORDS) {
    for (const k of kws) {
      const ascii = /^[\x00-\x7f]+$/.test(k);
      if (ascii) {
        if (tokens.some(t => t === k || t.startsWith(k))) return role;
      } else if (name.includes(k)) {
        return role;
      }
    }
  }
  return null;
}

function renderPhClassified() {
  const box = document.getElementById('ph-classified');
  if (!box) return;
  const picked = Array.from(document.getElementById('ph-files').files || []);
  if (!picked.length) { box.innerHTML = ''; return; }
  const rows = picked.map(f => {
    const role = classifyCsv(f.name);
    const tag = role
      ? `<span style="color:var(--green)">→ ${_PH_ROLE_LABEL[role]}</span>`
      : `<span style="color:var(--red)">→ 無法辨識</span>`;
    return `<div style="display:flex;justify-content:space-between;gap:1rem"><span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escHtml(f.name)}</span>${tag}</div>`;
  });
  box.innerHTML = `<div style="border:1px solid var(--border);border-radius:6px;padding:0.5rem 0.7rem">${rows.join('')}</div>`;
}

// ── Pipeline builder ──────────────────────────────────────────────────────────

const PIPELINE_TYPE_OPTIONS = [
  { value: 'google_form_fill',    label: 'Form Fill' },
  { value: 'web_scraper',         label: 'Web Scraper' },
  { value: 'hacker_news_digest',  label: 'HN Digest' },
  { value: 'x_scraper',          label: 'X Scraper' },
  { value: 'email_sender',       label: 'Email Sender' },
  { value: 'google_sheet_reader', label: 'Sheet Reader' },
  { value: 'shopee_seller_scraper', label: 'Shopee Sellers' },
  { value: 'email_collect',        label: 'Email Collector' },
];

function renderPipelineStepFields(stepIdx, jobType) {
  const tipHtml = stepIdx > 0
    ? `<div style="font-size:0.67rem;color:var(--accent);margin-bottom:0.45rem">
         Prev output: <code style="font-family:ui-monospace,monospace">{{steps.${stepIdx - 1}.result}}</code>
         &nbsp;·&nbsp; field: <code style="font-family:ui-monospace,monospace">{{steps.${stepIdx - 1}.result.summary}}</code>
       </div>`
    : '';
  switch (jobType) {
    case 'google_form_fill':
      return `${tipHtml}
        <div class="field"><label>Company Name</label><input type="text" class="ps-field" data-field="company_name" placeholder="e.g. Acme Corp" /></div>
        <div class="field"><label>Company Size</label>
          <select class="ps-field" data-field="company_size">
            <option value="0-10">0 – 10</option><option value="11-100">11 – 100</option>
            <option value="200 up">200+</option><option value="其他">其他 (Other)</option>
          </select>
        </div>
        <div class="field"><label>AI Problem</label><textarea class="ps-field" data-field="ai_problem" rows="2" placeholder="Describe what you want AI to solve…"></textarea></div>`;
    case 'web_scraper':
      return `${tipHtml}
        <div class="field"><label>URL</label><input type="text" class="ps-field" data-field="url" placeholder="https://example.com" /></div>`;
    case 'hacker_news_digest':
      return `${tipHtml}
        <div class="field"><label>Stories (1–10)</label><input type="text" class="ps-field" data-field="limit" value="5" placeholder="5" /></div>`;
    case 'x_scraper':
      return `${tipHtml}
        <div class="field"><label>X Username</label><input type="text" class="ps-field" data-field="username" placeholder="username (no @)" /></div>
        <div class="field"><label>Post Limit</label><input type="text" class="ps-field" data-field="limit" value="5" placeholder="5" /></div>`;
    case 'email_sender':
      return `${tipHtml}
        <div class="field"><label>To</label><textarea class="ps-field" data-field="to" rows="2" placeholder="recipient@example.com"></textarea></div>
        <div class="field"><label>CC (optional)</label><input type="text" class="ps-field" data-field="cc" placeholder="cc@example.com" /></div>
        <div class="field"><label>Subject</label><input type="text" class="ps-field" data-field="subject" placeholder="Subject line" /></div>
        <div class="field"><label>Body</label><textarea class="ps-field" data-field="body" rows="3" placeholder="Email body or {{steps.${Math.max(0, stepIdx - 1)}.result.summary}}"></textarea></div>`;
    case 'google_sheet_reader':
      return `${tipHtml}
        <div class="field"><label>Sheet URL</label><input type="text" class="ps-field" data-field="url" placeholder="https://docs.google.com/spreadsheets/d/…" /></div>
        <div class="field"><label>Max Rows</label><input type="text" class="ps-field" data-field="limit" value="200" placeholder="200" /></div>`;
    case 'shopee_seller_scraper':
      return `${tipHtml}
        <div class="field"><label>Search Keyword</label><input type="text" class="ps-field" data-field="keyword" placeholder="e.g. 無線耳機" /></div>
        <div class="field"><label>Products (1–100)</label><input type="text" class="ps-field" data-field="limit" value="5" placeholder="5" /></div>`;
    case 'email_collect':
      return `${tipHtml}
        <div class="field"><label>Business Query</label><input type="text" class="ps-field" data-field="query" placeholder="e.g. marketing agency" /></div>
        <div class="field"><label>Region</label><input type="text" class="ps-field" data-field="region" placeholder="e.g. Taipei" /></div>
        <div class="field"><label>Limit (1–40)</label><input type="text" class="ps-field" data-field="limit" value="15" placeholder="15" /></div>`;
    default: return '';
  }
}

function addPipelineStep(jobType = 'x_scraper') {
  const list = document.getElementById('pipeline-steps-list');
  const idx  = list.querySelectorAll('.pipeline-step-card').length;

  // Arrow connector between steps
  if (idx > 0) {
    const arrow = document.createElement('div');
    arrow.className = 'pipeline-arrow';
    arrow.setAttribute('data-arrow', '');
    arrow.textContent = '↓';
    list.appendChild(arrow);
  }

  const card = document.createElement('div');
  card.className = 'pipeline-step-card';
  card.dataset.stepType = jobType;

  const typeOpts = PIPELINE_TYPE_OPTIONS
    .map(o => `<option value="${o.value}"${o.value === jobType ? ' selected' : ''}>${escHtml(o.label)}</option>`)
    .join('');

  card.innerHTML = `
    <div class="pipeline-step-header">
      <span class="pipeline-step-num">Step ${idx + 1}</span>
      <select class="pipeline-step-type-sel">${typeOpts}</select>
      <button type="button" class="pipeline-remove-step" title="Remove step">&times;</button>
    </div>
    <div class="pipeline-step-fields">${renderPipelineStepFields(idx, jobType)}</div>
    <div class="pipeline-step-hint">Output: <code>{{steps.${idx}.result}}</code></div>`;

  card.querySelector('.pipeline-step-type-sel').addEventListener('change', (e) => {
    card.dataset.stepType = e.target.value;
    const curIdx = [...list.querySelectorAll('.pipeline-step-card')].indexOf(card);
    card.querySelector('.pipeline-step-fields').innerHTML = renderPipelineStepFields(curIdx, e.target.value);
  });

  card.querySelector('.pipeline-remove-step').addEventListener('click', () => {
    // Remove preceding arrow if any
    const prev = card.previousElementSibling;
    if (prev?.getAttribute('data-arrow') !== null) prev.remove();
    card.remove();
    renumberPipelineSteps();
  });

  list.appendChild(card);
}

function renumberPipelineSteps() {
  const cards = document.querySelectorAll('.pipeline-step-card');
  cards.forEach((card, i) => {
    card.querySelector('.pipeline-step-num').textContent = `Step ${i + 1}`;
    const hint = card.querySelector('.pipeline-step-hint code');
    if (hint) hint.textContent = `{{steps.${i}.result}}`;
  });
}

function collectPipelineSteps() {
  return [...document.querySelectorAll('.pipeline-step-card')].map(card => {
    const jobType = card.dataset.stepType;
    const payload = {};
    card.querySelectorAll('.ps-field').forEach(el => {
      const field = el.dataset.field;
      if (!field) return;
      const raw = el.tagName === 'SELECT' ? el.value : el.value.trim();
      if (!raw) return;
      const n = Number(raw);
      payload[field] = (el.type !== 'number' && isNaN(n)) ? raw : (!isNaN(n) && raw === String(n) ? n : raw);
    });
    return { job_type: jobType, payload };
  });
}

document.getElementById('pipeline-add-step').addEventListener('click', () => addPipelineStep('email_sender'));

// ── Form submit ───────────────────────────────────────────────────────────────

runForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const jobType = document.getElementById('job-type').value;
  let payload, jobName;

  if (jobType === 'google_form_fill') {
    const company = document.getElementById('company-name').value.trim();
    if (!company) { showToast('Company name is required', 'error'); return; }
    payload = {
      company_name: company,
      company_size: document.getElementById('company-size').value,
      ai_problem:   document.getElementById('ai-problem').value.trim(),
    };
    jobName = `Form: ${company}`;
  } else if (jobType === 'web_scraper') {
    const url = document.getElementById('scrape-url').value.trim();
    if (!url) { showToast('URL is required', 'error'); return; }
    payload = { url };
    jobName = `Scrape: ${new URL(url).hostname}`;
  } else if (jobType === 'hacker_news_digest') {
    const limit = parseInt(document.getElementById('hn-limit').value, 10) || 5;
    payload = { limit };
    jobName = `HN Digest (top ${limit})`;
  } else if (jobType === 'x_scraper') {
    const username = document.getElementById('x-username').value.trim().replace(/^@/, '');
    if (!username) { showToast('X username is required', 'error'); return; }
    payload = { username, limit: parseInt(document.getElementById('x-limit').value, 10) || 5 };
    jobName = `X: @${username}`;

  } else if (jobType === 'email_sender') {
    const to = document.getElementById('email-to').value.trim();
    const subject = document.getElementById('email-subject').value.trim();
    const body = document.getElementById('email-body').value.trim();
    if (!to)      { showToast('Recipients are required', 'error'); return; }
    if (!subject) { showToast('Subject is required', 'error'); return; }
    if (!body)    { showToast('Body is required', 'error'); return; }
    const cc = document.getElementById('email-cc').value.trim();
    payload = { to, subject, body, ...(cc ? { cc } : {}) };
    const recipientCount = to.split(',').filter(e => e.trim()).length;
    jobName = `Email: ${subject} → ${recipientCount} recipient${recipientCount !== 1 ? 's' : ''}`;

  } else if (jobType === 'google_sheet_reader') {
    const url = document.getElementById('sheet-url').value.trim();
    if (!url) { showToast('Sheet URL is required', 'error'); return; }
    const limit = parseInt(document.getElementById('sheet-limit').value, 10) || 200;
    payload = { url, limit };
    try {
      const urlObj = new URL(url);
      const parts = urlObj.pathname.split('/');
      const idIdx = parts.indexOf('d');
      const sheetId = idIdx >= 0 ? parts[idIdx + 1] : 'sheet';
      jobName = `Sheet: ${sheetId.slice(0, 12)}…`;
    } catch (_) {
      jobName = 'Google Sheet';
    }

  } else if (jobType === 'shopee_seller_scraper') {
    const keyword = document.getElementById('shopee-keyword').value.trim();
    if (!keyword) { showToast('Search keyword is required', 'error'); return; }
    const limit = parseInt(document.getElementById('shopee-limit').value, 10) || 5;
    payload = { keyword, limit };
    jobName = `Shopee: ${keyword}`;

  } else if (jobType === 'profit_health_check') {
    const picked = Array.from(document.getElementById('ph-files').files || []);
    if (!picked.length) { showToast('請選取蝦皮 CSV 檔（可一次全選）', 'error'); return; }
    const MAX = 2 * 1024 * 1024;
    for (const f of picked) {
      if (f.size > MAX) { showToast(`${f.name} 超過 2 MB 上限`, 'error'); return; }
    }
    // Client-side preview only — the server is the source of truth for routing.
    const roles = picked.reduce((acc, f) => { const r = classifyCsv(f.name); if (r) acc[r] = f.name; return acc; }, {});
    if (!roles.sales || !roles.cost) {
      showToast('檔名需可辨識出銷售 (sales) 與成本 (cost)；請確認檔名含對應關鍵字', 'error');
      return;
    }
    const fd = new FormData();
    picked.forEach(f => fd.append('files', f));
    let uploadId;
    try {
      const up = await fetch('/api/uploads', { method: 'POST', body: fd });
      if (!up.ok) {
        const e = await up.json().catch(() => ({}));
        showToast(`上傳失敗：${e.detail || up.status}`, 'error');
        return;
      }
      uploadId = (await up.json()).upload_id;
    } catch (err) {
      showToast(`上傳失敗：${err}`, 'error');
      return;
    }
    payload = { upload_id: uploadId };
    jobName = `利潤健檢：${roles.sales}`;

  } else if (jobType === 'tasker_apply') {
    const categories = document.getElementById('tasker-categories').value.trim();
    if (!categories) { showToast('分類 ID 為必填 (e.g. 110)', 'error'); return; }
    const minCharge = parseInt(document.getElementById('tasker-min').value, 10);
    const maxCharge = parseInt(document.getElementById('tasker-max').value, 10);
    if (isNaN(minCharge) || isNaN(maxCharge)) { showToast('請填寫初次估價最低與最高金額', 'error'); return; }
    if (minCharge < 1000) { showToast('最低金額不可小於 1000 元', 'error'); return; }
    if (minCharge > maxCharge) { showToast('最低金額不可大於最高金額', 'error'); return; }
    const template = document.getElementById('tasker-template').value.trim();
    const taskFilter = document.getElementById('tasker-filter').value.trim();
    payload = {
      category_ids: categories,
      min_charge: minCharge,
      max_charge: maxCharge,
      max_cases: parseInt(document.getElementById('tasker-max-cases').value, 10) || 5,
      dry_run: document.getElementById('tasker-dry-run').checked,
      ...(template ? { proposal_template: template } : {}),
      ...(taskFilter ? { task_filter: taskFilter } : {}),
    };
    jobName = `Tasker 提案: ${categories}${payload.dry_run ? ' (dry-run)' : ''}`;

  } else if (jobType === 'tw104_apply') {
    const keyword = document.getElementById('tw104-keyword').value.trim();
    if (!keyword) { showToast('職缺關鍵字為必填 (e.g. 軟體工程師)', 'error'); return; }
    const area = document.getElementById('tw104-area').value.trim();
    const coverLetter = document.getElementById('tw104-cover').value.trim();
    const taskFilter = document.getElementById('tw104-filter').value.trim();
    payload = {
      keyword,
      max_applications: parseInt(document.getElementById('tw104-max').value, 10) || 5,
      max_pages: parseInt(document.getElementById('tw104-max-pages').value, 10) || 10,
      remote: document.getElementById('tw104-remote').checked,
      part_time: document.getElementById('tw104-part-time').checked,
      dry_run: document.getElementById('tw104-dry-run').checked,
      ...(area ? { area } : {}),
      ...(coverLetter ? { cover_letter: coverLetter } : {}),
      ...(taskFilter ? { task_filter: taskFilter } : {}),
    };
    jobName = `104 應徵: ${keyword}${payload.dry_run ? ' (dry-run)' : ''}`;

  } else if (jobType === 'email_collect') {
    const query = document.getElementById('lead-query').value.trim();
    if (!query) { showToast('Business query is required', 'error'); return; }
    const region   = document.getElementById('lead-region').value.trim();
    const industry = document.getElementById('lead-industry').value.trim();
    const offer    = document.getElementById('lead-offer').value.trim();
    const limit    = parseInt(document.getElementById('lead-limit').value, 10) || 15;
    payload = {
      query, limit,
      smtp_check: document.getElementById('lead-smtp').checked,
      ...(region   ? { region }   : {}),
      ...(industry ? { industry } : {}),
      ...(offer    ? { offer }    : {}),
    };
    jobName = `Leads: ${query}${region ? ' @ ' + region : ''}`;

  } else if (jobType === 'pipeline') {
    const steps = collectPipelineSteps();
    if (!steps.length) { showToast('Add at least one step', 'error'); return; }
    for (let i = 0; i < steps.length; i++) {
      const { job_type: jt, payload: sp } = steps[i];
      const missing = f => !String(sp[f] ?? '').trim();
      if (jt === 'email_sender') {
        if (missing('to'))      { showToast(`Step ${i + 1}: Recipients are required`, 'error'); return; }
        if (missing('subject')) { showToast(`Step ${i + 1}: Subject is required`, 'error'); return; }
        if (missing('body'))    { showToast(`Step ${i + 1}: Body is required`, 'error'); return; }
      } else if (jt === 'web_scraper'      && missing('url'))          { showToast(`Step ${i + 1}: URL is required`, 'error'); return; }
      else if   (jt === 'x_scraper'        && missing('username'))      { showToast(`Step ${i + 1}: X Username is required`, 'error'); return; }
      else if   (jt === 'google_form_fill' && missing('company_name')) { showToast(`Step ${i + 1}: Company name is required`, 'error'); return; }
      else if   (jt === 'shopee_seller_scraper' && missing('keyword'))  { showToast(`Step ${i + 1}: Search keyword is required`, 'error'); return; }
      else if   (jt === 'email_collect' && missing('query'))            { showToast(`Step ${i + 1}: Business query is required`, 'error'); return; }
    }
    const typeNames = steps.map(s => (AUTO_CATALOG[s.job_type]?.name || s.job_type));
    payload  = { steps };
    jobName  = `Pipeline: ${typeNames.join(' → ')}`;
  }

  // Attach LLM config to payload
  payload.llm_provider = document.getElementById('llm-provider').value;
  payload.llm_model    = document.getElementById('llm-model').value;

  const schedule = document.getElementById('job-schedule').value.trim();

  closeModalFn();
  runForm.reset();
  selectJobType('google_form_fill');
  updateModelOptions();

  if (schedule) {
    // Scheduled job: save it (server validates the cron) and let the scheduler
    // run it — do NOT trigger a run now.
    await createScheduledJob(jobType, jobName, payload, schedule);
  } else {
    navigate('activity');
    await triggerRun(jobType, jobName, payload);
  }
});

// ── Create a scheduled (cron) job without running it now ───────────────────────

async function createScheduledJob(jobType, jobName, payload, schedule) {
  try {
    const resp = await fetch('/api/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: jobName, job_type: jobType, payload, schedule }),
    });
    if (resp.status === 400) {
      const e = await resp.json().catch(() => ({}));
      showToast(e.detail || 'Invalid cron expression', 'error');
      return;
    }
    if (!resp.ok) throw new Error('Failed to save scheduled job');
    showToast(`Scheduled "${jobName}" (${schedule})`, 'success');
    navigate('schedules');
  } catch (err) {
    showToast(err.message, 'error');
  }
}

// ── Trigger a new run ─────────────────────────────────────────────────────────

async function triggerRun(jobType, jobName, payload) {
  try {
    const jobResp = await fetch('/api/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: jobName, job_type: jobType, payload }),
    });
    if (!jobResp.ok) throw new Error('Failed to create job');
    const job = await jobResp.json();

    const runResp = await fetch(`/api/jobs/${job.id}/run`, { method: 'POST' });
    if (!runResp.ok) throw new Error('Failed to trigger run');
    const { run_id } = await runResp.json();

    showProgress(jobName, jobType);
    await loadHistory();
    scrollToRun(run_id);
    document.getElementById(`detail-${run_id}`)?.classList.remove('hidden');
    startSSE(run_id);
  } catch (err) {
    showToast(err.message, 'error');
  }
}

// ── Re-run ────────────────────────────────────────────────────────────────────

async function rerun(jobId) {
  try {
    const runResp = await fetch(`/api/jobs/${jobId}/run`, { method: 'POST' });
    if (!runResp.ok) throw new Error('Re-run failed');
    const { run_id } = await runResp.json();
    const cached = cachedRuns.find(r => r.job_id === jobId);
    const jobName = cached?.job_name ?? `job ${jobId}`;
    showProgress(jobName, cached?.job_type);
    await loadHistory();
    scrollToRun(run_id);
    document.getElementById(`detail-${run_id}`)?.classList.remove('hidden');
    startSSE(run_id);
  } catch (err) {
    showToast(err.message, 'error');
  }
}

// ── Delete single run ─────────────────────────────────────────────────────────

async function deleteRun(runId) {
  const run = cachedRuns.find(r => r.id === runId);
  if (run?.status === 'pending' || run?.status === 'running') {
    showToast('Cannot delete an active run', 'error');
    return;
  }
  try {
    const resp = await fetch(`/api/runs/${runId}`, { method: 'DELETE' });
    if (resp.status === 409) { showToast('Cannot delete an active run', 'error'); return; }
    if (!resp.ok) throw new Error('Delete failed');
    selectedRunIds.delete(runId);
    updateBulkActionButtons();
    await loadHistory();
    showToast('Run deleted', 'success');
  } catch (err) {
    showToast(err.message, 'error');
  }
}

// ── Bulk delete ───────────────────────────────────────────────────────────────

document.getElementById('delete-selected-btn').addEventListener('click', async () => {
  if (!selectedRunIds.size) return;
  const ok = await confirmDialog('Delete Selected Runs', `Delete ${selectedRunIds.size} selected run(s)? Active runs will be skipped.`);
  if (!ok) return;
  try {
    const ids = [...selectedRunIds].join(',');
    const resp = await fetch(`/api/runs?ids=${ids}`, { method: 'DELETE' });
    if (!resp.ok) throw new Error('Bulk delete failed');
    const { deleted } = await resp.json();
    selectedRunIds.clear();
    updateBulkActionButtons();
    await loadHistory();
    showToast(`Deleted ${deleted} run(s)`, 'success');
  } catch (err) {
    showToast(err.message, 'error');
  }
});

document.getElementById('delete-all-btn').addEventListener('click', async () => {
  const completedCount = cachedRuns.filter(r => r.status !== 'pending' && r.status !== 'running').length;
  if (!completedCount) { showToast('No completed runs to delete', 'error'); return; }
  const ok = await confirmDialog('Delete All Runs', `Delete all ${completedCount} completed run(s)? Active runs will be kept.`);
  if (!ok) return;
  try {
    const resp = await fetch('/api/runs?delete_all=true', { method: 'DELETE' });
    if (!resp.ok) throw new Error('Delete all failed');
    const { deleted } = await resp.json();
    selectedRunIds.clear();
    updateBulkActionButtons();
    await loadHistory();
    showToast(`Deleted ${deleted} run(s)`, 'success');
  } catch (err) {
    showToast(err.message, 'error');
  }
});

// ── Confirm dialog ────────────────────────────────────────────────────────────

function confirmDialog(title, msg) {
  return new Promise((resolve) => {
    document.getElementById('confirm-title').textContent = title;
    document.getElementById('confirm-msg').textContent = msg;
    confirmModal.classList.remove('hidden');
    confirmResolve = resolve;
  });
}

document.getElementById('confirm-ok').addEventListener('click', () => {
  confirmModal.classList.add('hidden');
  confirmResolve?.(true);
});
document.getElementById('confirm-cancel').addEventListener('click', () => {
  confirmModal.classList.add('hidden');
  confirmResolve?.(false);
});
document.getElementById('confirm-close').addEventListener('click', () => {
  confirmModal.classList.add('hidden');
  confirmResolve?.(false);
});
confirmModal.addEventListener('click', (e) => {
  if (e.target === confirmModal) { confirmModal.classList.add('hidden'); confirmResolve?.(false); }
});

// ── Select all checkbox ───────────────────────────────────────────────────────

document.getElementById('select-all-cb').addEventListener('change', (e) => {
  const checked = e.target.checked;
  cachedRuns.forEach(run => {
    if (run.status !== 'pending' && run.status !== 'running') {
      if (checked) selectedRunIds.add(run.id);
      else selectedRunIds.delete(run.id);
    }
  });
  document.querySelectorAll('.run-cb').forEach(cb => {
    const run = cachedRuns.find(r => r.id === parseInt(cb.dataset.runId, 10));
    if (run && run.status !== 'pending' && run.status !== 'running') cb.checked = checked;
  });
  updateBulkActionButtons();
});

function updateBulkActionButtons() {
  const btn = document.getElementById('delete-selected-btn');
  if (selectedRunIds.size > 0) {
    btn.classList.remove('hidden');
    btn.textContent = `Delete Selected (${selectedRunIds.size})`;
  } else {
    btn.classList.add('hidden');
  }
}

// ── Table event delegation ────────────────────────────────────────────────────

document.getElementById('history-tbody').addEventListener('click', (e) => {
  // Checkbox toggle
  const cb = e.target.closest('.run-cb');
  if (cb) {
    const runId = parseInt(cb.dataset.runId, 10);
    if (cb.checked) selectedRunIds.add(runId);
    else selectedRunIds.delete(runId);
    const row = document.querySelector(`tr.data-row[data-run-id="${runId}"]`);
    row?.classList.toggle('selected', cb.checked);
    updateBulkActionButtons();
    return;
  }

  const actionEl = e.target.closest('[data-action]');
  if (actionEl) {
    const action = actionEl.dataset.action;
    const runId  = parseInt(actionEl.dataset.runId, 10);
    const jobId  = parseInt(actionEl.dataset.jobId, 10);
    const tab    = actionEl.dataset.tab;
    switch (action) {
      case 'tab':    switchTab(runId, tab); return;
      case 'rerun':  rerun(jobId);          return;
      case 'delete': deleteRun(runId);      return;
      case 'copy':   copyResult(runId);     return;
    }
    return;
  }

  // Guard: clicks inside an open detail pane must not toggle the parent row.
  if (e.target.closest('tr.detail-row')) return;
  const row = e.target.closest('tr.data-row');
  if (row) toggleDetail(parseInt(row.dataset.runId, 10));
});

// ── Scroll to & highlight new row ─────────────────────────────────────────────

function scrollToRun(runId) {
  setTimeout(() => {
    const row = document.querySelector(`tr[data-run-id="${runId}"]`);
    if (!row) return;
    row.scrollIntoView({ behavior: 'smooth', block: 'center' });
    row.classList.add('row-highlight');
    setTimeout(() => row.classList.remove('row-highlight'), 2000);
  }, 120);
}

// ── Progress panel ────────────────────────────────────────────────────────────

function showProgress(jobName, jobType) {
  activeJobType = jobType || null;
  activeLogs = [];
  progressLog.innerHTML = '';
  progressJobName.textContent = jobName;
  progressPulse.style.display = '';
  progressPanel.classList.remove('hidden');
  document.getElementById('progress-step-graph').innerHTML =
    renderStepGraph(activeJobType, activeLogs, 'running');
}

function finishProgress(status, durationSecs) {
  progressPulse.style.display = 'none';
  const ok = status === 'success';
  const color = ok ? 'var(--green)' : 'var(--red)';
  progressJobName.innerHTML = `<span style="color:${color}">${ok ? '✓ Completed' : '✗ Failed'} in ${durationSecs}s</span>`;
  document.getElementById('progress-step-graph').innerHTML =
    renderStepGraph(activeJobType, activeLogs, status);
  setTimeout(hideProgress, 3500);
}

function hideProgress() { progressPanel.classList.add('hidden'); }

function appendProgressEntry(entry) {
  activeLogs.push(entry);
  const li = document.createElement('li');
  li.innerHTML = `<span class="log-ts">${escHtml(entry.ts)}</span>${escHtml(entry.msg)}`;
  progressLog.appendChild(li);
  progressLog.scrollTop = progressLog.scrollHeight;
  // Live-update the step graph as new log entries arrive
  document.getElementById('progress-step-graph').innerHTML =
    renderStepGraph(activeJobType, activeLogs, 'running');
}

// ── Server-Sent Events ────────────────────────────────────────────────────────

function startSSE(runId) {
  if (activeEventSource) activeEventSource.close();
  sseStartTime = Date.now();
  activeEventSource = new EventSource(`/api/runs/${runId}/stream`);

  activeEventSource.onmessage = (e) => {
    const data = JSON.parse(e.data);
    if (data.new_logs) data.new_logs.forEach(appendProgressEntry);
    updateRow(runId, data);

    if (data.status === 'success' || data.status === 'failed') {
      activeEventSource.close();
      activeEventSource = null;
      finishProgress(data.status, ((Date.now() - sseStartTime) / 1000).toFixed(1));
    }
  };

  activeEventSource.onerror = () => {
    activeEventSource.close();
    activeEventSource = null;
    hideProgress();
  };
}

function updateRow(runId, data) {
  const row = document.querySelector(`tr[data-run-id="${runId}"]`);
  if (!row) { loadHistory(); return; }

  const badge = row.querySelector('.badge');
  if (badge) { badge.textContent = data.status; badge.className = `badge badge-${data.status}`; }

  if (data.result) {
    const cell = row.querySelector('.result-cell');
    if (cell) cell.textContent = extractResultText(data.result);
  }

  if (data.status === 'success' || data.status === 'failed') {
    stopElapsedTimer(runId);
    loadHistory();
  }
}

// ── History table ─────────────────────────────────────────────────────────────

// `reset` sends the view back to page 1 (used by filters / after a run finishes).
async function loadHistory(reset = true) {
  try {
    if (reset) runsPage = 0;
    const params = new URLSearchParams({ limit: RUNS_PAGE_SIZE, offset: runsPage * RUNS_PAGE_SIZE });
    if (historyFilters.job_type)       params.set('job_type', historyFilters.job_type);
    if (historyFilters.status)         params.set('status', historyFilters.status);
    if (historyFilters.started_after)  params.set('started_after', historyFilters.started_after);
    if (historyFilters.started_before) params.set('started_before', historyFilters.started_before);
    const resp = await fetch(`/api/runs?${params.toString()}`);
    if (!resp.ok) return;
    runsTotal = parseInt(resp.headers.get('X-Total-Count'), 10) || 0;
    const pageCount = Math.max(1, Math.ceil(runsTotal / RUNS_PAGE_SIZE));
    // A page can go stale (e.g. rows deleted); clamp and refetch if we overshot.
    if (runsPage > 0 && runsPage >= pageCount) { runsPage = pageCount - 1; return loadHistory(false); }
    const runs = await resp.json();
    renderHistory(runs);
    renderPagination();
  } catch (_) {}
}

function renderHistory(runs) {
  cachedRuns = runs;
  const tbody = document.getElementById('history-tbody');

  // Snapshot which detail rows are open and which tab is active before wiping the DOM.
  const openState = new Map(); // runId → active pane name ('result' | 'log')
  tbody.querySelectorAll('.detail-row:not(.hidden)').forEach(el => {
    const runId = parseInt(el.id.replace('detail-', ''), 10);
    const active = el.querySelector('.detail-pane.active');
    openState.set(runId, active?.dataset.pane ?? 'result');
  });

  if (!runs.length) {
    const filtered = Object.values(historyFilters).some(Boolean);
    const msg = filtered
      ? 'No runs match the current filters.'
      : 'No runs yet. Click "New Run" to get started.';
    tbody.innerHTML = `<tr><td colspan="8" class="empty-state">${msg}</td></tr>`;
    document.getElementById('select-all-cb').checked = false;
    return;
  }

  tbody.innerHTML = runs.map((run) => {
    const meta     = TYPE_META[run.job_type] || { chip: run.job_type?.toUpperCase() || '?', cls: 'chip-unknown' };
    const isActive = run.status === 'running' || run.status === 'pending';
    const duration = run.finished_at
      ? `${((new Date(run.finished_at) - new Date(run.started_at)) / 1000).toFixed(1)}s`
      : '—';
    const durCell  = isActive
      ? `<span class="elapsed-timer" data-started="${run.started_at}">…</span>`
      : duration;
    const started  = new Date(run.started_at).toLocaleString();

    let resultText = '', resultJson = null;
    if (run.result) {
      try { resultJson = JSON.parse(run.result); resultText = extractResultText(resultJson); }
      catch (_) { resultText = run.result; }
    }

    const totalTok = (run.tokens_in || 0) + (run.tokens_out || 0);
    const provider = run.llm_provider || '';
    const llmBadge = provider
      ? `<span class="llm-badge llm-${provider}">${provider === 'anthropic' ? 'claude' : provider}</span>`
      : '';
    const costStr = run.cost_usd > 0
      ? (run.cost_usd < 0.0001 ? '<$0.0001' : '$' + run.cost_usd.toFixed(4))
      : '';
    const costBadge = costStr ? `<span class="cost-badge">${costStr}</span>` : '';
    const retryBadge = run.retry_count > 0 ? `<span class="retry-badge">${run.retry_count}↺</span>` : '';
    const hasEval = run.eval_score !== null && run.eval_score !== undefined;
    const evalCls = !hasEval ? '' : run.eval_score >= 80 ? 'eval-high' : run.eval_score >= 50 ? 'eval-mid' : 'eval-low';
    const evalBadge = hasEval
      ? `<span class="eval-badge ${evalCls}" title="Quality score ${run.eval_score}/100 · confidence ${run.eval_confidence ?? '—'} · ${run.eval_method || ''}${run.eval_notes ? ' — ' + escHtml(run.eval_notes) : ''}">★ ${Math.round(run.eval_score)}</span>`
      : '';
    const hasUsage = totalTok > 0 || hasEval;

    let logJson = null;
    if (run.log) { try { logJson = JSON.parse(run.log); } catch (_) {} }

    const hasFlow = !!(FLOW_STEPS[run.job_type] || run.job_type === 'pipeline');
    const flowTab = hasFlow
      ? `<button class="detail-tab" data-action="tab" data-tab="flow" data-run-id="${run.id}">Flow</button>`
      : '';
    const logTab = logJson
      ? `<button class="detail-tab" data-action="tab" data-tab="log" data-run-id="${run.id}">Log (${logJson.length})</button>`
      : '';
    const usageTab = hasUsage
      ? `<button class="detail-tab" data-action="tab" data-tab="usage" data-run-id="${run.id}">Usage</button>`
      : '';
    const logPane = logJson
      ? `<div class="detail-pane" id="pane-log-${run.id}" data-pane="log">
           <ul class="log-list">${logJson.map(e =>
             `<li><span class="log-ts">${escHtml(e.ts)}</span>${escHtml(e.msg)}</li>`
           ).join('')}</ul>
         </div>`
      : '';
    const evalRows = hasEval
      ? `<li><span class="log-ts">eval score</span><span class="${evalCls}">${run.eval_score}/100</span></li>
         <li><span class="log-ts">eval confidence</span>${run.eval_confidence ?? '—'}</li>
         <li><span class="log-ts">eval method</span>${escHtml(run.eval_method || '—')}</li>
         <li style="grid-column:1/-1"><span class="log-ts">eval notes</span>${escHtml(run.eval_notes || '—')}</li>`
      : '';
    const usagePane = hasUsage
      ? `<div class="detail-pane" id="pane-usage-${run.id}" data-pane="usage">
           <ul class="log-list" style="column-gap:2rem;display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr))">
             <li><span class="log-ts">provider</span>${escHtml(run.llm_provider || '—')}</li>
             <li><span class="log-ts">model</span>${escHtml(run.llm_model || '—')}</li>
             <li><span class="log-ts">tokens in</span>${(run.tokens_in || 0).toLocaleString()}</li>
             <li><span class="log-ts">tokens out</span>${(run.tokens_out || 0).toLocaleString()}</li>
             <li><span class="log-ts">total tokens</span>${totalTok.toLocaleString()}</li>
             <li><span class="log-ts">cost</span>${costStr || '$0.000000'}</li>
             <li><span class="log-ts">retries</span>${run.retry_count || 0}</li>
             ${evalRows}
           </ul>
         </div>`
      : '';
    const flowPane = hasFlow
      ? `<div class="detail-pane" id="pane-flow-${run.id}" data-pane="flow">
           <div class="flow-tab-graph">
             ${renderStepGraph(run.job_type, logJson || [], run.status)}
           </div>
         </div>`
      : '';

    const deleteDis  = isActive ? 'disabled title="Cannot delete an active run"' : 'title="Delete run"';
    const rerunLabel = run.status === 'failed' ? '↺ Retry' : '↺';
    const rerunCls   = run.status === 'failed' ? 'btn-rerun btn-retry' : 'btn-rerun';
    const cbChecked  = selectedRunIds.has(run.id) ? 'checked' : '';
    const cbDis      = isActive ? 'disabled' : '';
    const rowSel     = selectedRunIds.has(run.id) ? 'selected' : '';
    // Admins see everyone's runs; tag each with its owner.
    const ownerBadge = (CURRENT_USER && CURRENT_USER.is_admin && run.owner)
      ? `<span class="badge badge-admin" title="Run by ${escHtml(run.owner)}">@${escHtml(run.owner)}</span>` : '';

    return `
      <tr class="data-row ${rowSel}" data-run-id="${run.id}">
        <td class="td-check" onclick="event.stopPropagation()">
          <input type="checkbox" class="run-cb" data-run-id="${run.id}" ${cbChecked} ${cbDis} />
        </td>
        <td style="color:var(--text-muted)">#${run.id}</td>
        <td>
          <div class="job-cell">
            <span class="job-name-text">${escHtml(run.job_name)}</span>
            <div style="display:flex;gap:0.3rem;align-items:center;flex-wrap:wrap">
              <span class="type-chip ${meta.cls}">${meta.chip}</span>
              ${ownerBadge}${llmBadge}${retryBadge}${evalBadge}
            </div>
          </div>
        </td>
        <td><span class="badge badge-${run.status}">${run.status}</span></td>
        <td style="color:var(--text-muted);font-size:0.8rem">${started}</td>
        <td style="color:var(--text-muted)">${durCell}</td>
        <td class="result-cell" style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:0.82rem;color:var(--text-muted)">
          ${escHtml(resultText)}
          ${costBadge ? `<div style="margin-top:0.2rem">${costBadge}${totalTok > 0 ? `<span class="cost-badge" style="margin-left:0.35rem">${totalTok.toLocaleString()}tok</span>` : ''}</div>` : ''}
        </td>
        <td style="padding:0.65rem 0.5rem">
          <div class="row-actions">
            <button class="${rerunCls}" data-action="rerun" data-job-id="${run.job_id}" data-run-id="${run.id}" title="Re-run">${rerunLabel}</button>
            <button class="btn-delete" data-action="delete" data-run-id="${run.id}" ${deleteDis}>🗑</button>
          </div>
        </td>
      </tr>
      <tr class="detail-row hidden" id="detail-${run.id}">
        <td colspan="8" style="padding:0">
          <div class="detail-tabs">
            <button class="detail-tab active" data-action="tab" data-tab="result" data-run-id="${run.id}">Result</button>
            ${flowTab}
            ${logTab}
            ${usageTab}
          </div>
          <div class="detail-pane active" id="pane-result-${run.id}" data-pane="result">
            <div class="pane-toolbar">
              <button class="btn-copy" data-action="copy" data-run-id="${run.id}">Copy JSON</button>
              ${resultJson && resultJson.pdf_url ? `<a class="btn-copy" href="/api/runs/${run.id}/report.pdf" target="_blank" rel="noopener">📄 下載 PDF 報告</a>` : ''}
              ${run.job_type === 'email_collect' && resultJson ? `<a class="btn-copy" href="/api/runs/${run.id}/leads.csv">⬇ Download CSV</a>` : ''}
            </div>
            ${run.job_type === 'email_collect' && resultJson ? renderLeadsTable(resultJson) : ''}
            <pre>${escHtml(resultJson ? JSON.stringify(resultJson, null, 2) : (run.result || ''))}</pre>
          </div>
          ${flowPane}
          ${logPane}
          ${usagePane}
        </td>
      </tr>`;
  }).join('');

  // Restore previously open detail rows (preserves state across periodic refreshes).
  openState.forEach((tab, runId) => {
    const detailRow = document.getElementById(`detail-${runId}`);
    if (detailRow) {
      detailRow.classList.remove('hidden');
      switchTab(runId, tab);
    }
  });

  initElapsedTimers();
  updateBulkActionButtons();
}

// Windowed numbered pagination: « ‹ 1 … 4 5 [6] 7 8 … 20 › »
function renderPagination() {
  const bar = document.getElementById('history-pagination');
  const pageCount = Math.max(1, Math.ceil(runsTotal / RUNS_PAGE_SIZE));

  // Nothing to page through — hide the bar entirely.
  if (runsTotal <= RUNS_PAGE_SIZE) { bar.classList.add('hidden'); return; }
  bar.classList.remove('hidden');

  const cur = runsPage; // 0-indexed
  document.getElementById('page-prev-btn').disabled = cur <= 0;
  document.getElementById('page-next-btn').disabled = cur >= pageCount - 1;

  const from = runsTotal ? cur * RUNS_PAGE_SIZE + 1 : 0;
  const to   = Math.min(runsTotal, (cur + 1) * RUNS_PAGE_SIZE);
  document.getElementById('page-info').textContent = `${from}–${to} of ${runsTotal}`;

  // Build the page-number window: always show first & last, plus ±1 around current.
  const pages = new Set([0, pageCount - 1, cur, cur - 1, cur + 1]);
  const shown = [...pages].filter(p => p >= 0 && p < pageCount).sort((a, b) => a - b);

  let html = '', prev = -1;
  for (const p of shown) {
    // A gap of exactly one page: show that page's button instead of a same-width "…".
    if (p - prev === 2) {
      html += `<button class="page-num" data-page-num="${p - 1}">${p}</button>`;
    } else if (p - prev > 2) {
      html += `<span class="page-num ellipsis">…</span>`;
    }
    html += `<button class="page-num ${p === cur ? 'active' : ''}" data-page-num="${p}">${p + 1}</button>`;
    prev = p;
  }
  document.getElementById('page-numbers').innerHTML = html;
}

function goToPage(page) {
  const pageCount = Math.max(1, Math.ceil(runsTotal / RUNS_PAGE_SIZE));
  const clamped = Math.max(0, Math.min(page, pageCount - 1));
  if (clamped === runsPage) return;
  runsPage = clamped;
  loadHistory(false);
  document.getElementById('history-filters')?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function toggleDetail(runId) {
  document.getElementById(`detail-${runId}`)?.classList.toggle('hidden');
}

function switchTab(runId, tab) {
  document.querySelectorAll(`#detail-${runId} .detail-tab`)
    .forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
  document.querySelectorAll(`#detail-${runId} .detail-pane`)
    .forEach(p => p.classList.toggle('active', p.dataset.pane === tab));
}

// ── Elapsed timers ────────────────────────────────────────────────────────────

function initElapsedTimers() {
  elapsedTimers.forEach((id, runId) => {
    if (!document.querySelector(`tr[data-run-id="${runId}"] .elapsed-timer`)) {
      clearInterval(id); elapsedTimers.delete(runId);
    }
  });
  document.querySelectorAll('.elapsed-timer').forEach(el => {
    const runId = parseInt(el.closest('tr')?.dataset.runId, 10);
    if (!runId || elapsedTimers.has(runId)) return;
    const startedAt = new Date(el.dataset.started);
    const tick = () => { el.textContent = `${Math.floor((Date.now() - startedAt) / 1000)}s…`; };
    tick();
    elapsedTimers.set(runId, setInterval(tick, 1000));
  });
}

function stopElapsedTimer(runId) {
  const id = elapsedTimers.get(runId);
  if (id !== undefined) { clearInterval(id); elapsedTimers.delete(runId); }
}

// ── Email Collector result table ──────────────────────────────────────────────

function renderLeadsTable(rj) {
  const leads = Array.isArray(rj.leads) ? rj.leads : [];
  const summary = `<div class="leads-summary">
      Discovered <b>${rj.discovered_count ?? 0}</b> · with website
      <b>${rj.with_website ?? 0}</b> · verified leads
      <b>${rj.lead_count ?? leads.length}</b></div>`;
  if (!leads.length) {
    return summary + '<div class="leads-summary">No contact emails collected.</div>';
  }
  const conf = c => `<span class="lead-conf lead-conf-${escHtml(c || 'low')}">${escHtml(c || '—')}</span>`;
  const rows = leads.map(l => `<tr>
      <td>${escHtml(l.company || '')}</td>
      <td><a href="mailto:${escHtml(l.email || '')}">${escHtml(l.email || '')}</a>${l.source === 'guessed' ? ' <span class="lead-guessed">guessed</span>' : ''}</td>
      <td>${conf(l.confidence)}</td>
      <td style="text-align:center">${l.icp_fit ?? ''}</td>
      <td>${escHtml(l.hook || '')}</td>
      <td>${l.website ? `<a href="${escHtml(l.website)}" target="_blank" rel="noopener">site</a>` : ''}</td>
    </tr>`).join('');
  return summary + `<div class="leads-table-wrap"><table class="leads-table">
      <thead><tr><th>Company</th><th>Email</th><th>Conf.</th><th>ICP</th><th>Hook</th><th>Site</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
}

// ── Copy result JSON ──────────────────────────────────────────────────────────

async function copyResult(runId) {
  const pre = document.querySelector(`#pane-result-${runId} pre`);
  if (!pre) return;
  try {
    await navigator.clipboard.writeText(pre.textContent);
    showToast('Copied to clipboard', 'success');
  } catch (_) {
    showToast('Copy failed — select manually', 'error');
  }
}

// ── System page ───────────────────────────────────────────────────────────────

async function loadSystemPage() {
  if (systemData) { renderSystemPage(); return; }
  document.getElementById('system-content').innerHTML = '<div class="loading-state">Loading system catalog…</div>';
  try {
    const resp = await fetch('/api/system');
    if (!resp.ok) throw new Error('Failed to load system data');
    systemData = await resp.json();
    renderSystemPage();
  } catch (err) {
    document.getElementById('system-content').innerHTML = `<div class="loading-state" style="color:var(--red)">Error: ${escHtml(err.message)}</div>`;
  }
}

document.getElementById('cat-tabs').addEventListener('click', (e) => {
  const tab = e.target.closest('.cat-tab');
  if (!tab) return;
  document.querySelectorAll('.cat-tab').forEach(t => t.classList.toggle('active', t === tab));
  systemCategory = tab.dataset.cat;
  renderSystemPage();
});

function renderSystemPage() {
  if (!systemData) return;
  const items = systemData[systemCategory] || [];
  const container = document.getElementById('system-content');

  if (!items.length) {
    container.innerHTML = '<div class="loading-state">No items found.</div>';
    return;
  }

  container.innerHTML = `<div class="system-cards">${items.map(item => renderSysCard(item)).join('')}</div>`;

  // Wire up card toggles
  container.querySelectorAll('.sys-card-header').forEach(hdr => {
    hdr.addEventListener('click', () => {
      hdr.closest('.sys-card').classList.toggle('open');
    });
  });

  // Wire up source code toggles
  container.querySelectorAll('.source-toggle').forEach(tog => {
    tog.addEventListener('click', (e) => {
      e.stopPropagation();
      const body = tog.nextElementSibling;
      const isOpen = body.classList.toggle('open');
      tog.querySelector('.source-arrow').textContent = isOpen ? '▲' : '▼';
    });
  });
}

function renderSysCard(item) {
  const cat = systemCategory;

  if (cat === 'agents') {
    return `
    <div class="sys-card">
      <div class="sys-card-header">
        <div class="sys-card-title">
          <span class="sys-card-name">${escHtml(item.name)}</span>
          <span class="sys-card-badge">${escHtml(item.job_type || '')}</span>
        </div>
        <span class="sys-card-toggle">▼</span>
      </div>
      <div class="sys-card-body">
        <div class="sys-card-meta">
          <div class="meta-item"><div class="meta-label">Role</div><div class="meta-value">${escHtml(item.role)}</div></div>
          <div class="meta-item"><div class="meta-label">Goal</div><div class="meta-value">${escHtml(item.goal)}</div></div>
          <div class="meta-item" style="grid-column:1/-1"><div class="meta-label">Backstory</div><div class="meta-value">${escHtml(item.backstory)}</div></div>
          <div class="meta-item">
            <div class="meta-label">Tools</div>
            <div class="meta-tags">${(item.tools||[]).map(t => `<span class="meta-tag">${escHtml(t)}</span>`).join('')}</div>
          </div>
          <div class="meta-item"><div class="meta-label">Crew</div><div class="meta-value mono">${escHtml(item.crew)}</div></div>
          <div class="meta-item"><div class="meta-label">Task</div><div class="meta-value mono">${escHtml(item.task)}</div></div>
        </div>
        ${renderSourceSection(item.source_code, item.source_file)}
      </div>
    </div>`;
  }

  if (cat === 'tools') {
    const inputs = (item.inputs || []).map(inp =>
      `<div class="auto-input-row">
         <span class="auto-input-name">${escHtml(inp.name)}</span>
         <span class="auto-input-type">${escHtml(inp.type)}</span>
         <span class="auto-input-desc">— ${escHtml(inp.description)}</span>
       </div>`
    ).join('');
    return `
    <div class="sys-card">
      <div class="sys-card-header">
        <div class="sys-card-title">
          <span class="sys-card-name">${escHtml(item.name)}</span>
          <span class="sys-card-sub">${escHtml(item.class)}</span>
        </div>
        <span class="sys-card-toggle">▼</span>
      </div>
      <div class="sys-card-body">
        <div class="sys-card-meta">
          <div class="meta-item" style="grid-column:1/-1"><div class="meta-label">Description</div><div class="meta-value">${escHtml(item.description)}</div></div>
          <div class="meta-item">
            <div class="meta-label">Inputs</div>
            <div class="auto-card-inputs">${inputs}</div>
          </div>
          <div class="meta-item">
            <div class="meta-label">Used By</div>
            <div class="meta-tags">${(item.used_by||[]).map(c => `<span class="meta-tag">${escHtml(c)}</span>`).join('')}</div>
          </div>
        </div>
        ${renderSourceSection(item.source_code, item.source_file)}
      </div>
    </div>`;
  }

  if (cat === 'crews') {
    const tasks = (item.tasks || []).map(t => `
      <div class="flow-step">
        <div class="flow-step-line"><div class="flow-step-dot"></div></div>
        <div class="flow-step-content">
          <div class="flow-step-name">${escHtml(t.name)}</div>
          <div class="flow-step-desc">${escHtml(t.description)}</div>
          <div style="margin-top:0.25rem;font-size:0.75rem;color:var(--text-muted)">Expected: <code style="font-family:ui-monospace,monospace;font-size:0.73rem">${escHtml(t.expected_output)}</code></div>
          ${t.config_code ? renderSourceSection(t.config_code, t.config_file || 'tasks.yaml', 'Task Config') : ''}
        </div>
      </div>`).join('');

    return `
    <div class="sys-card">
      <div class="sys-card-header">
        <div class="sys-card-title">
          <span class="sys-card-name">${escHtml(item.name)}</span>
          <span class="sys-card-badge">process: ${escHtml(item.process)}</span>
        </div>
        <span class="sys-card-toggle">▼</span>
      </div>
      <div class="sys-card-body">
        <div class="sys-card-meta">
          <div class="meta-item">
            <div class="meta-label">Agents</div>
            <div class="meta-tags">${(item.agents||[]).map(a => `<span class="meta-tag">${escHtml(a)}</span>`).join('')}</div>
          </div>
          <div class="meta-item"><div class="meta-label">Flow</div><div class="meta-value mono">${escHtml(item.flow)}</div></div>
          <div class="meta-item"><div class="meta-label">Job Type</div><div class="meta-value mono">${escHtml(item.job_type)}</div></div>
        </div>
        <div style="padding:0 1.1rem 0.25rem"><div class="meta-label" style="margin-bottom:0.5rem">Tasks</div></div>
        <div class="flow-steps">${tasks}</div>
        ${renderSourceSection(item.source_code, item.source_file)}
      </div>
    </div>`;
  }

  if (cat === 'workflows') {
    const steps = (item.steps || []).map((step, i) => `
      <div class="flow-step">
        <div class="flow-step-line">
          <div class="flow-step-dot"></div>
          ${i < item.steps.length - 1 ? '<div class="flow-step-connector"></div>' : ''}
        </div>
        <div class="flow-step-content">
          <div class="flow-step-dec">${escHtml(step.decorator)}</div>
          <div class="flow-step-name">${escHtml(step.name)}</div>
          <div class="flow-step-desc">${escHtml(step.description)}</div>
        </div>
      </div>`).join('');

    const stateFields = (item.state_fields || []).map(f =>
      `<div class="auto-input-row">
         <span class="auto-input-name">${escHtml(f.name)}</span>
         <span class="auto-input-type">${escHtml(f.type)}</span>
         <span class="auto-input-desc">= ${escHtml(String(f.default))}</span>
       </div>`
    ).join('');

    return `
    <div class="sys-card">
      <div class="sys-card-header">
        <div class="sys-card-title">
          <span class="sys-card-name">${escHtml(item.name)}</span>
          <span class="sys-card-badge">${escHtml(item.job_type)}</span>
        </div>
        <span class="sys-card-toggle">▼</span>
      </div>
      <div class="sys-card-body">
        <div class="sys-card-meta">
          <div class="meta-item">
            <div class="meta-label">State Fields</div>
            <div class="auto-card-inputs">${stateFields}</div>
          </div>
          <div class="meta-item"><div class="meta-label">Crew</div><div class="meta-value mono">${escHtml(item.crew)}</div></div>
        </div>
        <div style="padding:0 1.1rem 0.25rem"><div class="meta-label" style="margin-bottom:0.5rem">Flow Steps</div></div>
        <div class="flow-steps">${steps}</div>
        ${renderSourceSection(item.source_code, item.source_file)}
      </div>
    </div>`;
  }

  return '';
}

function renderSourceSection(code, filePath, label = 'Source Code') {
  if (!code) return '';
  return `
    <div class="sys-card-source">
      <div class="source-toggle">
        <span>${escHtml(label)}${filePath ? ` <span style="font-weight:400;opacity:0.55;font-family:ui-monospace,monospace;font-size:0.72rem">${escHtml(filePath)}</span>` : ''}</span>
        <span class="source-arrow">▼</span>
      </div>
      <div class="source-body">
        <pre>${highlightCode(code, filePath)}</pre>
      </div>
    </div>`;
}

// ── Automations page ──────────────────────────────────────────────────────────

function renderAutomationsPage() {
  const grid = document.getElementById('auto-grid');
  const visible = visibleTypeSet();
  grid.innerHTML = Object.entries(AUTO_CATALOG).filter(([type]) => visible.has(type)).map(([type, meta]) => {
    const inputs = meta.inputs.map(inp =>
      `<div class="auto-input-row">
         <span class="auto-input-name">${escHtml(inp.name)}</span>
         <span class="auto-input-type">${escHtml(inp.type)}</span>
         <span class="auto-input-desc">— ${escHtml(inp.desc)}</span>
       </div>`
    ).join('');

    return `
    <div class="auto-card">
      <div class="auto-card-header">
        <div class="auto-card-icon">${meta.icon}</div>
        <div>
          <div class="auto-card-name">${escHtml(meta.name)}</div>
          <div class="auto-card-type">${escHtml(type)}</div>
        </div>
      </div>
      <div class="auto-card-desc">${escHtml(meta.desc)}</div>
      <div class="auto-card-section">
        <div class="auto-card-section-label">Inputs</div>
        <div class="auto-card-inputs">${inputs}</div>
      </div>
      <div class="auto-card-section">
        <div class="auto-card-section-label">Pipeline</div>
        <div class="auto-card-links">
          <span class="auto-link-chip">Flow: ${escHtml(meta.flow)}</span>
          <span class="auto-link-chip">Crew: ${escHtml(meta.crew)}</span>
          <span class="auto-link-chip">Agent: ${escHtml(meta.agent)}</span>
          ${meta.tools.map(t => `<span class="auto-link-chip" style="background:rgba(59,130,246,0.1);color:var(--blue);border-color:rgba(59,130,246,0.2)">Tool: ${escHtml(t)}</span>`).join('')}
        </div>
      </div>
      <div class="auto-card-footer">
        <button class="btn btn-primary" style="width:100%" data-run-type="${type}">▶ Run ${escHtml(meta.name)}</button>
      </div>
    </div>`;
  }).join('');

  grid.querySelectorAll('[data-run-type]').forEach(btn => {
    btn.addEventListener('click', () => openModal(btn.dataset.runType));
  });
}

// ── Performance page ──────────────────────────────────────────────────────────

let _perfDays = 7;  // selectable trend window (7 | 14 | 30)

async function loadPerformancePage(days) {
  if (days) _perfDays = days;
  document.getElementById('perf-content').innerHTML = '<div class="loading-state">Loading metrics…</div>';
  try {
    const resp = await fetch(`/api/stats?days=${_perfDays}`);
    if (!resp.ok) throw new Error('Failed to load stats');
    renderPerformancePage(await resp.json());
  } catch (err) {
    document.getElementById('perf-content').innerHTML = `<div class="loading-state" style="color:var(--red)">Error: ${escHtml(err.message)}</div>`;
  }
}

function renderPerformancePage(s) {
  const maxTrend = Math.max(...s.trend.map(d => d.total), 1);

  const trendRows = s.trend.map(d => {
    const sw = Math.round((d.success / maxTrend) * 100);
    const fw = Math.round((d.failed  / maxTrend) * 100);
    const cost = d.cost || 0;
    const costStr = cost > 0 ? (cost < 0.01 ? '$' + cost.toFixed(5) : '$' + cost.toFixed(3)) : '·';
    const sc = d.avg_score;
    const scStr = sc == null ? '·' : Math.round(sc);
    const scColor = sc == null ? 'var(--text-muted)' : sc >= 80 ? 'var(--green)' : sc >= 50 ? 'var(--text-muted)' : 'var(--red)';
    return `
      <div class="trend-row">
        <div class="trend-label">${escHtml(d.label)}</div>
        <div class="trend-bar-wrap">
          <div class="trend-bar-success" style="width:${sw}%"></div>
          <div class="trend-bar-failed"  style="width:${fw}%"></div>
        </div>
        <div class="trend-count">${d.total}</div>
        <div style="min-width:64px;text-align:right;font-family:ui-monospace,monospace;font-size:0.72rem;color:var(--yellow)" title="cost this day">${costStr}</div>
        <div style="min-width:34px;text-align:right;font-size:0.72rem;color:${scColor}" title="avg quality score this day">${scStr}</div>
      </div>`;
  }).join('');

  const winBtn = (n) => `<button class="perf-win-btn${_perfDays === n ? ' active' : ''}" onclick="loadPerformancePage(${n})" style="padding:0.15rem 0.6rem;margin-left:0.35rem;border:1px solid var(--border);border-radius:5px;background:${_perfDays === n ? 'var(--accent,#3b82f6)' : 'transparent'};color:${_perfDays === n ? '#fff' : 'var(--text-muted)'};cursor:pointer;font-size:0.75rem">${n}d</button>`;

  const typeRows = Object.entries(s.by_type).map(([type, data]) => {
    const total = data.total || 1;
    const sw = Math.round((data.success / total) * 100);
    const fw = Math.round((data.failed  / total) * 100);
    const meta = TYPE_META[type] || { chip: type.toUpperCase(), cls: 'chip-unknown' };
    return `
      <tr>
        <td><span class="type-chip ${meta.cls}">${meta.chip}</span> ${escHtml(type)}</td>
        <td style="text-align:right">${data.total}</td>
        <td style="text-align:right;color:var(--green)">${data.success}</td>
        <td style="text-align:right;color:var(--red)">${data.failed}</td>
        <td>
          <div class="mini-bar-wrap">
            <div class="mini-bar-s" style="width:${sw}%"></div>
            <div class="mini-bar-f" style="width:${fw}%"></div>
          </div>
        </td>
        <td style="text-align:right;color:${data.retried > 0 ? 'var(--red)' : 'var(--text-muted)'}">${data.retried > 0 ? data.retried : '—'}</td>
        <td style="color:var(--text-muted)">${data.avg_duration > 0 ? data.avg_duration + 's' : '—'}</td>
      </tr>`;
  }).join('');

  const successRateColor = s.success_rate >= 80 ? 'green' : s.success_rate >= 50 ? '' : 'red';

  // Eval trust & reliability — is the headline quality number actually trustworthy?
  const indepRate = s.eval_independent_rate;   // % of scored runs graded by an independent LLM judge
  const indepColor = indepRate == null ? '' : indepRate >= 80 ? 'green' : indepRate >= 50 ? '' : 'red';
  const indepStr = indepRate == null ? '—' : indepRate + '%';
  const heurCount = s.eval_heuristic || 0;
  const retryColor = s.retry_rate === 0 ? 'green' : s.retry_rate <= 20 ? '' : 'red';
  // Fallback rate — lower is better (fewer cross-model failovers)
  const fbColor = s.fallback_rate === 0 ? 'green' : s.fallback_rate <= 10 ? '' : 'red';
  // Validation pass rate — higher is better
  const vpr = s.validation_pass_rate;
  const vprColor = vpr == null ? '' : vpr >= 90 ? 'green' : vpr >= 70 ? '' : 'red';
  const vprStr = vpr == null ? '—' : vpr + '%';
  const p95 = s.p95_duration_secs;
  const p50 = s.p50_duration_secs;
  const trustSection = `
    <div class="perf-section">
      <div class="perf-section-title">Eval Trust &amp; Reliability</div>
      <div class="stat-grid">
        <div class="stat-card">
          <div class="stat-label" title="Share of scored runs graded by an independent LLM judge, excluding self-graded and heuristic fallbacks. Low = the quality score is not trustworthy.">Independent Judge</div>
          <div class="stat-value ${indepColor}">${indepStr}</div>
          <div class="stat-sub">${s.eval_independent || 0} of ${s.eval_scored || 0} scored</div>
        </div>
        <div class="stat-card">
          <div class="stat-label" title="Runs whose quality score is a heuristic fallback (no LLM judge available). These inflate/deflate Avg Quality without a real judgment.">Heuristic Scores</div>
          <div class="stat-value ${heurCount > 0 ? 'orange' : 'green'}" style="font-size:1.4rem">${heurCount}</div>
          <div class="stat-sub">${s.eval_llm || 0} LLM-judged</div>
        </div>
        <div class="stat-card">
          <div class="stat-label" title="Percentage of all runs that needed at least one retry (validation failure or transient LLM error). High = flaky provider or weak prompts.">Retry Rate</div>
          <div class="stat-value ${retryColor}">${s.retry_rate || 0}%</div>
          <div class="stat-sub">${s.retried_runs || 0} runs · avg ${s.avg_retries || 0}</div>
        </div>
        <div class="stat-card">
          <div class="stat-label" title="Percentage of runs where the requested model failed and a different model had to serve the result. High = unreliable primary model/provider.">Fallback Rate</div>
          <div class="stat-value ${fbColor}">${s.fallback_rate || 0}%</div>
          <div class="stat-sub">${s.fallback_runs || 0} runs failed over</div>
        </div>
        <div class="stat-card">
          <div class="stat-label" title="Percentage of runs that passed the validator quality gate on the attempt that was recorded. Low = outputs frequently malformed.">Validation Pass</div>
          <div class="stat-value ${vprColor}">${vprStr}</div>
          <div class="stat-sub">${s.validation_fails || 0} failed of ${s.validated_runs || 0}</div>
        </div>
        <div class="stat-card">
          <div class="stat-label" title="95th-percentile run duration — the slow tail that averages hide.">p95 Duration</div>
          <div class="stat-value" style="font-size:1.4rem">${p95 == null ? '—' : p95 + 's'}</div>
          <div class="stat-sub">p50 ${p50 == null ? '—' : p50 + 's'}</div>
        </div>
      </div>
    </div>`;

  const totalCostStr = s.total_cost_usd > 0
    ? (s.total_cost_usd < 0.01 ? s.total_cost_usd.toFixed(5) : s.total_cost_usd.toFixed(3))
    : '0';
  const totalTokStr = s.total_tokens > 0 ? s.total_tokens.toLocaleString() : '0';

  // Per-provider cards with per-model breakdown
  const byModel = s.by_model || {};
  const allModelToks = Object.values(byModel).flat().map(m => m.tokens_in + m.tokens_out);
  const maxModelTok  = Math.max(...allModelToks, 1);

  const providerCards = Object.entries(s.by_provider || {}).map(([prov, d]) => {
    const provLabel = prov === 'anthropic' ? 'Claude' : prov === 'openai' ? 'OpenAI' : prov === 'gemini' ? 'Gemini' : prov;
    const badgeCls  = `llm-${prov}`;
    const provTok   = d.tokens_in + d.tokens_out;
    const provCost  = d.cost_usd > 0 ? (d.cost_usd < 0.01 ? '$' + d.cost_usd.toFixed(5) : '$' + d.cost_usd.toFixed(3)) : '$0';
    const provTokStr = provTok > 999 ? (provTok / 1000).toFixed(1) + 'k' : provTok.toString();

    const models = byModel[prov] || [];
    const modelRows = models.map(m => {
      const tok  = m.tokens_in + m.tokens_out;
      const inW  = Math.round((m.tokens_in  / Math.max(tok, 1)) * 100);
      const outW = 100 - inW;
      const barW = Math.round((tok / maxModelTok) * 72);
      const sr   = m.runs > 0 ? Math.round((m.success / m.runs) * 100) : 0;
      const srCls = sr >= 80 ? 'srate-high' : sr >= 50 ? 'srate-mid' : 'srate-low';
      const costStr = m.cost_usd > 0 ? (m.cost_usd < 0.0001 ? '<$0.0001' : '$' + m.cost_usd.toFixed(4)) : '$0';
      const modelShort = m.model.split('/').pop();
      const durStr = m.avg_duration > 0 ? m.avg_duration + 's' : '—';
      const score = m.avg_eval_score;
      const scoreCls = score == null ? 'srate-mid' : score >= 80 ? 'srate-high' : score >= 50 ? 'srate-mid' : 'srate-low';
      const scoreStr = score == null ? '—' : Math.round(score);
      const conf = m.avg_eval_confidence;
      const confStr = conf == null ? '—' : conf.toFixed(2);
      // Share of scored runs judged by an independent LLM (vs heuristic fallback)
      const scored = m.scored || 0;
      const llmPct = scored > 0 ? Math.round((m.llm_judged / scored) * 100) : 0;
      const judgeCls = scored === 0 ? 'srate-mid' : llmPct >= 80 ? 'srate-high' : llmPct >= 50 ? 'srate-mid' : 'srate-low';
      const judgeStr = scored === 0 ? '—' : llmPct + '%';
      const judgeTitle = scored === 0 ? 'no scored runs' : `${m.llm_judged}/${scored} scored by LLM judge, rest heuristic`;
      const retStr = m.retried > 0 ? m.retried : '—';
      const cps = m.cost_per_success;
      const cpsStr = cps == null ? '—' : cps < 0.0001 ? '<$0.0001' : '$' + cps.toFixed(4);
      const fbBadge = m.fallback > 0
        ? ` <span title="${m.fallback} run(s) failed over to this model" style="color:var(--red);font-size:0.65rem">⤳${m.fallback}</span>`
        : '';
      return `<tr>
        <td><span class="model-name-mono">${escHtml(modelShort)}</span>${fbBadge}</td>
        <td style="text-align:right">${m.runs}</td>
        <td><span class="srate-pill ${srCls}">${sr}%</span></td>
        <td><span class="srate-pill ${scoreCls}" title="confidence ${confStr}">${scoreStr}</span></td>
        <td><span class="srate-pill ${judgeCls}" title="${judgeTitle}">${judgeStr}</span></td>
        <td style="text-align:right;color:${m.retried > 0 ? 'var(--red)' : 'var(--text-muted)'}" title="runs that needed a retry">${retStr}</td>
        <td>
          <div class="tok-bar-wrap" style="width:${barW}px" title="${m.tokens_in.toLocaleString()} in · ${m.tokens_out.toLocaleString()} out">
            <div class="tok-bar-in"  style="width:${inW}%"></div>
            <div class="tok-bar-out" style="width:${outW}%"></div>
          </div>
          <div style="font-size:0.65rem;color:var(--text-muted);margin-top:0.15rem">${tok.toLocaleString()}</div>
        </td>
        <td style="color:var(--yellow);font-family:ui-monospace,monospace;font-size:0.8rem">${costStr}</td>
        <td style="color:var(--text-muted);font-family:ui-monospace,monospace;font-size:0.75rem" title="cost per successful run">${cpsStr}</td>
        <td style="color:var(--text-muted)">${durStr}</td>
      </tr>`;
    }).join('');

    return `
    <div class="llm-pcard pcard-${prov}">
      <div class="llm-pcard-header">
        <span class="llm-badge ${badgeCls}" style="font-size:0.75rem;padding:0.1rem 0.5rem">${escHtml(provLabel)}</span>
        <span class="llm-pcard-name">${escHtml(provLabel)}</span>
        <div class="llm-pcard-stats">
          <div class="llm-pcard-stat">
            <div class="llm-pcard-stat-val blue">${d.runs}</div>
            <div class="llm-pcard-stat-lab">Runs</div>
          </div>
          <div class="llm-pcard-stat">
            <div class="llm-pcard-stat-val" style="color:var(--text-muted)">${provTokStr}</div>
            <div class="llm-pcard-stat-lab">Tokens</div>
          </div>
          <div class="llm-pcard-stat">
            <div class="llm-pcard-stat-val yellow">${provCost}</div>
            <div class="llm-pcard-stat-lab">Cost</div>
          </div>
        </div>
      </div>
      ${modelRows ? `<table class="llm-model-table">
        <thead><tr>
          <th>Model</th><th style="text-align:right">Runs</th><th>Success</th><th>Score</th>
          <th title="% of scored runs graded by an independent LLM judge (vs heuristic fallback)">LLM&nbsp;Judged</th>
          <th style="text-align:right" title="runs that needed at least one retry">Retries</th>
          <th>Tokens <span style="opacity:0.5;font-weight:400">■ in ■ out</span></th>
          <th>Cost</th><th title="cost per successful run">$/Success</th><th>Avg Dur</th>
        </tr></thead>
        <tbody>${modelRows}</tbody>
      </table>` : ''}
    </div>`;
  }).join('');

  document.getElementById('perf-content').innerHTML = `
    <div class="stat-grid">
      <div class="stat-card">
        <div class="stat-label">Total Runs</div>
        <div class="stat-value blue">${s.total_runs}</div>
        <div class="stat-sub">${s.active} active</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Success Rate</div>
        <div class="stat-value ${successRateColor}">${s.success_rate}%</div>
        <div class="stat-sub">${s.success} succeeded</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Failed</div>
        <div class="stat-value ${s.failed > 0 ? 'red' : ''}">${s.failed}</div>
        <div class="stat-sub">of ${s.total_runs} total</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Avg Duration</div>
        <div class="stat-value">${s.avg_duration_secs > 0 ? s.avg_duration_secs + 's' : '—'}</div>
        <div class="stat-sub">completed runs</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Avg Quality</div>
        <div class="stat-value ${s.avg_eval_score == null ? '' : s.avg_eval_score >= 80 ? 'green' : s.avg_eval_score >= 50 ? '' : 'red'}">${s.avg_eval_score == null ? '—' : s.avg_eval_score + '/100'}</div>
        <div class="stat-sub">${s.avg_eval_confidence == null ? 'eval score' : 'confidence ' + s.avg_eval_confidence}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Total Tokens</div>
        <div class="stat-value orange" style="font-size:1.4rem">${totalTokStr}</div>
        <div class="stat-sub">${(s.total_tokens_in||0).toLocaleString()} in · ${(s.total_tokens_out||0).toLocaleString()} out</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Total Cost</div>
        <div class="stat-value yellow" style="font-size:1.4rem">$${totalCostStr}</div>
        <div class="stat-sub">USD (estimated)</div>
      </div>
    </div>

    ${trustSection}

    <div class="perf-section">
      <div class="perf-section-title" style="display:flex;align-items:center;justify-content:space-between">
        <span>Last ${s.trend_days || _perfDays} Days <span style="font-weight:400;color:var(--text-muted);font-size:0.75rem">· runs · cost · quality</span></span>
        <span>${winBtn(7)}${winBtn(14)}${winBtn(30)}</span>
      </div>
      <div class="trend-chart">${trendRows}</div>
    </div>

    ${typeRows ? `
    <div class="perf-section">
      <div class="perf-section-title">By Automation Type</div>
      <div class="perf-table-wrap">
        <table class="perf-table">
          <thead>
            <tr>
              <th>Type</th>
              <th style="text-align:right">Total</th>
              <th style="text-align:right">Success</th>
              <th style="text-align:right">Failed</th>
              <th>Rate</th>
              <th style="text-align:right" title="runs that needed at least one retry">Retries</th>
              <th>Avg Duration</th>
            </tr>
          </thead>
          <tbody>${typeRows}</tbody>
        </table>
      </div>
    </div>` : ''}

    ${providerCards ? `
    <div class="perf-section">
      <div class="perf-section-title">LLM Usage — by Provider &amp; Model</div>
      <div class="llm-provider-cards">${providerCards}</div>
    </div>` : ''}`;
}

// ── Syntax highlighting ────────────────────────────────────────────────────────

const _PY_KW_ITALIC = new Set(['def','class','async','await','lambda']);
const _PY_KW = new Set([
  'False','None','True','and','as','assert','break','continue','del','elif',
  'else','except','finally','for','from','global','if','import','in','is',
  'nonlocal','not','or','pass','raise','return','try','while','with','yield',
]);
const _PY_BI = new Set([
  'abs','all','any','bin','bool','bytes','callable','chr','dict','dir',
  'divmod','enumerate','eval','exec','filter','float','format','frozenset',
  'getattr','globals','hasattr','hash','help','hex','id','input','int',
  'isinstance','issubclass','iter','len','list','locals','map','max','min',
  'next','object','oct','open','ord','pow','print','property','range','repr',
  'reversed','round','set','setattr','slice','sorted','staticmethod','str',
  'sum','super','tuple','type','vars','zip','self','cls',
]);

function highlightCode(raw, filePath) {
  const ext = (filePath || '').split('.').pop().toLowerCase();
  try {
    if (ext === 'py')              return _hlPython(raw);
    if (ext === 'yaml' || ext === 'yml') return _hlYaml(raw);
  } catch (_) {}
  return escHtml(raw);
}

function _hlPython(code) {
  const out = [];
  let i = 0;
  const n = code.length;

  while (i < n) {
    const ch = code[i];

    // Triple-quoted strings (handle """ and ''')
    if ((ch === '"' || ch === "'") && code[i+1] === ch && code[i+2] === ch) {
      const q = ch.repeat(3);
      const end = code.indexOf(q, i + 3);
      const len = end === -1 ? n - i : end - i + 3;
      out.push(`<span class="hl-s">${escHtml(code.slice(i, i + len))}</span>`);
      i += len;
      continue;
    }

    // Single-line strings
    if (ch === '"' || ch === "'") {
      let j = i + 1;
      while (j < n && code[j] !== ch && code[j] !== '\n') {
        if (code[j] === '\\') j++;
        j++;
      }
      if (j < n && code[j] === ch) j++;
      out.push(`<span class="hl-s">${escHtml(code.slice(i, j))}</span>`);
      i = j;
      continue;
    }

    // Comments
    if (ch === '#') {
      const nl = code.indexOf('\n', i);
      const end = nl === -1 ? n : nl;
      out.push(`<span class="hl-c">${escHtml(code.slice(i, end))}</span>`);
      i = end;
      continue;
    }

    // Decorator
    if (ch === '@') {
      const m = code.slice(i).match(/^@[\w.]+/);
      if (m) {
        out.push(`<span class="hl-d">${escHtml(m[0])}</span>`);
        i += m[0].length;
        continue;
      }
    }

    // Word token
    if (/[a-zA-Z_]/.test(ch)) {
      const m = code.slice(i).match(/^\w+/);
      if (m) {
        const w = m[0];
        let span = '';
        if (_PY_KW_ITALIC.has(w)) {
          span = `<span class="hl-ki">${escHtml(w)}</span>`;
        } else if (_PY_KW.has(w)) {
          span = `<span class="hl-k">${escHtml(w)}</span>`;
        } else if (_PY_BI.has(w)) {
          span = `<span class="hl-bi">${escHtml(w)}</span>`;
        } else {
          // Function/class name after def/class keyword
          const pre = code.slice(Math.max(0, i - 12), i);
          if (/\b(?:def|class)\s+$/.test(pre)) {
            span = `<span class="hl-fn">${escHtml(w)}</span>`;
          } else if (i > 0 && code[i - 1] === '.') {
            // Method access
            span = `<span class="hl-fn">${escHtml(w)}</span>`;
          } else {
            span = escHtml(w);
          }
        }
        out.push(span);
        i += w.length;
        continue;
      }
    }

    // Number
    if (/\d/.test(ch) && (i === 0 || !/\w/.test(code[i-1]))) {
      const m = code.slice(i).match(/^\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/);
      if (m) {
        out.push(`<span class="hl-n">${escHtml(m[0])}</span>`);
        i += m[0].length;
        continue;
      }
    }

    out.push(escHtml(ch));
    i++;
  }

  return out.join('');
}

function _hlYaml(code) {
  return code.split('\n').map(line => {
    // Full-line comment
    if (/^\s*#/.test(line)) {
      return `<span class="hl-c">${escHtml(line)}</span>`;
    }
    // key: value  (skip lines that start with - or are pure values)
    const km = line.match(/^(\s*)([\w][\w _-]*)(\s*:\s*)(.*)?$/);
    if (km) {
      const [, ws, key, sep, val = ''] = km;
      let valHtml = '';
      const vt = val.trim();
      if (!vt) {
        // No value — just a key block
        valHtml = '';
      } else if (vt.startsWith('#')) {
        valHtml = `<span class="hl-c">${escHtml(val)}</span>`;
      } else if (vt.startsWith('"') || vt.startsWith("'")) {
        valHtml = `<span class="hl-yv">${escHtml(val)}</span>`;
      } else if (vt === '>' || vt === '|') {
        valHtml = `<span class="hl-k">${escHtml(val)}</span>`;
      } else if (/^(true|false|yes|no|null|~)$/i.test(vt)) {
        valHtml = `<span class="hl-bi">${escHtml(val)}</span>`;
      } else if (/^-?\d/.test(vt)) {
        valHtml = `<span class="hl-n">${escHtml(val)}</span>`;
      } else {
        valHtml = `<span class="hl-yv">${escHtml(val)}</span>`;
      }
      return `${escHtml(ws)}<span class="hl-yk">${escHtml(key)}</span><span style="color:#5c6370">${escHtml(sep)}</span>${valHtml}`;
    }
    // List item marker
    if (/^\s*-\s/.test(line)) {
      const lm = line.match(/^(\s*-\s)(.*)/);
      if (lm) {
        return `<span class="hl-k">${escHtml(lm[1])}</span><span class="hl-yv">${escHtml(lm[2])}</span>`;
      }
    }
    // Continuation / folded block lines (indented plain text)
    if (/^\s+\S/.test(line)) {
      return `<span class="hl-yv">${escHtml(line)}</span>`;
    }
    return escHtml(line);
  }).join('\n');
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function extractResultText(r) {
  if (!r) return '';
  if (r.steps && Array.isArray(r.steps)) {
    const types = r.steps.map(s => (AUTO_CATALOG[s.job_type]?.name || s.job_type)).join(' → ');
    return `${r.steps.length}-step pipeline: ${types}`;
  }
  if (r.columns && r.row_count !== undefined) {
    return `${r.row_count} rows · ${r.columns.length} columns${r.summary ? ' · ' + r.summary.slice(0, 80) : ''}`;
  }
  return r.answer
    || r.story_of_the_day?.title
    || r.summary
    || r.confirmation
    || r.confirmation_text
    || r.error
    || r.message
    || (r.title ? `${r.title} (${r.word_count ?? '?'} words)` : '')
    || '';
}

function escHtml(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function showToast(msg, type = 'error') {
  const banner = document.getElementById('toast-banner');
  banner.textContent = msg;
  banner.style.background = type === 'success' ? 'var(--green)' : 'var(--red)';
  banner.classList.remove('hidden');
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => banner.classList.add('hidden'), type === 'success' ? 2000 : 4000);
}

// ── Init ──────────────────────────────────────────────────────────────────────

document.getElementById('page-prev-btn').addEventListener('click', () => goToPage(runsPage - 1));
document.getElementById('page-next-btn').addEventListener('click', () => goToPage(runsPage + 1));
document.getElementById('page-numbers').addEventListener('click', (e) => {
  const btn = e.target.closest('[data-page-num]');
  if (btn) goToPage(parseInt(btn.dataset.pageNum, 10));
});

// ── History filters ─────────────────────────────────────────────────────────
(function initHistoryFilters() {
  const autoSel = document.getElementById('filter-automation');
  // Populate the automation dropdown from the known job types.
  autoSel.insertAdjacentHTML('beforeend', ALL_TYPES.map(t =>
    `<option value="${t}">${escHtml(AUTO_CATALOG[t]?.name || TYPE_META[t]?.chip || t)}</option>`
  ).join(''));

  const statusSel = document.getElementById('filter-status');
  const afterInp  = document.getElementById('filter-after');
  const beforeInp = document.getElementById('filter-before');

  const apply = () => {
    historyFilters.job_type       = autoSel.value;
    historyFilters.status         = statusSel.value;
    historyFilters.started_after  = afterInp.value;
    historyFilters.started_before = beforeInp.value;
    loadHistory();  // reset=true → offset back to 0
  };

  [autoSel, statusSel, afterInp, beforeInp].forEach(el => el.addEventListener('change', apply));

  document.getElementById('filter-clear-btn').addEventListener('click', () => {
    autoSel.value = statusSel.value = afterInp.value = beforeInp.value = '';
    apply();
  });
})();

// History loads once authenticated (see onAuthenticated); poll only while logged in.
// reset=false → the poll refreshes in place instead of snapping back to page 1.
setInterval(() => { if (CURRENT_USER && !activeEventSource) loadHistory(false); }, 5000);
