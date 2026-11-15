// main.js — logged-out boot: /api/auth/me 401 → login overlay + SSO redirect error.
// (The logged-in boot needs its own process: see main-logged-in.test.js.)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { setupDom, importUi, flush, $ } from './harness.js';

const { fetchMock } = setupDom({
  url: 'http://localhost/?login_error=account_disabled&keep=1#run',
  routes: [
    ['GET', '/api/auth/me', () => 401],
    ['GET', '/api/auth/providers', () => ({ providers: [{ name: 'github', label: 'GitHub' }] })],
  ],
});

await importUi('main.js');
await flush(10);

test('checks the session once via /api/auth/me', () => {
  assert.equal(fetchMock.callsTo('/api/auth/me', 'GET').length, 1);
});

test('shows the login overlay and keeps the app locked', () => {
  assert.ok($('login-overlay').classList.contains('show'));
  assert.equal($('user-menu').hidden, true);
  assert.equal($('nav-admin').hidden, true);
});

test('does not load authenticated data (manifest, runs, associations)', () => {
  for (const path of ['/api/automations/manifest', '/api/runs', '/api/associations']) {
    assert.equal(fetchMock.callsTo(path).length, 0, path);
  }
  assert.equal($('type-grid').children.length, 0);
});

test('surfaces ?login_error and strips it from the URL, keeping other params and hash', () => {
  assert.equal($('login-error').hidden, false);
  assert.equal($('login-error').textContent, 'Your account is disabled. Contact an admin.');
  assert.equal(location.pathname + location.search + location.hash, '/?keep=1#run');
});

test('renders the configured SSO buttons on the login card', () => {
  const links = [...$('login-sso').querySelectorAll('a.btn-sso')];
  assert.deepEqual(links.map((a) => a.getAttribute('href')), ['/api/auth/oauth/github/login']);
  assert.equal($('login-sso-divider').hidden, false);
});

test('publishes the inline-handler bridge on window', async () => {
  const perf = await importUi('performance.js');
  assert.equal(window.loadPerformancePage, perf.loadPerformancePage);
});
