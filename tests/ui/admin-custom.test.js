// admin.js — Custom (no-code) automations tab and browser Sessions tab.
// Separate process from admin.test.js: creating a custom automation reloads the
// manifest (mutates catalog.ALL_TYPES), and the Sessions poll uses mocked timers.
import { test, beforeEach, mock } from 'node:test';
import assert from 'node:assert/strict';
import { setupDom, importUi, flush, click, setValue, jsonResponse, $ } from './harness.js';

// ── Fake backend (shapes mirror src/routers/admin.py + src/routers/sessions.py) ─
const S = {
  customs: [],
  customsStatus: 200,
  createResp: null,           // () => Response
  manifest: [],
  sessions: null,
  sessionsStatus: 200,
  sessionsGate: null,         // Promise to await before answering GET /api/sessions
  loginResp: null,            // () => Response
};

const routes = [
  ['GET', '/api/admin/custom-automations', () => (S.customsStatus === 200 ? S.customs : S.customsStatus)],
  ['POST', '/api/admin/custom-automations', (u, i, call) => (S.createResp ? S.createResp(call)
    : jsonResponse({ id: 50, slug: 'x', job_type: 'custom:x', enabled: true, ...call.body }, { status: 201 }))],
  ['PATCH', /^\/api\/admin\/custom-automations\/\d+$/, (u, i, call) => ({ id: +call.path.split('/').pop(), ...call.body })],
  ['DELETE', /^\/api\/admin\/custom-automations\/\d+$/, () => 204],
  ['GET', '/api/automations/manifest', () => ({ automations: S.manifest })],
  ['GET', '/api/sessions', async () => {
    if (S.sessionsGate) await S.sessionsGate;
    return S.sessionsStatus === 200 ? S.sessions : S.sessionsStatus;
  }],
  ['POST', /^\/api\/sessions\/[^/]+\/login$/, (u, i, call) => (S.loginResp ? S.loginResp(call)
    : jsonResponse({ status: 'running' }, { status: 202 }))],
  ['GET', '/api/runs', () => []],
];

const { window, document, fetchMock } = setupDom({ routes });
const alerts = [];
window.alert = (m) => alerts.push(m);

const { loadAdminPage } = await importUi('admin.js');

// ── helpers ───────────────────────────────────────────────────────────────────
async function until(pred, what = 'condition') {
  for (let i = 0; i < 60; i++) {
    if (pred()) return;
    await flush(2);
  }
  assert.fail(`timed out waiting for ${what}`);
}
// Clears the panel first so `until(...)` waits for *this* render, not the last test's.
function switchTab(tab) {
  if (!$('admin-tabs').dataset.wired) loadAdminPage();
  $(`admin-${tab}`).innerHTML = '';
  click(document.querySelector(`#admin-tabs [data-adm="${tab}"]`));
}
const custom = () => $('admin-custom');
async function openCustom() {
  switchTab('custom');
  await until(() => custom().querySelector('#cust-create'), 'custom form');
}
const fieldRows = () => [...custom().querySelectorAll('.cust-field-row')];
const toast = () => $('toast-banner');
const XSS = '<img src=x onerror="window.__xss=1">';

beforeEach(() => {
  fetchMock.reset();
  alerts.length = 0;
  S.customs = [];
  S.customsStatus = 200;
  S.createResp = null;
  S.sessionsStatus = 200;
  S.sessionsGate = null;
  S.loginResp = null;
  toast().classList.add('hidden');
  toast().textContent = '';
});

// ── Custom automations ────────────────────────────────────────────────────────

test('custom: empty list shows the placeholder and a form with one default input row', async () => {
  await openCustom();
  assert.equal($('admin-custom').hidden, false);
  assert.match(custom().querySelector('tbody').textContent, /No custom automations yet/);
  assert.equal(fieldRows().length, 1);
  assert.equal(fieldRows()[0].querySelector('.cf-name').value, 'product');
  assert.equal(fieldRows()[0].querySelector('.cf-label').value, 'Product');
  assert.equal(fieldRows()[0].querySelector('.cf-type').value, 'text');
  assert.equal($('cust-icon').value, '✨');
  assert.equal($('cust-temp').value, '0.3');
});

