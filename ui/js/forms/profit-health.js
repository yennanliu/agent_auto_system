// Bespoke run form: profit_health_check CSV filename → role classification.

import { escHtml } from '../helpers.js';

// ── Profit health check: filename → role classification (mirrors uploads.py) ──

const _PH_ROLE_KEYWORDS = [
  ['returns', ['return', 'refund', '退貨', '退款']],
  ['cost',    ['cost', '成本']],
  ['ads',     ['ads', 'advert', '廣告', 'discount', '折扣']],
  ['sales',   ['sales', 'sale', 'order', '銷售', '訂單']],
];
const _PH_ROLE_LABEL = { sales: '銷售', cost: '成本', ads: '廣告', returns: '退貨' };

// Mirrors _classify in src/routers/uploads.py: match keywords against whole tokens
// (not raw substrings) so a short keyword can't hide inside a word (e.g. "ad" in
// "download"). CJK keywords have no token separators, so fall back to substring.
export function classifyCsv(filename) {
  const name = (filename || '').toLowerCase();
  const tokens = name.match(/[a-z0-9]+/g) || [];
  for (const [role, kws] of _PH_ROLE_KEYWORDS) {
    for (const k of kws) {
      const ascii = /^[\x00-\x7f]+$/.test(k);
      if (ascii) {
        if (tokens.some(t => t === k || t.startsWith(k))) return role;
      } else if (name.includes(k)) {
        return role;
      }
    }
  }
  return null;
}

export function renderPhClassified() {
  const box = document.getElementById('ph-classified');
  if (!box) return;
  const picked = Array.from(document.getElementById('ph-files').files || []);
  if (!picked.length) { box.innerHTML = ''; return; }
  const rows = picked.map(f => {
    const role = classifyCsv(f.name);
    const tag = role
      ? `<span style="color:var(--green)">→ ${_PH_ROLE_LABEL[role]}</span>`
      : `<span style="color:var(--red)">→ 無法辨識</span>`;
    return `<div style="display:flex;justify-content:space-between;gap:1rem"><span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escHtml(f.name)}</span>${tag}</div>`;
  });
  box.innerHTML = `<div style="border:1px solid var(--border);border-radius:6px;padding:0.5rem 0.7rem">${rows.join('')}</div>`;
}
