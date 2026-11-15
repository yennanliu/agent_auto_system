// dom.js — shared element refs resolved once at import time against index.html.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { setupDom, importUi, $ } from './harness.js';

setupDom();
const dom = await importUi('dom.js');

const EXPECTED = {
  modal:           'modal',
  runForm:         'run-form',
  progressPanel:   'progress-panel',
  progressLog:     'progress-log',
  progressJobName: 'progress-job-name',
  progressPulse:   'progress-pulse',
  confirmModal:    'confirm-modal',
};

test('exports exactly the documented refs', () => {
  assert.deepEqual(Object.keys(dom).sort(), Object.keys(EXPECTED).sort());
});

for (const [name, id] of Object.entries(EXPECTED)) {
  test(`${name} is the live #${id} element of index.html`, () => {
    assert.ok(dom[name], `#${id} missing from index.html`);
    assert.equal(dom[name], $(id));
    assert.ok(dom[name].isConnected);
  });
}

test('refs have the element kinds the other modules rely on', () => {
  assert.equal(dom.runForm.tagName, 'FORM');          // runForm.reset() in run-modal.js
  assert.ok(dom.modal.classList.contains('hidden'));   // modals start closed
  assert.ok(dom.confirmModal.classList.contains('hidden'));
  assert.ok(dom.confirmModal.classList.contains('modal-overlay'));
});