test('custom: rows render name/slug/description, declared input names and status', async () => {
  S.customs = [
    { id: 1, slug: 'tagline_writer', job_type: 'custom:tagline_writer', name: 'Tagline Writer', icon: '✍️',
      description: 'One-liners', instructions: 'Write a tagline', output_hint: '',
      fields: [{ name: 'product', label: 'Product', type: 'text', required: true },
        { name: 'tone', label: 'Tone', type: 'textarea', required: true }],
      temperature: 0.5, enabled: true },
    { id: 2, slug: 'quiet', job_type: 'custom:quiet', name: 'Quiet', icon: '', description: '',
      instructions: 'x', output_hint: '', fields: [], temperature: 0.3, enabled: false },
  ];
  await openCustom();
  const rows = [...custom().querySelectorAll('tbody tr')];
  assert.equal(rows.length, 2);
  assert.equal(rows[0].querySelector('strong').textContent, 'Tagline Writer');
  assert.match(rows[0].cells[0].textContent, /custom:tagline_writer/);
  assert.equal(rows[0].cells[1].textContent, 'One-liners');
  assert.equal(rows[0].cells[2].textContent, 'product, tone', 'fields round-trip into the Inputs column');
  assert.equal(rows[0].querySelector('.pill').textContent, 'enabled');
  assert.ok(rows[0].querySelector('.pill').classList.contains('pill-ok'));
  assert.equal(rows[0].querySelector('[data-cust-toggle]').textContent, 'Disable');

  assert.equal(rows[1].cells[1].textContent, '—', 'empty description');
  assert.equal(rows[1].cells[2].textContent, '—', 'no fields');
  assert.equal(rows[1].querySelector('.pill').textContent, 'disabled');
  assert.equal(rows[1].querySelector('[data-cust-toggle]').textContent, 'Enable');
});

test('custom: name, icon, description and field names are HTML-escaped', async () => {
  S.customs = [{ id: 3, slug: 'evil', name: XSS, icon: '<b>i</b>', description: '<script>d</script>',
    fields: [{ name: '<u>f</u>' }], enabled: true }];
  await openCustom();
  const tbody = custom().querySelector('tbody');
  assert.equal(tbody.querySelector('img, b, script, u'), null);
  assert.equal(window.__xss, undefined);
  assert.equal(tbody.querySelector('strong').textContent, XSS);
  assert.equal(tbody.rows[0].cells[2].textContent, '<u>f</u>');
});

test('custom: list load failure still renders the empty table and the create form', async () => {
  S.customsStatus = 500;
  await openCustom();
  assert.match(custom().querySelector('tbody').textContent, /No custom automations yet/);
});

test('custom: "+ Add input" appends a blank row and ✕ removes a row', async () => {
  await openCustom();
  click($('cust-add-field'));
  click($('cust-add-field'));
  assert.equal(fieldRows().length, 3);
  assert.equal(fieldRows()[1].querySelector('.cf-name').value, '');
  click(fieldRows()[0].querySelector('.cf-del'));
  assert.equal(fieldRows().length, 2);
  assert.ok(fieldRows().every((r) => r.querySelector('.cf-name').value === ''));
});

test('custom: name and instructions are required — error toast, no POST', async () => {
  await openCustom();
  setValue('#cust-name', '   ');
  setValue('#cust-instructions', 'Do things');
  click($('cust-create'));
  await flush();
  assert.equal(toast().textContent, 'Name and instructions are required');
  assert.equal(toast().classList.contains('hidden'), false);
  assert.equal(toast().style.background, 'var(--red)');

  setValue('#cust-name', 'Writer');
  setValue('#cust-instructions', '  ');
  click($('cust-create'));
  await flush();
  assert.equal(fetchMock.callsTo('/api/admin/custom-automations', 'POST').length, 0);
});

