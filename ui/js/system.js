// System page (agents / tasks / tools / crews catalog) and Automations page.

import { AUTO_CATALOG } from './catalog.js';
import { escHtml } from './helpers.js';
import { highlightCode } from './highlight.js';
import { visibleTypeSet } from './navigation.js';
import { openModal } from './run-modal.js';

let systemData         = null;
let systemCategory     = 'agents';

// ── System page ───────────────────────────────────────────────────────────────

export async function loadSystemPage() {
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

export function renderAutomationsPage() {
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
