// Run history (ui/js/runs.js): table rendering, pagination, filters, detail
// tabs, selection + bulk/single delete, abort, copy, download links, elapsed
// timers and the 5 s background poller.
import { test, beforeEach, mock } from 'node:test';
import assert from 'node:assert/strict';
import { setupDom, flush, click, setValue, jsonResponse, FakeEventSource, $, importUi } from './harness.js';

// Fake clock *before* runs.js is imported so its top-level 5 s poller (and the
// per-row elapsed timers / scrollToRun timeouts) are driven by mock.timers.
const NOW = Date.parse('2026-11-15T08:01:00Z');
mock.timers.enable({ apis: ['setInterval', 'setTimeout', 'Date'], now: NOW });

let runsData = [];
let totalCount = null; // X-Total-Count; defaults to runsData.length
const { window, fetchMock } = setupDom({
  routes: [
    ['GET', '/api/runs', () => jsonResponse(runsData, {
      headers: { 'X-Total-Count': String(totalCount ?? runsData.length) },
    })],
  ],
});

const catalog = await importUi('catalog.js');
const runs = await importUi('runs.js');
const progress = await importUi('progress.js');
const auth = await importUi('auth.js');
const { loadHistory, scrollToRun, stopElapsedTimer } = runs;

catalog.FLOW_STEPS.hacker_news_digest = [
  { label: 'Fetch', trigger: 'Fetching' },
  { label: 'Summarise', trigger: 'Summarising' },
];

// ── fixtures / helpers ──────────────────────────────────────────────────────────

let nextId = 1;
const mk = (over = {}) => ({
  id: nextId++, job_id: 10, job_name: 'Digest', job_type: 'hacker_news_digest',
  status: 'success', started_at: '2026-11-15T08:00:00Z', finished_at: '2026-11-15T08:00:12.5Z',
  result: null, log: null, ...over,
});

const tbody = () => $('history-tbody');
const dataRow = (id) => tbody().querySelector(`tr.data-row[data-run-id="${id}"]`);
const detailRow = (id) => $(`detail-${id}`);
const tds = (id) => [...dataRow(id).querySelectorAll('td')];
const toast = () => $('toast-banner');
const runsCalls = () => fetchMock.callsTo('/api/runs', 'GET');
const lastRunsQuery = () => runsCalls().at(-1).url.searchParams;
const pageButtons = () => [...$('page-numbers').children].map((e) => e.textContent);

async function show(list, total = null) {
  runsData = list;
  totalCount = total;
  await loadHistory();
}

async function confirmYes() { await flush(); click('#confirm-ok'); await flush(); }
async function confirmNo() { await flush(); click('#confirm-cancel'); await flush(); }

beforeEach(async () => {
  // Clear any selection left by a previous test (select-all off over the same ids).
  await show([mk({ id: 1 }), mk({ id: 2 }), mk({ id: 3 })]);
  setValue('#select-all-cb', false);
  click('#filter-clear-btn'); // also resets the module's filter state
  await flush();
  fetchMock.reset();
  toast().classList.add('hidden');
  toast().textContent = '';
});

// ── rendering ───────────────────────────────────────────────────────────────────

test('loadHistory requests page 1 (limit 50, offset 0) and renders one row + detail row per run', async () => {
  await show([mk({ id: 5 }), mk({ id: 6, status: 'failed' })]);
  const q = lastRunsQuery();
  assert.equal(q.get('limit'), '50');
  assert.equal(q.get('offset'), '0');
  assert.equal(tbody().querySelectorAll('tr.data-row').length, 2);
  assert.equal(tbody().querySelectorAll('tr.detail-row.hidden').length, 2);
  assert.deepEqual(runs.cachedRuns.map((r) => r.id), [5, 6], 'cachedRuns mirrors the rendered page');

  const [, idCell, jobCell, statusCell, startedCell, durCell] = tds(5);
  assert.equal(idCell.textContent, '#5');
  assert.equal(jobCell.querySelector('.job-name-text').textContent, 'Digest');
  assert.equal(jobCell.querySelector('.type-chip').textContent, 'HN');
  assert.equal(statusCell.querySelector('.badge').className, 'badge badge-success');
  assert.equal(startedCell.textContent, new Date('2026-11-15T08:00:00Z').toLocaleString());
  assert.equal(durCell.textContent, '12.5s');
  assert.equal(tds(6)[3].querySelector('.badge').className, 'badge badge-failed');
});

