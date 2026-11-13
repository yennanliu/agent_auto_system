// Small shared helpers: result-text extraction, HTML escaping, toast banner.

import { AUTO_CATALOG } from './catalog.js';

let toastTimer         = null;

// ── Helpers ───────────────────────────────────────────────────────────────────

export function extractResultText(r) {
  if (!r) return '';
  if (r.steps && Array.isArray(r.steps)) {
    const types = r.steps.map(s => (AUTO_CATALOG[s.job_type]?.name || s.job_type)).join(' → ');
    return `${r.steps.length}-step pipeline: ${types}`;
  }
  if (r.columns && r.row_count !== undefined) {
    return `${r.row_count} rows · ${r.columns.length} columns${r.summary ? ' · ' + r.summary.slice(0, 80) : ''}`;
  }
  return r.answer
    || r.story_of_the_day?.title
    || r.summary
    || r.confirmation
    || r.confirmation_text
    || r.error
    || r.message
    || (r.title ? `${r.title} (${r.word_count ?? '?'} words)` : '')
    || '';
}

export function escHtml(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

export function showToast(msg, type = 'error') {
  const banner = document.getElementById('toast-banner');
  banner.textContent = msg;
  banner.style.background = type === 'success' ? 'var(--green)' : 'var(--red)';
  banner.classList.remove('hidden');
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => banner.classList.add('hidden'), type === 'success' ? 2000 : 4000);
}
