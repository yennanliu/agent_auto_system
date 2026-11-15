// Runs: history table (render, pagination, filters, detail tabs), delete / abort /
// bulk delete, elapsed timers, leads table, copy result, background poll.

import { ALL_TYPES, TYPE_META, AUTO_CATALOG, FLOW_STEPS } from './catalog.js';
import { extractResultText, escHtml, showToast } from './helpers.js';
import { confirmDialog } from './confirm.js';
import { renderStepGraph } from './steps.js';
import { CURRENT_USER } from './auth.js';
import { rerun } from './jobs.js';
import { activeEventSource } from './progress.js';

// ── State ─────────────────────────────────────────────────────────────────────

export let cachedRuns         = [];
const elapsedTimers    = new Map();
let selectedRunIds     = new Set();
let runsPage           = 0;    // 0-indexed current page
let runsTotal          = 0;    // total rows matching current filters
const RUNS_PAGE_SIZE   = 50;
const historyFilters   = { job_type: '', status: '', started_after: '', started_before: '' };

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

// ── Abort a running run ───────────────────────────────────────────────────────

async function abortRun(runId, btn) {
  const run = cachedRuns.find(r => r.id === runId);
  if (run && run.status !== 'pending' && run.status !== 'running') {
    showToast('Run is no longer active', 'error');
    return;
  }
  const ok = await confirmDialog('Abort Run', `Force-cancel run #${runId}? The automation stops immediately and is marked failed.`);
  if (!ok) return;
  if (btn) { btn.disabled = true; btn.textContent = 'Aborting…'; }
  try {
    const resp = await fetch(`/api/runs/${runId}/cancel`, { method: 'POST' });
    if (resp.status === 409) { showToast('Run is no longer active', 'error'); await loadHistory(); return; }
    if (!resp.ok) throw new Error('Abort failed');
    const { cancelled } = await resp.json();
    showToast(cancelled ? `Run #${runId} aborted` : `Run #${runId} was already finishing`, 'success');
    await loadHistory();
  } catch (err) {
    if (btn) { btn.disabled = false; btn.textContent = '⨯ Abort'; }
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
      case 'abort':  abortRun(runId, actionEl); return;
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

export function scrollToRun(runId) {
  setTimeout(() => {
    const row = document.querySelector(`tr[data-run-id="${runId}"]`);
    if (!row) return;
    row.scrollIntoView({ behavior: 'smooth', block: 'center' });
    row.classList.add('row-highlight');
    setTimeout(() => row.classList.remove('row-highlight'), 2000);
  }, 120);
}

// ── History table ─────────────────────────────────────────────────────────────

// `reset` sends the view back to page 1 (used by filters / after a run finishes).
export async function loadHistory(reset = true) {
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
            ${isActive
              ? `<button class="btn-abort" data-action="abort" data-run-id="${run.id}" title="Abort this running automation">⨯ Abort</button>`
              : `<button class="${rerunCls}" data-action="rerun" data-job-id="${run.job_id}" data-run-id="${run.id}" title="Re-run">${rerunLabel}</button>`}
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

export function stopElapsedTimer(runId) {
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
