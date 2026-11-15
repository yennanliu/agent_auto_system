// Auth gate: login overlay, SSO buttons, 401 handling (fetch wrapper), logout, app boot hook.

import { _origFetch } from './net.js';
import { loadManifest } from './catalog.js';
import { navigate, VALID_PAGES } from './navigation.js';
import { renderTypeCards } from './run-modal.js';
import { loadAssociationDirectories } from './forms/email-collect.js';
import { loadHistory } from './runs.js';

// ── Auth gate ───────────────────────────────────────────────────────────────
// The whole app sits behind a login. We check /api/auth/me on boot; a 401 (here
// or on any later request) drops a full-screen login overlay back over the app.
export let CURRENT_USER = null;

const loginOverlay = document.getElementById('login-overlay');
const loginForm    = document.getElementById('login-form');
const loginError   = document.getElementById('login-error');
const userMenu     = document.getElementById('user-menu');
const userChip     = document.getElementById('user-chip');

export function showLogin() {
  CURRENT_USER = null;
  if (userMenu) userMenu.hidden = true;
  loginOverlay.classList.add('show');
  const u = document.getElementById('login-username');
  if (u) u.focus();
  renderSsoButtons();
}

// SSO ("Sign in with …") buttons — rendered only for providers the server says
// are configured (GET /api/auth/providers). Each button is just a link that
// kicks off the server-side OAuth redirect flow.
const SSO_ICONS = {
  google: '<svg viewBox="0 0 48 48" aria-hidden="true"><path fill="#EA4335" d="M24 9.5c3.5 0 6.6 1.2 9 3.6l6.7-6.7C35.6 2.4 30.1 0 24 0 14.6 0 6.5 5.4 2.6 13.2l7.8 6.1C12.3 13.3 17.7 9.5 24 9.5z"/><path fill="#4285F4" d="M46.5 24.5c0-1.6-.1-3.1-.4-4.5H24v9h12.7c-.6 3-2.3 5.5-4.8 7.2l7.4 5.7c4.3-4 6.8-9.9 6.8-17.4z"/><path fill="#FBBC05" d="M10.4 28.3c-.5-1.4-.7-2.9-.7-4.3s.3-2.9.7-4.3l-7.8-6.1C.9 16.7 0 20.2 0 24s.9 7.3 2.6 10.4l7.8-6.1z"/><path fill="#34A853" d="M24 48c6.1 0 11.3-2 15-5.5l-7.4-5.7c-2 1.4-4.7 2.3-7.6 2.3-6.3 0-11.7-3.8-13.6-9.3l-7.8 6.1C6.5 42.6 14.6 48 24 48z"/></svg>',
  github: '<svg viewBox="0 0 24 24" aria-hidden="true" fill="currentColor"><path d="M12 .5C5.4.5 0 5.9 0 12.6c0 5.3 3.4 9.8 8.2 11.4.6.1.8-.3.8-.6v-2c-3.3.7-4-1.6-4-1.6-.6-1.4-1.3-1.8-1.3-1.8-1.1-.7.1-.7.1-.7 1.2.1 1.8 1.2 1.8 1.2 1.1 1.8 2.8 1.3 3.5 1 .1-.8.4-1.3.8-1.6-2.7-.3-5.5-1.3-5.5-5.9 0-1.3.5-2.4 1.2-3.2-.1-.3-.5-1.5.1-3.2 0 0 1-.3 3.3 1.2a11.5 11.5 0 0 1 6 0C17.3 4.7 18.3 5 18.3 5c.6 1.7.2 2.9.1 3.2.8.8 1.2 1.9 1.2 3.2 0 4.6-2.8 5.6-5.5 5.9.4.4.8 1.1.8 2.2v3.3c0 .3.2.7.8.6A12 12 0 0 0 24 12.6C24 5.9 18.6.5 12 .5z"/></svg>',
};

let _ssoRendered = false;
async function renderSsoButtons() {
  if (_ssoRendered) return;
  const container = document.getElementById('login-sso');
  const divider = document.getElementById('login-sso-divider');
  if (!container) return;
  try {
    const resp = await _origFetch('/api/auth/providers');
    if (!resp.ok) return;
    const { providers } = await resp.json();
    if (!providers || !providers.length) return;
    container.innerHTML = providers.map(p =>
      `<a class="btn-sso" href="/api/auth/oauth/${p.name}/login">` +
      `${SSO_ICONS[p.name] || ''}<span>Sign in with ${p.label}</span></a>`
    ).join('');
    if (divider) divider.hidden = false;
    _ssoRendered = true;
  } catch { /* no SSO buttons if discovery fails */ }
}

// Surface a redirect-flow error (e.g. ?login_error=sso_failed) once, then clean
// it out of the URL so a refresh doesn't re-show it.
export function showSsoRedirectError() {
  const params = new URLSearchParams(location.search);
  const err = params.get('login_error');
  if (!err) return;
  const msg = err === 'account_disabled'
    ? 'Your account is disabled. Contact an admin.'
    : 'SSO sign-in failed. Please try again.';
  if (loginError) { loginError.textContent = msg; loginError.hidden = false; }
  params.delete('login_error');
  const qs = params.toString();
  history.replaceState(null, '', location.pathname + (qs ? '?' + qs : '') + location.hash);
}

export function onAuthenticated(user) {
  CURRENT_USER = user;
  loginOverlay.classList.remove('show');
  if (userMenu) userMenu.hidden = false;
  if (userChip) userChip.textContent = user.username + (user.is_admin ? ' · admin' : '');
  const navAdmin = document.getElementById('nav-admin');
  if (navAdmin) navAdmin.hidden = !user.is_admin;
  loginError.hidden = true;
  if (loginForm) loginForm.reset();
  loadHistory();  // populate the activity panel now that we're authenticated
}

export function bootApp() {
  const initialPage = (location.hash.slice(1) || 'landing');
  navigate(VALID_PAGES.includes(initialPage) ? initialPage : 'landing');
  // Authenticated-only fetch, so it belongs here rather than at module scope —
  // firing it before sign-in only 401s and bounces back to the login overlay.
  loadAssociationDirectories();
}

// Any 401 from an /api/ call (e.g. session expiry) re-shows the login overlay.
// The login request itself is exempt so a bad password shows an inline error.
window.fetch = async (input, init) => {
  const resp = await _origFetch(input, init);
  const url = typeof input === 'string' ? input : (input && (input.url || input.href)) || '';
  if (resp.status === 401 && url.includes('/api/') && !url.includes('/api/auth/login')) {
    showLogin();
  }
  return resp;
};

if (loginForm) {
  loginForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    loginError.hidden = true;
    const submitBtn = document.getElementById('login-submit');
    submitBtn.disabled = true;
    try {
      const resp = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: document.getElementById('login-username').value,
          password: document.getElementById('login-password').value,
        }),
      });
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        loginError.textContent = body.detail || 'Sign in failed';
        loginError.hidden = false;
        return;
      }
      onAuthenticated(await resp.json());
      await loadManifest();
      renderTypeCards();
      bootApp();
    } finally {
      submitBtn.disabled = false;
    }
  });
}

const logoutBtn = document.getElementById('logout-btn');
if (logoutBtn) {
  logoutBtn.addEventListener('click', async () => {
    await fetch('/api/auth/logout', { method: 'POST' });
    showLogin();
  });
}
