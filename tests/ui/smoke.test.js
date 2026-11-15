// Harness smoke test: the whole module graph loads against the real index.html.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { setupDom, importUi, flush, $ } from './harness.js';

const { fetchMock } = setupDom({ routes: [['GET', '/api/auth/me', () => 401]] });

test('main.js loads every module and shows the login overlay without a session', async () => {
  await importUi('main.js');
  await flush();
  assert.ok(fetchMock.callsTo('/api/auth/me').length >= 1);
  assert.ok(!$('login-overlay').classList.contains('hidden'));
  assert.equal(typeof window.loadPerformancePage, 'function');
});
