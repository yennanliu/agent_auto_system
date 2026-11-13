// Promise-based confirm dialog.

import { confirmModal } from './dom.js';

let confirmResolve     = null;

// ── Confirm dialog ────────────────────────────────────────────────────────────

export function confirmDialog(title, msg) {
  return new Promise((resolve) => {
    document.getElementById('confirm-title').textContent = title;
    document.getElementById('confirm-msg').textContent = msg;
    confirmModal.classList.remove('hidden');
    confirmResolve = resolve;
  });
}

document.getElementById('confirm-ok').addEventListener('click', () => {
  confirmModal.classList.add('hidden');
  confirmResolve?.(true);
});
document.getElementById('confirm-cancel').addEventListener('click', () => {
  confirmModal.classList.add('hidden');
  confirmResolve?.(false);
});
document.getElementById('confirm-close').addEventListener('click', () => {
  confirmModal.classList.add('hidden');
  confirmResolve?.(false);
});
confirmModal.addEventListener('click', (e) => {
  if (e.target === confirmModal) { confirmModal.classList.add('hidden'); confirmResolve?.(false); }
});
