// net.js — `_origFetch` is the fetch captured *before* auth.js wraps window.fetch.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { setupDom, importUi, flush, $ } from './harness.js';

const { fetchMock } = setupDom({
  routes: [
    ['GET', '/api/ping', () => ({ pong: true })],
    ['GET', '/api/secret', () => 401],
    ['GET', '/api/auth/providers', () => ({ providers: [] })],
  ],
});

const { _origFetch } = await importUi('net.js');

test('_origFetch is bound to window and reaches the installed fetch', async () => {
  const detached = _origFetch;                 // called without a receiver
  const resp = await detached('/api/ping');
  assert.equal(resp.status, 200);
  assert.deepEqual(await resp.json(), { pong: true });
  assert.equal(fetchMock.callsTo('/api/ping').length, 1);
});

test('_origFetch forwards init (method, body)', async () => {
  fetchMock.on('POST', '/api/echo', (_u, _i, call) => ({ got: call.body }));
  const resp = await _origFetch('/api/echo', { method: 'POST', body: JSON.stringify({ a: 1 }) });
  assert.deepEqual(await resp.json(), { got: { a: 1 } });
});

test('after auth.js wraps window.fetch, _origFetch still bypasses the 401 → login wrapper', async () => {
  await importUi('auth.js');
  assert.notEqual(window.fetch, fetchMock, 'auth.js should have installed its wrapper');
  const overlay = $('login-overlay');
  overlay.classList.remove('show');

  const resp = await _origFetch('/api/secret');
  await flush();
  assert.equal(resp.status, 401);
  assert.ok(!overlay.classList.contains('show'), '_origFetch must not trigger the login overlay');

  // Contrast: the wrapped window.fetch does.
  await window.fetch('/api/secret');
  await flush();
  assert.ok(overlay.classList.contains('show'));
});
