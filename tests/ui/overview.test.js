// Task Overview page (ui/js/overview.js): automation list, Airflow-style grid
// (runs × steps), and the per-cell step-log drill-down.
import { test, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { setupDom, importUi, flush, click, $ } from './harness.js';

let types = [];
const grids = {};
const stepLogs = {};

const { fetchMock } = setupDom({
  routes: [
    ['GET', '/api/overview', () => types],
    ['GET', /^\/api\/overview\/[^/]+$/, (url) => grids[decodeURIComponent(url.pathname.split('/').pop())] ?? 404],
    ['GET', /^\/api\/runs\/\d+\/steps$/, (url) => stepLogs[url.pathname.split('/')[3]] ?? 404],
  ],
});

const { loadOverviewPage } = await importUi('overview.js');

const run = (id, status, steps, over = {}) => ({
  run_id: id,
  job_name: `job-${id}`,
  status,
  started_at: '2026-11-15T08:00:00Z',
  duration_secs: 10,
  steps: steps.map((s) => ({ status: s })),
  ...over,
});

const list = () => $('ov-joblist');
const wrap = () => $('ov-grid-wrap');
const dataRows = () => [...wrap().querySelectorAll('.ov-row:not(.ov-row-head)')];

// The module remembers the selected automation across loads, so tests that need a
// specific grid load the page and then pick it explicitly.
async function openGrid(jobType = 'hacker_news_digest') {
  await loadOverviewPage();
  await flush();
  click(list().querySelector(`[data-job-type="${jobType}"]`));
  await flush();
}

beforeEach(() => {
  fetchMock.reset();
  types = [
    { job_type: 'hacker_news_digest', run_count: 3 },
    { job_type: 'web_scraper', run_count: 1 },
  ];
  for (const k of Object.keys(grids)) delete grids[k];
  for (const k of Object.keys(stepLogs)) delete stepLogs[k];
  grids.hacker_news_digest = {
    job_type: 'hacker_news_digest',
    task_names: ['Fetch', 'Summarise'],
    runs: [
      run(1, 'success', ['success', 'success'], { duration_secs: 20 }),
      run(2, 'failed', ['success', 'failed'], { duration_secs: 5 }),
      run(3, 'running', ['running'], { duration_secs: 0 }),
    ],
  };
  grids.web_scraper = {
    job_type: 'web_scraper',
    task_names: ['Scrape'],
    runs: [run(9, 'success', ['success'])],
  };
});

test('loadOverviewPage lists automations and auto-selects the first one', async () => {
  await loadOverviewPage();
  await flush();

  assert.equal(fetchMock.callsTo('/api/overview').length, 1);
  const btns = list().querySelectorAll('.ov-job');
  assert.equal(btns.length, 2);
  assert.equal(btns[0].dataset.jobType, 'hacker_news_digest');
  assert.equal(btns[0].querySelector('.type-chip').textContent, 'HN');
  assert.equal(btns[0].querySelector('.ov-job-count').textContent, '3');
  assert.ok(btns[0].classList.contains('active'));
  assert.ok(!btns[1].classList.contains('active'));

  const [gridCall] = fetchMock.callsTo('/api/overview/hacker_news_digest');
  assert.equal(gridCall.url.searchParams.get('limit'), '40');
});

test('the grid has one column per run and one row per task, with status classes', async () => {
  await openGrid();

  // Header row: one clickable cell per run (step -1 = full log) with the run status.
  const head = [...wrap().querySelectorAll('.ov-row-head .ov-cell')];
  assert.deepEqual(head.map((c) => c.dataset.runId), ['1', '2', '3']);
  assert.ok(head.every((c) => c.dataset.step === '-1'));
  assert.ok(head[0].classList.contains('ov-fill-success'));
  assert.equal(head[0].textContent, '✓');
  assert.ok(head[1].classList.contains('ov-fill-failed'));
  assert.equal(head[1].textContent, '✕');
  assert.ok(head[2].classList.contains('ov-fill-running'));

  const rows = dataRows();
  assert.deepEqual(rows.map((r) => r.querySelector('.ov-row-label').textContent), ['Fetch', 'Summarise']);
  const fill = (r) => [...r.querySelectorAll('.ov-cell')].map((c) =>
    [...c.classList].find((k) => k.startsWith('ov-fill-')).slice('ov-fill-'.length));
  assert.deepEqual(fill(rows[0]), ['success', 'success', 'running']);
  // Run 3 has no step 2 yet → pending.
  assert.deepEqual(fill(rows[1]), ['success', 'failed', 'pending']);
  assert.deepEqual([...rows[1].querySelectorAll('.ov-cell')].map((c) => c.dataset.step), ['1', '1', '1']);

  // Summary counts.
  const stats = [...wrap().querySelectorAll('.ov-stat b')].map((b) => b.textContent);
  assert.deepEqual(stats, ['3', '1', '1']);

  // Duration bars scale to the longest run (20s → 46px), with a 3px floor.
  const bars = [...wrap().querySelectorAll('.ov-duration-chart .ov-bar')];
  assert.deepEqual(bars.map((b) => b.style.height), ['46px', '12px', '3px']);
  assert.match(bars[0].title, /job-1 · run #1 · success · 20s/);
  assert.match(bars[2].title, /run #3 · running · <1s/); // fmtDur(0) → '<1s'
});

test('clicking another automation in the list loads its grid', async () => {
  await loadOverviewPage();
  await flush();
  fetchMock.reset();

  click(list().querySelector('[data-job-type="web_scraper"] .ov-job-name'));
  assert.match(wrap().textContent, /Loading/);
  await flush();

  assert.equal(fetchMock.callsTo('/api/overview/web_scraper').length, 1);
  assert.ok(list().querySelector('[data-job-type="web_scraper"]').classList.contains('active'));
  assert.ok(!list().querySelector('[data-job-type="hacker_news_digest"]').classList.contains('active'));
  assert.deepEqual(dataRows().map((r) => r.querySelector('.ov-row-label').textContent), ['Scrape']);
});

test('reloading keeps the previously selected automation', async () => {
  await loadOverviewPage();
  await flush();
  click(list().querySelector('[data-job-type="web_scraper"]'));
  await flush();
  fetchMock.reset();

  await loadOverviewPage();
  await flush();
  assert.equal(fetchMock.callsTo('/api/overview/web_scraper').length, 1);
  assert.equal(fetchMock.callsTo('/api/overview/hacker_news_digest').length, 0);
});

test('clicking a step cell shows that step\'s log; close hides it', async () => {
  stepLogs[2] = {
    status: 'failed',
    steps: [
      { name: 'Fetch', status: 'success', logs: [{ ts: '08:00:01', msg: 'fetched 30' }] },
      { name: 'Summarise', status: 'failed', logs: [{ ts: '08:00:05', msg: 'LLM <timeout>' }] },
    ],
  };
  await openGrid();

  const cell = dataRows()[1].querySelector('.ov-cell[data-run-id="2"]');
  click(cell);
  assert.ok(cell.classList.contains('selected'));
  await flush();

  assert.equal(fetchMock.callsTo('/api/runs/2/steps').length, 1);
  const panel = $('ov-step-log');
  assert.ok(!panel.classList.contains('hidden'));
  assert.equal(panel.querySelector('strong').textContent, 'Summarise');
  assert.ok(panel.querySelector('.ov-step-log-head .ov-cell').classList.contains('ov-fill-failed'));
  assert.match(panel.querySelector('.muted').textContent, /job-2 · run #2/);
  const items = [...panel.querySelectorAll('.ov-step-log-list li')];
  assert.equal(items.length, 1);
  assert.equal(items[0].querySelector('.ov-log-ts').textContent, '08:00:05');
  assert.match(items[0].textContent, /LLM <timeout>/, 'log text is escaped, not parsed as HTML');
  assert.equal(panel.querySelector('timeout'), null);

  // Selecting another cell moves the highlight.
  const other = dataRows()[0].querySelector('.ov-cell[data-run-id="2"]');
  click(other);
  await flush();
  assert.ok(other.classList.contains('selected'));
  assert.ok(!cell.classList.contains('selected'));
  assert.equal($('ov-step-log').querySelector('strong').textContent, 'Fetch');

  click('#ov-step-log-close');
  assert.ok($('ov-step-log').classList.contains('hidden'));
  assert.equal(wrap().querySelectorAll('.ov-cell.selected').length, 0);
});

test('clicking a run header cell shows the full run log across steps', async () => {
  stepLogs[1] = {
    status: 'success',
    steps: [
      { name: 'Fetch', status: 'success', logs: [{ ts: '1', msg: 'a' }] },
      { name: 'Summarise', status: 'success', logs: [{ ts: '2', msg: 'b' }, { ts: '3', msg: 'c' }] },
    ],
  };
  await openGrid();
  click(wrap().querySelector('.ov-row-head .ov-cell[data-run-id="1"]'));
  await flush();

  const panel = $('ov-step-log');
  assert.equal(panel.querySelector('strong').textContent, 'Full run log');
  assert.ok(panel.querySelector('.ov-step-log-head .ov-cell').classList.contains('ov-fill-success'));
  assert.deepEqual([...panel.querySelectorAll('.ov-log-ts')].map((e) => e.textContent), ['1', '2', '3']);
});

test('a step the server has no entry for falls back to "step N" / pending with an empty-state', async () => {
  stepLogs[3] = { status: 'running', steps: [{ name: 'Fetch', status: 'running', logs: [] }] };
  await openGrid();
  click(dataRows()[1].querySelector('.ov-cell[data-run-id="3"]'));
  await flush();
  const panel = $('ov-step-log');
  assert.equal(panel.querySelector('strong').textContent, 'step 2');
  assert.ok(panel.querySelector('.ov-step-log-head .ov-cell').classList.contains('ov-fill-pending'));
  assert.match(panel.textContent, /No log entries for this step/);
});

test('a failed step-log request shows the error in the panel', async () => {
  await openGrid();
  click(dataRows()[0].querySelector('.ov-cell[data-run-id="1"]')); // no stepLogs → 404
  await flush();
  assert.equal($('ov-step-log').textContent.trim(), 'Failed to load step log');
});

test('clicks on the grid outside a cell are ignored', async () => {
  await openGrid();
  fetchMock.reset();
  click(wrap().querySelector('.ov-row-label'));
  await flush();
  assert.equal(fetchMock.calls.length, 0);
  assert.ok($('ov-step-log').classList.contains('hidden'));
});

test('an automation with no runs shows an empty state', async () => {
  grids.hacker_news_digest = { job_type: 'hacker_news_digest', task_names: ['Fetch'], runs: [] };
  await openGrid();
  assert.match(wrap().textContent, /No runs for this automation yet/);
  assert.equal(wrap().querySelectorAll('.ov-cell[data-run-id]').length, 0);
});

test('no automations at all → empty list and grid placeholder, no grid fetch', async () => {
  types = [];
  await loadOverviewPage();
  await flush();
  assert.match(list().textContent, /No runs/);
  assert.match(wrap().textContent, /No runs yet. Run an automation first/);
  assert.equal(fetchMock.callsTo(/^\/api\/overview\//).length, 0);
});

test('failed list / grid requests render their error messages', async () => {
  fetchMock.on('GET', '/api/overview', () => 500);
  try {
    await loadOverviewPage();
    assert.equal(list().textContent.trim(), 'Failed to load overview');
  } finally {
    fetchMock.on('GET', '/api/overview', () => types);
  }

  await openGrid('web_scraper');
  delete grids.hacker_news_digest; // → 404
  await openGrid('hacker_news_digest');
  assert.equal(wrap().textContent.trim(), 'Failed to load overview');
});

test('job names and types from the server are HTML-escaped', async () => {
  types = [{ job_type: '<i>x</i>', run_count: 1 }];
  grids['<i>x</i>'] = {
    job_type: '<i>x</i>',
    task_names: ['<b>t</b>'],
    runs: [run(4, 'success', ['success'], { job_name: '<img src=x onerror=1>' })],
  };
  await loadOverviewPage();
  await flush();
  assert.equal(list().querySelector('i'), null);
  assert.equal(list().querySelector('.ov-job-name').textContent, '<i>x</i>');
  assert.equal(wrap().querySelector('i:not(.ov-cell)'), null);
  assert.equal(wrap().querySelector('b:not(.ov-stat b)'), null);
  assert.equal(wrap().querySelector('img'), null);
  assert.equal(dataRows()[0].querySelector('.ov-row-label').textContent, '<b>t</b>');
  assert.equal(fetchMock.callsTo(/^\/api\/overview\/%3Ci%3Ex%3C%2Fi%3E$/).length, 1, 'type is URL-encoded');
});
