// steps.js — fmtDur + renderStepGraph (single-flow graph from FLOW_STEPS, pipeline graph from logs).
import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { setupDom, importUi } from './harness.js';

const { fetchMock } = setupDom();
fetchMock.on('GET', '/api/automations/manifest', () => ({ automations: [
  { job_type: 'web_scraper', steps: [['Fetch page', 'Fetching'], ['Analyze', 'Analyzing'], ['Done', 'Finished']] },
  { job_type: 'xss_type', steps: [['<img src=x onerror=alert(1)>', 'go'], ['b & "c"', 'next']] },
] }));
const { loadManifest } = await importUi('catalog.js');
const { fmtDur, renderStepGraph } = await importUi('steps.js');
await loadManifest();

/** Parse a rendered graph into [{state, dot, label, metric}] + connector classes. */
function parse(html) {
  const host = document.createElement('div');
  host.innerHTML = html;
  const nodes = [...host.querySelectorAll('.step-node')].map((n) => ({
    state: [...n.classList].find((c) => c.startsWith('sn-')).slice(3),
    dot: n.querySelector('.step-dot').textContent,
    label: n.querySelector('.step-label').textContent,
    metric: n.querySelector('.step-metric')?.textContent ?? null,
  }));
  const conns = [...host.querySelectorAll('.step-conn')].map((c) =>
    [...c.classList].filter((x) => x !== 'step-conn').join(' '));
  return { host, nodes, conns };
}
const states = (html) => parse(html).nodes.map((n) => n.state);
const log = (ts, msg) => ({ ts, msg });

describe('fmtDur', () => {
  test('null / undefined → null', () => {
    assert.equal(fmtDur(null), null);
    assert.equal(fmtDur(undefined), null);
  });
  test('sub-second → "<1s"; otherwise "Ns"', () => {
    assert.equal(fmtDur(0), '<1s');
    assert.equal(fmtDur(0.4), '<1s');
    assert.equal(fmtDur(1), '1s');
    assert.equal(fmtDur(125), '125s');
  });
});

describe('renderStepGraph — single flow', () => {
  test('unknown job type → empty string', () => {
    assert.equal(renderStepGraph('nope', [], 'running'), '');
  });

  test('no logs → every step pending, numbered dots, plain connectors, no metrics', () => {
    for (const logs of [[], null, undefined]) {
      const { nodes, conns, host } = parse(renderStepGraph('web_scraper', logs, 'pending'));
      assert.ok(host.firstElementChild.classList.contains('step-graph'));
      assert.deepEqual(nodes.map((n) => n.state), ['pending', 'pending', 'pending']);
      assert.deepEqual(nodes.map((n) => n.dot), ['1', '2', '3']);
      assert.deepEqual(nodes.map((n) => n.label), ['Fetch page', 'Analyze', 'Done']);
      assert.deepEqual(conns, ['', '']);
      assert.ok(nodes.every((n) => n.metric === null));
    }
  });

  test('in-progress: earlier steps done, reached step running, partial connector', () => {
    const logs = [log('10:00:00', 'Fetching https://x'), log('10:00:07', 'Analyzing content')];
    const { nodes, conns } = parse(renderStepGraph('web_scraper', logs, 'running'));
    assert.deepEqual(nodes.map((n) => n.state), ['done', 'running', 'pending']);
    assert.deepEqual(nodes.map((n) => n.dot), ['✓', '…', '3']);
    assert.deepEqual(conns, ['sc-partial', '']);
    assert.equal(nodes[0].metric, '7s');      // until the next trigger
    assert.equal(nodes[1].metric, null);      // still running, no end yet
  });

  test('a later trigger marks skipped-over steps done', () => {
    const logs = [log('10:00:00', 'Analyzing')];
    assert.deepEqual(states(renderStepGraph('web_scraper', logs, 'running')), ['done', 'running', 'pending']);
  });

  test('failed run → reached step failed', () => {
    const logs = [log('10:00:00', 'Fetching'), log('10:00:02', 'Analyzing'), log('10:00:05', 'boom')];
    const { nodes, conns } = parse(renderStepGraph('web_scraper', logs, 'failed'));
    assert.deepEqual(nodes.map((n) => n.state), ['done', 'failed', 'pending']);
    assert.equal(nodes[1].dot, '✕');
    assert.deepEqual(conns, ['sc-done', '']);
    assert.equal(nodes[1].metric, '3s', 'terminal: last step measured to the last log line');
  });

  test('success → all reached steps done, full connectors, durations', () => {
    const logs = [log('10:00:00', 'Fetching'), log('10:00:00', 'Analyzing'), log('10:01:30', 'Finished'), log('10:01:31', 'saved')];
    const { nodes, conns } = parse(renderStepGraph('web_scraper', logs, 'success'));
    assert.deepEqual(nodes.map((n) => n.state), ['done', 'done', 'done']);
    assert.deepEqual(conns, ['sc-done', 'sc-done']);
    assert.deepEqual(nodes.map((n) => n.metric), ['<1s', '90s', '1s']);
  });

  test('missing / malformed timestamps → no metric, states still inferred', () => {
    const logs = [{ msg: 'Fetching' }, log('bad', 'Analyzing'), log('1:2', 'Finished')];
    const { nodes } = parse(renderStepGraph('web_scraper', logs, 'success'));
    assert.deepEqual(nodes.map((n) => n.state), ['done', 'done', 'done']);
    assert.ok(nodes.every((n) => n.metric === null));
  });

  test('log entries without msg are ignored', () => {
    assert.deepEqual(states(renderStepGraph('web_scraper', [{ ts: '10:00:00' }], 'running')),
      ['pending', 'pending', 'pending']);
  });

  test('labels from the manifest are HTML-escaped', () => {
    const html = renderStepGraph('xss_type', [], 'pending');
    const { host, nodes } = parse(html);
    assert.equal(host.querySelector('img'), null);
    assert.equal(nodes[0].label, '<img src=x onerror=alert(1)>');
    assert.equal(nodes[1].label, 'b & "c"');
  });
});

