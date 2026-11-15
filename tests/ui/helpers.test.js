// helpers.js — extractResultText, escHtml, showToast.
import { test, describe, mock } from 'node:test';
import assert from 'node:assert/strict';
import { setupDom, importUi, $ } from './harness.js';

setupDom();
const { extractResultText, escHtml, showToast } = await importUi('helpers.js');

describe('extractResultText', () => {
  test('null / undefined / empty object → empty string', () => {
    assert.equal(extractResultText(null), '');
    assert.equal(extractResultText(undefined), '');
    assert.equal(extractResultText({}), '');
  });

  test('pipeline result: step count + catalog names, unknown types passed through', () => {
    const r = { steps: [{ job_type: 'web_scraper' }, { job_type: 'hacker_news_digest' }, { job_type: 'custom:foo' }] };
    assert.equal(extractResultText(r), '3-step pipeline: Web Scraper → HN Digest → custom:foo');
  });

  test('pipeline with zero steps', () => {
    assert.equal(extractResultText({ steps: [] }), '0-step pipeline: ');
  });

  test('non-array `steps` is not treated as a pipeline', () => {
    assert.equal(extractResultText({ steps: 'nope', answer: 'A' }), 'A');
  });

  test('sheet result: rows · columns, optional summary truncated to 80 chars', () => {
    assert.equal(extractResultText({ columns: ['a', 'b'], row_count: 10 }), '10 rows · 2 columns');
    assert.equal(extractResultText({ columns: [], row_count: 0 }), '0 rows · 0 columns');
    const long = 'x'.repeat(200);
    assert.equal(
      extractResultText({ columns: ['a'], row_count: 1, summary: long }),
      `1 rows · 1 columns · ${'x'.repeat(80)}`,
    );
  });

  test('columns without row_count fall through to the generic fields', () => {
    assert.equal(extractResultText({ columns: ['a'], summary: 'S' }), 'S');
  });

  test('generic fields in priority order', () => {
    const full = {
      answer: 'answer',
      story_of_the_day: { title: 'story' },
      summary: 'summary',
      confirmation: 'confirmation',
      confirmation_text: 'confirmation_text',
      error: 'error',
      message: 'message',
      title: 'title',
    };
    const order = ['answer', 'story_of_the_day', 'summary', 'confirmation', 'confirmation_text', 'error', 'message'];
    const r = { ...full };
    for (const key of order) {
      const expected = key === 'story_of_the_day' ? 'story' : key;
      assert.equal(extractResultText(r), expected, `expected ${key} to win`);
      delete r[key];
    }
    assert.equal(extractResultText(r), 'title (? words)');
  });

  test('title shape includes word_count (0 is kept, missing → ?)', () => {
    assert.equal(extractResultText({ title: 'Page', word_count: 512 }), 'Page (512 words)');
    assert.equal(extractResultText({ title: 'Page', word_count: 0 }), 'Page (0 words)');
    assert.equal(extractResultText({ title: 'Page' }), 'Page (? words)');
  });

  test('empty-string fields are skipped', () => {
    assert.equal(extractResultText({ answer: '', summary: 'S' }), 'S');
    assert.equal(extractResultText({ story_of_the_day: {}, error: 'boom' }), 'boom');
  });
});

describe('escHtml', () => {
  test('escapes &, <, > and "', () => {
    assert.equal(escHtml('<a href="x">Tom & Jerry</a>'), '&lt;a href=&quot;x&quot;&gt;Tom &amp; Jerry&lt;/a&gt;');
  });

  test('& is escaped first (no double-escaping of the produced entities)', () => {
    assert.equal(escHtml('&lt;'), '&amp;lt;');
    assert.equal(escHtml('<'), '&lt;');
  });

  test('null / undefined → empty string; non-strings are stringified', () => {
    assert.equal(escHtml(null), '');
    assert.equal(escHtml(undefined), '');
    assert.equal(escHtml(0), '0');
    assert.equal(escHtml(false), 'false');
    assert.equal(escHtml(42.5), '42.5');
  });

  test('output is inert when injected as HTML (text and double-quoted attribute)', () => {
    const payload = '"><img src=x onerror="window.__xss=1"><script>window.__xss=1</script>';
    const host = document.createElement('div');
    host.innerHTML = `<span title="${escHtml(payload)}">${escHtml(payload)}</span>`;
    assert.equal(host.querySelectorAll('img,script').length, 0);
    assert.equal(host.firstElementChild.getAttribute('title'), payload);
    assert.equal(host.textContent, payload);
  });

  test('single quotes are left as-is (only safe inside double-quoted attributes)', () => {
    assert.equal(escHtml("it's"), "it's");
  });
});

describe('showToast', () => {
  const banner = $('toast-banner');

  test('error (default): red, visible, text-only, hides after 4 s', (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    showToast('<b>Boom</b>');
    assert.equal(banner.textContent, '<b>Boom</b>');
    assert.equal(banner.children.length, 0);
    assert.equal(banner.style.background, 'var(--red)');
    assert.ok(!banner.classList.contains('hidden'));
    t.mock.timers.tick(3999);
    assert.ok(!banner.classList.contains('hidden'));
    t.mock.timers.tick(1);
    assert.ok(banner.classList.contains('hidden'));
  });

  test('success: green, hides after 2 s', (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    showToast('Saved', 'success');
    assert.equal(banner.textContent, 'Saved');
    assert.equal(banner.style.background, 'var(--green)');
    t.mock.timers.tick(1999);
    assert.ok(!banner.classList.contains('hidden'));
    t.mock.timers.tick(1);
    assert.ok(banner.classList.contains('hidden'));
  });

  test('any non-success type is styled as an error', (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    showToast('Careful', 'warning');
    assert.equal(banner.style.background, 'var(--red)');
    t.mock.timers.tick(4000);
    assert.ok(banner.classList.contains('hidden'));
  });

  test('a new toast restarts the hide timer instead of stacking', (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    showToast('first');
    t.mock.timers.tick(3000);
    showToast('second');
    assert.equal(banner.textContent, 'second');
    t.mock.timers.tick(1500);            // first toast's 4 s deadline has passed
    assert.ok(!banner.classList.contains('hidden'), 'old timer must have been cleared');
    t.mock.timers.tick(2500);
    assert.ok(banner.classList.contains('hidden'));
  });

  test('success after error switches colour and uses the shorter delay', (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    showToast('bad');
    showToast('good', 'success');
    assert.equal(banner.style.background, 'var(--green)');
    t.mock.timers.tick(2000);
    assert.ok(banner.classList.contains('hidden'));
  });
});
