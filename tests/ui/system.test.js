// system.js — System catalog page (GET /api/system → agents / tools / crews /
// workflows cards, cat-tabs switching, collapsible cards + source viewers) and the
// Automations page (AUTO_CATALOG cards filtered by the user's visible types).
import { test, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { setupDom, importUi, flush, click, $ } from './harness.js';

const XSS = '<img src=x onerror="window.__xss=1">';

// Shape mirrors src/routers/system.py get_system(): source_code/config_code inlined.
const SYSTEM = {
  agents: [
    {
      id: 'web_analyst', name: 'Web Content Analyst', job_type: 'web_scraper',
      role: 'Analyst', goal: 'Summarize pages', backstory: 'Reads a lot',
      tools: ['Web Scraper', 'HN Top Stories'], crew: 'WebScraperCrew', task: 'scrape_task',
      source_file: 'src/automation/crews/web_scraper_crew/crew.py',
      source_code: 'def crew(self):\n    return Crew(agents=[], tasks=[])  # build fresh\n',
    },
    {
      id: 'evil', name: XSS, job_type: '', role: '<b>r</b>', goal: 'g', backstory: 'b',
      tools: [], crew: 'C', task: 'T',
    },
  ],
  tools: [
    {
      id: 'web_scraper', name: 'Web Scraper', class: 'WebScraperTool',
      description: 'Fetch a page',
      inputs: [{ name: 'url', type: 'str', description: 'Page URL' }],
      used_by: ['WebScraperCrew', 'PipelineCrew'],
      source_file: 'src/automation/tools/web_scraper_tool.py',
      source_code: 'class WebScraperTool:\n    name = "<script>x</script>"\n',
    },
  ],
  crews: [
    {
      id: 'web_scraper_crew', name: 'WebScraperCrew', process: 'sequential',
      agents: ['Web Content Analyst'], flow: 'WebScraperFlow', job_type: 'web_scraper',
      tasks: [
        { name: 'scrape_task', description: 'Scrape it', expected_output: 'JSON summary',
          config_file: 'src/automation/crews/web_scraper_crew/config/tasks.yaml',
          config_code: 'scrape_task:\n  description: Scrape {url}\n' },
        { name: 'no_config', description: 'Plain', expected_output: 'text' },
      ],
      source_file: 'src/automation/crews/web_scraper_crew/crew.py',
      source_code: 'x = 1\n',
    },
  ],
  workflows: [
    {
      id: 'web_scraper_flow', name: 'WebScraperFlow', job_type: 'web_scraper', crew: 'WebScraperCrew',
      steps: [
        { decorator: '@start()', name: 'validate', description: 'Check inputs' },
        { decorator: '@listen(validate)', name: 'execute_crew', description: 'Run the crew' },
        { decorator: '@listen(execute_crew)', name: 'finish', description: 'Store result' },
      ],
      state_fields: [
        { name: 'url', type: 'str', default: '' },
        { name: 'result', type: 'dict | None', default: null },
        { name: 'dry_run', type: 'bool', default: false },
      ],
    },
  ],
  empty: [],
};

const S = { status: 200, body: SYSTEM, fail: null };
const { window, document, fetchMock } = setupDom({
  routes: [
    ['GET', '/api/system', () => {
      if (S.fail) throw new Error(S.fail);
      return S.status === 200 ? S.body : S.status;
    }],
    ['GET', '/api/runs', () => []],
  ],
});

const { loadSystemPage, renderAutomationsPage } = await importUi('system.js');
const auth = await importUi('auth.js');
const { AUTO_CATALOG } = await importUi('catalog.js');

const content = () => $('system-content');
const cards = () => [...content().querySelectorAll('.sys-card')];
const tab = (cat) => document.querySelector(`#cat-tabs [data-cat="${cat}"]`);

beforeEach(() => fetchMock.reset());

// Order matters in this file: systemData is module state cached after the first
// successful load, so the failure paths run first.

test('load failure (HTTP 500) shows an error and leaves the cache empty so the next call retries', async () => {
  S.status = 500;
  const p = loadSystemPage();
  assert.match(content().textContent, /Loading system catalog…/, 'loading state shown synchronously');
  await p;
  assert.equal(content().textContent, 'Error: Failed to load system data');
  S.status = 200;
});

test('a network error message is shown HTML-escaped', async () => {
  S.fail = '<b>offline</b>';
  await loadSystemPage();
  assert.equal(content().querySelector('b'), null);
  assert.equal(content().textContent, 'Error: <b>offline</b>');
  S.fail = null;
});

test('tab clicks before the catalog loads only switch the highlighted tab', async () => {
  const before = content().innerHTML;
  click(tab('tools'));
  assert.ok(tab('tools').classList.contains('active'));
  assert.equal(content().innerHTML, before);
  assert.equal(fetchMock.calls.length, 0);
  click(tab('agents'));
});

test('first successful load fetches /api/system and renders agent cards', async () => {
  await loadSystemPage();
  assert.equal(fetchMock.callsTo('/api/system').length, 1);
  assert.equal(cards().length, 2);
  const [a] = cards();
  assert.equal(a.querySelector('.sys-card-name').textContent, 'Web Content Analyst');
  assert.equal(a.querySelector('.sys-card-badge').textContent, 'web_scraper');
  const meta = Object.fromEntries([...a.querySelectorAll('.meta-item')].map((m) =>
    [m.querySelector('.meta-label').textContent, m.querySelector('.meta-value, .meta-tags').textContent]));
  assert.deepEqual(meta, {
    Role: 'Analyst', Goal: 'Summarize pages', Backstory: 'Reads a lot',
    Tools: 'Web ScraperHN Top Stories', Crew: 'WebScraperCrew', Task: 'scrape_task',
  });
  assert.deepEqual([...a.querySelectorAll('.meta-tag')].map((t) => t.textContent), ['Web Scraper', 'HN Top Stories']);
  const src = a.querySelector('.sys-card-source');
  assert.match(src.querySelector('.source-toggle').textContent, /Source Code\s+src\/automation\/crews\/web_scraper_crew\/crew\.py/);
  assert.equal(src.querySelector('pre').textContent, SYSTEM.agents[0].source_code, 'highlighting keeps the code text intact');
  assert.ok(src.querySelector('pre span'), 'python source is syntax-highlighted');
});

test('agent fields are escaped and a card without source has no source viewer', () => {
  const evil = cards()[1];
  assert.equal(content().querySelector('img'), null);
  assert.equal(window.__xss, undefined);
  assert.equal(evil.querySelector('.sys-card-name').textContent, XSS);
  assert.equal(evil.querySelector('.meta-value').textContent, '<b>r</b>');
  assert.equal(evil.querySelector('.sys-card-badge').textContent, '', 'missing job_type → empty badge');
  assert.equal(evil.querySelector('.sys-card-source'), null);
});

test('a second loadSystemPage re-renders from cache without refetching', async () => {
  content().innerHTML = '';
  await loadSystemPage();
  assert.equal(fetchMock.callsTo('/api/system').length, 0);
  assert.equal(cards().length, 2);
});

test('clicking a card header toggles it open; the source toggle flips its body and arrow', () => {
  const [card] = cards();
  click(card.querySelector('.sys-card-header'));
  assert.ok(card.classList.contains('open'));
  click(card.querySelector('.sys-card-title'));  // clicks inside the header bubble up
  assert.equal(card.classList.contains('open'), false);

  const tog = card.querySelector('.source-toggle');
  const body = tog.nextElementSibling;
  click(tog);
  assert.ok(body.classList.contains('open'));
  assert.equal(tog.querySelector('.source-arrow').textContent, '▲');
  assert.equal(card.classList.contains('open'), false, 'source toggle does not collapse/expand the card');
  click(tog);
  assert.equal(body.classList.contains('open'), false);
  assert.equal(tog.querySelector('.source-arrow').textContent, '▼');
});

test('Tools tab: class, description, typed inputs, used-by tags and escaped source', () => {
  click(tab('tools'));
  const active = [...document.querySelectorAll('#cat-tabs .cat-tab.active')].map((t) => t.dataset.cat);
  assert.deepEqual(active, ['tools']);
  assert.equal(cards().length, 1);
  const [c] = cards();
  assert.equal(c.querySelector('.sys-card-sub').textContent, 'WebScraperTool');
  const row = c.querySelector('.auto-input-row');
  assert.equal(row.querySelector('.auto-input-name').textContent, 'url');
  assert.equal(row.querySelector('.auto-input-type').textContent, 'str');
  assert.equal(row.querySelector('.auto-input-desc').textContent, '— Page URL');
  assert.deepEqual([...c.querySelectorAll('.meta-tag')].map((t) => t.textContent), ['WebScraperCrew', 'PipelineCrew']);
  assert.equal(c.querySelector('pre script'), null, 'source code HTML is escaped');
  assert.match(c.querySelector('pre').textContent, /<script>x<\/script>/);
});

test('Crews tab: process badge, agents, tasks with expected output and per-task YAML config', () => {
  click(tab('crews'));
  const [c] = cards();
  assert.equal(c.querySelector('.sys-card-badge').textContent, 'process: sequential');
  assert.deepEqual([...c.querySelectorAll('.sys-card-meta .meta-tag')].map((t) => t.textContent), ['Web Content Analyst']);
  const steps = [...c.querySelectorAll('.flow-step')];
  assert.equal(steps.length, 2);
  assert.equal(steps[0].querySelector('.flow-step-name').textContent, 'scrape_task');
  assert.match(steps[0].textContent, /Expected: JSON summary/);
  const cfg = steps[0].querySelector('.sys-card-source');
  assert.match(cfg.querySelector('.source-toggle').textContent, /Task Config\s+src\/automation\/crews\/web_scraper_crew\/config\/tasks\.yaml/);
  assert.equal(cfg.querySelector('pre').textContent, SYSTEM.crews[0].tasks[0].config_code);
  assert.equal(steps[1].querySelector('.sys-card-source'), null, 'no config_code → no viewer');
  // Crew source + task config = two toggles, each independently wired.
  const toggles = c.querySelectorAll('.source-toggle');
  assert.equal(toggles.length, 2);
  click(toggles[0]);
  assert.ok(toggles[0].nextElementSibling.classList.contains('open'));
  assert.equal(toggles[1].nextElementSibling.classList.contains('open'), false);
});

test('Workflows tab: decorators, connectors between steps only, state-field defaults stringified', () => {
  click(tab('workflows'));
  const [c] = cards();
  const steps = [...c.querySelectorAll('.flow-step')];
  assert.deepEqual(steps.map((s) => s.querySelector('.flow-step-dec').textContent),
    ['@start()', '@listen(validate)', '@listen(execute_crew)']);
  assert.equal(c.querySelectorAll('.flow-step-connector').length, 2, 'n-1 connectors');
  assert.equal(steps[2].querySelector('.flow-step-connector'), null);
  const defaults = [...c.querySelectorAll('.auto-input-desc')].map((d) => d.textContent);
  assert.deepEqual(defaults, ['= ', '= null', '= false']);
  assert.equal(c.querySelector('.sys-card-source'), null);
});

test('an empty or missing category shows "No items found."', () => {
  // Synthetic tabs: `empty` is [] in the payload, `missing` is absent from it.
  for (const cat of ['empty', 'missing']) {
    const b = document.createElement('button');
    b.className = 'cat-tab';
    b.dataset.cat = cat;
    $('cat-tabs').appendChild(b);
    click(b);
    assert.equal(content().textContent, 'No items found.', cat);
    b.remove();
  }
  click(tab('agents'));
  assert.equal(cards().length, 2);
});

// ── Automations page ──────────────────────────────────────────────────────────

test('Automations page is empty without a signed-in user', () => {
  renderAutomationsPage();
  assert.equal($('auto-grid').children.length, 0);
});

test('Automations page shows enabled types for admins, in catalog order, with pipeline chips', () => {
  auth.onAuthenticated({ id: 1, username: 'root', is_admin: true, allowed_automations: '*',
    enabled_automations: ['hacker_news_digest', 'web_scraper'] });
  renderAutomationsPage();
  const cardsEl = [...$('auto-grid').querySelectorAll('.auto-card')];
  assert.deepEqual(cardsEl.map((c) => c.querySelector('.auto-card-type').textContent), ['web_scraper', 'hacker_news_digest']);
  const ws = cardsEl[0];
  const meta = AUTO_CATALOG.web_scraper;
  assert.equal(ws.querySelector('.auto-card-name').textContent, meta.name);
  assert.equal(ws.querySelector('.auto-card-icon').textContent, meta.icon);
  assert.equal(ws.querySelector('.auto-card-desc').textContent, meta.desc);
  assert.equal(ws.querySelectorAll('.auto-input-row').length, meta.inputs.length);
  assert.deepEqual([...ws.querySelectorAll('.auto-link-chip')].map((c) => c.textContent),
    [`Flow: ${meta.flow}`, `Crew: ${meta.crew}`, `Agent: ${meta.agent}`, ...meta.tools.map((t) => `Tool: ${t}`)]);
  assert.equal(ws.querySelector('[data-run-type]').textContent, `▶ Run ${meta.name}`);
});

test('non-admins only see types that are both enabled and on their allowlist', () => {
  auth.onAuthenticated({ id: 2, username: 'alice', is_admin: false,
    allowed_automations: ['web_scraper', 'x_scraper'],
    enabled_automations: ['web_scraper', 'hacker_news_digest'] });
  renderAutomationsPage();
  assert.deepEqual([...$('auto-grid').querySelectorAll('.auto-card-type')].map((e) => e.textContent), ['web_scraper']);
});

test('the Run button opens the run modal preselected on that automation', () => {
  auth.onAuthenticated({ id: 1, username: 'root', is_admin: true, allowed_automations: '*',
    enabled_automations: ['hacker_news_digest', 'web_scraper'] });
  renderAutomationsPage();
  $('modal').classList.add('hidden');
  click($('auto-grid').querySelector('[data-run-type="hacker_news_digest"]'));
  assert.equal($('modal').classList.contains('hidden'), false);
  assert.equal($('job-type').value, 'hacker_news_digest');
});

test('Automations page escapes catalog text', () => {
  const orig = { ...AUTO_CATALOG.web_scraper };
  Object.assign(AUTO_CATALOG.web_scraper, { name: XSS, desc: '<i>d</i>', tools: ['<u>t</u>'],
    inputs: [{ name: '<s>n</s>', type: 'str', desc: 'x' }] });
  try {
    renderAutomationsPage();
    const grid = $('auto-grid');
    assert.equal(grid.querySelector('img, i, u, s'), null);
    assert.equal(grid.querySelector('.auto-card-name').textContent, XSS);
    assert.equal(grid.querySelector('.auto-input-name').textContent, '<s>n</s>');
  } finally {
    Object.assign(AUTO_CATALOG.web_scraper, orig);
  }
});
