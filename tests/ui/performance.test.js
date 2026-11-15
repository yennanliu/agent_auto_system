// performance.js — Analytics page: loadPerformancePage(days) → GET /api/stats?days=N
// → stat cards, eval-trust section, N-day trend, by-type table, per-provider/model
// cards. Also the window bridge main.js publishes for the inline trend-window onclick.
import { test, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { setupDom, importUi, flush, $ } from './harness.js';

// ── Stats payloads (shape mirrors src/routers/runs.py get_stats / _empty_stats) ─
function emptyStats(days = 7) {
  const trend = Array.from({ length: days }, (_, i) => ({
    date: `2026-11-${String(i + 1).padStart(2, '0')}`, label: `D${i + 1}`,
    total: 0, success: 0, failed: 0, cost: 0.0, tokens: 0, avg_score: null,
  }));
  return {
    total_runs: 0, success: 0, failed: 0, active: 0, success_rate: 0, avg_duration_secs: 0,
    avg_eval_score: null, avg_eval_confidence: null,
    eval_scored: 0, eval_llm: 0, eval_heuristic: 0, eval_independent: 0,
    eval_independent_rate: null, eval_llm_rate: null,
    retried_runs: 0, retry_rate: 0, avg_retries: 0,
    fallback_runs: 0, fallback_rate: 0, validated_runs: 0, validation_fails: 0, validation_pass_rate: null,
    p50_duration_secs: null, p95_duration_secs: null, trend_days: days,
    by_type: {}, trend,
    total_tokens_in: 0, total_tokens_out: 0, total_tokens: 0,
    total_cost_usd: 0.0, by_provider: {}, by_model: {},
  };
}

function fullStats() {
  return {
    ...emptyStats(7),
    total_runs: 40, success: 30, failed: 6, active: 4, success_rate: 83.3, avg_duration_secs: 12.4,
    avg_eval_score: 86.5, avg_eval_confidence: 0.91,
    eval_scored: 30, eval_llm: 27, eval_heuristic: 3, eval_independent: 24, eval_independent_rate: 80.0,
    retried_runs: 10, retry_rate: 25.0, avg_retries: 1.2,
    fallback_runs: 2, fallback_rate: 5.0, validated_runs: 36, validation_fails: 6, validation_pass_rate: 83.3,
    p50_duration_secs: 9.1, p95_duration_secs: 41.7,
    trend: [
      { date: '2026-11-09', label: 'Mon', total: 10, success: 8, failed: 2, cost: 0.1234, tokens: 9000, avg_score: 91.4 },
      { date: '2026-11-10', label: '<b>Tue</b>', total: 5, success: 1, failed: 4, cost: 0.005, tokens: 100, avg_score: 42 },
      { date: '2026-11-11', label: 'Wed', total: 0, success: 0, failed: 0, cost: 0.0, tokens: 0, avg_score: null },
      { date: '2026-11-12', label: 'Thu', total: 3, success: 2, failed: 0, cost: 0.02, tokens: 50, avg_score: 65 },
    ],
    by_type: {
      web_scraper: { total: 20, success: 15, failed: 5, pending: 0, running: 0, retried: 3, avg_duration: 8.2 },
      mystery_type: { total: 0, success: 0, failed: 0, pending: 0, running: 0, retried: 0, avg_duration: 0 },
    },
    total_tokens_in: 1000000, total_tokens_out: 234567, total_tokens: 1234567,
    total_cost_usd: 12.5,
    by_provider: {
      anthropic: { runs: 25, tokens_in: 1200, tokens_out: 300, cost_usd: 1.2345, models: ['claude-sonnet-4-6'] },
      gemini: { runs: 10, tokens_in: 500, tokens_out: 499, cost_usd: 0.004, models: ['gemini/gemini-2.5-pro', 'gemini/gemini-2.5-flash'] },
      '<i>unknown</i>': { runs: 5, tokens_in: 0, tokens_out: 0, cost_usd: 0, models: [] },
    },
    by_model: {
      anthropic: [{
        model: 'claude-sonnet-4-6', runs: 25, success: 21, tokens_in: 1200, tokens_out: 300, cost_usd: 1.2345,
        avg_duration: 10.5, avg_eval_score: 88.4, avg_eval_confidence: 0.876, scored: 20, llm_judged: 19,
        retried: 4, fallback: 2, cost_per_success: 0.058786, cost_per_quality: 0.0007,
      }],
      gemini: [
        { model: 'gemini/gemini-2.5-pro', runs: 4, success: 2, tokens_in: 500, tokens_out: 499, cost_usd: 0.00005,
          avg_duration: 0, avg_eval_score: 49.6, avg_eval_confidence: null, scored: 4, llm_judged: 1,
          retried: 0, fallback: 0, cost_per_success: 0.000025, cost_per_quality: null },
        { model: 'gemini/gemini-2.5-flash', runs: 0, success: 0, tokens_in: 0, tokens_out: 0, cost_usd: 0,
          avg_duration: 0, avg_eval_score: null, avg_eval_confidence: null, scored: 0, llm_judged: 0,
          retried: 0, fallback: 0, cost_per_success: null, cost_per_quality: null },
      ],
    },
  };
}

const S = { stats: fullStats(), status: 200, fail: null };
const { window, fetchMock } = setupDom({
  routes: [
    ['GET', '/api/stats', () => {
      if (S.fail) throw new Error(S.fail);
      return S.status === 200 ? S.stats : S.status;
    }],
    ['GET', '/api/auth/me', () => 401],
  ],
});

const { loadPerformancePage } = await importUi('performance.js');

// ── helpers ───────────────────────────────────────────────────────────────────
const perf = () => $('perf-content');
/** The stat card whose label is `label` (first match across the page). */
function card(label) {
  const lab = [...perf().querySelectorAll('.stat-label')].find((l) => l.textContent === label);
  assert.ok(lab, `stat card "${label}"`);
  const c = lab.closest('.stat-card');
  return { value: c.querySelector('.stat-value'), sub: c.querySelector('.stat-sub').textContent };
}
const valueClasses = (el) => [...el.classList].filter((c) => c !== 'stat-value');
const trendRows = () => [...perf().querySelectorAll('.trend-row')];
const daysParam = (call) => call.url.searchParams.get('days');
const pcard = (prov) => perf().querySelector(`.pcard-${prov}`);  // only used with plain ids

beforeEach(() => {
  fetchMock.reset();
  S.stats = fullStats();
  S.status = 200;
  S.fail = null;
});

// ── loading / windows ─────────────────────────────────────────────────────────

test('loads a 7-day window by default and shows a loading state first', async () => {
  const p = loadPerformancePage();
  assert.match(perf().textContent, /Loading metrics…/);
  await p;
  const [call] = fetchMock.callsTo('/api/stats');
  assert.equal(daysParam(call), '7');
  assert.ok(perf().querySelector('.stat-grid'));
});

test('a chosen window is used and remembered for later argument-less reloads', async () => {
  S.stats = { ...fullStats(), trend_days: 30 };
  await loadPerformancePage(30);
  await loadPerformancePage();
  assert.deepEqual(fetchMock.callsTo('/api/stats').map(daysParam), ['30', '30']);
  const active = [...perf().querySelectorAll('.perf-win-btn.active')].map((b) => b.textContent);
  assert.deepEqual(active, ['30d']);
  await loadPerformancePage(7);  // restore for the rest of the file
});

test('window buttons call the global loadPerformancePage(n) that main.js publishes', async () => {
  await importUi('main.js');
  await flush();
  assert.equal(window.loadPerformancePage, loadPerformancePage);
  await loadPerformancePage(7);
  const btns = [...perf().querySelectorAll('.perf-win-btn')];
  assert.deepEqual(btns.map((b) => b.getAttribute('onclick')),
    ['loadPerformancePage(7)', 'loadPerformancePage(14)', 'loadPerformancePage(30)']);
  // Emulate the inline handler's scope (jsdom doesn't run on* attributes without runScripts).
  fetchMock.reset();
  S.stats = { ...fullStats(), trend_days: 14 };
  await new Function('window', `with (window) { return ${btns[1].getAttribute('onclick')}; }`)(window);
  assert.equal(daysParam(fetchMock.callsTo('/api/stats')[0]), '14');
  assert.match(perf().querySelector('.perf-section-title span').textContent, /^Last 14 Days/);
  assert.equal(perf().querySelector('.perf-win-btn.active').textContent, '14d');
  await loadPerformancePage(7);
});

test('trend title falls back to the selected window when the API omits trend_days', async () => {
  const s = fullStats();
  delete s.trend_days;
  S.stats = s;
  await loadPerformancePage(14);
  assert.match(perf().querySelector('.perf-section-title span').textContent, /^Last 14 Days/);
  await loadPerformancePage(7);
});

test('HTTP errors and network errors render an (escaped) error line', async () => {
  S.status = 500;
  await loadPerformancePage();
  assert.equal(perf().textContent, 'Error: Failed to load stats');

  S.status = 200;
  S.fail = '<img src=x>';
  await loadPerformancePage();
  assert.equal(perf().querySelector('img'), null);
  assert.equal(perf().textContent, 'Error: <img src=x>');
});

// ── headline stat cards ───────────────────────────────────────────────────────

test('headline cards: counts, rates, duration, quality, tokens and cost', async () => {
  await loadPerformancePage();
  assert.equal(card('Total Runs').value.textContent, '40');
  assert.equal(card('Total Runs').sub, '4 active');
  assert.equal(card('Success Rate').value.textContent, '83.3%');
  assert.deepEqual(valueClasses(card('Success Rate').value), ['green']);
  assert.equal(card('Success Rate').sub, '30 succeeded');
  assert.deepEqual(valueClasses(card('Failed').value), ['red']);
  assert.equal(card('Failed').sub, 'of 40 total');
  assert.equal(card('Avg Duration').value.textContent, '12.4s');
  assert.equal(card('Avg Quality').value.textContent, '86.5/100');
  assert.deepEqual(valueClasses(card('Avg Quality').value), ['green']);
  assert.equal(card('Avg Quality').sub, 'confidence 0.91');
  assert.equal(card('Total Tokens').value.textContent, (1234567).toLocaleString());
  assert.equal(card('Total Tokens').sub, `${(1000000).toLocaleString()} in · ${(234567).toLocaleString()} out`);
  assert.equal(card('Total Cost').value.textContent, '$12.500');
});

test('headline colour thresholds and sub-cent cost formatting', async () => {
  S.stats = { ...fullStats(), success_rate: 50, avg_eval_score: 49.9, failed: 0, total_cost_usd: 0.0042 };
  await loadPerformancePage();
  assert.deepEqual(valueClasses(card('Success Rate').value), []);
  assert.deepEqual(valueClasses(card('Avg Quality').value), ['red']);
  assert.deepEqual(valueClasses(card('Failed').value), []);
  assert.equal(card('Total Cost').value.textContent, '$0.00420');

  S.stats = { ...fullStats(), success_rate: 12.5, avg_eval_score: 50 };
  await loadPerformancePage();
  assert.deepEqual(valueClasses(card('Success Rate').value), ['red']);
  assert.deepEqual(valueClasses(card('Avg Quality').value), []);
});

test('empty stats (no runs): zeros and dashes, no NaN/Infinity, optional sections omitted', async () => {
  S.stats = emptyStats(7);
  await loadPerformancePage();
  const text = perf().textContent;
  assert.doesNotMatch(text, /NaN|Infinity|undefined/);
  assert.equal(card('Total Runs').value.textContent, '0');
  assert.equal(card('Avg Duration').value.textContent, '—');
  assert.equal(card('Avg Quality').value.textContent, '—');
  assert.equal(card('Avg Quality').sub, 'eval score');
  assert.equal(card('Total Tokens').value.textContent, '0');
  assert.equal(card('Total Cost').value.textContent, '$0');
  assert.equal(card('Independent Judge').value.textContent, '—');
  assert.equal(card('Independent Judge').sub, '0 of 0 scored');
  assert.equal(card('Validation Pass').value.textContent, '—');
  assert.equal(card('p95 Duration').value.textContent, '—');
  assert.equal(card('p95 Duration').sub, 'p50 —');
  assert.equal(card('Retry Rate').value.textContent, '0%');
  assert.deepEqual(valueClasses(card('Retry Rate').value), ['green']);
  assert.deepEqual(valueClasses(card('Fallback Rate').value), ['green']);
  assert.deepEqual(valueClasses(card('Heuristic Scores').value), ['green']);
  assert.equal(trendRows().length, 7);
  for (const r of trendRows()) {
    assert.equal(r.querySelector('.trend-bar-success').style.width, '0%');
    assert.equal(r.querySelector('.trend-count').textContent, '0');
  }
  const titles = [...perf().querySelectorAll('.perf-section-title')].map((t) => t.textContent);
  assert.ok(!titles.some((t) => /By Automation Type|LLM Usage/.test(t)), 'no type table / provider cards');
});

test('an empty trend list and absent optional counters still render', async () => {
  const s = emptyStats(7);
  s.trend = [];
  delete s.eval_heuristic;
  delete s.by_model;
  delete s.by_provider;
  delete s.total_tokens_in;
  S.stats = s;
  await loadPerformancePage();
  assert.equal(trendRows().length, 0);
  assert.equal(card('Heuristic Scores').value.textContent, '0');
  assert.match(card('Total Tokens').sub, /^0 in · 0 out$/);
});

// ── eval trust section ────────────────────────────────────────────────────────

test('trust section: independent-judge, heuristic, retry, fallback, validation and latency cards', async () => {
  await loadPerformancePage();
  assert.equal(card('Independent Judge').value.textContent, '80%');
  assert.deepEqual(valueClasses(card('Independent Judge').value), ['green']);
  assert.equal(card('Independent Judge').sub, '24 of 30 scored');
  assert.equal(card('Heuristic Scores').value.textContent, '3');
  assert.deepEqual(valueClasses(card('Heuristic Scores').value), ['orange']);
  assert.equal(card('Heuristic Scores').sub, '27 LLM-judged');
  assert.equal(card('Retry Rate').value.textContent, '25%');
  assert.deepEqual(valueClasses(card('Retry Rate').value), ['red']);
  assert.equal(card('Retry Rate').sub, '10 runs · avg 1.2');
  assert.equal(card('Fallback Rate').value.textContent, '5%');
  assert.deepEqual(valueClasses(card('Fallback Rate').value), []);
  assert.equal(card('Fallback Rate').sub, '2 runs failed over');
  assert.equal(card('Validation Pass').value.textContent, '83.3%');
  assert.deepEqual(valueClasses(card('Validation Pass').value), []);
  assert.equal(card('Validation Pass').sub, '6 failed of 36');
  assert.equal(card('p95 Duration').value.textContent, '41.7s');
  assert.equal(card('p95 Duration').sub, 'p50 9.1s');
});

test('trust thresholds: low independence / high fallback / low validation are red', async () => {
  S.stats = { ...fullStats(), eval_independent_rate: 49, fallback_rate: 10.5, validation_pass_rate: 69.9, retry_rate: 20 };
  await loadPerformancePage();
  assert.deepEqual(valueClasses(card('Independent Judge').value), ['red']);
  assert.deepEqual(valueClasses(card('Fallback Rate').value), ['red']);
  assert.deepEqual(valueClasses(card('Validation Pass').value), ['red']);
  assert.deepEqual(valueClasses(card('Retry Rate').value), []);

  S.stats = { ...fullStats(), eval_independent_rate: 50, validation_pass_rate: 90 };
  await loadPerformancePage();
  assert.deepEqual(valueClasses(card('Independent Judge').value), []);
  assert.deepEqual(valueClasses(card('Validation Pass').value), ['green']);
});

// ── trend ─────────────────────────────────────────────────────────────────────

test('trend rows: bars scaled to the busiest day, per-day cost and score formatting', async () => {
  await loadPerformancePage();
  const rows = trendRows();
  assert.equal(rows.length, 4);
  const cells = (r) => ({
    label: r.querySelector('.trend-label').textContent,
    s: r.querySelector('.trend-bar-success').style.width,
    f: r.querySelector('.trend-bar-failed').style.width,
    total: r.querySelector('.trend-count').textContent,
    cost: r.querySelector('[title="cost this day"]').textContent,
    score: r.querySelector('[title="avg quality score this day"]'),
  });
  const [mon, tue, wed, thu] = rows.map(cells);
  assert.deepEqual([mon.label, mon.s, mon.f, mon.total, mon.cost, mon.score.textContent],
    ['Mon', '80%', '20%', '10', '$0.123', '91']);
  assert.equal(mon.score.style.color, 'var(--green)');
  assert.deepEqual([tue.label, tue.s, tue.f, tue.cost, tue.score.textContent],
    ['<b>Tue</b>', '10%', '40%', '$0.00500', '42'], 'label escaped; sub-cent cost to 5 dp');
  assert.equal(tue.score.style.color, 'var(--red)');
  assert.deepEqual([wed.s, wed.f, wed.cost, wed.score.textContent], ['0%', '0%', '·', '·']);
  assert.equal(wed.score.style.color, 'var(--text-muted)');
  assert.deepEqual([thu.s, thu.cost, thu.score.textContent], ['20%', '$0.020', '65']);
  assert.equal(thu.score.style.color, 'var(--text-muted)');
  assert.equal(perf().querySelector('.trend-label b'), null);
});

// ── by type ───────────────────────────────────────────────────────────────────

test('by-type table: chip, counts, success/fail bars, retries, duration; zero-total rows are safe', async () => {
  await loadPerformancePage();
  const rows = [...perf().querySelectorAll('.perf-table tbody tr')];
  assert.equal(rows.length, 2);
  const [ws, myst] = rows;
  assert.equal(ws.querySelector('.type-chip').textContent, 'WEB');
  assert.ok(ws.querySelector('.type-chip').classList.contains('chip-web'));
  assert.deepEqual([...ws.cells].slice(1, 4).map((c) => c.textContent), ['20', '15', '5']);
  assert.equal(ws.querySelector('.mini-bar-s').style.width, '75%');
  assert.equal(ws.querySelector('.mini-bar-f').style.width, '25%');
  assert.equal(ws.cells[5].textContent, '3');
  assert.equal(ws.cells[6].textContent, '8.2s');

  assert.equal(myst.querySelector('.type-chip').textContent, 'MYSTERY_TYPE', 'unknown type → uppercased chip');
  assert.ok(myst.querySelector('.type-chip').classList.contains('chip-unknown'));
  assert.equal(myst.querySelector('.mini-bar-s').style.width, '0%', 'total 0 does not divide by zero');
  assert.equal(myst.cells[5].textContent, '—');
  assert.equal(myst.cells[6].textContent, '—');
});

// ── providers / models ───────────────────────────────────────────────────────

test('provider cards: friendly label, runs, token and cost summaries', async () => {
  await loadPerformancePage();
  const a = pcard('anthropic');
  assert.equal(a.querySelector('.llm-pcard-name').textContent, 'Claude');
  assert.ok(a.querySelector('.llm-badge').classList.contains('llm-anthropic'));
  const stats = [...a.querySelectorAll('.llm-pcard-stat-val')].map((v) => v.textContent);
  assert.deepEqual(stats, ['25', '1.5k', '$1.234']);

  const g = pcard('gemini');
  assert.equal(g.querySelector('.llm-pcard-name').textContent, 'Gemini');
  assert.deepEqual([...g.querySelectorAll('.llm-pcard-stat-val')].map((v) => v.textContent), ['10', '999', '$0.00400']);

  const u = perf().querySelectorAll('.llm-pcard')[2];
  assert.equal(u.querySelector('.llm-pcard-name').textContent, '<i>unknown</i>', 'raw provider label escaped');
  assert.equal(u.querySelector('.llm-pcard-name i'), null);
  assert.deepEqual([...u.querySelectorAll('.llm-pcard-stat-val')].map((v) => v.textContent), ['5', '0', '$0']);
  assert.equal(u.querySelector('table'), null, 'no models → no model table');
});

test('model rows: short name, success %, score, judge share, retries, token bar, cost, $/success', async () => {
  await loadPerformancePage();
  const row = pcard('anthropic').querySelector('tbody tr');
  const c = [...row.cells];
  assert.equal(c[0].querySelector('.model-name-mono').textContent, 'claude-sonnet-4-6');
  assert.match(c[0].textContent, /⤳2/, 'fallback badge');
  assert.equal(c[1].textContent, '25');
  assert.equal(c[2].textContent, '84%');
  assert.ok(c[2].querySelector('.srate-pill').classList.contains('srate-high'));
  assert.equal(c[3].textContent, '88');
  assert.equal(c[3].querySelector('.srate-pill').title, 'confidence 0.88');
  assert.equal(c[4].textContent, '95%');
  assert.equal(c[4].querySelector('.srate-pill').title, '19/20 scored by LLM judge, rest heuristic');
  assert.equal(c[5].textContent, '4');
  const bar = c[6].querySelector('.tok-bar-wrap');
  assert.equal(bar.style.width, '72px', 'largest model gets the full bar');
  assert.equal(c[6].querySelector('.tok-bar-in').style.width, '80%');
  assert.equal(c[6].querySelector('.tok-bar-out').style.width, '20%');
  assert.equal(c[6].textContent.trim(), (1500).toLocaleString());
  assert.equal(c[7].textContent, '$1.2345');
  assert.equal(c[8].textContent, '$0.0588');
  assert.equal(c[9].textContent, '10.5s');
});

test('model rows: tiny costs, mid/low pills, no scored runs and zero-run models', async () => {
  await loadPerformancePage();
  const [pro, flash] = [...pcard('gemini').querySelectorAll('tbody tr')].map((r) => [...r.cells]);
  assert.equal(pro[0].querySelector('.model-name-mono').textContent, 'gemini-2.5-pro', 'provider prefix stripped');
  assert.doesNotMatch(pro[0].textContent, /⤳/);
  assert.equal(pro[2].textContent, '50%');
  assert.ok(pro[2].querySelector('.srate-pill').classList.contains('srate-mid'));
  assert.equal(pro[3].textContent, '50');
  assert.equal(pro[3].querySelector('.srate-pill').title, 'confidence —');
  assert.equal(pro[4].textContent, '25%');
  assert.ok(pro[4].querySelector('.srate-pill').classList.contains('srate-low'));
  assert.equal(pro[5].textContent, '—');
  assert.equal(pro[6].querySelector('.tok-bar-wrap').style.width, '48px', 'round(999/1500*72)');
  assert.equal(pro[7].textContent, '<$0.0001');
  assert.equal(pro[8].textContent, '<$0.0001');
  assert.equal(pro[9].textContent, '—');

  assert.equal(flash[2].textContent, '0%', 'runs 0 → 0% (no division by zero)');
  assert.ok(flash[2].querySelector('.srate-pill').classList.contains('srate-low'));
  assert.equal(flash[3].textContent, '—');
  assert.equal(flash[4].textContent, '—');
  assert.equal(flash[4].querySelector('.srate-pill').title, 'no scored runs');
  assert.equal(flash[6].querySelector('.tok-bar-wrap').style.width, '0px');
  assert.equal(flash[6].querySelector('.tok-bar-in').style.width, '0%');
  assert.equal(flash[6].querySelector('.tok-bar-out').style.width, '100%');
  assert.equal(flash[7].textContent, '$0');
  assert.equal(flash[8].textContent, '—');
});

test('all-zero token usage keeps model bars at 0px (max falls back to 1)', async () => {
  const s = fullStats();
  for (const list of Object.values(s.by_model)) for (const m of list) { m.tokens_in = 0; m.tokens_out = 0; }
  S.stats = s;
  await loadPerformancePage();
  const widths = [...perf().querySelectorAll('.tok-bar-wrap')].map((b) => b.style.width);
  assert.deepEqual(widths, ['0px', '0px', '0px']);
  assert.doesNotMatch(perf().textContent, /NaN/);
});
