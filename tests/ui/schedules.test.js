// Schedules page (ui/js/schedules.js): table rendering, refresh, and the tbody
// actions (Run now / Edit / Unschedule).
import { test, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { setupDom, importUi, flush, click, $, FakeEventSource } from './harness.js';

const NEXT = '2026-11-16T09:00:00Z';
const LAST = '2026-11-15T09:00:00Z';

let scheduleRows = [];
const { window, fetchMock } = setupDom({
  routes: [
    ['GET', '/api/schedules', () => scheduleRows],
    ['GET', '/api/runs', () => []],
  ],
});

const { loadSchedulesPage } = await importUi('schedules.js');

const row = (over = {}) => ({
  job_id: 7,
  name: 'Daily HN',
  job_type: 'hacker_news_digest',
  schedule: '0 9 * * *',
  valid: true,
  next_run_at: NEXT,
  run_count: 12,
  last_run: { status: 'success', started_at: LAST },
  ...over,
});

const tbody = () => $('schedules-tbody');
const cells = (tr) => [...tr.querySelectorAll('td')];
const toast = () => $('toast-banner');

beforeEach(() => {
  fetchMock.reset();
  scheduleRows = [];
  toast().classList.add('hidden');
  toast().textContent = '';
  window.prompt = () => null;
});

test('loadSchedulesPage renders one row per schedule with next run, count and last run', async () => {
  scheduleRows = [row(), row({ job_id: 8, name: 'Weekly sheet', job_type: 'google_sheet_reader', last_run: null })];
  await loadSchedulesPage();

  assert.equal(fetchMock.callsTo('/api/schedules', 'GET').length, 1);
  const trs = tbody().querySelectorAll('tr');
  assert.equal(trs.length, 2);

  const [name, type, cron, next, count, last] = cells(trs[0]);
  assert.equal(name.textContent.trim(), 'Daily HN');
  assert.equal(type.querySelector('.type-chip').textContent, 'HN');
  assert.ok(type.querySelector('.type-chip').classList.contains('chip-hn'));
  assert.equal(cron.querySelector('code').textContent, '0 9 * * *');
  assert.ok(!cron.textContent.includes('(invalid)'));
  assert.equal(next.textContent, new Date(NEXT).toLocaleString());
  assert.equal(count.textContent, '12');
  const badge = last.querySelector('.badge');
  assert.equal(badge.textContent, 'success');
  assert.ok(badge.classList.contains('badge-success'));
  assert.ok(last.textContent.includes(new Date(LAST).toLocaleString()));

  // Second row: never run.
  assert.equal(cells(trs[1])[5].textContent.trim(), 'never');
  assert.equal(cells(trs[1])[1].querySelector('.type-chip').textContent, 'SHEET');

  // Action buttons carry the job id / cron / name.
  const actions = cells(trs[0])[6];
  assert.equal(actions.querySelector('[data-sched-run]').dataset.schedRun, '7');
  assert.equal(actions.querySelector('[data-sched-edit]').dataset.cron, '0 9 * * *');
  assert.equal(actions.querySelector('[data-sched-unset]').dataset.name, 'Daily HN');
});

test('missing next_run_at shows a dash, invalid cron is flagged, unknown type gets a fallback chip', async () => {
  scheduleRows = [row({ next_run_at: null, valid: false, schedule: 'bogus', job_type: 'mystery_type' })];
  await loadSchedulesPage();
  const [, type, cron, next] = cells(tbody().querySelector('tr'));
  assert.equal(next.textContent, '—');
  assert.ok(cron.textContent.includes('(invalid)'));
  assert.match(cron.querySelector('code').getAttribute('style'), /var\(--red\)/);
  const chip = type.querySelector('.type-chip');
  assert.equal(chip.textContent, 'MYSTERY_TYPE');
  assert.ok(chip.classList.contains('chip-unknown'));
});

test('schedule names and cron strings are HTML-escaped', async () => {
  scheduleRows = [row({ name: '<img src=x onerror=alert(1)>', schedule: '<b>0</b> * * * *' })];
  await loadSchedulesPage();
  const tr = tbody().querySelector('tr');
  assert.equal(tr.querySelector('img'), null);
  assert.equal(tr.querySelector('code b'), null);
  assert.equal(cells(tr)[0].textContent.trim(), '<img src=x onerror=alert(1)>');
  assert.equal(tr.querySelector('[data-sched-unset]').dataset.name, '<img src=x onerror=alert(1)>');
  assert.equal(tr.querySelector('[data-sched-edit]').dataset.cron, '<b>0</b> * * * *');
});

test('empty list shows the empty-state message', async () => {
  scheduleRows = [];
  await loadSchedulesPage();
  const td = tbody().querySelector('td.empty-state');
  assert.match(td.textContent, /No scheduled jobs/);
  assert.equal(td.getAttribute('colspan'), '7');
});

test('a failed request renders the error in the table', async () => {
  fetchMock.on('GET', '/api/schedules', () => 500);
  try {
    await loadSchedulesPage();
    assert.equal(tbody().querySelector('td.empty-state').textContent, 'Failed to load schedules');
  } finally {
    fetchMock.on('GET', '/api/schedules', () => scheduleRows);
  }
});

test('the refresh button refetches and re-renders', async () => {
  scheduleRows = [row()];
  await loadSchedulesPage();
  fetchMock.reset();
  scheduleRows = [row(), row({ job_id: 9, name: 'Third' })];
  click('#schedules-refresh-btn');
  // Loading placeholder is shown synchronously.
  assert.match(tbody().textContent, /Loading/);
  await flush();
  assert.equal(fetchMock.callsTo('/api/schedules', 'GET').length, 1);
  assert.equal(tbody().querySelectorAll('tr').length, 2);
  assert.ok(tbody().textContent.includes('Third'));
});

test('Edit: PATCHes the trimmed cron, toasts and reloads', async () => {
  scheduleRows = [row()];
  await loadSchedulesPage();
  let promptArgs;
  window.prompt = (...a) => { promptArgs = a; return '  */5 * * * *  '; };
  fetchMock.on('PATCH', '/api/jobs/7', () => ({ ok: true }));
  fetchMock.reset();

  click(tbody().querySelector('[data-sched-edit]'));
  await flush();

  assert.equal(promptArgs[1], '0 9 * * *', 'prompt is prefilled with the current cron');
  const [patch] = fetchMock.callsTo('/api/jobs/7', 'PATCH');
  assert.deepEqual(patch.body, { schedule: '*/5 * * * *' });
  assert.equal(patch.init.headers['Content-Type'], 'application/json');
  assert.equal(toast().textContent, 'Schedule updated');
  assert.equal(fetchMock.callsTo('/api/schedules', 'GET').length, 1, 'reloads the page');
});

test('Edit: cancelling the prompt or entering blank does not PATCH', async () => {
  scheduleRows = [row()];
  await loadSchedulesPage();
  fetchMock.reset();

  window.prompt = () => null;
  click(tbody().querySelector('[data-sched-edit]'));
  await flush();
  assert.equal(fetchMock.callsTo('/api/jobs/7', 'PATCH').length, 0);
  assert.ok(toast().classList.contains('hidden'));

  window.prompt = () => '   ';
  click(tbody().querySelector('[data-sched-edit]'));
  await flush();
  assert.equal(fetchMock.callsTo('/api/jobs/7', 'PATCH').length, 0);
  assert.match(toast().textContent, /Empty — use Unschedule/);
});

test('Edit: a 400 shows the server detail and does not reload', async () => {
  scheduleRows = [row()];
  await loadSchedulesPage();
  window.prompt = () => '99 99 * * *';
  fetchMock.on('PATCH', '/api/jobs/7', () => new Response(JSON.stringify({ detail: 'bad cron: hour' }), { status: 400 }));
  fetchMock.reset();
  click(tbody().querySelector('[data-sched-edit]'));
  await flush();
  assert.equal(toast().textContent, 'bad cron: hour');
  assert.equal(fetchMock.callsTo('/api/schedules').length, 0);

  // 400 without a JSON body falls back to a generic message.
  fetchMock.on('PATCH', '/api/jobs/7', () => new Response('nope', { status: 400 }));
  click(tbody().querySelector('[data-sched-edit]'));
  await flush();
  assert.equal(toast().textContent, 'Invalid cron expression');

  // Other non-OK statuses surface "Update failed".
  fetchMock.on('PATCH', '/api/jobs/7', () => 500);
  click(tbody().querySelector('[data-sched-edit]'));
  await flush();
  assert.equal(toast().textContent, 'Update failed');
});

test('Unschedule: confirm → PATCH schedule:null → reload', async () => {
  scheduleRows = [row()];
  await loadSchedulesPage();
  fetchMock.on('PATCH', '/api/jobs/7', () => ({ ok: true }));
  fetchMock.reset();

  click(tbody().querySelector('[data-sched-unset]'));
  await flush();
  assert.ok(!$('confirm-modal').classList.contains('hidden'));
  assert.equal($('confirm-title').textContent, 'Remove schedule');
  assert.match($('confirm-msg').textContent, /"Daily HN"/);
  assert.equal(fetchMock.callsTo('/api/jobs/7', 'PATCH').length, 0, 'nothing sent before confirming');

  click('#confirm-ok');
  await flush();
  const [patch] = fetchMock.callsTo('/api/jobs/7', 'PATCH');
  assert.deepEqual(patch.body, { schedule: null });
  assert.equal(toast().textContent, 'Schedule removed');
  assert.equal(fetchMock.callsTo('/api/schedules', 'GET').length, 1);
});

test('Unschedule: cancelling the dialog sends nothing; a failed PATCH toasts', async () => {
  scheduleRows = [row()];
  await loadSchedulesPage();
  fetchMock.reset();

  click(tbody().querySelector('[data-sched-unset]'));
  await flush();
  click('#confirm-cancel');
  await flush();
  assert.equal(fetchMock.callsTo('/api/jobs/7', 'PATCH').length, 0);

  fetchMock.on('PATCH', '/api/jobs/7', () => 500);
  click(tbody().querySelector('[data-sched-unset]'));
  await flush();
  click('#confirm-ok');
  await flush();
  assert.equal(fetchMock.callsTo('/api/jobs/7', 'PATCH').length, 1);
  assert.equal(toast().textContent, 'Update failed');
  assert.equal(fetchMock.callsTo('/api/schedules').length, 0);
});

test('Run now: navigates to activity, POSTs the run and opens the SSE stream', async () => {
  scheduleRows = [row()];
  await loadSchedulesPage();
  fetchMock.on('POST', '/api/jobs/7/run', () => ({ run_id: 55 }));
  fetchMock.reset();
  const before = FakeEventSource.instances.length;

  click(tbody().querySelector('[data-sched-run]'));
  assert.ok($('page-activity').classList.contains('active'), 'switched to the activity page');
  await flush(10);

  assert.equal(fetchMock.callsTo('/api/jobs/7/run', 'POST').length, 1);
  assert.equal(FakeEventSource.instances.length, before + 1);
  assert.equal(FakeEventSource.instances.at(-1).url, '/api/runs/55/stream');
  assert.ok(!$('progress-panel').classList.contains('hidden'));
  FakeEventSource.instances.at(-1).fail(); // close the stream
});

test('clicks on non-action parts of the table do nothing', async () => {
  scheduleRows = [row()];
  await loadSchedulesPage();
  fetchMock.reset();
  click(tbody().querySelector('td'));
  await flush();
  assert.equal(fetchMock.calls.length, 0);
});
