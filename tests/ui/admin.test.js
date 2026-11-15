// admin.js — Users tab (table, create/edit modal, toggle, reset PW, delete), tab
// switching, LLM Keys, Verification-LLM (eval judge) and Automations toggles.
// Custom automations + Sessions live in admin-custom.test.js (fresh module state).
import { test, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { setupDom, importUi, flush, click, setValue, jsonResponse, $ } from './harness.js';

// ── Fake backend (shapes mirror src/routers/admin.py) ─────────────────────────
const XSS = '<img src=x onerror="window.__xss=1">';
const S = {
  users: [],
  usersStatus: 200,
  keys: [],
  keysStatus: 200,
  autos: { all: [], enabled: [] },
  autosStatus: 200,
  autosPutStatus: 200,
  judge: null,
  judgeStatus: 200,
  errors: {},          // "METHOD path" → [status, body] | () => Response (reset per test)
};

function err(method, path) {
  const e = S.errors[`${method} ${path}`];
  if (typeof e === 'function') return e();  // raw Response (e.g. non-JSON body)
  return e ? jsonResponse(e[1], { status: e[0] }) : null;
}

const routes = [
  ['GET', '/api/admin/users', () => (S.usersStatus === 200 ? S.users : S.usersStatus)],
  ['POST', '/api/admin/users', (u, i, call) => err('POST', call.path)
    || jsonResponse({ id: 99, ...call.body, is_active: true }, { status: 201 })],
  ['PATCH', /^\/api\/admin\/users\/\d+$/, (u, i, call) => err('PATCH', call.path) || { id: +call.path.split('/').pop(), ...call.body }],
  ['DELETE', /^\/api\/admin\/users\/\d+$/, (u, i, call) => err('DELETE', call.path) || 204],
  ['POST', /^\/api\/admin\/users\/\d+\/password$/, (u, i, call) => err('POST', call.path) || { ok: true }],
  ['GET', '/api/admin/llm-keys', () => (S.keysStatus === 200 ? S.keys : S.keysStatus)],
  ['PUT', /^\/api\/admin\/llm-keys\/[^/]+$/, (u, i, call) => err('PUT', call.path) || { provider: call.path.split('/').pop() }],
  ['GET', '/api/admin/automations', () => (S.autosStatus === 200 ? S.autos : S.autosStatus)],
  ['PUT', '/api/admin/automations', (u, i, call) => (S.autosPutStatus === 200
    ? { all: S.autos.all, enabled: call.body.enabled }
    : jsonResponse({ detail: 'nope' }, { status: S.autosPutStatus }))],
  ['GET', '/api/admin/eval-judge', () => (S.judgeStatus === 200 ? S.judge : S.judgeStatus)],
  ['PUT', '/api/admin/eval-judge', (u, i, call) => err('PUT', call.path) || S.judge],
  ['GET', '/api/runs', () => []],
];

const { window, document, fetchMock } = setupDom({ routes });
const alerts = [];
window.alert = (m) => alerts.push(m);
let confirmAnswer = true;
const confirms = [];
window.confirm = (m) => { confirms.push(m); return confirmAnswer; };
let promptAnswer = null;
const prompts = [];
window.prompt = (m) => { prompts.push(m); return promptAnswer; };

const { loadAdminPage } = await importUi('admin.js');
const auth = await importUi('auth.js');
const { ALL_TYPES, AUTO_CATALOG } = await importUi('catalog.js');

// ── helpers ───────────────────────────────────────────────────────────────────
async function until(pred, what = 'condition') {
  for (let i = 0; i < 60; i++) {
    if (pred()) return;
    await flush(2);
  }
  assert.fail(`timed out waiting for ${what}`);
}
const usersTable = () => $('admin-users').querySelector('tbody');
const userRow = (name) => [...usersTable().rows].find((r) => r.cells[0].textContent === name);
const actBtn = (act, id) => $('admin-users').querySelector(`[data-act="${act}"][data-id="${id}"]`);
const submitUserForm = () => $('auser-form').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
// Clears the panel first so `until(...)` waits for *this* render, not the last test's.
function switchTab(tab) {
  if (!$('admin-tabs').dataset.wired) loadAdminPage();  // wires the tab bar (isolated runs)
  $(`admin-${tab}`).innerHTML = '';
  click(document.querySelector(`#admin-tabs [data-adm="${tab}"]`));
}
async function renderUsers() {
  switchTab('users');
  await until(() => $('admin-add-user'), 'users table');
}

beforeEach(() => {
  fetchMock.reset();
  alerts.length = 0;
  confirms.length = 0;
  prompts.length = 0;
  confirmAnswer = true;
  promptAnswer = null;
  S.errors = {};
  S.usersStatus = 200;
  S.users = [
    { id: 1, username: 'root', is_admin: true, is_active: true, allowed_automations: '*', last_login_at: null },
    { id: 2, username: 'alice', is_admin: false, is_active: true, allowed_automations: ['web_scraper', 'hacker_news_digest'], last_login_at: null },
    { id: 3, username: XSS, is_admin: false, is_active: false, allowed_automations: [], last_login_at: null },
    { id: 4, username: 'wild', is_admin: false, is_active: true, allowed_automations: '*', last_login_at: null },
  ];
});

// ── Tabs ──────────────────────────────────────────────────────────────────────

test('loadAdminPage opens the Users tab first and wires tab clicks once', async () => {
  loadAdminPage();
  loadAdminPage();  // second call must not double-wire the tab listener
  await until(() => $('admin-add-user'), 'users table');
  assert.equal($('admin-users').hidden, false);
  for (const id of ['admin-keys', 'admin-judge', 'admin-autos', 'admin-custom', 'admin-sessions']) {
    assert.equal($(id).hidden, true, `${id} hidden`);
  }
  assert.ok(document.querySelector('#admin-tabs [data-adm="users"]').classList.contains('active'));
  assert.equal($('admin-tabs').dataset.wired, '1');

  fetchMock.reset();
  S.keys = [];
  switchTab('keys');
  await flush();
  assert.equal(fetchMock.callsTo('/api/admin/llm-keys', 'GET').length, 1, 'one fetch per click (wired once)');
  assert.equal($('admin-keys').hidden, false);
  assert.equal($('admin-users').hidden, true);
  const active = [...document.querySelectorAll('#admin-tabs .cat-tab.active')].map((b) => b.dataset.adm);
  assert.deepEqual(active, ['keys']);
});

test('re-opening the admin page restores the last tab', async () => {
  S.keys = [];
  switchTab('keys');
  await flush();
  fetchMock.reset();
  loadAdminPage();
  await flush();
  assert.equal(fetchMock.callsTo('/api/admin/llm-keys').length, 1);
  assert.equal(fetchMock.callsTo('/api/admin/users').length, 0);
  assert.equal($('admin-keys').hidden, false);
});

test('clicking the tab bar outside a tab button does nothing', async () => {
  await renderUsers();
  fetchMock.reset();
  click($('admin-tabs'));
  await flush();
  assert.equal(fetchMock.calls.length, 0);
  assert.equal($('admin-users').hidden, false);
});

// ── Users table ───────────────────────────────────────────────────────────────

test('users table renders role, status and allowlist badges per user', async () => {
  await renderUsers();
  assert.equal(usersTable().rows.length, 4);
  assert.match($('admin-users').querySelector('.admin-bar .muted').textContent, /^4 user\(s\)$/);

  const root = userRow('root');
  assert.equal(root.cells[1].textContent, 'admin');
  assert.equal(root.cells[3].textContent, '—', 'admins show no allowlist');

  const alice = userRow('alice');
  assert.equal(alice.cells[1].textContent, 'user');
  assert.equal(alice.cells[2].textContent, 'active');
  assert.deepEqual([...alice.cells[3].querySelectorAll('.badge')].map((b) => b.textContent),
    ['web_scraper', 'hacker_news_digest']);
  assert.equal(actBtn('toggle', 2).textContent, 'Disable');

  const wild = userRow('wild');
  assert.equal(wild.cells[3].textContent, 'all');

  const evil = userRow(XSS);
  assert.ok(evil, 'XSS username shown verbatim as text');
  assert.equal(evil.cells[2].textContent, 'disabled');
  assert.equal(evil.cells[3].textContent, 'none', 'empty allowlist');
  assert.equal(actBtn('toggle', 3).textContent, 'Enable');
});

test('usernames and allowlist entries are HTML-escaped (no element injection)', async () => {
  S.users = [{ id: 7, username: XSS, is_admin: false, is_active: true,
    allowed_automations: ['<script>x</script>'], last_login_at: null }];
  await renderUsers();
  assert.equal($('admin-users').querySelector('img'), null);
  assert.equal($('admin-users').querySelector('script'), null);
  assert.equal(window.__xss, undefined);
  assert.equal(usersTable().rows[0].cells[3].textContent, '<script>x</script>');
});

test('users load failure shows an inline error', async () => {
  S.usersStatus = 500;
  switchTab('users');
  await until(() => /Failed to load users/.test($('admin-users').textContent), 'error text');
  assert.equal($('admin-add-user'), null);
});

test('toggle PATCHes the inverse is_active and reloads the table', async () => {
  await renderUsers();
  fetchMock.reset();
  click(actBtn('toggle', 2));
  await until(() => fetchMock.callsTo('/api/admin/users', 'GET').length === 1, 'reload');
  const [patch] = fetchMock.callsTo('/api/admin/users/2', 'PATCH');
  assert.deepEqual(patch.body, { is_active: false });
  assert.equal(patch.init.headers['Content-Type'], 'application/json');

  fetchMock.reset();
  click(actBtn('toggle', 3));  // disabled user → enable
  await until(() => fetchMock.callsTo('/api/admin/users/3', 'PATCH').length === 1, 'patch');
  assert.deepEqual(fetchMock.callsTo('/api/admin/users/3', 'PATCH')[0].body, { is_active: true });
  assert.equal(alerts.length, 0);
});

test('toggle error alerts the server detail and still reloads', async () => {
  S.errors['PATCH /api/admin/users/1'] = [400, { detail: 'Cannot remove the last active admin' }];
  await renderUsers();
  fetchMock.reset();
  click(actBtn('toggle', 1));
  await until(() => fetchMock.callsTo('/api/admin/users', 'GET').length === 1, 'reload');
  assert.deepEqual(alerts, ['Cannot remove the last active admin']);
});

test('toggle error with a non-JSON body falls back to "Failed"', async () => {
  S.errors['PATCH /api/admin/users/2'] = () => new Response('boom', { status: 500 });
  await renderUsers();
  click(actBtn('toggle', 2));
  await until(() => alerts.length === 1, 'alert');
  assert.deepEqual(alerts, ['Failed']);
});

test('delete asks for confirmation and does nothing when declined', async () => {
  await renderUsers();
  fetchMock.reset();
  confirmAnswer = false;
  click(actBtn('del', 2));
  await flush();
  assert.equal(confirms.length, 1);
  assert.match(confirms[0], /Delete user "alice"\? This cannot be undone\./);
  assert.equal(fetchMock.callsTo(/\/api\/admin\/users\/2/, 'DELETE').length, 0);
  assert.equal(fetchMock.callsTo('/api/admin/users', 'GET').length, 0);
});

test('delete confirmed sends DELETE and reloads; errors alert the detail', async () => {
  await renderUsers();
  fetchMock.reset();
  click(actBtn('del', 2));
  await until(() => fetchMock.callsTo('/api/admin/users', 'GET').length === 1, 'reload');
  assert.equal(fetchMock.callsTo('/api/admin/users/2', 'DELETE').length, 1);
  assert.equal(alerts.length, 0);

  S.errors['DELETE /api/admin/users/1'] = [400, { detail: 'You cannot delete your own account' }];
  await until(() => actBtn('del', 1), 'rerender');
  click(actBtn('del', 1));
  await until(() => alerts.length === 1, 'alert');
  assert.deepEqual(alerts, ['You cannot delete your own account']);
});

test('reset PW: cancelled prompt sends nothing; a value POSTs new_password', async () => {
  await renderUsers();
  fetchMock.reset();
  promptAnswer = null;
  click(actBtn('pw', 2));
  await flush();
  assert.match(prompts[0], /New password for "alice" \(min 8 characters\)/);
  assert.equal(fetchMock.calls.length, 0);

  promptAnswer = 'hunter2hunter2';
  click(actBtn('pw', 2));
  await until(() => alerts.length === 1, 'alert');
  const [post] = fetchMock.callsTo('/api/admin/users/2/password', 'POST');
  assert.deepEqual(post.body, { new_password: 'hunter2hunter2' });
  assert.deepEqual(alerts, ['Password updated.']);
  assert.equal(fetchMock.callsTo('/api/admin/users', 'GET').length, 0, 'no table reload after PW reset');
});

test('reset PW server rejection alerts the detail', async () => {
  S.errors['POST /api/admin/users/2/password'] = [400, { detail: 'Password must be at least 8 characters' }];
  await renderUsers();
  promptAnswer = 'short';
  click(actBtn('pw', 2));
  await until(() => alerts.length === 1, 'alert');
  assert.deepEqual(alerts, ['Password must be at least 8 characters']);
});

// ── User modal (create / edit) ────────────────────────────────────────────────

test('New User opens a blank create modal with one checkbox per automation type', async () => {
  await renderUsers();
  $('auser-error').hidden = false;
  click($('admin-add-user'));
  assert.equal($('admin-user-modal').classList.contains('hidden'), false);
  assert.equal($('auser-title').textContent, 'New User');
  assert.equal($('auser-id').value, '');
  assert.equal($('auser-username').value, '');
  assert.equal($('auser-username').disabled, false);
  assert.equal($('auser-password-field').hidden, false);
  assert.equal($('auser-admin').checked, false);
  assert.equal($('auser-allow-all').checked, false);
  assert.equal($('auser-error').hidden, true, 'stale error cleared');
  const boxes = [...document.querySelectorAll('#auser-allow-grid .auser-allow')];
  assert.deepEqual(boxes.map((b) => b.value), ALL_TYPES);
  assert.ok(boxes.every((b) => !b.checked && !b.disabled));
  const labelFor = (t) => boxes.find((b) => b.value === t).parentElement.textContent.trim();
  assert.equal(labelFor('web_scraper'), AUTO_CATALOG.web_scraper.name);
});

test('creating a user POSTs username (trimmed), password, role and picked automations', async () => {
  await renderUsers();
  click($('admin-add-user'));
  setValue('#auser-username', '  bob  ');
  setValue('#auser-password', 'secret-pass');
  setValue('#auser-allow-grid input[value="web_scraper"]', true);
  setValue('#auser-allow-grid input[value="email_collect"]', true);
  fetchMock.reset();
  submitUserForm();
  await until(() => fetchMock.callsTo('/api/admin/users', 'GET').length === 1, 'reload');
  const [post] = fetchMock.callsTo('/api/admin/users', 'POST');
  assert.deepEqual(post.body, {
    username: 'bob', password: 'secret-pass', is_admin: false,
    allowed_automations: ['web_scraper', 'email_collect'],
  });
  assert.ok($('admin-user-modal').classList.contains('hidden'), 'modal closes on success');
});

test('"All automations" and Administrator both send the ["*"] wildcard and disable the grid', async () => {
  await renderUsers();
  click($('admin-add-user'));
  setValue('#auser-username', 'carol');
  setValue('#auser-password', 'secret-pass');
  setValue('#auser-allow-grid input[value="web_scraper"]', true);
  setValue('#auser-allow-all', true);
  assert.ok([...document.querySelectorAll('.auser-allow')].every((b) => b.disabled));
  assert.equal($('auser-allow-all').disabled, false);
  submitUserForm();
  await until(() => fetchMock.callsTo('/api/admin/users', 'POST').length === 1, 'post');
  assert.deepEqual(fetchMock.callsTo('/api/admin/users', 'POST')[0].body.allowed_automations, ['*']);

  fetchMock.reset();
  click($('admin-add-user'));
  setValue('#auser-username', 'dave');
  setValue('#auser-password', 'secret-pass');
  setValue('#auser-admin', true);
  assert.equal($('auser-allow-all').disabled, true);
  assert.equal($('auser-allow-field').style.opacity, '0.5');
  assert.ok([...document.querySelectorAll('.auser-allow')].every((b) => b.disabled));
  submitUserForm();
  await until(() => fetchMock.callsTo('/api/admin/users', 'POST').length === 1, 'post');
  const body = fetchMock.callsTo('/api/admin/users', 'POST')[0].body;
  assert.equal(body.is_admin, true);
  assert.deepEqual(body.allowed_automations, ['*']);

  setValue('#auser-admin', false);  // un-ticking restores the grid
  assert.equal($('auser-allow-field').style.opacity, '1');
  assert.equal($('auser-allow-all').disabled, false);
});

test('no automations ticked sends an empty allowlist', async () => {
  await renderUsers();
  click($('admin-add-user'));
  setValue('#auser-username', 'erin');
  setValue('#auser-password', 'secret-pass');
  submitUserForm();
  await until(() => fetchMock.callsTo('/api/admin/users', 'POST').length === 1, 'post');
  assert.deepEqual(fetchMock.callsTo('/api/admin/users', 'POST')[0].body.allowed_automations, []);
});

test('create errors (409 / non-JSON 500) stay in the modal and show the message', async () => {
  await renderUsers();
  click($('admin-add-user'));
  setValue('#auser-username', 'alice');
  setValue('#auser-password', 'secret-pass');

  S.errors['POST /api/admin/users'] = [409, { detail: 'Username already exists' }];
  fetchMock.reset();
  submitUserForm();
  await until(() => !$('auser-error').hidden, 'error shown');
  assert.equal($('auser-error').textContent, 'Username already exists');
  assert.equal($('admin-user-modal').classList.contains('hidden'), false);
  assert.equal(fetchMock.callsTo('/api/admin/users', 'GET').length, 0, 'no reload on failure');

  S.errors['POST /api/admin/users'] = () => new Response('<html>oops</html>', { status: 500 });
  submitUserForm();
  await until(() => $('auser-error').textContent === 'Save failed', 'fallback message');
  assert.equal($('auser-error').hidden, false);
});

test('Edit pre-fills the modal and PATCHes is_admin + allowed_automations', async () => {
  await renderUsers();
  click(actBtn('edit', 2));
  assert.equal($('auser-title').textContent, 'Edit alice');
  assert.equal($('auser-id').value, '2');
  assert.equal($('auser-username').value, 'alice');
  assert.equal($('auser-username').disabled, true);
  assert.equal($('auser-password-field').hidden, true, 'password only set at creation');
  const checked = () => [...document.querySelectorAll('.auser-allow:checked')].map((b) => b.value);
  assert.deepEqual(checked(), ['web_scraper', 'hacker_news_digest'].sort((a, b) => ALL_TYPES.indexOf(a) - ALL_TYPES.indexOf(b)));

  setValue('#auser-allow-grid input[value="hacker_news_digest"]', false);
  setValue('#auser-allow-grid input[value="x_scraper"]', true);
  fetchMock.reset();
  submitUserForm();
  await until(() => fetchMock.callsTo('/api/admin/users', 'GET').length === 1, 'reload');
  assert.equal(fetchMock.callsTo('/api/admin/users', 'POST').length, 0);
  const [patch] = fetchMock.callsTo('/api/admin/users/2', 'PATCH');
  assert.deepEqual(patch.body, { is_admin: false, allowed_automations: ['web_scraper', 'x_scraper'] });
  assert.equal('username' in patch.body, false);
});

test('Edit of a wildcard user ticks "All automations"; of an admin ticks Administrator', async () => {
  await renderUsers();
  click(actBtn('edit', 4));
  assert.equal($('auser-allow-all').checked, true);
  assert.ok([...document.querySelectorAll('.auser-allow')].every((b) => b.disabled && !b.checked));

  click(actBtn('edit', 1));
  assert.equal($('auser-admin').checked, true);
  assert.equal($('auser-allow-all').disabled, true);

  click(actBtn('edit', 3));  // empty allowlist, non-admin: everything editable again
  assert.equal($('auser-admin').checked, false);
  assert.equal($('auser-allow-all').checked, false);
  assert.ok([...document.querySelectorAll('.auser-allow')].every((b) => !b.disabled && !b.checked));
});

test('PATCH failure on edit shows the detail inline', async () => {
  S.errors['PATCH /api/admin/users/1'] = [400, { detail: 'Cannot remove the last active admin' }];
  await renderUsers();
  click(actBtn('edit', 1));
  setValue('#auser-admin', false);
  submitUserForm();
  await until(() => !$('auser-error').hidden, 'error');
  assert.equal($('auser-error').textContent, 'Cannot remove the last active admin');
});

test('modal closes via ✕, Cancel and backdrop click — but not a click inside the dialog', async () => {
  await renderUsers();
  const m = $('admin-user-modal');
  for (const closer of ['auser-close', 'auser-cancel']) {
    click($('admin-add-user'));
    assert.equal(m.classList.contains('hidden'), false);
    click($(closer));
    assert.ok(m.classList.contains('hidden'), `${closer} closes`);
  }
  click($('admin-add-user'));
  click($('auser-title'));
  assert.equal(m.classList.contains('hidden'), false, 'inner click keeps it open');
  click(m);
  assert.ok(m.classList.contains('hidden'), 'backdrop closes');
});

// ── LLM keys ──────────────────────────────────────────────────────────────────

const KEYS = () => [
  { provider: 'openai', env: 'OPENAI_API_KEY', models: ['gpt-4o-mini', 'gpt-4o'], configured: true, source: 'db', masked: 'sk-…abcd' },
  { provider: 'anthropic', env: 'ANTHROPIC_API_KEY', models: ['claude-sonnet-4-6'], configured: true, source: 'env', masked: 'sk-ant…<b>9</b>' },
  { provider: 'gemini', env: 'GEMINI_API_KEY', models: ['gemini/gemini-2.5-pro'], configured: false, source: null, masked: null },
];
const keyCard = (p) => $('admin-keys').querySelector(`[data-key-input="${p}"]`).closest('.admin-card');

test('LLM keys: masked value, source badge, env var/models and Clear only for db keys', async () => {
  S.keys = KEYS();
  switchTab('keys');
  await until(() => $('admin-keys').querySelectorAll('.admin-card').length === 3, 'key cards');

  const openai = keyCard('openai');
  assert.match(openai.textContent, /configured/);
  assert.equal(openai.querySelector('.badge-src').textContent, 'db');
  assert.match(openai.textContent, /sk-…abcd/);
  assert.match(openai.textContent, /env var: OPENAI_API_KEY · models: gpt-4o-mini, gpt-4o/);
  assert.ok(openai.querySelector('[data-key-clear="openai"]'));
  assert.equal(openai.querySelector('[data-key-input="openai"]').type, 'password');

  const anth = keyCard('anthropic');
  assert.equal(anth.querySelector('[data-key-clear]'), null, 'env keys cannot be cleared');
  assert.equal(anth.querySelector('b'), null, 'masked value is escaped');
  assert.match(anth.textContent, /sk-ant…<b>9<\/b>/);

  const gem = keyCard('gemini');
  assert.match(gem.textContent, /not set/);
  assert.equal(gem.querySelector('.badge-src'), null);
  assert.equal(gem.querySelector('[data-key-clear]'), null);
});

test('LLM keys: Save PUTs {api_key} for that provider and re-renders', async () => {
  S.keys = KEYS();
  switchTab('keys');
  await until(() => $('admin-keys').querySelector('[data-key-save="gemini"]'), 'keys');
  $('admin-keys').querySelector('[data-key-input="gemini"]').value = 'AIza-new';
  fetchMock.reset();
  click($('admin-keys').querySelector('[data-key-save="gemini"]'));
  await until(() => fetchMock.callsTo('/api/admin/llm-keys', 'GET').length === 1, 're-render');
  const puts = fetchMock.callsTo(/^\/api\/admin\/llm-keys\//, 'PUT');
  assert.equal(puts.length, 1);
  assert.equal(puts[0].path, '/api/admin/llm-keys/gemini');
  assert.deepEqual(puts[0].body, { api_key: 'AIza-new' });
  assert.equal(alerts.length, 0);
});

test('LLM keys: Save error alerts the server detail', async () => {
  S.keys = KEYS();
  S.errors['PUT /api/admin/llm-keys/openai'] = [404, { detail: 'Unknown provider' }];
  switchTab('keys');
  await until(() => $('admin-keys').querySelector('[data-key-save="openai"]'), 'keys');
  $('admin-keys').querySelector('[data-key-input="openai"]').value = 'x';
  click($('admin-keys').querySelector('[data-key-save="openai"]'));
  await until(() => alerts.length === 1, 'alert');
  assert.deepEqual(alerts, ['Unknown provider']);
});

test('LLM keys: Save with an empty input must not silently wipe a stored key', {
  todo: 'admin.js:403-411 — Save sends {api_key: ""} with no guard; the server treats an empty key as "clear the DB override", so an accidental click wipes the key without the confirm() that Clear asks for',
}, async () => {
  S.keys = KEYS();
  switchTab('keys');
  await until(() => $('admin-keys').querySelector('[data-key-save="openai"]'), 'keys');
  fetchMock.reset();
  confirmAnswer = false;
  click($('admin-keys').querySelector('[data-key-save="openai"]'));
  await flush();
  assert.equal(fetchMock.callsTo('/api/admin/llm-keys/openai', 'PUT').length, 0);
});

test('LLM keys: Clear confirms first, then PUTs an empty key', async () => {
  S.keys = KEYS();
  switchTab('keys');
  await until(() => $('admin-keys').querySelector('[data-key-clear="openai"]'), 'keys');
  fetchMock.reset();
  confirmAnswer = false;
  click($('admin-keys').querySelector('[data-key-clear="openai"]'));
  await flush();
  assert.match(confirms[0], /Clear the stored openai key and revert to the environment variable\?/);
  assert.equal(fetchMock.calls.length, 0);

  confirmAnswer = true;
  click($('admin-keys').querySelector('[data-key-clear="openai"]'));
  await until(() => fetchMock.callsTo('/api/admin/llm-keys', 'GET').length === 1, 're-render');
  const [put] = fetchMock.callsTo('/api/admin/llm-keys/openai', 'PUT');
  assert.deepEqual(put.body, { api_key: '' });
});

test('LLM keys: load failure shows an inline error', async () => {
  S.keysStatus = 403;
  switchTab('keys');
  await until(() => /Failed to load keys/.test($('admin-keys').textContent), 'error');
  S.keysStatus = 200;
});

// ── Automations toggles ───────────────────────────────────────────────────────

test('Automations: one switch per type, checked when enabled; unknown types fall back to the raw id', async () => {
  S.autos = { all: ['web_scraper', 'hacker_news_digest', 'custom:<b>x</b>'], enabled: ['web_scraper'] };
  switchTab('autos');
  await until(() => $('admin-autos').querySelectorAll('[data-auto]').length === 3, 'toggles');
  const rows = [...$('admin-autos').querySelectorAll('.toggle-row')];
  assert.equal(rows[0].querySelector('strong').textContent, AUTO_CATALOG.web_scraper.name);
  assert.equal(rows[0].querySelector('.muted').textContent, 'web_scraper');
  assert.equal(rows[2].querySelector('strong').textContent, 'custom:<b>x</b>', 'unknown name escaped');
  assert.equal($('admin-autos').querySelector('b'), null);
  const states = [...$('admin-autos').querySelectorAll('[data-auto]')].map((c) => c.checked);
  assert.deepEqual(states, [true, false, false]);
});

test('Automations: flipping a switch PUTs the full enabled list and syncs CURRENT_USER', async () => {
  auth.onAuthenticated({ id: 1, username: 'root', is_admin: true, allowed_automations: '*', enabled_automations: ['web_scraper'] });
  S.autos = { all: ['web_scraper', 'hacker_news_digest', 'x_scraper'], enabled: ['web_scraper'] };
  $('lp-autos').dataset.rendered = '1';
  switchTab('autos');
  await until(() => $('admin-autos').querySelectorAll('[data-auto]').length === 3, 'toggles');
  fetchMock.reset();
  setValue('#admin-autos [data-auto="x_scraper"]', true);
  await until(() => auth.CURRENT_USER.enabled_automations.length === 2, 'user sync');
  const [put] = fetchMock.callsTo('/api/admin/automations', 'PUT');
  assert.deepEqual(put.body, { enabled: ['web_scraper', 'x_scraper'] });
  assert.deepEqual(auth.CURRENT_USER.enabled_automations, ['web_scraper', 'x_scraper']);
  assert.equal($('lp-autos').dataset.rendered, undefined, 'landing grid forced to re-filter');

  setValue('#admin-autos [data-auto="web_scraper"]', false);
  await until(() => fetchMock.callsTo('/api/admin/automations', 'PUT').length === 2, 'second put');
  assert.deepEqual(fetchMock.callsTo('/api/admin/automations', 'PUT')[1].body, { enabled: ['x_scraper'] });
});

test('Automations: a failed PUT alerts and re-renders from the server state', async () => {
  S.autos = { all: ['web_scraper', 'x_scraper'], enabled: ['web_scraper'] };
  S.autosPutStatus = 403;
  switchTab('autos');
  await until(() => $('admin-autos').querySelectorAll('[data-auto]').length === 2, 'toggles');
  fetchMock.reset();
  setValue('#admin-autos [data-auto="web_scraper"]', false);
  await until(() => fetchMock.callsTo('/api/admin/automations', 'GET').length === 1, 're-render');
  assert.deepEqual(alerts, ['Failed to update']);
  await until(() => $('admin-autos').querySelector('[data-auto="web_scraper"]')?.checked === true, 'restored');
  S.autosPutStatus = 200;
});

test('Automations: load failure shows an inline error', async () => {
  S.autosStatus = 500;
  switchTab('autos');
  await until(() => /Failed to load\./.test($('admin-autos').textContent), 'error');
  S.autosStatus = 200;
});

// ── Eval judge ────────────────────────────────────────────────────────────────

const JUDGE = () => ({
  provider: 'anthropic',
  model: 'claude-sonnet-4-6',
  default: { provider: 'gemini', model: 'gemini/gemini-2.5-flash' },
  providers: {
    openai: ['gpt-4o-mini', 'gpt-4o'],
    anthropic: ['claude-haiku-4-5-20251001', 'claude-sonnet-4-6'],
    '<i>evil</i>': ['m"1'],
  },
});

test('Judge: provider + model selects reflect the saved choice', async () => {
  S.judge = JUDGE();
  switchTab('judge');
  await until(() => $('admin-judge').querySelector('#judge-provider'), 'judge form');
  const prov = $('judge-provider');
  const model = $('judge-model');
  assert.deepEqual([...prov.options].map((o) => o.value), ['', 'openai', 'anthropic', '<i>evil</i>']);
  assert.equal(prov.options[0].textContent, 'Auto — default (gemini / gemini/gemini-2.5-flash)');
  assert.equal(prov.value, 'anthropic');
  assert.equal(prov.querySelector('i'), null, 'provider names escaped');
  assert.equal(model.disabled, false);
  assert.deepEqual([...model.options].map((o) => o.value), ['', 'claude-haiku-4-5-20251001', 'claude-sonnet-4-6']);
  assert.equal(model.options[0].textContent, 'anthropic default');
  assert.equal(model.value, 'claude-sonnet-4-6');
});

test('Judge: changing provider refills models; Auto disables the model select', async () => {
  S.judge = JUDGE();
  switchTab('judge');
  await until(() => $('judge-provider'), 'judge form');
  setValue('#judge-provider', 'openai');
  assert.deepEqual([...$('judge-model').options].map((o) => o.value), ['', 'gpt-4o-mini', 'gpt-4o']);
  assert.equal($('judge-model').value, '', 'saved model belongs to another provider → provider default');

  setValue('#judge-provider', '<i>evil</i>');
  assert.deepEqual([...$('judge-model').options].map((o) => o.value), ['', 'm"1'], 'attribute-escaped model');

  setValue('#judge-provider', '');
  assert.equal($('judge-model').disabled, true);
  assert.deepEqual([...$('judge-model').options].map((o) => o.textContent), ['—']);
});

test('Judge: Save PUTs provider/model (nulls for Auto / provider default) and re-renders', async () => {
  S.judge = JUDGE();
  switchTab('judge');
  await until(() => $('judge-provider'), 'judge form');
  setValue('#judge-provider', 'openai');
  setValue('#judge-model', 'gpt-4o');
  fetchMock.reset();
  click($('judge-save'));
  await until(() => fetchMock.callsTo('/api/admin/eval-judge', 'GET').length === 1, 're-render');
  assert.deepEqual(fetchMock.callsTo('/api/admin/eval-judge', 'PUT')[0].body, { provider: 'openai', model: 'gpt-4o' });

  await until(() => $('judge-provider'), 'judge form');
  setValue('#judge-provider', 'openai');  // model left at "openai default"
  fetchMock.reset();
  click($('judge-save'));
  await until(() => fetchMock.callsTo('/api/admin/eval-judge', 'PUT').length === 1, 'put');
  assert.deepEqual(fetchMock.callsTo('/api/admin/eval-judge', 'PUT')[0].body, { provider: 'openai', model: null });

  await until(() => $('judge-provider'), 'judge form');
  setValue('#judge-provider', '');
  fetchMock.reset();
  click($('judge-save'));
  await until(() => fetchMock.callsTo('/api/admin/eval-judge', 'PUT').length === 1, 'put');
  assert.deepEqual(fetchMock.callsTo('/api/admin/eval-judge', 'PUT')[0].body, { provider: null, model: null });
});

test('Judge: Save error alerts and does not re-render', async () => {
  S.judge = JUDGE();
  S.errors['PUT /api/admin/eval-judge'] = [400, { detail: 'Unknown model for provider' }];
  switchTab('judge');
  await until(() => $('judge-provider'), 'judge form');
  fetchMock.reset();
  click($('judge-save'));
  await until(() => alerts.length === 1, 'alert');
  assert.deepEqual(alerts, ['Unknown model for provider']);
  assert.equal(fetchMock.callsTo('/api/admin/eval-judge', 'GET').length, 0);
});

test('Judge: unset provider renders on Auto with the model select disabled; load failure is shown', async () => {
  S.judge = { ...JUDGE(), provider: null, model: null };
  switchTab('judge');
  await until(() => $('judge-provider'), 'judge form');
  assert.equal($('judge-provider').value, '');
  assert.equal($('judge-model').disabled, true);

  S.judgeStatus = 500;
  switchTab('judge');
  await until(() => /Failed to load\./.test($('admin-judge').textContent), 'error');
  S.judgeStatus = 200;
});