test('status-dependent actions: completed → rerun + delete, failed → Retry, active → Abort and no delete/select', async () => {
  await show([
    mk({ id: 1, status: 'success' }),
    mk({ id: 2, status: 'failed' }),
    mk({ id: 3, status: 'running', finished_at: null }),
    mk({ id: 4, status: 'pending', finished_at: null }),
    mk({ id: 5, status: 'queued', finished_at: null }),
  ]);
  const actions = (id) => dataRow(id).querySelector('.row-actions');
  assert.equal(actions(1).querySelector('[data-action="rerun"]').textContent, '↺');
  assert.ok(!actions(1).querySelector('[data-action="delete"]').disabled);
  assert.equal(actions(2).querySelector('[data-action="rerun"]').textContent, '↺ Retry');
  assert.ok(actions(2).querySelector('.btn-retry'));
  for (const id of [3, 4, 5]) {
    assert.ok(actions(id).querySelector('[data-action="abort"]'));
    assert.equal(actions(id).querySelector('[data-action="rerun"]'), null);
    assert.ok(actions(id).querySelector('[data-action="delete"]').disabled);
    assert.ok(dataRow(id).querySelector('.run-cb').disabled);
  }
  assert.ok(!dataRow(1).querySelector('.run-cb').disabled);
});

test('result text: JSON results are summarised, plain-text results shown raw', async () => {
  await show([
    mk({ id: 1, result: JSON.stringify({ answer: 'The answer is 42' }) }),
    mk({ id: 2, result: 'plain text, not json' }),
    mk({ id: 3, result: null }),
  ]);
  const resultCell = (id) => dataRow(id).querySelector('.result-cell').textContent.trim();
  assert.equal(resultCell(1), 'The answer is 42');
  assert.equal(resultCell(2), 'plain text, not json');
  assert.equal(resultCell(3), '');
  // The Result pane holds pretty-printed JSON / raw text.
  assert.equal($('pane-result-1').querySelector('pre').textContent, JSON.stringify({ answer: 'The answer is 42' }, null, 2));
  assert.equal($('pane-result-2').querySelector('pre').textContent, 'plain text, not json');
});

test('run results, job names and log lines are HTML-escaped (no XSS)', async () => {
  const evil = '<img src=x onerror="window.__pwned=1">';
  await show([
    mk({ id: 1, job_name: evil, result: JSON.stringify({ answer: evil }), log: JSON.stringify([{ ts: '<b>t</b>', msg: evil }]) }),
    mk({ id: 2, result: evil, eval_score: 90, eval_notes: evil }),
  ]);
  assert.equal(tbody().querySelector('img'), null);
  assert.equal(tbody().querySelector('.log-list b'), null);
  assert.equal(dataRow(1).querySelector('.job-name-text').textContent, evil);
  assert.equal(dataRow(1).querySelector('.result-cell').textContent.trim(), evil);
  assert.equal(dataRow(2).querySelector('.result-cell').textContent.trim(), evil);
  assert.equal($('pane-result-1').querySelector('pre').textContent, JSON.stringify({ answer: evil }, null, 2));
  assert.match($('pane-log-1').textContent, /<b>t<\/b>/);
  assert.equal(window.__pwned, undefined);
});

