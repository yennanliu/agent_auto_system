// Admin page: users, API keys, enabled automations, eval judge, browser sessions,
// custom (no-code) automations.

import { ALL_TYPES, AUTO_CATALOG, loadManifest } from './catalog.js';
import { escHtml, showToast } from './helpers.js';
import { confirmDialog } from './confirm.js';
import { CURRENT_USER } from './auth.js';
import { renderTypeCards } from './run-modal.js';

// ── Admin page ────────────────────────────────────────────────────────────
// All endpoints are admin-only server-side; the UI just renders/edits.
let ADMIN_TAB = 'users';

export function loadAdminPage() {
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
  document.getElementById('admin-custom').hidden = tab !== 'custom';
  document.getElementById('admin-sessions').hidden = tab !== 'sessions';
  if (tab === 'users') renderAdminUsers();
  if (tab === 'keys')  renderAdminKeys();
  if (tab === 'judge') renderAdminJudge();
  if (tab === 'autos') renderAdminAutos();
  if (tab === 'custom') renderAdminCustom();
  if (tab === 'sessions') renderAdminSessions();
}

// ── Custom (no-code) automations — Phase 3G ─────────────────────────────────────
async function renderAdminCustom() {
  const el = document.getElementById('admin-custom');
  let rows = [];
  try {
    const resp = await fetch('/api/admin/custom-automations');
    rows = resp.ok ? await resp.json() : [];
  } catch (_) { /* show the empty form anyway */ }

  const list = rows.length ? rows.map(r => `
    <tr>
      <td>${escHtml(r.icon)} <strong>${escHtml(r.name)}</strong><div class="muted" style="font-size:0.75rem">custom:${escHtml(r.slug)}</div></td>
      <td>${escHtml(r.description || '—')}</td>
      <td>${(r.fields || []).map(f => escHtml(f.name)).join(', ') || '—'}</td>
      <td><span class="pill ${r.enabled ? 'pill-ok' : ''}">${r.enabled ? 'enabled' : 'disabled'}</span></td>
      <td style="text-align:right">
        <button class="btn-sm" data-cust-toggle="${r.id}" data-enabled="${r.enabled}">${r.enabled ? 'Disable' : 'Enable'}</button>
        <button class="btn-sm" style="color:var(--red)" data-cust-del="${r.id}">Delete</button>
      </td>
    </tr>`).join('') : `<tr><td colspan="5" class="muted" style="padding:1rem">No custom automations yet — create one below.</td></tr>`;

  el.innerHTML = `
    <p class="muted" style="margin-bottom:0.75rem">
      Admin-authored, no-code automations. They run as a <strong>single LLM agent with no tools</strong>
      (it only transforms the inputs you declare into JSON) through the normal harness — validated, scored, and cost-tracked.
      They appear in the picker automatically after a page reload.
    </p>
    <table class="admin-table" style="margin-bottom:1.5rem">
      <thead><tr><th>Automation</th><th>Description</th><th>Inputs</th><th>Status</th><th></th></tr></thead>
      <tbody>${list}</tbody>
    </table>

    <h3 style="font-size:0.95rem;margin-bottom:0.5rem">New custom automation</h3>
    <div class="field" style="display:flex;gap:0.5rem">
      <div style="flex:3"><label>Name</label><input type="text" id="cust-name" placeholder="e.g. Tagline Writer" /></div>
      <div style="flex:1"><label>Icon</label><input type="text" id="cust-icon" value="✨" maxlength="4" /></div>
      <div style="flex:1"><label>Temp</label><input type="number" id="cust-temp" value="0.3" min="0" max="1" step="0.1" /></div>
    </div>
    <div class="field"><label>Short description</label><input type="text" id="cust-desc" placeholder="Shown on the picker tile" /></div>
    <div class="field"><label>Instructions (what the agent should produce)</label>
      <textarea id="cust-instructions" rows="4" placeholder="e.g. Write a punchy one-line marketing tagline for the given product. Return JSON: {&quot;tagline&quot;: &quot;…&quot;}"></textarea></div>
    <div class="field"><label>Expected output (optional hint for the grader)</label>
      <input type="text" id="cust-output" placeholder="e.g. A JSON object with a 'tagline' string" /></div>
    <div class="field">
      <label>Inputs</label>
      <div id="cust-fields"></div>
      <button type="button" class="btn btn-ghost btn-sm" id="cust-add-field" style="margin-top:0.4rem">+ Add input</button>
    </div>
    <button type="button" class="btn btn-primary" id="cust-create">Create automation</button>
  `;

  const fieldsBox = el.querySelector('#cust-fields');
  const addField = (name = '', label = '') => {
    const row = document.createElement('div');
    row.className = 'cust-field-row';
    row.style = 'display:flex;gap:0.5rem;margin-bottom:0.4rem';
    row.innerHTML = `
      <input type="text" class="cf-name" placeholder="key (e.g. product)" value="${escHtml(name)}" style="flex:1" />
      <input type="text" class="cf-label" placeholder="Label" value="${escHtml(label)}" style="flex:1" />
      <select class="cf-type" style="flex:0 0 auto"><option value="text">text</option><option value="textarea">textarea</option><option value="number">number</option></select>
      <button type="button" class="btn-sm cf-del">✕</button>`;
    row.querySelector('.cf-del').onclick = () => row.remove();
    fieldsBox.appendChild(row);
  };
  addField('product', 'Product');
  el.querySelector('#cust-add-field').onclick = () => addField();

  el.querySelectorAll('[data-cust-del]').forEach(b => b.onclick = async () => {
    if (!await confirmDialog('Delete Custom Automation', 'Delete this custom automation?')) return;
    await fetch(`/api/admin/custom-automations/${b.dataset.custDel}`, { method: 'DELETE' });
    renderAdminCustom();
  });
  el.querySelectorAll('[data-cust-toggle]').forEach(b => b.onclick = async () => {
    await fetch(`/api/admin/custom-automations/${b.dataset.custToggle}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: b.dataset.enabled !== 'true' }),
    });
    renderAdminCustom();
  });

  el.querySelector('#cust-create').onclick = async () => {
    const fields = [...fieldsBox.querySelectorAll('.cust-field-row')].map(r => ({
      name: r.querySelector('.cf-name').value.trim(),
      label: r.querySelector('.cf-label').value.trim(),
      type: r.querySelector('.cf-type').value,
      required: true,
    })).filter(f => f.name);
    const body = {
      name: document.getElementById('cust-name').value.trim(),
      icon: document.getElementById('cust-icon').value.trim() || '✨',
      description: document.getElementById('cust-desc').value.trim(),
      instructions: document.getElementById('cust-instructions').value.trim(),
      output_hint: document.getElementById('cust-output').value.trim(),
      temperature: parseFloat(document.getElementById('cust-temp').value) || 0.3,
      fields,
    };
    if (!body.name || !body.instructions) { showToast('Name and instructions are required', 'error'); return; }
    const resp = await fetch('/api/admin/custom-automations', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    if (resp.status === 201) {
      showToast('Custom automation created — reload to see it in the picker', 'success');
      await loadManifest(); renderTypeCards();
      renderAdminCustom();
    } else {
      const e = await resp.json().catch(() => ({}));
      showToast(e.detail || 'Create failed', 'error');
    }
  };
}

// Human "3 days ago" from an epoch-seconds mtime.
function relAge(seconds) {
  if (seconds == null) return '—';
  const d = Math.floor(seconds / 86400), h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d > 0) return `${d}d ${h}h ago`;
  if (h > 0) return `${h}h ${m}m ago`;
  if (m > 0) return `${m}m ago`;
  return 'just now';
}

let SESSION_POLL = null;

async function renderAdminSessions() {
  // A queued SESSION_POLL can fire after the operator switches tabs — skip it.
  if (ADMIN_TAB !== 'sessions') return;
  const el = document.getElementById('admin-sessions');
  if (!el.dataset.rendered) el.innerHTML = '<div class="loading-state">Loading…</div>';
  let data;
  try {
    const resp = await fetch('/api/sessions');
    if (!resp.ok) throw new Error();
    data = await resp.json();
  } catch (err) {
    el.innerHTML = '<div class="loading-state">Failed to load.</div>';
    return;
  }
  el.dataset.rendered = '1';

  const disabledNote = data.login_enabled ? '' :
    `<div class="muted" style="margin-bottom:0.6rem">Browser login is <strong>disabled</strong> on this server (BROWSER_LOGIN_ENABLED=0). It only works when the server runs locally.</div>`;

  const rows = data.sessions.map(s => {
    let state, cls;
    if (!s.exists)      { state = 'no session'; cls = 'badge-off'; }
    else if (!s.fresh)  { state = `stale · ${relAge(s.age_seconds)}`; cls = 'badge-off'; }
    else                { state = `fresh · ${relAge(s.age_seconds)}`; cls = 'badge-on'; }

    const t = s.last_login;
    let taskLine = '';
    if (t) {
      const map = { running: 'muted', succeeded: 'badge-on', failed: 'badge-off' };
      taskLine = `<div class="muted" style="font-size:0.78rem;margin-top:0.25rem">
        <span class="badge ${map[t.status] || ''}">${escHtml(t.status)}</span> ${escHtml(t.message || '')}</div>`;
    }
    const busy = s.login_in_progress || !data.login_enabled;
    return `<div class="toggle-row" style="align-items:flex-start">
      <div>
        <strong>${escHtml(s.label)}</strong> <span class="muted" style="font-size:0.78rem">${escHtml(s.name)}</span>
        <div class="muted" style="font-size:0.78rem"><span class="badge ${cls}">${escHtml(state)}</span> <code>${escHtml(s.state_path)}</code></div>
        ${taskLine}
      </div>
      <button class="btn btn-ghost btn-sm" data-session-login="${escHtml(s.name)}" ${busy ? 'disabled' : ''}>
        ${s.login_in_progress ? 'Logging in…' : 'Refresh login'}</button>
    </div>`;
  }).join('');

  el.innerHTML = `<div class="admin-card">
    <div class="muted" style="margin-bottom:0.6rem">Refresh a persisted login session by opening a real browser <strong>on the machine running this server</strong>. Log in there (handle any captcha / OTP); the session is saved automatically once the site confirms you're in.</div>
    ${disabledNote}
    ${rows || '<div class="muted">No browser-login automations.</div>'}
  </div>`;

  el.querySelectorAll('[data-session-login]').forEach(btn => btn.onclick = async () => {
    const name = btn.dataset.sessionLogin;
    btn.disabled = true;
    const r = await fetch(`/api/sessions/${encodeURIComponent(name)}/login`, { method: 'POST' });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      alert(err.detail || 'Failed to start login');
      btn.disabled = false;
      return;
    }
    renderAdminSessions();
  });

  // Poll while a login is running so the UI reflects success/failure live.
  const anyRunning = data.sessions.some(s => s.login_in_progress);
  if (SESSION_POLL) { clearTimeout(SESSION_POLL); SESSION_POLL = null; }
  if (anyRunning && ADMIN_TAB === 'sessions') {
    SESSION_POLL = setTimeout(renderAdminSessions, 2000);
  }
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
