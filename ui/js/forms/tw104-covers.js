// Bespoke run form: 104 saved cover-letter (自我推薦信) patterns.

import { showToast } from '../helpers.js';

// ── 104 saved cover-letter (自我推薦信) patterns ──────────────────────────────
let _tw104Covers = [];

export async function loadTw104CoverLetters(selectName) {
  const sel = document.getElementById('tw104-cover-saved');
  if (!sel) return;
  try {
    const resp = await fetch('/api/cover-letters');
    _tw104Covers = resp.ok ? await resp.json() : [];
  } catch { _tw104Covers = []; }
  sel.innerHTML = '<option value="">— 載入已存推薦信範本 —</option>' +
    _tw104Covers.map(c => `<option value="${encodeURIComponent(c.name)}">${c.name}</option>`).join('');
  if (selectName) sel.value = encodeURIComponent(selectName);
}

export function _tw104CoverCount() {
  const ta = document.getElementById('tw104-cover');
  const el = document.getElementById('tw104-cover-count');
  if (ta && el) el.textContent = String(ta.value.length);
}

function wireTw104CoverLetters() {
  const ta = document.getElementById('tw104-cover');
  const sel = document.getElementById('tw104-cover-saved');
  const saveBtn = document.getElementById('tw104-cover-save');
  const delBtn = document.getElementById('tw104-cover-delete');
  if (!ta || !sel || !saveBtn || !delBtn) return;

  ta.addEventListener('input', _tw104CoverCount);

  sel.addEventListener('change', () => {
    const name = decodeURIComponent(sel.value || '');
    const found = _tw104Covers.find(c => c.name === name);
    if (found) { ta.value = found.text || ''; _tw104CoverCount(); }
  });

  saveBtn.addEventListener('click', async () => {
    const current = sel.value ? decodeURIComponent(sel.value) : '';
    const name = (prompt('儲存為推薦信範本，請輸入名稱：', current) || '').trim();
    if (!name) return;
    const resp = await fetch('/api/cover-letters', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, text: ta.value }),
    });
    if (resp.ok) { await loadTw104CoverLetters(name); showToast(`已儲存範本「${name}」`, 'success'); }
    else { const b = await resp.json().catch(() => ({})); showToast(b.detail || '儲存失敗', 'error'); }
  });

  delBtn.addEventListener('click', async () => {
    const name = sel.value ? decodeURIComponent(sel.value) : '';
    if (!name) { showToast('請先從下拉選單選擇要刪除的範本', 'error'); return; }
    if (!confirm(`刪除推薦信範本「${name}」？`)) return;
    const resp = await fetch(`/api/cover-letters/${encodeURIComponent(name)}`, { method: 'DELETE' });
    if (resp.ok) { await loadTw104CoverLetters(); showToast(`已刪除範本「${name}」`, 'success'); }
    else showToast('刪除失敗', 'error');
  });
}
wireTw104CoverLetters();