test('badges: provider, cost, tokens, retries and eval score bands', async () => {
  await show([
    mk({ id: 1, llm_provider: 'anthropic', cost_usd: 0.01234, tokens_in: 1000, tokens_out: 234, retry_count: 2, eval_score: 85 }),
    mk({ id: 2, llm_provider: 'openai', cost_usd: 0.00001, eval_score: 60 }),
    mk({ id: 3, eval_score: 10 }),
    mk({ id: 4 }),
  ]);
  const job = (id) => tds(id)[2];
  assert.equal(job(1).querySelector('.llm-badge').textContent, 'claude');
  assert.ok(job(1).querySelector('.llm-anthropic'));
  assert.equal(job(2).querySelector('.llm-badge').textContent, 'openai');
  assert.equal(job(1).querySelector('.retry-badge').textContent, '2↺');
  assert.equal(job(1).querySelector('.eval-badge').textContent, '★ 85');
  assert.ok(job(1).querySelector('.eval-badge.eval-high'));
  assert.ok(job(2).querySelector('.eval-badge.eval-mid'));
  assert.ok(job(3).querySelector('.eval-badge.eval-low'));
  assert.equal(job(4).querySelector('.eval-badge, .llm-badge, .retry-badge'), null);

  const costs = (id) => [...dataRow(id).querySelectorAll('.result-cell .cost-badge')].map((e) => e.textContent);
  assert.deepEqual(costs(1), ['$0.0123', '1,234tok']);
  assert.deepEqual(costs(2), ['<$0.0001']);
  assert.deepEqual(costs(4), []);
});

test('detail tabs: Flow only for flow types, Log with entry count, Usage when tokens/eval exist', async () => {
  await show([
    mk({ id: 1, log: JSON.stringify([{ ts: '1', msg: 'Fetching' }, { ts: '2', msg: 'Summarising' }]), tokens_in: 5 }),
    mk({ id: 2, job_type: 'web_scraper' }),
  ]);
  const tabs = (id) => [...detailRow(id).querySelectorAll('.detail-tab')].map((t) => t.textContent);
  assert.deepEqual(tabs(1), ['Result', 'Flow', 'Log (2)', 'Usage']);
  assert.deepEqual(tabs(2), ['Result']);
  assert.ok($('pane-flow-1').querySelector('.step-graph'));
  assert.equal($('pane-flow-2'), null);
  assert.match($('pane-usage-1').textContent, /tokens in\s*5/);
});

test('empty results: default message, or a filter-specific one when filters are set', async () => {
  $('select-all-cb').checked = true;
  await show([]);
  assert.match(tbody().textContent, /No runs yet/);
  assert.equal($('select-all-cb').checked, false);

  runsData = [];
  setValue('#filter-status', 'failed');
  await flush();
  assert.match(tbody().textContent, /No runs match the current filters/);
});

test('a non-OK or failed /api/runs request leaves the table untouched', async () => {
  const before = tbody().innerHTML;
  fetchMock.on('GET', '/api/runs', () => 500);
  await loadHistory();
  assert.equal(tbody().innerHTML, before);
  fetchMock.on('GET', '/api/runs', () => { throw new Error('network down'); });
  await loadHistory(); // must not reject
  assert.equal(tbody().innerHTML, before);
  fetchMock.on('GET', '/api/runs', () => jsonResponse(runsData, {
    headers: { 'X-Total-Count': String(totalCount ?? runsData.length) },
  }));
});

// ── filters ─────────────────────────────────────────────────────────────────────

test('filters are sent as query params and the clear button removes them', async () => {
  assert.ok($('filter-automation').querySelector('option[value="hacker_news_digest"]'), 'automation filter populated');
  setValue('#filter-automation', 'hacker_news_digest');
  await flush();
  $('filter-status').value = 'failed';
  $('filter-after').value = '2026-11-01';
  setValue('#filter-before', '2026-11-30');
  await flush();
  const q = lastRunsQuery();
  assert.equal(q.get('job_type'), 'hacker_news_digest');
  assert.equal(q.get('status'), 'failed');
  assert.equal(q.get('started_after'), '2026-11-01');
  assert.equal(q.get('started_before'), '2026-11-30');
  assert.equal(q.get('offset'), '0');

  click('#filter-clear-btn');
  await flush();
  const cleared = lastRunsQuery();
  for (const k of ['job_type', 'status', 'started_after', 'started_before']) assert.equal(cleared.has(k), false, k);
  assert.equal($('filter-status').value, '');
});

// ── pagination ──────────────────────────────────────────────────────────────────

