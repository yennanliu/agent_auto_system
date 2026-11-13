// Live run progress: progress panel, SSE stream, in-place row updates.

import { progressPanel, progressLog, progressJobName, progressPulse } from './dom.js';
import { extractResultText, escHtml } from './helpers.js';
import { renderStepGraph } from './steps.js';
import { loadHistory, stopElapsedTimer } from './runs.js';

export let activeEventSource  = null;
let sseStartTime       = null;
let activeJobType      = null;
let activeLogs         = [];

// ── Progress panel ────────────────────────────────────────────────────────────

export function showProgress(jobName, jobType) {
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

export function startSSE(runId) {
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
