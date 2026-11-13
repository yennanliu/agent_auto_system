// Bespoke run form: pipeline builder (ordered steps, each with its own fields).

import { escHtml } from '../helpers.js';

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
         &nbsp;·&nbsp; list → CSV: <code style="font-family:ui-monospace,monospace">{{steps.${stepIdx - 1}.result.leads[].email}}</code>
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
    case 'email_sender': {
      // Only suggest a prior-step token when a prior step actually exists.
      const toPh = stepIdx > 0
        ? `recipient@example.com — or {{steps.${stepIdx - 1}.result.leads[].email}} to email everyone a prior step collected`
        : 'recipient@example.com, another@example.com';
      const bodyPh = stepIdx > 0
        ? `Email body or {{steps.${stepIdx - 1}.result.summary}}`
        : 'Email body';
      return `${tipHtml}
        <div class="field"><label>To</label><textarea class="ps-field" data-field="to" rows="2" placeholder="${toPh}"></textarea></div>
        <div class="field"><label>CC (optional)</label><input type="text" class="ps-field" data-field="cc" placeholder="cc@example.com" /></div>
        <div class="field"><label>Subject</label><input type="text" class="ps-field" data-field="subject" placeholder="Subject line" /></div>
        <div class="field"><label>Body</label><textarea class="ps-field" data-field="body" rows="3" placeholder="${bodyPh}"></textarea></div>`;
    }
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

export function addPipelineStep(jobType = 'x_scraper') {
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

export function collectPipelineSteps() {
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