test('pagination bar is hidden when everything fits on one page', async () => {
  await show([mk()], 50);
  assert.ok($('history-pagination').classList.contains('hidden'));
});

test('pagination: info, prev/next and numbered pages drive offset', async () => {
  await show([mk({ id: 1 })], 230); // 5 pages
  const bar = $('history-pagination');
  assert.ok(!bar.classList.contains('hidden'));
  assert.equal($('page-info').textContent, '1–50 of 230');
  assert.equal($('page-prev-btn').disabled, true);
  assert.equal($('page-next-btn').disabled, false);
  assert.deepEqual(pageButtons(), ['1', '2', '…', '5']);
  assert.ok($('page-numbers').querySelector('.active').textContent === '1');

  click('#page-next-btn');
  await flush();
  assert.equal(lastRunsQuery().get('offset'), '50');
  assert.equal($('page-info').textContent, '51–100 of 230');
  assert.equal($('page-prev-btn').disabled, false);
  // cur=1 → {0,1,2,4}: the one-page gap (page 4) is filled with its button, not "…".
  assert.deepEqual(pageButtons(), ['1', '2', '3', '4', '5']);

  click($('page-numbers').querySelector('[data-page-num="4"]'));
  await flush();
  assert.equal(lastRunsQuery().get('offset'), '200');
  assert.equal($('page-info').textContent, '201–230 of 230');
  assert.equal($('page-next-btn').disabled, true);

  // Next on the last page is a no-op (clamped, no fetch).
  const n = runsCalls().length;
  click('#page-next-btn');
  await flush();
  assert.equal(runsCalls().length, n);

  click('#page-prev-btn');
  await flush();
  assert.equal(lastRunsQuery().get('offset'), '150');
});

test('page window fills a single-page gap with the page itself instead of "…"', async () => {
  await show([mk()], 1000); // 20 pages
  click($('page-numbers').querySelector('[data-page-num="1"]'));
  await flush();
  click($('page-numbers').querySelector('[data-page-num="2"]'));
  await flush();
  click($('page-numbers').querySelector('[data-page-num="3"]'));
  await flush();
  // cur=3 → shown {0,2,3,4,19}; 0→2 is a one-page gap, rendered as button "2".
  assert.deepEqual(pageButtons(), ['1', '2', '3', '4', '5', '…', '20']);
  assert.equal($('page-numbers').querySelector('.active').textContent, '4');
});

test('loadHistory(false) keeps the page; loadHistory() resets to page 1; a stale page is clamped', async () => {
  await show([mk()], 230);
  click($('page-numbers').querySelector('[data-page-num="4"]'));
  await flush();
  assert.equal(lastRunsQuery().get('offset'), '200');

  await loadHistory(false);
  assert.equal(lastRunsQuery().get('offset'), '200');

  // Rows were deleted elsewhere: only 120 left → page 5 no longer exists → refetch page 3.
  totalCount = 120;
  fetchMock.reset();
  await loadHistory(false);
  assert.deepEqual(runsCalls().map((c) => c.url.searchParams.get('offset')), ['200', '100']);
  assert.equal($('page-info').textContent, '101–120 of 120');

  await loadHistory();
  assert.equal(lastRunsQuery().get('offset'), '0');
  assert.equal($('page-info').textContent, '1–50 of 120');
});

// ── detail rows ─────────────────────────────────────────────────────────────────

test('clicking a row toggles its detail; clicks inside the detail do not', async () => {
  click(tds(2)[2]);
  assert.ok(!detailRow(2).classList.contains('hidden'));
  click(detailRow(2).querySelector('pre'));
  assert.ok(!detailRow(2).classList.contains('hidden'));
  click(tds(2)[1]);
  assert.ok(detailRow(2).classList.contains('hidden'));
});

