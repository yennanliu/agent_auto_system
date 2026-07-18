'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { buildBackendEnv, resolveBackend } = require('../lib/backend-config');

test('buildBackendEnv sets desktop single-admin + loopback wiring', () => {
  const env = buildBackendEnv({ port: 51234, dataDir: '/data' });
  assert.equal(env.PORT, '51234');
  assert.equal(env.DESKTOP_MODE, '1'); // single local admin, no login screen
  assert.equal(env.BROWSER_LOGIN_ENABLED, '0');
  assert.equal(env.APP_DATA_DIR, '/data');
});

test('buildBackendEnv keeps DB + uploads inside the data dir (not the repo/bundle)', () => {
  const env = buildBackendEnv({ port: 1, dataDir: '/data' });
  assert.equal(env.DATABASE_URL, `sqlite:///${path.join('/data', 'app.db')}`);
  assert.equal(env.UPLOAD_DIR, path.join('/data', 'uploads'));
});

test('buildBackendEnv merges baseEnv but overrides collisions', () => {
  const env = buildBackendEnv({
    port: 1,
    dataDir: '/data',
    baseEnv: { HOME: '/Users/x', DESKTOP_MODE: 'should-be-overridden' },
  });
  assert.equal(env.HOME, '/Users/x'); // passthrough
  assert.equal(env.DESKTOP_MODE, '1'); // ours wins
});

test('resolveBackend (dev) drives uvicorn through uv from the project root', () => {
  const r = resolveBackend({
    port: 8137,
    dataDir: '/data',
    isPackaged: false,
    resourcesPath: '/res',
    projectRoot: '/repo',
  });
  assert.equal(r.command, 'uv');
  assert.deepEqual(r.args, [
    'run', 'uvicorn', 'src.main:app', '--host', '127.0.0.1', '--port', '8137',
  ]);
  assert.equal(r.cwd, '/repo');
  assert.equal(r.env.PORT, '8137');
});

test('resolveBackend (packaged) points at the frozen binary under resources', () => {
  const r = resolveBackend({
    port: 9000,
    dataDir: '/data',
    isPackaged: true,
    resourcesPath: '/res',
    projectRoot: '/repo',
  });
  assert.equal(r.command, path.join('/res', 'backend', 'agent-auto-system'));
  assert.deepEqual(r.args, []);
  assert.equal(r.cwd, '/res');
});
