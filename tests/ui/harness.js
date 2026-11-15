// Test harness for the ES-module web UI (ui/js/**).
//
// The modules bind to elements of the real ui/index.html at import time (top-level
// `document.getElementById(...).addEventListener(...)`), so every test file loads
// that page into jsdom, exposes the browser globals the modules use, installs a
// routable fetch mock, and only then imports the module under test.
//
// `node --test` runs each *.test.js file in its own process, and ES modules are
// cached per process — so call `setupDom()` once per file (top level or `before`),
// then import modules with `importUi()`. Tests within a file share one page.

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { JSDOM } from 'jsdom';

const UI_DIR = new URL('../../ui/', import.meta.url);

// ── fetch mock ────────────────────────────────────────────────────────────────

/** Build a Response-like object (what the UI code reads: ok/status/json/text/blob/headers). */
export function jsonResponse(body, { status = 200, headers = {} } = {}) {
  const text = typeof body === 'string' ? body : JSON.stringify(body);
  return new Response(text, {
    status,
    headers: { 'content-type': 'application/json', ...headers },
  });
}

/**
 * Routable fetch mock. `routes` is a list of `[method, pathOrRegex, handler]`;
 * handler gets `(url: URL, init, call)` and returns a Response, a plain object
 * (→ JSON 200) or a number (→ empty response with that status). Unmatched
 * requests return 404 `{detail: "no mock route"}` and are recorded like any other
 * call, so tests can assert on `fetchMock.calls`.
 */
export function createFetchMock(routes = []) {
  const state = { routes: [...routes], calls: [] };
  async function fetchMock(input, init = {}) {
    const raw = typeof input === 'string' ? input : (input?.href ?? input?.url ?? String(input));
    const url = new URL(raw, 'http://localhost/');
    const method = (init.method || 'GET').toUpperCase();
    let body = init.body;
    if (typeof body === 'string') {
      try { body = JSON.parse(body); } catch { /* keep string */ }
    }
    const call = { method, path: url.pathname, url, init, body };
    state.calls.push(call);
    // Last-registered route wins, so tests can override defaults with `fetchMock.on`.
    for (let i = state.routes.length - 1; i >= 0; i--) {
      const [m, pattern, handler] = state.routes[i];
      if (m !== '*' && m.toUpperCase() !== method) continue;
      const hit = pattern instanceof RegExp ? pattern.test(url.pathname) : pattern === url.pathname;
      if (!hit) continue;
      const out = await handler(url, init, call);
      if (out instanceof Response) return out;
      if (typeof out === 'number') return new Response(null, { status: out });
      return jsonResponse(out ?? {});
    }
    return jsonResponse({ detail: 'no mock route' }, { status: 404 });
  }
  fetchMock.calls = state.calls;
  /** Add (or override) a route. */
  fetchMock.on = (method, pattern, handler) => { state.routes.push([method, pattern, handler]); };
  /** Calls whose path matches `pattern` (string = exact, RegExp = test), optionally by method. */
  fetchMock.callsTo = (pattern, method) => state.calls.filter((c) =>
    (pattern instanceof RegExp ? pattern.test(c.path) : c.path === pattern)
    && (!method || c.method === method.toUpperCase()));
  fetchMock.reset = () => { state.calls.length = 0; };
  return fetchMock;
}

// ── Browser API stubs jsdom doesn't provide ─────────────────────────────────────

export class FakeEventSource {
  static instances = [];
  constructor(url) {
    this.url = url;
    this.closed = false;
    this.onmessage = null;
    this.onerror = null;
    FakeEventSource.instances.push(this);
  }
  close() { this.closed = true; }
  /** Deliver a server-sent event; `data` is JSON-encoded unless already a string. */
  emit(data) {
    this.onmessage?.({ data: typeof data === 'string' ? data : JSON.stringify(data) });
  }
  fail() { this.onerror?.(new Event('error')); }
}

// ── DOM setup ──────────────────────────────────────────────────────────────────

let current = null;

/**
 * Load ui/index.html into jsdom and expose its globals on `globalThis`.
 * Returns `{ window, document, fetchMock, EventSource }`. Idempotent per process.
 *
 * Options:
 *   routes  — initial fetch-mock routes (see createFetchMock)
 *   url     — page URL (default http://localhost/), e.g. to test `?sso_error=…` / `#hash`
 */