test('switching tabs activates the matching pane, and open detail + tab survive a refresh', async () => {
  await show([mk({ id: 7, log: JSON.stringify([{ ts: '1', msg: 'Fetching' }]) })]);
  click(tds(7)[1]);
  click(detailRow(7).querySelector('[data-tab="log"]'));
  const active = () => detailRow(7).querySelector('.detail-pane.active').dataset.pane;
  assert.equal(active(), 'log');
  assert.ok(detailRow(7).querySelector('.detail-tab[data-tab="log"]').classList.contains('active'));
  assert.ok(!detailRow(7).querySelector('.detail-tab[data-tab="result"]').classList.contains('active'));

  await loadHistory(false);
  assert.ok(!detailRow(7).classList.contains('hidden'), 'still open after re-render');
  assert.equal(active(), 'log');
});

// ── selection / bulk delete ─────────────────────────────────────────────────────

test('select-all selects only completed runs and drives the Delete Selected button', async () => {
  await show([mk({ id: 1 }), mk({ id: 2, status: 'failed' }), mk({ id: 3, status: 'running', finished_at: null }),
    mk({ id: 4, status: 'queued', finished_at: null })]);
  const btn = $('delete-selected-btn');
  assert.ok(btn.classList.contains('hidden'));

  setValue('#select-all-cb', true);
  assert.equal(dataRow(1).querySelector('.run-cb').checked, true);
  assert.equal(dataRow(2).querySelector('.run-cb').checked, true);
  assert.equal(dataRow(3).querySelector('.run-cb').checked, false);
  assert.equal(dataRow(4).querySelector('.run-cb').checked, false, 'queued runs are active');
  assert.ok(!btn.classList.contains('hidden'));
  assert.equal(btn.textContent, 'Delete Selected (2)');

  setValue('#select-all-cb', false);
  assert.equal(dataRow(1).querySelector('.run-cb').checked, false);
  assert.ok(btn.classList.contains('hidden'));
});

test('row checkbox toggles selection and row highlight; selection survives a re-render', async () => {
  const cb = dataRow(2).querySelector('.run-cb');
  click(cb);
  assert.equal(cb.checked, true);
  assert.ok(dataRow(2).classList.contains('selected'));
  assert.equal($('delete-selected-btn').textContent, 'Delete Selected (1)');
  assert.ok(detailRow(2).classList.contains('hidden'), 'checkbox click does not open the detail');

  await loadHistory(false);
  assert.equal(dataRow(2).querySelector('.run-cb').checked, true);
  assert.ok(dataRow(2).classList.contains('selected'));

  click(dataRow(2).querySelector('.run-cb'));
  assert.ok(!dataRow(2).classList.contains('selected'));
  assert.ok($('delete-selected-btn').classList.contains('hidden'));
});

test('Delete Selected: confirm → DELETE /api/runs?ids=… → reload, toast, selection cleared', async () => {
  fetchMock.on('DELETE', '/api/runs', () => ({ deleted: 2 }));
  click(dataRow(1).querySelector('.run-cb'));
  click(dataRow(3).querySelector('.run-cb'));
  click('#delete-selected-btn');
  await flush();
  assert.equal($('confirm-title').textContent, 'Delete Selected Runs');
  assert.match($('confirm-msg').textContent, /Delete 2 selected run/);
  assert.equal(fetchMock.callsTo('/api/runs', 'DELETE').length, 0);

  runsData = [mk({ id: 2 })];
  await confirmYes();
  const [del] = fetchMock.callsTo('/api/runs', 'DELETE');
  assert.equal(del.url.searchParams.get('ids'), '1,3');
  assert.equal(runsCalls().length, 1, 'reloaded');
  assert.equal(toast().textContent, 'Deleted 2 run(s)');
  assert.ok($('delete-selected-btn').classList.contains('hidden'));
  assert.deepEqual(runs.cachedRuns.map((r) => r.id), [2]);
});

test('Delete Selected: cancel sends nothing; server error toasts; empty selection is a no-op', async () => {
  click('#delete-selected-btn'); // nothing selected
  await flush();
  assert.ok($('confirm-modal').classList.contains('hidden'));

  click(dataRow(1).querySelector('.run-cb'));
  click('#delete-selected-btn');
  await confirmNo();
  assert.equal(fetchMock.callsTo('/api/runs', 'DELETE').length, 0);

  fetchMock.on('DELETE', '/api/runs', () => 500);
  click('#delete-selected-btn');
  await confirmYes();
  assert.equal(toast().textContent, 'Bulk delete failed');
  assert.equal($('delete-selected-btn').textContent, 'Delete Selected (1)', 'selection kept on failure');
});

