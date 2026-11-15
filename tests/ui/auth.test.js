// auth.js — login overlay, SSO buttons, ?login_error handling, 401 fetch wrapper,
// onAuthenticated / bootApp, login form and logout.
// One page per file, so the order matters: SSO discovery failures run before the
// successful render (it renders once), and the user state is set per test via the API.
import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { setupDom, importUi, jsonResponse, click, flush, setValue, $ } from './harness.js';

const { window, fetchMock } = setupDom({
  routes: [
    ['GET', '/api/runs', () => []],
    ['GET', '/api/associations', () => ({ associations: [] })],
    ['GET', '/api/system', () => ({ agents: [] })],
    ['GET', '/api/automations/manifest', () => ({ automations: [
      { job_type: 'web_scraper', name: 'Web <b>Scraper</b>', icon: '🌐', desc: 'd', steps: [] },
    ] })],
    ['POST', '/api/auth/logout', () => ({ ok: true })],
  ],
});
let providersHandler = () => 500;
fetchMock.on('GET', '/api/auth/providers', (...a) => providersHandler(...a));

const auth = await importUi('auth.js');
const { showLogin, showSsoRedirectError, onAuthenticated, bootApp } = auth;

const overlay = $('login-overlay');
const loginError = $('login-error');
const shown = () => overlay.classList.contains('show');
const hideOverlay = () => overlay.classList.remove('show');
const submitLogin = () => $('login-form').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));

describe('showLogin + SSO buttons', () => {
  test('shows the overlay, clears the user, hides the user menu, focuses username', async () => {
    onAuthenticated({ username: 'a', is_admin: false });
    assert.ok(auth.CURRENT_USER);
    showLogin();
    await flush();
    assert.ok(shown());
    assert.equal(auth.CURRENT_USER, null);
    assert.equal($('user-menu').hidden, true);
    assert.equal(document.activeElement, $('login-username'));
  });

  test('providers endpoint error / empty list → no SSO buttons, divider stays hidden', async () => {
    for (const h of [() => 500, () => ({ providers: [] }), () => ({}), () => { throw new TypeError('offline'); }]) {
      providersHandler = h;
      showLogin();
      await flush();
      assert.equal($('login-sso').children.length, 0);
      assert.equal($('login-sso-divider').hidden, true);
    }
  });

  test('configured providers render one sign-in link each, then discovery is cached', async () => {
    providersHandler = () => ({ providers: [
      { name: 'google', label: 'Google' },
      { name: 'github', label: 'GitHub' },
      { name: 'okta', label: 'Okta' },
    ] });
    showLogin();
    await flush();
    const links = [...$('login-sso').querySelectorAll('a.btn-sso')];
    assert.deepEqual(links.map((a) => a.getAttribute('href')),
      ['/api/auth/oauth/google/login', '/api/auth/oauth/github/login', '/api/auth/oauth/okta/login']);
    assert.deepEqual(links.map((a) => a.textContent), ['Sign in with Google', 'Sign in with GitHub', 'Sign in with Okta']);
    assert.ok(links[0].querySelector('svg') && links[1].querySelector('svg'));
    assert.equal(links[2].querySelector('svg'), null, 'unknown provider → no icon');
    assert.equal($('login-sso-divider').hidden, false);

    const n = fetchMock.callsTo('/api/auth/providers').length;
    showLogin();
    await flush();
    assert.equal(fetchMock.callsTo('/api/auth/providers').length, n, 'rendered once, not refetched');
  });
});

describe('showSsoRedirectError', () => {
  test('no login_error param → no message, URL untouched', () => {
    loginError.hidden = true;
    history.replaceState(null, '', '/?foo=1#run');
    showSsoRedirectError();
    assert.equal(loginError.hidden, true);
    assert.equal(location.search, '?foo=1');
    assert.equal(location.hash, '#run');
  });

  test('sso_failed → generic message, param removed, other params + hash kept', () => {
    history.replaceState(null, '', '/?foo=1&login_error=sso_failed&bar=2#system');
    showSsoRedirectError();
    assert.equal(loginError.hidden, false);
    assert.equal(loginError.textContent, 'SSO sign-in failed. Please try again.');
    assert.equal(location.pathname + location.search + location.hash, '/?foo=1&bar=2#system');
  });

  test('account_disabled → specific message; sole param leaves a clean URL', () => {
    history.replaceState(null, '', '/?login_error=account_disabled');
    showSsoRedirectError();
    assert.equal(loginError.textContent, 'Your account is disabled. Contact an admin.');
    assert.equal(location.href, 'http://localhost/');
  });

  test('unknown error codes are shown as text (never as HTML)', () => {
    history.replaceState(null, '', '/?login_error=%3Cimg%20src%3Dx%3E');
    showSsoRedirectError();
    assert.equal(loginError.textContent, 'SSO sign-in failed. Please try again.');
    assert.equal(loginError.querySelector('img'), null);
    assert.equal(location.search, '');
  });

  test('a second call after cleanup is a no-op', () => {
    loginError.hidden = true;
    showSsoRedirectError();
    assert.equal(loginError.hidden, true);
  });
});

