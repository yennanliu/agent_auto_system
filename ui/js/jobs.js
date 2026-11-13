// Jobs: run-form submit (payload building per job type), scheduled-job creation,
// triggering a run, re-run / edit-config prefill, run-now.

import { modal, runForm } from './dom.js';
import { AUTO_CATALOG, MANIFEST_BY_TYPE } from './catalog.js';
import { escHtml, showToast } from './helpers.js';
import { navigate, visibleTypeSet } from './navigation.js';
import {
  updateModelOptions, closeModalFn, collectGenericPayload, selectJobType,
} from './run-modal.js';
import {
  leadSourceAcceptsQuery, selectedAssociations, syncLeadSourceOptions,
} from './forms/email-collect.js';
import { _tw104CoverCount } from './forms/tw104-covers.js';
import { classifyCsv } from './forms/profit-health.js';
import { addPipelineStep, collectPipelineSteps } from './forms/pipeline.js';
import { cachedRuns, scrollToRun, loadHistory } from './runs.js';
import { showProgress, startSSE } from './progress.js';

// ── Form submit ───────────────────────────────────────────────────────────────

runForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const jobType = document.getElementById('job-type').value;
  let payload, jobName;

  // Manifest-driven automations (custom_ui=false) collect their payload
  // generically from the rendered form — no per-type branch needed here.
  const _m = MANIFEST_BY_TYPE[jobType];
  if (_m && !_m.custom_ui) {
    const collected = collectGenericPayload(_m);
    if (!collected) return;  // a required field was empty (toast already shown)
    payload = collected.payload;
    jobName = collected.jobName;
  } else if (jobType === 'google_form_fill') {
    const company = document.getElementById('company-name').value.trim();
    if (!company) { showToast('Company name is required', 'error'); return; }
    payload = {
      company_name: company,
      company_size: document.getElementById('company-size').value,
      ai_problem:   document.getElementById('ai-problem').value.trim(),
    };
    jobName = `Form: ${company}`;
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

  } else if (jobType === 'profit_health_check') {
    const picked = Array.from(document.getElementById('ph-files').files || []);
    const reuseUpload = !picked.length && rerunCtx?.jobType === 'profit_health_check' && rerunCtx.extras?.upload_id;
    if (!picked.length && !reuseUpload) { showToast('請選取蝦皮 CSV 檔（可一次全選）', 'error'); return; }
    if (reuseUpload) {
      payload = { upload_id: rerunCtx.extras.upload_id };
      jobName = rerunCtx.jobName;
    } else {
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
    }

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

  } else if (jobType === 'linkedin_apply') {
    const keywords = document.getElementById('li-keywords').value.trim();
    if (!keywords) { showToast('Job keywords are required (e.g. Software Engineer)', 'error'); return; }
    const location = document.getElementById('li-location').value.trim();
    const nationality = document.getElementById('li-nationality').value.trim();
    const phone = document.getElementById('li-phone').value.trim();
    const taskFilter = document.getElementById('li-filter').value.trim();
    const liYears = parseInt(document.getElementById('li-years').value, 10);
    payload = {
      keywords,
      years_experience: Number.isNaN(liYears) ? 3 : liYears,  // preserve an explicit 0
      max_applications: parseInt(document.getElementById('li-max').value, 10) || 5,
      max_pages: parseInt(document.getElementById('li-max-pages').value, 10) || 10,
      remote: document.getElementById('li-remote').checked,
      dry_run: document.getElementById('li-dry-run').checked,
      ...(location ? { location } : {}),
      ...(nationality ? { nationality } : {}),
      ...(phone ? { phone } : {}),
      ...(taskFilter ? { task_filter: taskFilter } : {}),
    };
    jobName = `LinkedIn 應徵: ${keywords}${payload.dry_run ? ' (dry-run)' : ''}`;

  } else if (jobType === 'email_collect') {
    const query = document.getElementById('lead-query').value.trim();
    if (!query) { showToast('Business query is required', 'error'); return; }
    const region   = document.getElementById('lead-region').value.trim();
    const industry = document.getElementById('lead-industry').value.trim();
    const offer    = document.getElementById('lead-offer').value.trim();
    const limit    = parseInt(document.getElementById('lead-limit').value, 10) || 15;
    const sources = [];
    if (document.getElementById('lead-src-maps').checked)  sources.push('maps');
    if (document.getElementById('lead-src-assoc').checked) sources.push('association');
    if (document.getElementById('lead-src-gov').checked)   sources.push('govbiz');
    if (!sources.length) { showToast('Pick at least one discovery source', 'error'); return; }
    const associations = selectedAssociations();
    if (sources.includes('association') && !associations.length) {
      showToast('Pick at least one 公會 directory (or add one by URL)', 'error'); return;
    }
    // Only block when the run cannot possibly return anything — i.e. not one
    // chosen source can use this query. With Maps on, English is legitimate;
    // so is an F###### 營業項目 code when 經濟部 is on.
    if (!sources.some(s => leadSourceAcceptsQuery(s, query))) {
      showToast(`"${query}" has no Chinese characters — 公會 名錄 / 經濟部 登記 would find 0 businesses. `
                + 'Use a Chinese keyword (行銷, 科技, 設計), an F###### 營業項目 code, or also tick Google Maps.',
                'error');
      return;
    }
    payload = {
      query, limit, sources,
      smtp_check: document.getElementById('lead-smtp').checked,
      include_social: document.getElementById('lead-social').checked,
      gcis_enrich: document.getElementById('lead-gcis-enrich').checked,
      ...(associations.length ? { associations } : {}),
      ...(sources.includes('govbiz')
        ? { resolve_missing_websites: document.getElementById('lead-resolve-sites').checked }
        : {}),
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

  // Re-run: carry over payload keys the form doesn't own (set via API, upload ids…).
  // Form-owned keys always win, so a field the user cleared stays cleared.
  if (rerunCtx && rerunCtx.jobType === jobType && rerunCtx.extras) {
    payload = { ...rerunCtx.extras, ...payload };
  }

  // Attach LLM config to payload
  payload.llm_provider = document.getElementById('llm-provider').value;
  payload.llm_model    = document.getElementById('llm-model').value;

  const schedule = document.getElementById('job-schedule').value.trim();

  closeModalFn();
  setRerunMode(null);
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
// Re-run opens the run form pre-filled with the previous job's settings so the
// user can review/edit them before executing. Submitting creates a NEW job with
// the (possibly edited) payload — the original job and its history are untouched.

// Payload key → form element id for each hand-written (custom_ui) form. Keys
// listed here are owned by the form; any other key in the old payload (e.g.
// profit_health_check's upload_id, or fields set via the API) is carried over.
const RERUN_FIELD_MAP = {
  google_form_fill: { company_name: 'company-name', company_size: 'company-size', ai_problem: 'ai-problem' },
  x_scraper:        { username: 'x-username', limit: 'x-limit' },
  email_sender:     { to: 'email-to', cc: 'email-cc', subject: 'email-subject', body: 'email-body' },
  tasker_apply: {
    category_ids: 'tasker-categories', min_charge: 'tasker-min', max_charge: 'tasker-max',
    max_cases: 'tasker-max-cases', dry_run: 'tasker-dry-run',
    proposal_template: 'tasker-template', task_filter: 'tasker-filter',
  },
  tw104_apply: {
    keyword: 'tw104-keyword', area: 'tw104-area', max_applications: 'tw104-max',
    max_pages: 'tw104-max-pages', remote: 'tw104-remote', part_time: 'tw104-part-time',
    dry_run: 'tw104-dry-run', cover_letter: 'tw104-cover', task_filter: 'tw104-filter',
  },
  linkedin_apply: {
    keywords: 'li-keywords', location: 'li-location', nationality: 'li-nationality',
    phone: 'li-phone', years_experience: 'li-years', max_applications: 'li-max',
    max_pages: 'li-max-pages', remote: 'li-remote', dry_run: 'li-dry-run', task_filter: 'li-filter',
  },
  email_collect: {
    query: 'lead-query', region: 'lead-region', industry: 'lead-industry', offer: 'lead-offer',
    limit: 'lead-limit', smtp_check: 'lead-smtp', include_social: 'lead-social',
    gcis_enrich: 'lead-gcis-enrich', resolve_missing_websites: 'lead-resolve-sites',
  },
};
// Payload keys handled specially (not via RERUN_FIELD_MAP) but still form-owned.
const RERUN_SPECIAL_KEYS = {
  email_collect: ['sources', 'associations'],
  pipeline: ['steps'],
};

// Active re-run context: { jobId, jobName, jobType, extras } or null.
export let rerunCtx = null;

export function setRerunMode(ctx) {
  rerunCtx = ctx;
  const banner = document.getElementById('rerun-banner');
  const title  = document.getElementById('modal-title');
  const submit = document.getElementById('run-submit-btn');
  document.getElementById('type-grid').classList.toggle('hidden', !!ctx);
  if (ctx) {
    title.textContent = 'Re-run Automation';
    submit.textContent = 'Execute ▶';
    banner.innerHTML = `Settings copied from <strong>${escHtml(ctx.jobName)}</strong>. `
      + 'Review or edit them, then click <strong>Execute</strong>. '
      + 'The original job is left unchanged.';
    banner.classList.remove('hidden');
  } else {
    title.textContent = 'New Automation Run';
    submit.textContent = 'Run ▶';
    banner.classList.add('hidden');
    banner.innerHTML = '';
  }
}

// Set one form control from a payload value (checkbox → checked, else value).
function _setControl(el, value) {
  if (!el) return;
  if (el.type === 'checkbox') { el.checked = !!value; return; }
  if (el.type === 'file') return;
  const str = value == null ? '' : String(value);
  if (el.tagName === 'SELECT' && str && ![...el.options].some(o => o.value === str)) {
    el.add(new Option(str, str));  // keep a value the current option list doesn't offer
  }
  el.value = str;
}

// Fill the (already reset + selected) run form from a stored payload. Returns the
// payload keys the form does not own, to be merged back in on submit.
function prefillRunForm(jobType, payload) {
  const owned = new Set(['llm_provider', 'llm_model', ...(RERUN_SPECIAL_KEYS[jobType] || [])]);
  const entry = MANIFEST_BY_TYPE[jobType];

  if (entry && !entry.custom_ui) {
    const host = document.getElementById('generic-fields');
    for (const f of (entry.fields || [])) {
      owned.add(f.name);
      if (f.name in payload) _setControl(host.querySelector(`[data-field="${f.name}"]`), payload[f.name]);
    }
  } else if (RERUN_FIELD_MAP[jobType]) {
    for (const [key, id] of Object.entries(RERUN_FIELD_MAP[jobType])) {
      owned.add(key);
      const el = document.getElementById(id);
      if (key in payload) _setControl(el, payload[key]);
      // The form omits empty optional text fields, so an absent key means "was
      // blank" — clear any HTML default (e.g. LinkedIn location "Taiwan").
      else if (el && el.type !== 'checkbox' && el.type !== 'number' && el.tagName !== 'SELECT') el.value = '';
    }
  }

  if (jobType === 'email_collect') {
    const sources = Array.isArray(payload.sources) ? payload.sources : ['maps'];
    document.getElementById('lead-src-maps').checked  = sources.includes('maps');
    document.getElementById('lead-src-assoc').checked = sources.includes('association');
    document.getElementById('lead-src-gov').checked   = sources.includes('govbiz');
    const assoc = Array.isArray(payload.associations) ? payload.associations : [];
    const slugBoxes = [...document.querySelectorAll('.lead-assoc-slug')];
    const known = new Set(slugBoxes.map(c => c.value));
    // Always sync — an empty list must clear the default-checked slug, or the
    // re-run would submit associations the original job never had.
    slugBoxes.forEach(c => { c.checked = assoc.includes(c.value); });
    document.getElementById('lead-associations').value = assoc.filter(a => !known.has(a)).join('\n');
    syncLeadSourceOptions();
  } else if (jobType === 'tw104_apply') {
    _tw104CoverCount();
  } else if (jobType === 'pipeline') {
    const list = document.getElementById('pipeline-steps-list');
    const steps = Array.isArray(payload.steps) ? payload.steps : [];
    if (steps.length) {
      list.innerHTML = '';
      steps.forEach(step => {
        addPipelineStep(step.job_type);
        const card = list.querySelector('.pipeline-step-card:last-of-type');
        const sp = step.payload || {};
        card?.querySelectorAll('.ps-field').forEach(el => {
          if (el.dataset.field in sp) _setControl(el, sp[el.dataset.field]);
        });
      });
    }
  }

  // LLM provider/model
  const provSel = document.getElementById('llm-provider');
  if (payload.llm_provider) _setControl(provSel, payload.llm_provider);
  updateModelOptions();
  if (payload.llm_model) _setControl(document.getElementById('llm-model'), payload.llm_model);

  return Object.fromEntries(Object.entries(payload).filter(([k]) => !owned.has(k)));
}

export async function rerun(jobId) {
  let job;
  try {
    const resp = await fetch(`/api/jobs/${jobId}`);
    if (!resp.ok) throw new Error('Could not load the previous run settings');
    job = await resp.json();
  } catch (err) {
    showToast(err.message, 'error');
    return;
  }
  if (!visibleTypeSet().has(job.job_type)) {
    showToast('This automation is not enabled for your account', 'error');
    return;
  }
  let payload = {};
  try { payload = typeof job.payload === 'string' ? JSON.parse(job.payload) : (job.payload || {}); }
  catch { payload = {}; }

  runForm.reset();
  document.getElementById('pipeline-steps-list').innerHTML = '';
  selectJobType(job.job_type);
  const extras = prefillRunForm(job.job_type, payload);
  setRerunMode({ jobId, jobName: job.name, jobType: job.job_type, extras });
  modal.classList.remove('hidden');
}

// Run a stored job again exactly as-is (used by Schedules → "Run now").
export async function runJobNow(jobId) {
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