test('custom: create POSTs the trimmed spec with declared fields, reloads the manifest and the picker', async () => {
  S.manifest = [
    { job_type: 'web_scraper', name: 'Web Scraper', icon: '🌐', desc: 'Scrape', steps: [] },
    { job_type: 'custom:slogan', name: 'Slogan <Bot>', icon: '✨', desc: 'Custom automation', steps: [] },
  ];
  await openCustom();
  setValue('#cust-name', '  Slogan Bot ');
  setValue('#cust-icon', '  ');
  setValue('#cust-temp', '0.7');
  setValue('#cust-desc', ' Writes slogans ');
  setValue('#cust-instructions', ' Return JSON {"slogan": "…"} ');
  setValue('#cust-output', ' a slogan ');
  click($('cust-add-field'));
  click($('cust-add-field'));
  const [, second, third] = fieldRows();
  second.querySelector('.cf-name').value = ' audience ';
  second.querySelector('.cf-label').value = ' Audience ';
  second.querySelector('.cf-type').value = 'textarea';
  third.querySelector('.cf-label').value = 'unnamed → dropped';
  fetchMock.reset();
  click($('cust-create'));
  await until(() => fetchMock.callsTo('/api/admin/custom-automations', 'GET').length === 1, 're-render');

  const [post] = fetchMock.callsTo('/api/admin/custom-automations', 'POST');
  assert.equal(post.init.headers['Content-Type'], 'application/json');
  assert.deepEqual(post.body, {
    name: 'Slogan Bot',
    icon: '✨',
    description: 'Writes slogans',
    instructions: 'Return JSON {"slogan": "…"}',
    output_hint: 'a slogan',
    temperature: 0.7,
    fields: [
      { name: 'product', label: 'Product', type: 'text', required: true },
      { name: 'audience', label: 'Audience', type: 'textarea', required: true },
    ],
  });
  assert.equal(fetchMock.callsTo('/api/automations/manifest').length, 1);
  assert.match(toast().textContent, /Custom automation created/);
  assert.equal(toast().style.background, 'var(--green)');
  const cards = [...document.querySelectorAll('#type-grid .type-card')];
  assert.deepEqual(cards.map((c) => c.dataset.type), ['web_scraper', 'custom:slogan']);
  assert.equal(cards[1].querySelector('.type-card-name').textContent, 'Slogan <Bot>');
});

test('custom: a non-numeric temperature falls back to 0.3', async () => {
  await openCustom();
  setValue('#cust-name', 'A');
  setValue('#cust-instructions', 'B');
  setValue('#cust-temp', '');
  click($('cust-create'));
  await until(() => fetchMock.callsTo('/api/admin/custom-automations', 'POST').length === 1, 'post');
  assert.equal(fetchMock.callsTo('/api/admin/custom-automations', 'POST')[0].body.temperature, 0.3);
});

test('custom: temperature 0 (deterministic) is sent as 0', {
  todo: 'admin.js:137 — `parseFloat(temp) || 0.3` turns a valid 0 into 0.3 (the input allows min=0 and the server clamps to [0,1])',
}, async () => {
  await openCustom();
  setValue('#cust-name', 'A');
  setValue('#cust-instructions', 'B');
  setValue('#cust-temp', '0');
  click($('cust-create'));
  await until(() => fetchMock.callsTo('/api/admin/custom-automations', 'POST').length === 1, 'post');
  assert.equal(fetchMock.callsTo('/api/admin/custom-automations', 'POST')[0].body.temperature, 0);
});

test('custom: create errors toast the server detail, or "Create failed" for a non-JSON body', async () => {
  await openCustom();
  setValue('#cust-name', 'Dup');
  setValue('#cust-instructions', 'x');
  S.createResp = () => jsonResponse({ detail: "a custom automation named like 'dup' already exists" }, { status: 400 });
  click($('cust-create'));
  await until(() => !toast().classList.contains('hidden'), 'toast');
  assert.equal(toast().textContent, "a custom automation named like 'dup' already exists");
  assert.equal(toast().style.background, 'var(--red)');
  assert.equal(fetchMock.callsTo('/api/automations/manifest').length, 0, 'no manifest reload on failure');

  S.createResp = () => new Response('Internal Server Error', { status: 500 });
  click($('cust-create'));
  await until(() => toast().textContent === 'Create failed', 'fallback toast');

  S.createResp = () => jsonResponse({ id: 1 }, { status: 200 });  // only 201 counts as created
  click($('cust-create'));
  await until(() => toast().textContent === 'Create failed', 'non-201 treated as failure');
});