export function setupDom({ routes = [], url = 'http://localhost/' } = {}) {
  if (current) return current;

  const html = readFileSync(new URL('index.html', UI_DIR), 'utf8')
    // The module entry point is imported explicitly by the tests that need it.
    .replace(/<script type="module"[^>]*><\/script>/, '');

  const dom = new JSDOM(html, { url, pretendToBeVisual: true });
  const { window } = dom;

  const fetchMock = createFetchMock(routes);
  window.fetch = fetchMock;
  window.EventSource = FakeEventSource;
  window.alert = () => {};
  window.confirm = () => true;
  window.prompt = () => null;
  window.scrollTo = () => {};
  window.HTMLElement.prototype.scrollIntoView = function () {};
  window.matchMedia ??= () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
  Object.defineProperty(window.navigator, 'clipboard', {
    configurable: true,
    value: { writes: [], async writeText(t) { this.writes.push(t); } },
  });

  // Expose the page globals the modules reference as bare identifiers.
  //  * Names Node lacks (document, HTMLElement, localStorage, …) → jsdom's.
  //  * DOM classes Node *also* defines (Event, EventTarget, FormData, File, Blob, …)
  //    → jsdom's too: jsdom elements only accept jsdom's own Event/FormData objects.
  //  * Everything else Node already has (URL, Response, timers, console, …) stays
  //    Node's, so the fetch mock's `Response` objects work with the UI code.
  const forceJsdom = /^(window|document|navigator|location|history|localStorage|sessionStorage|HTML|SVG|Element|Node|Event|Document|Text|Comment|DOM|Mouse|Keyboard|Input|Custom|Focus|Form|File|Blob|Storage|Mutation)/;
  for (const key of Object.getOwnPropertyNames(window)) {
    if (key in globalThis && !forceJsdom.test(key)) continue;
    try {
      Object.defineProperty(globalThis, key, {
        configurable: true,
        get: () => window[key],
        set: (v) => { window[key] = v; },
      });
    } catch { /* non-configurable Node global — leave it */ }
  }
  // In a browser `window === globalThis`, so `window.fetch = …` (auth.js's 401
  // wrapper) changes what a bare `fetch(...)` calls. Mirror that link.
  for (const key of ['fetch', 'EventSource', 'alert', 'confirm', 'getComputedStyle', 'requestAnimationFrame', 'cancelAnimationFrame']) {
    Object.defineProperty(globalThis, key, {
      configurable: true,
      get: () => window[key],
      set: (v) => { window[key] = v; },
    });
  }

  // runs.js polls with a top-level `setInterval`; unref every timer so a test
  // process exits when its tests finish instead of hanging on the poller.
  const realSetInterval = globalThis.setInterval;
  const realSetTimeout = globalThis.setTimeout;
  globalThis.setInterval = (...a) => { const t = realSetInterval(...a); t?.unref?.(); return t; };
  globalThis.setTimeout = (...a) => { const t = realSetTimeout(...a); t?.unref?.(); return t; };
  window.setInterval = globalThis.setInterval;
  window.setTimeout = globalThis.setTimeout;

  current = { dom, window, document: window.document, fetchMock, EventSource: FakeEventSource };
  return current;
}

/** Import a UI module by its path under ui/js/, e.g. `importUi('helpers.js')`. */
export function importUi(rel) {
  if (!current) throw new Error('call setupDom() before importUi()');
  return import(new URL(`js/${rel}`, UI_DIR).href);
}

/** Absolute filesystem path of a UI file (for static checks). */
export function uiPath(rel) {
  return fileURLToPath(new URL(rel, UI_DIR));
}

/** Let pending promise chains / zero-delay timers run (fetch → json → render). */
export async function flush(times = 5) {
  for (let i = 0; i < times; i++) await new Promise((r) => setImmediate(r));
}

/** Dispatch a bubbling click on an element (or a selector). */
export function click(target) {
  const el = typeof target === 'string' ? current.document.querySelector(target) : target;
  if (!el) throw new Error(`click: no element for ${target}`);
  el.dispatchEvent(new current.window.MouseEvent('click', { bubbles: true, cancelable: true }));
  return el;
}

/** Set a form control's value and fire `input` + `change`. */
export function setValue(target, value) {
  const el = typeof target === 'string' ? current.document.querySelector(target) : target;
  if (!el) throw new Error(`setValue: no element for ${target}`);
  if (el.type === 'checkbox' || el.type === 'radio') el.checked = Boolean(value);
  else el.value = value;
  el.dispatchEvent(new current.window.Event('input', { bubbles: true }));
  el.dispatchEvent(new current.window.Event('change', { bubbles: true }));
  return el;
}

/** Shorthand for `document.getElementById`. */
export const $ = (id) => current.document.getElementById(id);