describe('renderStepGraph — pipeline', () => {
  const start = (ts, i, n, type) => log(ts, `[Step ${i}/${n}] Starting ${type}...`);
  const done = (ts, i, n) => log(ts, `[Step ${i}/${n}] Completed ok`);

  test('no logs → a single pending placeholder step', () => {
    const { nodes, conns } = parse(renderStepGraph('pipeline', [], 'running'));
    assert.deepEqual(nodes, [{ state: 'pending', dot: '1', label: 'step 1', metric: null }]);
    assert.deepEqual(conns, []);
  });

  test('no logs + success/failed → the placeholder takes the final status', () => {
    assert.deepEqual(states(renderStepGraph('pipeline', null, 'success')), ['done']);
    assert.deepEqual(states(renderStepGraph('pipeline', null, 'failed')), ['failed']);
  });

  test('in-progress: done / running / pending with labels from the log', () => {
    const logs = [
      start('09:00:00', 1, 3, 'web_scraper'),
      done('09:00:12', 1, 3),
      start('09:00:12', 2, 3, 'hacker_news_digest'),
    ];
    const { nodes, conns } = parse(renderStepGraph('pipeline', logs, 'running'));
    assert.deepEqual(nodes.map((n) => n.state), ['done', 'running', 'pending']);
    assert.deepEqual(nodes.map((n) => n.label), ['web scraper', 'hacker news digest', 'step 3']);
    assert.deepEqual(nodes.map((n) => n.metric), ['12s', null, null]);
    assert.deepEqual(conns, ['sc-done', 'sc-partial']);
  });

  test('success: last step done, measured to the last log line', () => {
    const logs = [
      start('09:00:00', 1, 2, 'web_scraper'), done('09:00:01', 1, 2),
      start('09:00:01', 2, 2, 'email_sender'), log('09:00:04', 'Pipeline finished'),
    ];
    const { nodes, conns } = parse(renderStepGraph('pipeline', logs, 'success'));
    assert.deepEqual(nodes.map((n) => n.state), ['done', 'done']);
    assert.deepEqual(nodes.map((n) => n.metric), ['1s', '3s']);
    assert.deepEqual(conns, ['sc-done']);
  });

  test('failure on the last step marks it failed', () => {
    const logs = [start('09:00:00', 1, 2, 'a'), done('09:00:00', 1, 2), start('09:00:00', 2, 2, 'b')];
    const { nodes } = parse(renderStepGraph('pipeline', logs, 'failed'));
    assert.deepEqual(nodes.map((n) => n.state), ['done', 'failed']);
    assert.equal(nodes[0].metric, '<1s');
  });

  test('duplicate Starting/Completed lines keep the first timestamps', () => {
    const logs = [
      start('09:00:00', 1, 1, 'a'), start('09:00:05', 1, 1, 'retry_name'),
      done('09:00:10', 1, 1), done('09:00:20', 1, 1),
    ];
    const { nodes } = parse(renderStepGraph('pipeline', logs, 'success'));
    assert.equal(nodes[0].label, 'a');
    assert.equal(nodes[0].metric, '10s');
  });

  test('step types parsed from the log are HTML-escaped', () => {
    const logs = [start('09:00:00', 1, 1, '<img src=x onerror=alert(1)>')];
    const { host, nodes } = parse(renderStepGraph('pipeline', logs, 'running'));
    assert.equal(host.querySelector('img'), null);
    assert.equal(nodes[0].label, '<img src=x onerror=alert(1)>');
  });

  test('connectors after not-yet-started steps are not drawn as done', {
    todo: 'steps.js:113 — `info?.endTs !== null` is true when `info` is undefined, so every '
      + 'connector after a pending (unstarted) pipeline step gets sc-done; the `info ? … : \'\'` '
      + 'branch is unreachable. Pre-existing in app.js.',
  }, () => {
    const logs = [start('09:00:00', 1, 4, 'a')];
    const { nodes, conns } = parse(renderStepGraph('pipeline', logs, 'running'));
    assert.deepEqual(nodes.map((n) => n.state), ['running', 'pending', 'pending', 'pending']);
    assert.deepEqual(conns, ['sc-partial', '', '']);
  });
});
