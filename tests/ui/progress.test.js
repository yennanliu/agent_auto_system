// Live run progress (ui/js/progress.js): the progress panel and the SSE stream
// (FakeEventSource) that appends log lines, updates the history row in place and
// closes on a terminal status or an error.
import { test, beforeEach, afterEach, mock } from 'node:test';
import assert from 'node:assert/strict';
import { setupDom, importUi, flush, jsonResponse, FakeEventSource, $ } from './harness.js';

let runsData = [];
const { fetchMock } = setupDom({
  routes: [
    ['GET', '/api/runs', () => jsonResponse(runsData, { headers: { 'X-Total-Count': String(runsData.length) } })],
  ],
});

const catalog = await importUi('catalog.js');
const progress = await importUi('progress.js');
const runs = await importUi('runs.js');
const { showProgress, startSSE } = progress;

// A deterministic 3-step flow for the step graph.
catalog.FLOW_STEPS.hacker_news_digest = [
  { label: 'Fetch', trigger: 'Fetching' },
  { label: 'Summarise', trigger: 'Summarising' },
  { label: 'Publish', trigger: 'Publishing' },
];

const run = (over = {}) => ({
  id: 42, job_id: 4, job_name: 'HN digest', job_type: 'hacker_news_digest',
  status: 'running', started_at: '2026-11-15T08:00:00Z', finished_at: null,
  result: null, log: null, ...over,
});

const lastES = () => FakeEventSource.instances.at(-1);
const stepStates = () => [...$('progress-step-graph').querySelectorAll('.step-node')]
  .map((n) => [...n.classList].find((c) => c.startsWith('sn-')).slice(3));
const row = (id = 42) => document.querySelector(`tr.data-row[data-run-id="${id}"]`);
const statusBadge = (id = 42) => row(id).querySelectorAll('td')[3].querySelector('.badge');

beforeEach(async () => {
  runsData = [run()];
  await runs.loadHistory();
  fetchMock.reset();
});

afterEach(() => {
  // Leave no stream open between tests.
  if (progress.activeEventSource) progress.activeEventSource.onerror();
  mock.timers.reset();
});

test('showProgress resets the log, shows the panel and renders a running step graph', () => {
  $('progress-log').innerHTML = '<li>stale</li>';
  $('progress-panel').classList.add('hidden');
  $('progress-pulse').style.display = 'none';

  showProgress('My job', 'hacker_news_digest');

  assert.equal($('progress-log').children.length, 0);
  assert.equal($('progress-job-name').textContent, 'My job');
  assert.equal($('progress-pulse').style.display, '');
  assert.ok(!$('progress-panel').classList.contains('hidden'));
  assert.deepEqual(stepStates(), ['pending', 'pending', 'pending']);
});

test('showProgress with an unknown/missing job type renders no step graph', () => {
  showProgress('X', undefined);
  assert.equal($('progress-step-graph').innerHTML, '');
  showProgress('Y', 'not_a_type');
  assert.equal($('progress-step-graph').innerHTML, '');
});

test('startSSE opens /api/runs/{id}/stream and exposes it as activeEventSource', () => {
  showProgress('HN digest', 'hacker_news_digest');
  startSSE(42);
  assert.equal(lastES().url, '/api/runs/42/stream');
  assert.equal(progress.activeEventSource, lastES());
  assert.equal(lastES().closed, false);
});

test('new_logs entries are appended (escaped) and advance the step graph', () => {
  showProgress('HN digest', 'hacker_news_digest');
  startSSE(42);
  lastES().emit({ status: 'running', new_logs: [{ ts: '08:00:01', msg: 'Fetching stories' }] });
  lastES().emit({ status: 'running', new_logs: [
    { ts: '08:00:03', msg: 'Summarising <script>alert(1)</script>' },
  ] });

  const items = [...$('progress-log').querySelectorAll('li')];
  assert.equal(items.length, 2);
  assert.equal(items[0].querySelector('.log-ts').textContent, '08:00:01');
  assert.match(items[0].textContent, /Fetching stories$/);
  assert.equal($('progress-log').querySelector('script'), null);
  assert.match(items[1].textContent, /<script>alert\(1\)<\/script>/);
  assert.deepEqual(stepStates(), ['done', 'running', 'pending']);
  assert.equal(fetchMock.callsTo('/api/runs').length, 0, 'non-terminal events do not reload history');
});

test('a non-terminal event updates the existing row in place', () => {
  startSSE(42);
  lastES().emit({ status: 'running', result: { answer: 'partial answer' } });
  assert.equal(statusBadge().textContent, 'running');
  assert.equal(statusBadge().className, 'badge badge-running');
  assert.equal(row().querySelector('.result-cell').textContent, 'partial answer');
  assert.equal(progress.activeEventSource, lastES());
});