test('custom: Disable/Enable PATCHes the flipped flag and re-renders', async () => {
  S.customs = [
    { id: 1, slug: 'a', name: 'A', icon: '✨', description: '', fields: [], enabled: true },
    { id: 2, slug: 'b', name: 'B', icon: '✨', description: '', fields: [], enabled: false },
  ];
  await openCustom();
  fetchMock.reset();
  click(custom().querySelector('[data-cust-toggle="1"]'));
  await until(() => fetchMock.callsTo('/api/admin/custom-automations', 'GET').length === 1, 're-render');
  assert.deepEqual(fetchMock.callsTo('/api/admin/custom-automations/1', 'PATCH')[0].body, { enabled: false });

  await until(() => custom().querySelector('[data-cust-toggle="2"]'), 'rows');
  click(custom().querySelector('[data-cust-toggle="2"]'));
  await until(() => fetchMock.callsTo('/api/admin/custom-automations/2', 'PATCH').length === 1, 'patch');
  assert.deepEqual(fetchMock.callsTo('/api/admin/custom-automations/2', 'PATCH')[0].body, { enabled: true });
});

test('custom: Delete goes through the confirm dialog (cancel = no request, OK = DELETE)', async () => {
  S.customs = [{ id: 9, slug: 'gone', name: 'Gone', icon: '✨', description: '', fields: [], enabled: true }];
  await openCustom();
  fetchMock.reset();
  click(custom().querySelector('[data-cust-del="9"]'));
  await flush();
  assert.equal($('confirm-modal').classList.contains('hidden'), false);
  assert.equal($('confirm-title').textContent, 'Delete Custom Automation');
  assert.equal($('confirm-msg').textContent, 'Delete this custom automation?');
  click($('confirm-cancel'));
  await flush();
  assert.equal(fetchMock.calls.length, 0);

  click(custom().querySelector('[data-cust-del="9"]'));
  await flush();
  click($('confirm-ok'));
  await until(() => fetchMock.callsTo('/api/admin/custom-automations', 'GET').length === 1, 're-render');
  assert.equal(fetchMock.callsTo('/api/admin/custom-automations/9', 'DELETE').length, 1);
  assert.ok($('confirm-modal').classList.contains('hidden'));
});

// ── Sessions ──────────────────────────────────────────────────────────────────

const SESSIONS = (over = {}) => ({
  login_enabled: true,
  sessions: [
    { name: 'tasker', label: 'Tasker', state_path: 'sessions/tasker.json', exists: true, fresh: true,
      age_seconds: 90061, login_in_progress: false, last_login: null },
    { name: 'tw104', label: '104', state_path: 'sessions/104.json', exists: true, fresh: false,
      age_seconds: 3720, login_in_progress: false,
      last_login: { status: 'failed', message: 'Timed out <waiting>' } },
    { name: 'shopee', label: 'Shopee', state_path: 'sessions/shopee.json', exists: false, fresh: false,
      age_seconds: null, login_in_progress: false, last_login: null },
  ],
  ...over,
});
const sessRow = (name) => $('admin-sessions').querySelector(`[data-session-login="${name}"]`).closest('.toggle-row');
async function openSessions() {
  switchTab('sessions');
  await until(() => $('admin-sessions').querySelector('.admin-card'), 'sessions card');
}

