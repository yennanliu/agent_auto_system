// New Run modal: open/close, provider→model dropdown, job-type cards, manifest-driven
// generic fields.

import { modal, runForm } from './dom.js';
import { LLM_MODELS, MANIFEST, MANIFEST_BY_TYPE } from './catalog.js';
import { escHtml, showToast } from './helpers.js';
import { visibleTypeSet } from './navigation.js';
import { loadTw104CoverLetters } from './forms/tw104-covers.js';
import { renderPhClassified } from './forms/profit-health.js';
import { addPipelineStep } from './forms/pipeline.js';
import { rerunCtx, setRerunMode } from './jobs.js';

export function updateModelOptions() {
  const provider = document.getElementById('llm-provider').value;
  const modelSel = document.getElementById('llm-model');
  const models = LLM_MODELS[provider] || LLM_MODELS.openai;
  modelSel.innerHTML = models.map(([v, l]) => `<option value="${v}">${l}</option>`).join('');
}

// ── Modal open/close ──────────────────────────────────────────────────────────

export function openModal(preselect) {
  if (rerunCtx) {
    // Leaving a cancelled re-run: drop its pre-filled values.
    runForm.reset();
    document.getElementById('pipeline-steps-list').innerHTML = '';
    updateModelOptions();
  }
  setRerunMode(null);
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
export function closeModalFn() { modal.classList.add('hidden'); }

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

export function renderTypeCards() {
  const grid = document.getElementById('type-grid');
  if (!grid || !MANIFEST.length) return;
  grid.innerHTML = MANIFEST.map(a => `
    <div class="type-card" data-type="${escHtml(a.job_type)}">
      <div class="type-card-header"><span class="type-card-icon">${escHtml(a.icon || '⚙️')}</span><span class="type-card-name">${escHtml(a.name)}</span></div>
      <div class="type-card-desc">${escHtml(a.desc || '')}</div>
    </div>`).join('');
}

// Render a manifest automation's inputs into #generic-fields (data-field per input).
function renderGenericFields(entry) {
  const host = document.getElementById('generic-fields');
  if (!host) return;
  const rows = (entry.fields || []).map(f => {
    const req = f.required ? ' <span style="color:var(--red)">*</span>' : '';
    const attrs = `data-field="${escHtml(f.name)}"`;
    let input;
    if (f.type === 'select') {
      input = `<select ${attrs}>${(f.options || []).map(([v, l]) =>
        `<option value="${escHtml(v)}">${escHtml(l)}</option>`).join('')}</select>`;
    } else if (f.type === 'textarea') {
      input = `<textarea ${attrs} placeholder="${escHtml(f.placeholder || '')}">${escHtml(f.default != null ? String(f.default) : '')}</textarea>`;
    } else if (f.type === 'checkbox') {
      input = `<input type="checkbox" ${attrs} ${f.default ? 'checked' : ''} style="width:auto;margin:0" />`;
    } else if (f.type === 'number') {
      const mn = f.min != null ? ` min="${f.min}"` : '';
      const mx = f.max != null ? ` max="${f.max}"` : '';
      const dv = f.default != null ? ` value="${escHtml(String(f.default))}"` : '';
      input = `<input type="number" ${attrs}${mn}${mx}${dv} />`;
    } else {
      const dv = f.default != null ? ` value="${escHtml(String(f.default))}"` : '';
      input = `<input type="${f.type === 'url' ? 'url' : 'text'}" ${attrs} placeholder="${escHtml(f.placeholder || '')}"${dv} />`;
    }
    const help = f.help
      ? `<div style="font-size:0.72rem;color:var(--text-muted);margin-top:0.3rem">${escHtml(f.help)}</div>` : '';
    return `<div class="field"><label>${escHtml(f.label)}${req}</label>${input}${help}</div>`;
  }).join('');
  const note = entry.help_note
    ? `<div style="font-size:0.75rem;color:var(--text-muted);background:var(--accent-soft);border:1px solid rgba(47,107,255,0.2);border-radius:10px;padding:0.6rem 0.75rem;margin-bottom:0.5rem">${entry.help_note}</div>`
    : '';
  host.innerHTML = rows + note;
}

// Collect {payload, jobName} from a manifest form, or null if a required field is empty.
export function collectGenericPayload(entry) {
  const host = document.getElementById('generic-fields');
  const payload = {};
  for (const f of (entry.fields || [])) {
    const el = host.querySelector(`[data-field="${f.name}"]`);
    if (!el) continue;
    let val;
    if (f.type === 'checkbox') { val = el.checked; }
    else if (f.type === 'number') { const n = parseInt(el.value, 10); val = isNaN(n) ? f.default : n; }
    else { val = el.value.trim(); }
    if (f.required && (val === '' || val == null)) {
      showToast(`${f.label.replace(/\s*\(.*\)$/, '')} is required`, 'error');
      return null;
    }
    if (f.type !== 'checkbox' && (val === '' || val == null)) {
      // A cleared field that has a default is sent as "" so the server default
      // doesn't silently come back; other empty optionals are omitted.
      if (f.type !== 'number' && f.default != null && f.default !== '') payload[f.name] = '';
      continue;
    }
    payload[f.name] = val;
  }
  let jobName = entry.name;
  if (entry.name_template) {
    jobName = entry.name_template.replace(/\{(\w+)\}/g, (_, k) => String(payload[k] ?? '')).trim();
  }
  return { payload, jobName };
}

export function selectJobType(type) {
  if (type !== 'pipeline') {
    document.getElementById('pipeline-steps-list').innerHTML = '';
    document.querySelector('#modal .modal').classList.remove('modal-wide');
  }
  document.querySelectorAll('.type-card')
    .forEach(c => c.classList.toggle('active', c.dataset.type === type));
  document.getElementById('job-type').value = type;

  // Show one form: the manifest-rendered generic form, or the bespoke
  // #fields-<type> escape hatch for custom_ui automations.
  document.querySelectorAll('#run-form [id^="fields-"]').forEach(el => el.classList.add('hidden'));
  const generic = document.getElementById('generic-fields');
  const entry = MANIFEST_BY_TYPE[type];
  if (entry && !entry.custom_ui) {
    renderGenericFields(entry);
    generic.classList.remove('hidden');
  } else {
    if (generic) { generic.classList.add('hidden'); generic.innerHTML = ''; }
    document.getElementById(`fields-${type}`)?.classList.remove('hidden');
  }

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
