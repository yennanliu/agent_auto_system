// confirm.js — promise-based confirm dialog wired to #confirm-modal.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { setupDom, importUi, click, flush, $ } from './harness.js';

setupDom();
const { confirmDialog } = await importUi('confirm.js');
const modal = $('confirm-modal');

/** Resolve to the dialog's value, or 'pending' if it hasn't settled after a flush. */
async function settled(p) {
  const marker = Symbol('pending');
  const v = await Promise.race([p, flush().then(() => marker)]);
  return v === marker ? 'pending' : v;
}

test('opens the modal with title/message as text (no HTML injection)', async () => {
  const p = confirmDialog('<b>Delete</b> run?', '<img src=x onerror="alert(1)">');
  assert.ok(!modal.classList.contains('hidden'));
  assert.equal($('confirm-title').textContent, '<b>Delete</b> run?');
  assert.equal($('confirm-msg').textContent, '<img src=x onerror="alert(1)">');
  assert.equal($('confirm-title').children.length, 0);
  assert.equal($('confirm-msg').querySelector('img'), null);
  assert.equal(await settled(p), 'pending');
  click('#confirm-ok');
  assert.equal(await p, true);
});

test('OK resolves true and hides the modal', async () => {
  const p = confirmDialog('t', 'm');
  click('#confirm-ok');
  assert.equal(await p, true);
  assert.ok(modal.classList.contains('hidden'));
});

test('Cancel resolves false and hides the modal', async () => {
  const p = confirmDialog('t', 'm');
  click('#confirm-cancel');
  assert.equal(await p, false);
  assert.ok(modal.classList.contains('hidden'));
});

test('the × close button resolves false', async () => {
  const p = confirmDialog('t', 'm');
  click('#confirm-close');
  assert.equal(await p, false);
  assert.ok(modal.classList.contains('hidden'));
});

test('clicking the backdrop resolves false', async () => {
  const p = confirmDialog('t', 'm');
  click(modal);
  assert.equal(await p, false);
  assert.ok(modal.classList.contains('hidden'));
});

test('clicking inside the dialog card (not the backdrop) does nothing', async () => {
  const p = confirmDialog('t', 'm');
  click('#confirm-msg');
  assert.equal(await settled(p), 'pending');
  assert.ok(!modal.classList.contains('hidden'));
  click('#confirm-cancel');
  assert.equal(await p, false);
});

test('a second dialog replaces the first; its answer goes to the newest promise', async () => {
  const first = confirmDialog('first', 'm');
  const second = confirmDialog('second', 'm2');
  assert.equal($('confirm-title').textContent, 'second');
  click('#confirm-ok');
  assert.equal(await second, true);
  assert.equal(await settled(first), 'pending');
});

test('buttons are safe to click when no dialog is pending', () => {
  modal.classList.remove('hidden');
  assert.doesNotThrow(() => click('#confirm-cancel'));
  assert.ok(modal.classList.contains('hidden'));
});