describe('onAuthenticated', () => {
  test('regular user: overlay hidden, chip = username, admin tab hidden, history loaded', async () => {
    showLogin();
    loginError.hidden = false;
    $('login-username').value = 'typed';
    const before = fetchMock.callsTo('/api/runs').length;
    const user = { username: 'alice', is_admin: false };
    onAuthenticated(user);
    await flush();
    assert.equal(auth.CURRENT_USER, user);
    assert.ok(!shown());
    assert.equal($('user-menu').hidden, false);
    assert.equal($('user-chip').textContent, 'alice');
    assert.equal($('nav-admin').hidden, true);
    assert.equal(loginError.hidden, true);
    assert.equal($('login-username').value, '', 'login form reset');
    assert.equal(fetchMock.callsTo('/api/runs').length, before + 1);
  });

  test('admin: chip suffix and Admin tab visible', () => {
    onAuthenticated({ username: 'root', is_admin: true });
    assert.equal($('user-chip').textContent, 'root · admin');
    assert.equal($('nav-admin').hidden, false);
  });

  test('username is rendered as text', () => {
    onAuthenticated({ username: '<img src=x onerror=alert(1)>', is_admin: false });
    assert.equal($('user-chip').querySelector('img'), null);
    assert.equal($('user-chip').textContent, '<img src=x onerror=alert(1)>');
    assert.equal($('nav-admin').hidden, true);
  });
});

describe('bootApp', () => {
  const cases = [['#system', 'system'], ['#bogus', 'landing'], ['', 'landing']];
  for (const [hash, page] of cases) {
    test(`hash "${hash}" → ${page} page, associations loaded`, async () => {
      onAuthenticated({ username: 'a', is_admin: false, allowed_automations: '*' });
      history.replaceState(null, '', `/${hash}`);
      const before = fetchMock.callsTo('/api/associations').length;
      bootApp();
      await flush();
      assert.ok($(`page-${page}`).classList.contains('active'));
      assert.equal(location.hash, `#${page}`);
      assert.equal(fetchMock.callsTo('/api/associations').length, before + 1);
    });
  }

  test('"#admin" for a non-admin lands on landing', () => {
    onAuthenticated({ username: 'a', is_admin: false });
    history.replaceState(null, '', '/#admin');
    bootApp();
    assert.ok($('page-landing').classList.contains('active'));
  });
});

describe('window.fetch 401 wrapper', () => {
  test('a 401 from an /api/ call shows the login overlay and returns the response', async () => {
    onAuthenticated({ username: 'a', is_admin: false });
    fetchMock.on('GET', '/api/jobs', () => 401);
    const resp = await window.fetch('/api/jobs');
    assert.equal(resp.status, 401);
    assert.ok(shown());
    assert.equal(auth.CURRENT_USER, null);
    assert.equal($('user-menu').hidden, true);
  });

  test('bare fetch() goes through the same wrapper', async () => {
    hideOverlay();
    await fetch('/api/jobs');
    assert.ok(shown());
  });

  // (A `URL` object input is also handled by auth.js via `.href`, but the harness fetch
  // mock only understands strings and Request objects, so it isn't exercised here.)
  test('Request inputs are recognised', async () => {
    hideOverlay();
    await window.fetch(new Request('http://localhost/api/jobs'));
    assert.ok(shown());
  });

  test('no overlay for: /api/auth/login 401, non-/api/ 401, non-401 errors, success', async () => {
    fetchMock.on('*', '/api/auth/login', () => 401);
    fetchMock.on('GET', '/static/x', () => 401);
    fetchMock.on('GET', '/api/forbidden', () => 403);
    fetchMock.on('GET', '/api/boom', () => 500);
    fetchMock.on('GET', '/api/ok', () => ({ ok: 1 }));
    for (const url of ['/api/auth/login', '/static/x', '/api/forbidden', '/api/boom', '/api/ok']) {
      hideOverlay();
      const resp = await window.fetch(url, url === '/api/auth/login' ? { method: 'POST' } : undefined);
      assert.ok(!shown(), url);
      assert.ok(resp instanceof Response);
    }
  });

  test('forwards init to the underlying fetch', async () => {
    fetchMock.on('PUT', '/api/echo', (_u, _i, call) => ({ body: call.body }));
    const resp = await window.fetch('/api/echo', { method: 'PUT', body: JSON.stringify({ x: 1 }) });
    assert.deepEqual(await resp.json(), { body: { x: 1 } });
  });

  test('network errors propagate to the caller', async () => {
    fetchMock.on('GET', '/api/down', () => { throw new TypeError('Failed to fetch'); });
    await assert.rejects(window.fetch('/api/down'), TypeError);
  });
});

