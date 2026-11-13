// Flow step inference + the live step graph (single flow and pipeline).

import { FLOW_STEPS } from './catalog.js';
import { escHtml } from './helpers.js';

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

export function fmtDur(secs) {
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

export function renderStepGraph(jobType, logs, finalStatus) {
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
