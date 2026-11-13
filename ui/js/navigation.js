// Page routing (`navigate`), nav clicks, the landing page, and per-user type visibility.

import { ALL_TYPES, TYPE_META, AUTO_CATALOG } from './catalog.js';
import { loadOverviewPage } from './overview.js';
import { loadSchedulesPage } from './schedules.js';
import { CURRENT_USER } from './auth.js';
import { loadAdminPage } from './admin.js';
import { openModal } from './run-modal.js';
import { loadSystemPage, renderAutomationsPage } from './system.js';
import { loadPerformancePage } from './performance.js';

// ── Page routing ──────────────────────────────────────────────────────────────

export function navigate(page) {
  if (page === 'admin' && !(CURRENT_USER && CURRENT_USER.is_admin)) page = 'landing';
  document.querySelectorAll('.nav-tab').forEach(t => t.classList.toggle('active', t.dataset.page === page));
  document.querySelectorAll('.page').forEach(p => {
    p.classList.toggle('active', p.id === `page-${page}`);
  });
  history.replaceState(null, '', `#${page}`);

  if (page === 'landing')   renderLandingAutos();
  if (page === 'system')    loadSystemPage();
  if (page === 'run')       renderAutomationsPage();
  if (page === 'analytics') loadPerformancePage();
  if (page === 'overview')  loadOverviewPage();
  if (page === 'schedules') loadSchedulesPage();
  if (page === 'admin')     loadAdminPage();
  if (page === 'landing')   window.scrollTo({ top: 0 });
}

// Delegated navigation: nav tabs, logo, footer links — anything with [data-page]
document.addEventListener('click', (e) => {
  const el = e.target.closest('[data-page]');
  if (el) { e.preventDefault(); navigate(el.dataset.page); }
});

// ── Landing page ────────────────────────────────────────────────────────────
function renderLandingAutos() {
  const grid = document.getElementById('lp-autos');
  if (!grid || grid.dataset.rendered) return;
  const visible = visibleTypeSet();
  grid.innerHTML = ALL_TYPES.filter(t => visible.has(t)).map(t => {
    const a = AUTO_CATALOG[t]; if (!a) return '';
    const m = TYPE_META[t] || {};
    return `<button class="lp-auto" data-type="${t}">
      <div class="lp-auto-top">
        <span class="lp-auto-icon">${a.icon}</span>
        <span class="type-chip ${m.cls || ''}">${m.chip || ''}</span>
      </div>
      <h4>${a.name}</h4>
      <p>${a.desc}</p>
    </button>`;
  }).join('');
  grid.dataset.rendered = '1';
  grid.querySelectorAll('.lp-auto').forEach(card =>
    card.addEventListener('click', () => openModal(card.dataset.type)));
}

document.getElementById('lp-launch').addEventListener('click', () => navigate('run'));
document.getElementById('lp-run').addEventListener('click', () => openModal());
document.getElementById('lp-cta-run').addEventListener('click', () => openModal());
document.getElementById('lp-cta-dash').addEventListener('click', () => navigate('activity'));

// Navigate to the page in the hash on load, defaulting to the landing page
export const VALID_PAGES = ['landing','run','activity','overview','schedules','system','analytics','admin'];

// Automations the current user may see/launch (admins see all; others get the
// intersection of globally-enabled and their personal allowlist).
export function visibleTypeSet() {
  if (!CURRENT_USER) return new Set();
  // The global enabled set applies to everyone (disabled = blocked for all,
  // including admins). Admins bypass only the per-user allowlist.
  const enabled = new Set(CURRENT_USER.enabled_automations || ALL_TYPES);
  // Custom (no-code) automations present in the manifest are enabled by definition
  // (the manifest only lists enabled ones); the server still gates running them.
  ALL_TYPES.forEach(t => { if (t.startsWith('custom:')) enabled.add(t); });
  if (CURRENT_USER.is_admin) return new Set(ALL_TYPES.filter(t => enabled.has(t)));
  const allowed = CURRENT_USER.allowed_automations;
  const allowAll = allowed === '*';
  return new Set(ALL_TYPES.filter(t =>
    enabled.has(t) && (allowAll || (Array.isArray(allowed) && allowed.includes(t)))));
}