test('sessions: freshness badge, relative age and last-login status per session', async () => {
  S.sessions = SESSIONS();
  await openSessions();
  assert.equal($('admin-sessions').hidden, false);
  assert.equal(sessRow('tasker').querySelector('.badge').textContent, 'fresh · 1d 1h ago');
  assert.ok(sessRow('tasker').querySelector('.badge').classList.contains('badge-on'));
  assert.equal(sessRow('tasker').querySelector('code').textContent, 'sessions/tasker.json');

  const t104 = sessRow('tw104');
  assert.equal(t104.querySelector('.badge').textContent, 'stale · 1h 2m ago');
  assert.ok(t104.querySelector('.badge').classList.contains('badge-off'));
  const status = t104.querySelectorAll('.badge')[1];
  assert.equal(status.textContent, 'failed');
  assert.ok(status.classList.contains('badge-off'));
  assert.match(t104.textContent, /Timed out <waiting>/, 'message escaped and shown');

  assert.equal(sessRow('shopee').querySelector('.badge').textContent, 'no session');
  const btns = [...$('admin-sessions').querySelectorAll('[data-session-login]')];
  assert.ok(btns.every((b) => !b.disabled && b.textContent.trim() === 'Refresh login'));
  assert.doesNotMatch($('admin-sessions').textContent, /BROWSER_LOGIN_ENABLED=0/);
});

test('sessions: minute / just-now / unknown ages', async () => {
  S.sessions = { login_enabled: true, sessions: [
    { name: 'a', label: 'A', state_path: 'a', exists: true, fresh: true, age_seconds: 125, login_in_progress: false, last_login: null },
    { name: 'b', label: 'B', state_path: 'b', exists: true, fresh: true, age_seconds: 59, login_in_progress: false, last_login: null },
    { name: 'c', label: 'C', state_path: 'c', exists: true, fresh: false, age_seconds: null, login_in_progress: false, last_login: null },
    { name: 'd', label: 'D', state_path: 'd', exists: true, fresh: true, age_seconds: 0, login_in_progress: false, last_login: { status: 'succeeded', message: null } },
  ] };
  await openSessions();
  assert.equal(sessRow('a').querySelector('.badge').textContent, 'fresh · 2m ago');
  assert.equal(sessRow('b').querySelector('.badge').textContent, 'fresh · just now');
  assert.equal(sessRow('c').querySelector('.badge').textContent, 'stale · —');
  assert.equal(sessRow('d').querySelector('.badge').textContent, 'fresh · just now');
  assert.ok(sessRow('d').querySelectorAll('.badge')[1].classList.contains('badge-on'));
});

test('sessions: labels, names and paths are HTML-escaped', async () => {
  S.sessions = { login_enabled: true, sessions: [
    { name: 'x"y', label: XSS, state_path: '<b>p</b>', exists: false, fresh: false, age_seconds: null, login_in_progress: false, last_login: null },
  ] };
  switchTab('sessions');
  await until(() => $('admin-sessions').querySelector('[data-session-login]'), 'row');
  assert.equal($('admin-sessions').querySelector('img, b'), null);
  assert.equal($('admin-sessions').querySelector('.toggle-row strong').textContent, XSS);
  assert.equal($('admin-sessions').querySelector('[data-session-login]').dataset.sessionLogin, 'x"y');
});

test('sessions: disabled server shows the note and disables every login button', async () => {
  S.sessions = SESSIONS({ login_enabled: false });
  await openSessions();
  assert.match($('admin-sessions').textContent, /BROWSER_LOGIN_ENABLED=0/);
  assert.ok([...$('admin-sessions').querySelectorAll('[data-session-login]')].every((b) => b.disabled));
});

test('sessions: empty list and load failure', async () => {
  S.sessions = { login_enabled: true, sessions: [] };
  await openSessions();
  assert.match($('admin-sessions').textContent, /No browser-login automations\./);

  S.sessionsStatus = 500;
  switchTab('sessions');
  await until(() => /Failed to load\./.test($('admin-sessions').textContent), 'error');
});