test('Delete All: confirms with the completed count and DELETEs ?delete_all=true', async () => {
  await show([mk({ id: 1 }), mk({ id: 2 }), mk({ id: 3, status: 'running', finished_at: null }),
    mk({ id: 4, status: 'queued', finished_at: null })]);
  fetchMock.reset();
  fetchMock.on('DELETE', '/api/runs', () => ({ deleted: 2 }));
  click('#delete-all-btn');
  await flush();
  assert.match($('confirm-msg').textContent, /Delete all 2 completed run/);
  await confirmYes();
  const [del] = fetchMock.callsTo('/api/runs', 'DELETE');
  assert.equal(del.url.searchParams.get('delete_all'), 'true');
  assert.equal(toast().textContent, 'Deleted 2 run(s)');
  assert.equal(runsCalls().length, 1);
});

test('Delete All with only active runs toasts and sends nothing', async () => {
  await show([mk({ id: 3, status: 'running', finished_at: null })]);
  fetchMock.reset();
  click('#delete-all-btn');
  await flush();
  assert.equal(toast().textContent, 'No completed runs to delete');
  assert.ok($('confirm-modal').classList.contains('hidden'));
  assert.equal(fetchMock.calls.length, 0);
});

// ── single-row actions ──────────────────────────────────────────────────────────

test('row delete: DELETE /api/runs/{id} → reload + toast; 409 and errors toast', async () => {
  fetchMock.on('DELETE', /^\/api\/runs\/\d+$/, () => 204);
  click(dataRow(2).querySelector('[data-action="delete"]'));
  await flush();
  assert.equal(fetchMock.callsTo('/api/runs/2', 'DELETE').length, 1);
  assert.equal(runsCalls().length, 1);
  assert.equal(toast().textContent, 'Run deleted');

  fetchMock.on('DELETE', /^\/api\/runs\/\d+$/, () => 409);
  click(dataRow(1).querySelector('[data-action="delete"]'));
  await flush();
  assert.equal(toast().textContent, 'Cannot delete an active run');

  fetchMock.on('DELETE', /^\/api\/runs\/\d+$/, () => 500);
  click(dataRow(1).querySelector('[data-action="delete"]'));
  await flush();
  assert.equal(toast().textContent, 'Delete failed');
});

test('abort: confirm → POST /cancel → toast + reload; "already finishing" when not cancelled', async () => {
  await show([mk({ id: 9, status: 'running', finished_at: null })]);
  fetchMock.reset();
  fetchMock.on('POST', '/api/runs/9/cancel', () => ({ cancelled: true }));
  const btn = dataRow(9).querySelector('[data-action="abort"]');
  click(btn);
  await flush();
  assert.equal($('confirm-title').textContent, 'Abort Run');
  await confirmYes();
  assert.equal(fetchMock.callsTo('/api/runs/9/cancel', 'POST').length, 1);
  assert.equal(toast().textContent, 'Run #9 aborted');
  assert.equal(runsCalls().length, 1);

  fetchMock.on('POST', '/api/runs/9/cancel', () => ({ cancelled: false }));
  click(dataRow(9).querySelector('[data-action="abort"]'));
  await confirmYes();
  assert.equal(toast().textContent, 'Run #9 was already finishing');
});

test('abort: cancel sends nothing; a server error re-enables the button', async () => {
  await show([mk({ id: 9, status: 'running', finished_at: null })]);
  fetchMock.reset();
  click(dataRow(9).querySelector('[data-action="abort"]'));
  await confirmNo();
  assert.equal(fetchMock.callsTo('/api/runs/9/cancel').length, 0);

  let release;
  fetchMock.on('POST', '/api/runs/9/cancel', () => new Promise((r) => { release = () => r(500); }));
  const btn = dataRow(9).querySelector('[data-action="abort"]');
  click(btn);
  await confirmYes();
  assert.equal(btn.disabled, true);
  assert.equal(btn.textContent, 'Aborting…');
  release();
  await flush();
  assert.equal(btn.disabled, false);
  assert.equal(btn.textContent, '⨯ Abort');
  assert.equal(toast().textContent, 'Abort failed');
});

