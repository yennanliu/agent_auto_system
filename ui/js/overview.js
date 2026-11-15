// Task Overview page: Airflow-style grid of recent runs × steps, per automation type.

import { TYPE_META, AUTO_CATALOG } from './catalog.js';
import { escHtml } from './helpers.js';
import { fmtDur } from './steps.js';

// ══════════════════════════════════════ TASK OVERVIEW ════════════════════════════
// Airflow-style grid grouped by AUTOMATION TYPE: one entry per automation, and its
// grid shows ALL runs of that automation together (columns = runs, rows = steps),
// so runs of the same automation can be compared side by side.

let ovTypes = [];
let ovSelectedType = null;
let ovCurrentRuns = [];   // runs of the currently displayed automation (for drill-down)

export async function loadOverviewPage() {
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
  ovCurrentRuns = runs || [];
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
  // Header run-cell → click shows the full run log (step -1).
  const runHead = runs.map(r =>
    `<div class="ov-cell ov-clickable ov-fill-${r.status}" data-run-id="${r.run_id}" data-step="-1" title="${tip(r)} · click for full log">${OV_ICON[r.status] || ''}</div>`
  ).join('');

  const rows = task_names.map((tname, ri) => {
    const cells = runs.map(r => {
      const status = r.steps[ri] ? r.steps[ri].status : 'pending';
      return `<div class="ov-cell ov-clickable ov-fill-${status}" data-run-id="${r.run_id}" data-step="${ri}" title="${escHtml(tname)} · ${escHtml(r.job_name)} · run #${r.run_id} · ${status} · click for log">${OV_ICON[status] || ''}</div>`;
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
      <span class="ov-legend-hint">— click any cell to see that step's log</span>
    </div>
    <div id="ov-step-log" class="ov-step-log hidden"></div>`;
}

document.getElementById('ov-joblist').addEventListener('click', (e) => {
  const btn = e.target.closest('[data-job-type]');
  if (btn) selectOverviewType(btn.dataset.jobType);
});

// Drill-down: click a grid cell → show that step's log below the grid.
document.getElementById('ov-grid-wrap').addEventListener('click', (e) => {
  const cell = e.target.closest('.ov-cell[data-run-id]');
  if (!cell) return;
  document.querySelectorAll('#ov-grid-wrap .ov-cell.selected').forEach(c => c.classList.remove('selected'));
  cell.classList.add('selected');
  showStepLog(parseInt(cell.dataset.runId, 10), parseInt(cell.dataset.step, 10));
});

async function showStepLog(runId, stepIdx) {
  const panel = document.getElementById('ov-step-log');
  if (!panel) return;
  panel.classList.remove('hidden');
  panel.innerHTML = '<div class="loading-state">Loading…</div>';
  const run = ovCurrentRuns.find(r => r.run_id === runId);
  const label = run ? `${escHtml(run.job_name)} · run #${runId}` : `run #${runId}`;
  try {
    const resp = await fetch(`/api/runs/${runId}/steps`);
    if (!resp.ok) throw new Error('Failed to load step log');
    const data = await resp.json();
    let title, logs, status;
    if (stepIdx < 0) {
      title = 'Full run log';
      status = data.status;
      logs = (data.steps || []).flatMap(s => s.logs || []);
    } else {
      const st = (data.steps || [])[stepIdx];
      title = st ? st.name : `step ${stepIdx + 1}`;
      status = st ? st.status : 'pending';
      logs = st ? (st.logs || []) : [];
    }
    const body = logs.length
      ? `<ul class="ov-step-log-list">${logs.map(l =>
          `<li><span class="ov-log-ts">${escHtml(l.ts || '')}</span> ${escHtml(l.msg || '')}</li>`).join('')}</ul>`
      : '<div class="empty-state" style="padding:1rem">No log entries for this step.</div>';
    panel.innerHTML = `
      <div class="ov-step-log-head">
        <span class="ov-cell ov-fill-${status}" style="cursor:default">${OV_ICON[status] || ''}</span>
        <strong>${escHtml(title)}</strong>
        <span class="muted">${label}</span>
        <button class="btn btn-ghost btn-sm" id="ov-step-log-close" style="margin-left:auto">✕ Close</button>
      </div>${body}`;
    document.getElementById('ov-step-log-close').addEventListener('click', () => {
      panel.classList.add('hidden');
      document.querySelectorAll('#ov-grid-wrap .ov-cell.selected').forEach(c => c.classList.remove('selected'));
    });
  } catch (err) {
    panel.innerHTML = `<div class="empty-state">${escHtml(err.message)}</div>`;
  }
}