test('sessions: the Loading placeholder only replaces content on the first render', async () => {
  S.sessions = SESSIONS();
  await openSessions();  // dataset.rendered is now set
  let release;
  S.sessionsGate = new Promise((r) => { release = r; });
  click(document.querySelector('#admin-tabs [data-adm="sessions"]'));  // no panel clear here
  await flush();
  assert.ok($('admin-sessions').querySelector('[data-session-login="tasker"]'), 'old content kept while reloading');
  assert.doesNotMatch($('admin-sessions').textContent, /Loading…/);
  release();
  await flush();
});

test('sessions: Refresh login POSTs to the URL-encoded session and re-renders', async () => {
  S.sessions = SESSIONS({ sessions: [{ name: 'my session', label: 'Mine', state_path: 'm', exists: false,
    fresh: false, age_seconds: null, login_in_progress: false, last_login: null }] });
  await openSessions();
  fetchMock.reset();
  const btn = $('admin-sessions').querySelector('[data-session-login]');
  click(btn);
  assert.equal(btn.disabled, true, 'disabled immediately to avoid double launches');
  await until(() => fetchMock.callsTo('/api/sessions', 'GET').length === 1, 're-render');
  const [post] = fetchMock.callsTo(/\/login$/, 'POST');
  assert.equal(post.url.pathname, '/api/sessions/my%20session/login');
});

test('sessions: a rejected login alerts the detail and re-enables the button', async () => {
  S.sessions = SESSIONS();
  S.loginResp = () => jsonResponse({ detail: 'Browser login is disabled on this server' }, { status: 403 });
  await openSessions();
  fetchMock.reset();
  const btn = $('admin-sessions').querySelector('[data-session-login="tasker"]');
  click(btn);
  await until(() => alerts.length === 1, 'alert');
  assert.deepEqual(alerts, ['Browser login is disabled on this server']);
  assert.equal(btn.disabled, false);
  assert.equal(fetchMock.callsTo('/api/sessions', 'GET').length, 0);

  S.loginResp = () => new Response('nope', { status: 500 });
  click(btn);
  await until(() => alerts.length === 2, 'second alert');
  assert.equal(alerts[1], 'Failed to start login');
});

test('sessions: a running login polls every 2 s and stops once the tab is left', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  S.sessions = SESSIONS({ sessions: [{ name: 'tasker', label: 'Tasker', state_path: 's', exists: true, fresh: false,
    age_seconds: 10, login_in_progress: true, last_login: { status: 'running', message: 'Waiting for login…' } }] });
  await openSessions();
  const btn = $('admin-sessions').querySelector('[data-session-login="tasker"]');
  assert.equal(btn.textContent.trim(), 'Logging in…');
  assert.equal(btn.disabled, true);
  assert.ok(sessRow('tasker').querySelectorAll('.badge')[1].classList.contains('muted'));

  fetchMock.reset();
  t.mock.timers.tick(1999);
  await flush();
  assert.equal(fetchMock.callsTo('/api/sessions').length, 0);
  t.mock.timers.tick(1);
  await until(() => fetchMock.callsTo('/api/sessions').length === 1, 'poll');

  // Login finished → no further poll scheduled.
  S.sessions = SESSIONS();
  await flush();
  t.mock.timers.tick(2000);
  await until(() => fetchMock.callsTo('/api/sessions').length === 2, 'second poll');
  await until(() => !$('admin-sessions').querySelector('[data-session-login="tasker"]').disabled, 'done');
  t.mock.timers.tick(10000);
  await flush();
  assert.equal(fetchMock.callsTo('/api/sessions').length, 2, 'polling stopped');

  // Running again, then switch tabs: the queued poll fires but must not fetch.
  S.sessions = SESSIONS({ sessions: [{ ...SESSIONS().sessions[0], login_in_progress: true }] });
  switchTab('sessions');
  await until(() => fetchMock.callsTo('/api/sessions').length === 3, 'reload');
  await flush();
  switchTab('custom');
  await flush();
  fetchMock.reset();
  t.mock.timers.tick(2000);
  await flush();
  assert.equal(fetchMock.callsTo('/api/sessions').length, 0, 'stale poll skipped after leaving the tab');
});
