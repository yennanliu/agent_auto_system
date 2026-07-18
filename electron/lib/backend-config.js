'use strict';

// Pure configuration helpers for launching the FastAPI sidecar. No electron / no
// fs imports on purpose — kept side-effect-free so it can be unit-tested with the
// plain Node test runner (see ../test/backend-config.test.js). main.js wires these
// to the real electron app + process.
const path = require('node:path');

/**
 * Build the environment the FastAPI sidecar runs under.
 * All writable paths point inside `dataDir` (the per-user app-data dir) so nothing
 * is written into the read-only .app bundle or the repo.
 */
function buildBackendEnv({ port, dataDir, baseEnv = {} }) {
  return {
    ...baseEnv,
    PORT: String(port),
    // Single local admin, auto-authenticated → no login screen (see src/auth.py).
    DESKTOP_MODE: '1',
    SCHEDULER_ENABLED: '1',
    // Headed browser-login refresh is a desktop v2; off in the packaged app.
    BROWSER_LOGIN_ENABLED: '0',
    APP_DATA_DIR: dataDir,
    DATABASE_URL: `sqlite:///${path.join(dataDir, 'app.db')}`,
    UPLOAD_DIR: path.join(dataDir, 'uploads'),
    PYTHONUNBUFFERED: '1',
  };
}

/**
 * Decide how to launch the backend.
 *  - packaged: a PyInstaller-frozen binary shipped under resourcesPath (Phase 2).
 *  - dev/spike: drive uvicorn through `uv` so deps resolve from the project .venv.
 */
function resolveBackend({
  port,
  dataDir,
  isPackaged,
  resourcesPath,
  projectRoot,
  baseEnv = {},
}) {
  const env = buildBackendEnv({ port, dataDir, baseEnv });

  if (isPackaged) {
    const bin = path.join(resourcesPath, 'backend', 'agent-auto-system');
    return { command: bin, args: [], env, cwd: resourcesPath };
  }

  return {
    command: 'uv',
    args: [
      'run',
      'uvicorn',
      'src.main:app',
      '--host',
      '127.0.0.1',
      '--port',
      String(port),
    ],
    env,
    cwd: projectRoot,
  };
}

module.exports = { buildBackendEnv, resolveBackend };
