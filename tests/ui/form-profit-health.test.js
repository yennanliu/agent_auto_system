// forms/profit-health.js: CSV filename → role classification (mirrors
// src/routers/uploads.py::_classify), the preview, and the upload → {upload_id} job
// flow that jobs.js drives for profit_health_check.
import { test, describe, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { setupDom, importUi, flush, jsonResponse, $ } from './harness.js';

const ADMIN = { username: 'admin', is_admin: true, allowed_automations: '*' };
const { fetchMock, window } = setupDom({
  routes: [
    ['GET', '/api/auth/me', () => ADMIN],
    ['GET', '/api/automations/manifest', () => ({ automations: [
      { job_type: 'profit_health_check', name: '利潤健檢', icon: '🧾', desc: '', custom_ui: true, fields: [], steps: [] },
    ] })],
    ['GET', '/api/runs', () => jsonResponse([], { headers: { 'X-Total-Count': '0' } })],
    ['GET', '/api/associations', () => ({ associations: [] })],
    ['POST', '/api/uploads', () => ({ upload_id: 'up-123' })],
    ['POST', '/api/jobs', () => ({ id: 3 })],
    ['POST', '/api/jobs/3/run', () => ({ run_id: 30 })],
  ],
});
const ph = await importUi('forms/profit-health.js');

const input = $('ph-files');
const box = $('ph-classified');
const file = (name, size = 3) => new window.File([new Uint8Array(size)], name, { type: 'text/csv' });
function pick(...files) {
  Object.defineProperty(input, 'files', { configurable: true, value: files });
}

describe('classifyCsv', () => {
  const cases = [
    ['sales.csv', 'sales'],
    ['SALES_2026-01.CSV', 'sales'],
    ['order_export.csv', 'sales'],
    ['orders.csv', 'sales'],
    ['蝦皮銷售報表.csv', 'sales'],
    ['訂單.csv', 'sales'],
    ['cost.csv', 'cost'],
    ['costs-q1.csv', 'cost'],
    ['商品成本.csv', 'cost'],
    ['ads.csv', 'ads'],
    ['advertising_report.csv', 'ads'],
    ['廣告費.csv', 'ads'],
    ['discount.csv', 'ads'],
    ['折扣.csv', 'ads'],
    ['returns.csv', 'returns'],
    ['refund-list.csv', 'returns'],
    ['退貨.csv', 'returns'],
    ['退款明細.csv', 'returns'],
    // Priority: returns > cost > ads > sales.
    ['sales_returns.csv', 'returns'],
    ['order_cost.csv', 'cost'],
    ['sales_ads.csv', 'ads'],
    // Whole-token match: keywords must start a token, not hide inside a word.
    ['download.csv', null],
    ['presales.csv', null],
    ['broadcast.csv', null],
    ['report.csv', null],
    ['', null],
    [null, null],
    [undefined, null],
  ];
  for (const [name, role] of cases) {
    test(`${JSON.stringify(name)} → ${role}`, () => {
      assert.equal(ph.classifyCsv(name), role);
    });
  }
});

describe('renderPhClassified', () => {
  beforeEach(() => { box.innerHTML = 'stale'; });

  test('clears the preview when nothing is picked', () => {
    pick();
    ph.renderPhClassified();
    assert.equal(box.innerHTML, '');
  });

  test('one row per file with its role label, or 無法辨識', () => {
    pick(file('sales.csv'), file('cost.csv'), file('ads.csv'), file('refund.csv'), file('misc.csv'));
    ph.renderPhClassified();
    const rows = [...box.querySelectorAll(':scope > div > div')].map((r) => [...r.children].map((c) => c.textContent));
    assert.deepEqual(rows, [
      ['sales.csv', '→ 銷售'],
      ['cost.csv', '→ 成本'],
      ['ads.csv', '→ 廣告'],
      ['refund.csv', '→ 退貨'],
      ['misc.csv', '→ 無法辨識'],
    ]);
    assert.match(box.querySelectorAll(':scope > div > div')[4].children[1].getAttribute('style'), /var\(--red\)/);
  });

  test('escapes file names', () => {
    pick(file('<img src=x onerror=alert(1)>sales.csv'));
    ph.renderPhClassified();
    assert.equal(box.querySelector('img'), null);
    assert.match(box.textContent, /<img src=x onerror=alert\(1\)>sales\.csv/);
  });
});

describe('upload flow (profit_health_check submit)', async () => {
  await importUi('main.js');
  await flush(10);
  const rm = await importUi('run-modal.js');
  const toast = () => $('toast-banner').textContent;

  async function submitWith(...files) {
    rm.selectJobType('profit_health_check');
    pick(...files);
    $('run-form').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
    await flush(15);
  }

  beforeEach(() => { fetchMock.reset(); $('toast-banner').textContent = ''; });

  test('uploads every file as multipart, then creates the job with only {upload_id}', async () => {
    await submitWith(file('sales.csv'), file('cost.csv'), file('ads.csv'));
    const posts = fetchMock.calls.filter((c) => c.method === 'POST').map((c) => c.path);
    assert.deepEqual(posts, ['/api/uploads', '/api/jobs', '/api/jobs/3/run']);
    const [up] = fetchMock.callsTo('/api/uploads');
    assert.ok(up.body instanceof window.FormData);
    assert.deepEqual(up.body.getAll('files').map((f) => f.name), ['sales.csv', 'cost.csv', 'ads.csv']);
    assert.deepEqual(fetchMock.callsTo('/api/jobs', 'POST')[0].body, {
      name: '利潤健檢：sales.csv', job_type: 'profit_health_check',
      payload: { upload_id: 'up-123', llm_provider: 'openai', llm_model: 'gpt-4o-mini' },
    });
  });

  test('no files → error, no upload', async () => {
    await submitWith();
    assert.equal(toast(), '請選取蝦皮 CSV 檔（可一次全選）');
    assert.equal(fetchMock.calls.filter((c) => c.method === 'POST').length, 0);
  });

  test('missing a sales or cost file → error, no upload', async () => {
    await submitWith(file('sales.csv'), file('ads.csv'));
    assert.match(toast(), /^檔名需可辨識出銷售 \(sales\) 與成本 \(cost\)/);
    await submitWith(file('cost.csv'));
    assert.match(toast(), /^檔名需可辨識出銷售/);
    assert.equal(fetchMock.callsTo('/api/uploads').length, 0);
  });

  test('a file over 2 MB is rejected before upload', async () => {
    await submitWith(file('sales.csv'), file('cost.csv', 2 * 1024 * 1024 + 1));
    assert.equal(toast(), 'cost.csv 超過 2 MB 上限');
    assert.equal(fetchMock.callsTo('/api/uploads').length, 0);
  });

  test('exactly 2 MB is allowed', async () => {
    await submitWith(file('sales.csv', 2 * 1024 * 1024), file('cost.csv'));
    assert.equal(fetchMock.callsTo('/api/uploads').length, 1);
  });

  test('a rejected upload shows the server detail (or status) and creates no job', async () => {
    fetchMock.on('POST', '/api/uploads', () => jsonResponse({ detail: 'bad csv' }, { status: 422 }));
    await submitWith(file('sales.csv'), file('cost.csv'));
    assert.equal(toast(), '上傳失敗：bad csv');
    fetchMock.on('POST', '/api/uploads', () => 413);
    await submitWith(file('sales.csv'), file('cost.csv'));
    assert.equal(toast(), '上傳失敗：413');
    assert.equal(fetchMock.callsTo('/api/jobs', 'POST').length, 0);
  });

  test('a network error during upload is reported', async () => {
    fetchMock.on('POST', '/api/uploads', () => { throw new Error('offline'); });
    await submitWith(file('sales.csv'), file('cost.csv'));
    assert.equal(toast(), '上傳失敗：Error: offline');
    assert.equal(fetchMock.callsTo('/api/jobs', 'POST').length, 0);
    fetchMock.on('POST', '/api/uploads', () => ({ upload_id: 'up-123' }));
  });
});