describe('login form', () => {
  test('success: posts credentials, authenticates, loads manifest, renders cards, boots', async () => {
    showLogin();
    history.replaceState(null, '', '/#system');
    let resolveLogin;
    fetchMock.on('POST', '/api/auth/login', () => new Promise((r) => { resolveLogin = r; }));
    setValue('#login-username', 'alice');
    setValue('#login-password', 's3cret');
    submitLogin();
    await flush();
    assert.equal($('login-submit').disabled, true, 'button disabled while in flight');
    const call = fetchMock.callsTo('/api/auth/login', 'POST').at(-1);
    assert.deepEqual(call.body, { username: 'alice', password: 's3cret' });
    assert.equal(call.init.headers['Content-Type'], 'application/json');

    resolveLogin(jsonResponse({ username: 'alice', is_admin: false, allowed_automations: '*' }));
    await flush(10);
    assert.equal($('login-submit').disabled, false);
    assert.ok(!shown());
    assert.equal(auth.CURRENT_USER.username, 'alice');
    assert.equal($('user-chip').textContent, 'alice');
    assert.ok(fetchMock.callsTo('/api/automations/manifest').length >= 1);
    const card = $('type-grid').querySelector('.type-card[data-type="web_scraper"]');
    assert.ok(card, 'type cards rendered from the manifest');
    assert.equal(card.querySelector('.type-card-name').textContent, 'Web <b>Scraper</b>');
    assert.equal(card.querySelector('b'), null);
    assert.ok($('page-system').classList.contains('active'), 'booted to the hash page');
  });

  const failures = [
    [401, { detail: 'Invalid username or password' }, 'Invalid username or password'],
    [429, { detail: 'Too many login attempts. Try again in 60s.' }, 'Too many login attempts. Try again in 60s.'],
    [500, '<html>Internal Server Error</html>', 'Sign in failed'],
    [400, {}, 'Sign in failed'],
  ];
  for (const [status, body, msg] of failures) {
    test(`HTTP ${status} → inline error "${msg}", overlay stays, user unset`, async () => {
      showLogin();
      fetchMock.on('POST', '/api/auth/login', () => jsonResponse(body, { status }));
      setValue('#login-username', 'alice');
      setValue('#login-password', 'wrong');
      const manifestCalls = fetchMock.callsTo('/api/automations/manifest').length;
      submitLogin();
      await flush(10);
      assert.equal(loginError.hidden, false);
      assert.equal(loginError.textContent, msg);
      assert.ok(shown());
      assert.equal(auth.CURRENT_USER, null);
      assert.equal($('login-submit').disabled, false, 'button re-enabled');
      assert.equal($('login-username').value, 'alice', 'form not reset on failure');
      assert.equal(fetchMock.callsTo('/api/automations/manifest').length, manifestCalls);
    });
  }

  test('error detail is rendered as text', async () => {
    fetchMock.on('POST', '/api/auth/login', () => jsonResponse({ detail: '<img src=x onerror=alert(1)>' }, { status: 401 }));
    submitLogin();
    await flush(10);
    assert.equal(loginError.querySelector('img'), null);
    assert.equal(loginError.textContent, '<img src=x onerror=alert(1)>');
  });

  test('a new attempt hides the previous error while in flight', async () => {
    let resolveLogin;
    fetchMock.on('POST', '/api/auth/login', () => new Promise((r) => { resolveLogin = r; }));
    assert.equal(loginError.hidden, false);
    submitLogin();
    await flush();
    assert.equal(loginError.hidden, true);
    resolveLogin(jsonResponse({ detail: 'nope' }, { status: 401 }));
    await flush(10);
    assert.equal(loginError.textContent, 'nope');
  });

  test.todo('network failure during login shows an inline error (auth.js:110-131 has try/finally '
    + 'but no catch: the button is re-enabled, but no message is shown and the submit handler '
    + 'rejects unhandled)');
});

describe('logout', () => {
  test('POSTs /api/auth/logout then shows the login overlay', async () => {
    onAuthenticated({ username: 'alice', is_admin: true });
    click('#logout-btn');
    await flush();
    assert.equal(fetchMock.callsTo('/api/auth/logout', 'POST').length, 1);
    assert.ok(shown());
    assert.equal(auth.CURRENT_USER, null);
    assert.equal($('user-menu').hidden, true);
  });
});
