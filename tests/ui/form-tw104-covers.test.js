// forms/tw104-covers.js: saved 104 cover-letter (自我推薦信) patterns — load, pick,
// count, save, delete.
import { test, describe, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { setupDom, importUi, flush, click, setValue, jsonResponse, $ } from './harness.js';

let covers = [];
const { fetchMock, window } = setupDom({
  routes: [
    ['GET', '/api/cover-letters', () => covers],
    ['POST', '/api/cover-letters', (_u, _i, call) => { covers.push(call.body); return { ok: true }; }],
    ['DELETE', /^\/api\/cover-letters\//, () => ({ ok: true })],
  ],
});
const tw = await importUi('forms/tw104-covers.js');

const sel = $('tw104-cover-saved');
const ta = $('tw104-cover');
const toast = () => $('toast-banner').textContent;
const opts = () => [...sel.options].map((o) => [o.value, o.textContent]);

beforeEach(async () => {
  covers = [{ name: '後端', text: '您好，我是後端工程師' }, { name: 'a/b & c', text: 'slash' }];
  fetchMock.reset();
  ta.value = '';
  $('toast-banner').textContent = '';
  window.prompt = () => null;
  window.confirm = () => true;
  await tw.loadTw104CoverLetters();
});

describe('loadTw104CoverLetters', () => {
  test('lists saved patterns after a placeholder, URI-encoding option values', () => {
    assert.deepEqual(opts(), [
      ['', '— 載入已存推薦信範本 —'],
      [encodeURIComponent('後端'), '後端'],
      [encodeURIComponent('a/b & c'), 'a/b & c'],
    ]);
    assert.equal(sel.value, '');
  });

  test('selects the named pattern when given one', async () => {
    await tw.loadTw104CoverLetters('a/b & c');
    assert.equal(decodeURIComponent(sel.value), 'a/b & c');
  });

  test('a failing or erroring request leaves only the placeholder', async () => {
    fetchMock.on('GET', '/api/cover-letters', () => 500);
    await tw.loadTw104CoverLetters();
    assert.deepEqual(opts(), [['', '— 載入已存推薦信範本 —']]);
    fetchMock.on('GET', '/api/cover-letters', () => { throw new Error('down'); });
    await tw.loadTw104CoverLetters();
    assert.deepEqual(opts(), [['', '— 載入已存推薦信範本 —']]);
    fetchMock.on('GET', '/api/cover-letters', () => covers);
  });

  test('pattern names are shown literally', {
    todo: 'tw104-covers.js:16 — option text interpolates c.name without escHtml, so markup in a saved name is parsed (pre-existing in app.js)',
  }, async () => {
    covers = [{ name: '<b>x</b> & y', text: '' }];
    await tw.loadTw104CoverLetters();
    assert.equal(sel.options[1].textContent, '<b>x</b> & y');
  });
});

describe('_tw104CoverCount', () => {
  test('shows the textarea length, and updates on input', () => {
    ta.value = 'abc';
    tw._tw104CoverCount();
    assert.equal($('tw104-cover-count').textContent, '3');
    setValue(ta, '你好');
    assert.equal($('tw104-cover-count').textContent, '2');
    setValue(ta, '');
    assert.equal($('tw104-cover-count').textContent, '0');
  });
});

describe('picking a saved pattern', () => {
  test('fills the textarea with its text and refreshes the count', () => {
    setValue(sel, encodeURIComponent('後端'));
    assert.equal(ta.value, '您好，我是後端工程師');
    assert.equal($('tw104-cover-count').textContent, String('您好，我是後端工程師'.length));
  });

  test('the placeholder leaves the textarea alone', () => {
    ta.value = 'draft';
    setValue(sel, '');
    assert.equal(ta.value, 'draft');
  });
});

describe('save button', () => {
  test('POSTs {name, text}, reloads and selects the saved pattern', async () => {
    let asked;
    window.prompt = (msg, def) => { asked = [msg, def]; return '  新範本 '; };
    ta.value = '內容';
    click('#tw104-cover-save');
    await flush();
    assert.deepEqual(asked, ['儲存為推薦信範本，請輸入名稱：', '']);
    const [post] = fetchMock.callsTo('/api/cover-letters', 'POST');
    assert.deepEqual(post.body, { name: '新範本', text: '內容' });
    assert.equal(decodeURIComponent(sel.value), '新範本');
    assert.equal(toast(), '已儲存範本「新範本」');
  });

  test('suggests the currently selected name as the default', async () => {
    let def;
    window.prompt = (_m, d) => { def = d; return null; };
    sel.value = encodeURIComponent('a/b & c');
    click('#tw104-cover-save');
    await flush();
    assert.equal(def, 'a/b & c');
  });

  test('a cancelled or blank name saves nothing', async () => {
    for (const answer of [null, '   ']) {
      window.prompt = () => answer;
      click('#tw104-cover-save');
      await flush();
    }
    assert.equal(fetchMock.callsTo('/api/cover-letters', 'POST').length, 0);
  });

  test('a server error shows its detail (or a generic message)', async () => {
    window.prompt = () => 'x';
    fetchMock.on('POST', '/api/cover-letters', () => jsonResponse({ detail: 'too long' }, { status: 422 }));
    click('#tw104-cover-save');
    await flush();
    assert.equal(toast(), 'too long');
    fetchMock.on('POST', '/api/cover-letters', () => 500);
    click('#tw104-cover-save');
    await flush();
    assert.equal(toast(), '儲存失敗');
    fetchMock.on('POST', '/api/cover-letters', (_u, _i, call) => { covers.push(call.body); return { ok: true }; });
  });
});

describe('delete button', () => {
  test('requires a selected pattern', async () => {
    click('#tw104-cover-delete');
    await flush();
    assert.equal(toast(), '請先從下拉選單選擇要刪除的範本');
    assert.equal(fetchMock.callsTo(/^\/api\/cover-letters\//, 'DELETE').length, 0);
  });

  test('asks for confirmation and does nothing when declined', async () => {
    let asked;
    window.confirm = (m) => { asked = m; return false; };
    sel.value = encodeURIComponent('後端');
    click('#tw104-cover-delete');
    await flush();
    assert.equal(asked, '刪除推薦信範本「後端」？');
    assert.equal(fetchMock.callsTo(/^\/api\/cover-letters\//, 'DELETE').length, 0);
  });

  test('DELETEs the URI-encoded name, then reloads the list', async () => {
    sel.value = encodeURIComponent('a/b & c');
    covers = covers.slice(0, 1);
    click('#tw104-cover-delete');
    await flush();
    const [del] = fetchMock.callsTo(/^\/api\/cover-letters\//, 'DELETE');
    assert.equal(del.url.pathname, `/api/cover-letters/${encodeURIComponent('a/b & c')}`);
    assert.equal(toast(), '已刪除範本「a/b & c」');
    assert.deepEqual(opts().map(([, t]) => t), ['— 載入已存推薦信範本 —', '後端']);
  });

  test('a failed delete shows an error', async () => {
    fetchMock.on('DELETE', /^\/api\/cover-letters\//, () => 404);
    sel.value = encodeURIComponent('後端');
    click('#tw104-cover-delete');
    await flush();
    assert.equal(toast(), '刪除失敗');
    fetchMock.on('DELETE', /^\/api\/cover-letters\//, () => ({ ok: true }));
  });
});
