// Entry point (loaded by index.html as <script type="module">).

// ── Module map (was the single ui/app.js) ─────────────────────────────────────
// Each module keeps the code of its old app.js region verbatim and in the same
// relative order; only `export` / `import` were added. The one exception is the
// old "State" block: import bindings are read-only, so each piece of state now
// lives in the module that reassigns it (CURRENT_USER → auth.js, activeEventSource
// / activeJobType / activeLogs → progress.js, cachedRuns / runsPage / runsTotal →
// runs.js, systemData → system.js, confirmResolve → confirm.js, toastTimer →
// helpers.js, ALL_TYPES / MANIFEST → catalog.js). Never assign to an imported name.
//
// To use something from another module, `export` it there and `import` it here —
// an un-imported name is a ReferenceError at call time (modules have no globals).
//
// The side-effect imports below load every module so its top-level wiring (event
// listeners, the fetch 401 wrapper) runs. Import cycles between the page modules
// are fine and expected: imported `const`/`let` are only dereferenced inside
// functions and event handlers, which run after the whole graph has been evaluated
// (imported *functions* are hoisted, so passing one to addEventListener at top
// level is safe). Keep it that way — module top-level code may only read
// `const`/`let` from the cycle-free leaves listed first (net.js, dom.js,
// catalog.js, helpers.js); otherwise it can hit a TDZ ReferenceError at load.
import './net.js';
import './dom.js';
import './catalog.js';
import './helpers.js';
import './highlight.js';
import './confirm.js';
import './steps.js';
import './overview.js';
import './schedules.js';
import './navigation.js';
import './auth.js';
import './admin.js';
import './run-modal.js';
import './forms/email-collect.js';
import './forms/tw104-covers.js';
import './forms/profit-health.js';
import './forms/pipeline.js';
import './jobs.js';
import './runs.js';
import './progress.js';
import './system.js';
import './performance.js';
import { _origFetch } from './net.js';
import { loadManifest } from './catalog.js';
import { showLogin, showSsoRedirectError, onAuthenticated, bootApp } from './auth.js';
import { renderTypeCards } from './run-modal.js';
import { loadPerformancePage } from './performance.js';

// ── Global bridge for inline handlers ─────────────────────────────────────────
// ES module scope is not global, so any function named in an inline `on*="…"`
// attribute (in index.html or in an HTML string built in JS) must be published on
// `window` HERE, and only here. Prefer `addEventListener` / data-attribute
// delegation for new code. tests/unit/test_ui_modules.py checks this list.
Object.assign(window, {
  loadPerformancePage,   // performance.js — trend-window buttons: onclick="loadPerformancePage(${n})"
});

// Boot: only enter the app if there's a live session.
(async () => {
  try {
    const resp = await _origFetch('/api/auth/me');
    if (resp.ok) {
      onAuthenticated(await resp.json());
      await loadManifest();
      renderTypeCards();
      bootApp();
    } else {
      showLogin();
      showSsoRedirectError();
    }
  } catch {
    showLogin();
    showSsoRedirectError();
  }
})();
