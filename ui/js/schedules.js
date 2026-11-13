// Schedules page: cron-scheduled jobs, edit / unschedule.

import { TYPE_META } from './catalog.js';
import { escHtml, showToast } from './helpers.js';
import { confirmDialog } from './confirm.js';
import { navigate } from './navigation.js';
import { runJobNow } from './jobs.js';

// ══════════════════════════════════════ SCHEDULES ════════════════════════════════

export async function loadSchedulesPage() {
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
  if (runBtn) { navigate('activity'); runJobNow(parseInt(runBtn.dataset.schedRun, 10)); return; }
  const editBtn = e.target.closest('[data-sched-edit]');
  if (editBtn) { editSchedule(parseInt(editBtn.dataset.schedEdit, 10), editBtn.dataset.cron); return; }
  const unsetBtn = e.target.closest('[data-sched-unset]');
  if (unsetBtn) { unscheduleJob(parseInt(unsetBtn.dataset.schedUnset, 10), unsetBtn.dataset.name); return; }
});