test('success: closes the stream, shows "Completed in Ns", reloads history, hides the panel after 3.5 s', async () => {
  mock.timers.enable({ apis: ['setTimeout', 'Date'], now: 0 });
  showProgress('HN digest', 'hacker_news_digest');
  startSSE(42);
  const es = lastES();
  es.emit({ status: 'running', new_logs: [{ ts: '08:00:01', msg: 'Fetching' }] });
  mock.timers.tick(2500);
  runsData = [run({ status: 'success', finished_at: '2026-11-15T08:00:02Z', result: '{"answer":"done"}' })];
  es.emit({ status: 'success', new_logs: [{ ts: '08:00:03', msg: 'Publishing' }], result: { answer: 'done' } });

  assert.equal(es.closed, true);
  assert.equal(progress.activeEventSource, null);
  assert.equal($('progress-pulse').style.display, 'none');
  assert.equal($('progress-job-name').textContent, '✓ Completed in 2.5s');
  assert.match($('progress-job-name').innerHTML, /var\(--green\)/);
  assert.deepEqual(stepStates(), ['done', 'done', 'done']);
  assert.equal(fetchMock.callsTo('/api/runs', 'GET').length, 1, 'terminal status reloads history');

  await flush();
  assert.equal(statusBadge().textContent, 'success', 'row re-rendered from the reload');

  assert.ok(!$('progress-panel').classList.contains('hidden'));
  mock.timers.tick(3499);
  assert.ok(!$('progress-panel').classList.contains('hidden'));
  mock.timers.tick(1);
  assert.ok($('progress-panel').classList.contains('hidden'));
});

test('failed: closes the stream and shows "Failed" with the failed step', () => {
  mock.timers.enable({ apis: ['setTimeout', 'Date'], now: 0 });
  showProgress('HN digest', 'hacker_news_digest');
  startSSE(42);
  const es = lastES();
  es.emit({ status: 'running', new_logs: [{ ts: '1', msg: 'Fetching' }, { ts: '2', msg: 'Summarising' }] });
  mock.timers.tick(1000);
  es.emit({ status: 'failed' });

  assert.equal(es.closed, true);
  assert.equal(progress.activeEventSource, null);
  assert.equal($('progress-job-name').textContent, '✗ Failed in 1.0s');
  assert.match($('progress-job-name').innerHTML, /var\(--red\)/);
  assert.deepEqual(stepStates(), ['done', 'failed', 'pending']);
  assert.equal(statusBadge().textContent, 'failed');
});

test('onerror closes the stream and hides the panel immediately', () => {
  showProgress('HN digest', 'hacker_news_digest');
  startSSE(42);
  const es = lastES();
  es.fail();
  assert.equal(es.closed, true);
  assert.equal(progress.activeEventSource, null);
  assert.ok($('progress-panel').classList.contains('hidden'));
  assert.equal(fetchMock.calls.length, 0);
});

test('opening a new stream closes the previous one', () => {
  startSSE(42);
  const first = lastES();
  startSSE(43);
  const second = lastES();
  assert.notEqual(first, second);
  assert.equal(first.closed, true);
  assert.equal(second.closed, false);
  assert.equal(second.url, '/api/runs/43/stream');
  assert.equal(progress.activeEventSource, second);
});

test('an event for a run not in the table reloads history instead of patching a row', async () => {
  startSSE(999);
  lastES().emit({ status: 'running' });
  assert.equal(fetchMock.callsTo('/api/runs', 'GET').length, 1);
  await flush();
  assert.equal(statusBadge(42).textContent, 'running', 'other rows untouched');
});

test('the in-place update targets the status badge, not the admin owner badge', { todo: 'progress.js:79 row.querySelector(".badge") hits the "@owner" badge-admin span (rendered before the status badge for admins), overwriting it with the status' }, async () => {
  const auth = await importUi('auth.js');
  auth.onAuthenticated({ username: 'root', is_admin: true });
  runsData = [run({ owner: 'alice' })];
  await runs.loadHistory();
  await flush();
  try {
    startSSE(42);
    lastES().emit({ status: 'running', result: { answer: 'x' } });
    assert.equal(row().querySelector('.badge-admin')?.textContent, '@alice');
    assert.equal(statusBadge().textContent, 'running');
  } finally {
    auth.showLogin();
  }
});

test('a server {error} event (run deleted mid-stream) does not blank the status badge', { todo: 'progress.js:80 sets badge text/class from data.status even when absent → empty "badge badge-undefined"' }, () => {
  startSSE(42);
  lastES().emit({ error: 'run not found' });
  assert.equal(statusBadge().textContent, 'running');
  assert.equal(statusBadge().className, 'badge badge-running');
});
