'use strict';

// Minimal, locked-down preload. contextIsolation is on and nodeIntegration is
// off in the renderer, so the web UI runs exactly as it does in a browser with
// no privileged Node access. Kept intentionally empty for v1; expose narrow,
// audited APIs via contextBridge here if the UI ever needs native affordances.