test('rerun button loads the job for the run form', async () => {
  fetchMock.on('GET', '/api/jobs/10', () => 404);
  click(dataRow(1).querySelector('[data-action="rerun"]'));
  await flush();
  assert.equal(fetchMock.callsTo('/api/jobs/10', 'GET').length, 1);
  assert.equal(toast().textContent, 'Could not load the previous run settings');
});

test('copy writes the Result pane JSON to the clipboard; a clipboard failure toasts', async () => {
  const result = { answer: 'copy me', n: 1 };
  await show([mk({ id: 4, result: JSON.stringify(result) })]);
  navigator.clipboard.writes.length = 0;
  click(detailRow(4).querySelector('[data-action="copy"]'));
  await flush();
  assert.deepEqual(navigator.clipboard.writes, [JSON.stringify(result, null, 2)]);
  assert.equal(toast().textContent, 'Copied to clipboard');

  const orig = navigator.clipboard.writeText;
  navigator.clipboard.writeText = async () => { throw new Error('denied'); };
  try {
    click(detailRow(4).querySelector('[data-action="copy"]'));
    await flush();
    assert.equal(toast().textContent, 'Copy failed — select manually');
  } finally {
    navigator.clipboard.writeText = orig;
  }
});

test('report.pdf link only for results with pdf_url; leads.csv + leads table only for email_collect JSON', async () => {
  await show([
    mk({ id: 1, job_type: 'profit_health_check', result: JSON.stringify({ summary: 's', pdf_url: '/x.pdf' }) }),
    mk({ id: 2, result: JSON.stringify({ answer: 'no pdf' }) }),
    mk({ id: 3, job_type: 'email_collect', result: JSON.stringify({
      discovered_count: 5, with_website: 4, lead_count: 2,
      leads: [
        { company: 'Acme <Ltd>', email: 'a@acme.tw', confidence: 'high', icp_fit: 8, hook: 'h', website: 'https://acme.tw' },
        { company: 'Beta', email: 'info@beta.tw', source: 'guessed', confidence: null },
      ],
    }) }),
    mk({ id: 4, job_type: 'email_collect', result: 'not json' }),
  ]);
  const links = (id) => [...$(`pane-result-${id}`).querySelectorAll('a.btn-copy')].map((a) => a.getAttribute('href'));
  assert.deepEqual(links(1), ['/api/runs/1/report.pdf']);
  assert.deepEqual(links(2), []);
  assert.deepEqual(links(3), ['/api/runs/3/leads.csv']);
  assert.deepEqual(links(4), []);
  assert.equal($('pane-result-4').querySelector('.leads-table'), null);

  const pane = $('pane-result-3');
  assert.match(pane.querySelector('.leads-summary').textContent, /Discovered 5 · with website\s+4 · verified leads\s+2/);
  const rows = [...pane.querySelectorAll('.leads-table tbody tr')];
  assert.equal(rows.length, 2);
  assert.equal(rows[0].querySelector('td').textContent, 'Acme <Ltd>');
  assert.equal(rows[0].querySelector('a[href^="mailto:"]').getAttribute('href'), 'mailto:a@acme.tw');
  assert.ok(rows[0].querySelector('.lead-conf-high'));
  assert.equal(rows[0].querySelector('a[target="_blank"]').getAttribute('href'), 'https://acme.tw');
  assert.ok(rows[1].querySelector('.lead-guessed'));
  assert.equal(rows[1].querySelector('.lead-conf').textContent, '—');
  assert.ok(rows[1].querySelector('.lead-conf-low'));
});

test('email_collect with no leads shows the "no contact emails" note', async () => {
  await show([mk({ id: 1, job_type: 'email_collect', result: JSON.stringify({ discovered_count: 3, leads: [] }) })]);
  assert.match($('pane-result-1').textContent, /No contact emails collected/);
  assert.equal($('pane-result-1').querySelector('.leads-table'), null);
});

// ── timers ──────────────────────────────────────────────────────────────────────

test('active runs show a ticking elapsed timer; stopElapsedTimer freezes it', async () => {
  await show([mk({ id: 8, status: 'running', finished_at: null, started_at: new Date(Date.now() - 60_000).toISOString() })]);
  const timer = () => dataRow(8).querySelector('.elapsed-timer');
  assert.equal(timer().textContent, '60s…');
  mock.timers.tick(3000);
  assert.equal(timer().textContent, '63s…');

  stopElapsedTimer(8);
  mock.timers.tick(5000);
  assert.equal(timer().textContent, '63s…');
  stopElapsedTimer(12345); // unknown id is a no-op
});

test('scrollToRun highlights the row after 120 ms and clears it after 2 s', async () => {
  const row = dataRow(2);
  let scrolled = 0;
  row.scrollIntoView = () => { scrolled++; };
  scrollToRun(2);
  assert.ok(!row.classList.contains('row-highlight'));
  mock.timers.tick(120);
  assert.equal(scrolled, 1);
  assert.ok(row.classList.contains('row-highlight'));
  mock.timers.tick(2000);
  assert.ok(!row.classList.contains('row-highlight'));

  scrollToRun(9999); // missing row: no throw
  mock.timers.tick(120);
});

test('poller: refreshes in place every 5 s only while logged in and no SSE stream is open', async () => {
  // Logged out → no polling.
  mock.timers.tick(5000);
  await flush();
  assert.equal(runsCalls().length, 0);

  await show([mk({ id: 1 })], 230);
  click($('page-numbers').querySelector('[data-page-num="1"]'));
  await flush();
  auth.onAuthenticated({ username: 'u', is_admin: false });
  await flush();
  try {
    fetchMock.reset();
    mock.timers.tick(5000);
    await flush();
    assert.equal(runsCalls().length, 1);
    assert.equal(lastRunsQuery().get('offset'), '0', 'onAuthenticated reset to page 1');

    click($('page-numbers').querySelector('[data-page-num="1"]'));
    await flush();
    fetchMock.reset();
    mock.timers.tick(5000);
    await flush();
    assert.equal(lastRunsQuery().get('offset'), '50', 'poll keeps the current page');

    // While a live stream is open the poller stays quiet.
    progress.startSSE(1);
    fetchMock.reset();
    mock.timers.tick(15000);
    await flush();
    assert.equal(runsCalls().length, 0);

    FakeEventSource.instances.at(-1).fail();
    mock.timers.tick(5000);
    await flush();
    assert.equal(runsCalls().length, 1);
  } finally {
    auth.showLogin();
  }
  fetchMock.reset();
  mock.timers.tick(5000);
  await flush();
  assert.equal(runsCalls().length, 0, 'stops after logout');
});

test('admins see an owner badge on each run', async () => {
  auth.onAuthenticated({ username: 'root', is_admin: true });
  try {
    await show([mk({ id: 1, owner: 'alice<x>' }), mk({ id: 2 })]);
    const badge = dataRow(1).querySelector('.badge-admin');
    assert.equal(badge.textContent, '@alice<x>');
    assert.equal(dataRow(2).querySelector('.badge-admin'), null);
  } finally {
    auth.showLogin();
  }
  await show([mk({ id: 1, owner: 'alice' })]);
  assert.equal(dataRow(1).querySelector('.badge-admin'), null, 'non-admins see no owner badge');
});

// ── isActiveStatus (E2: 'queued' = waiting for a concurrency slot) ─────────────

test('isActiveStatus: pending/queued/running are active, terminal statuses are not', () => {
  for (const st of ['pending', 'queued', 'running']) assert.equal(runs.isActiveStatus(st), true, st);
  for (const st of ['success', 'failed', undefined, null, '']) assert.equal(runs.isActiveStatus(st), false, String(st));
});
