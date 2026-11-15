// navigation.js — navigate(), delegated [data-page] clicks, landing page, visibleTypeSet().
// Tests share one page + module state and run in order: logged-out checks first, then a
// restricted non-admin (the landing grid renders once), then admin / visibility matrix.
import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { setupDom, importUi, click, flush, $ } from './harness.js';

const { window, fetchMock } = setupDom({
  routes: [
    ['GET', '/api/runs', () => []],
    ['GET', '/api/system', () => ({ agents: [], tasks: [], tools: [], crews: [] })],
    ['GET', '/api/stats', () => ({ trend: [] })],
    ['GET', '/api/overview', () => []],
    ['GET', '/api/schedules', () => []],
    ['GET', '/api/admin/users', () => []],
  ],
});
const scrolls = [];
window.scrollTo = (opts) => scrolls.push(opts);

const nav = await importUi('navigation.js');
const auth = await importUi('auth.js');
const cat = await importUi('catalog.js');
const { navigate, visibleTypeSet, VALID_PAGES } = nav;

const login = (user) => auth.onAuthenticated({ username: 'u', ...user });
const activePages = () => [...document.querySelectorAll('.page.active')].map((p) => p.id);
const activeTabs = () => [...document.querySelectorAll('.nav-tab.active')].map((t) => t.dataset.page);
function assertOn(page) {
  assert.deepEqual(activePages(), [`page-${page}`]);
  assert.deepEqual(activeTabs(), [page]);
  assert.equal(location.hash, `#${page}`);
}

describe('logged out', () => {
  test('VALID_PAGES lists every routable page, each with a #page-* section', () => {
    assert.deepEqual(VALID_PAGES, ['landing', 'run', 'activity', 'overview', 'schedules', 'system', 'analytics', 'admin']);
    for (const p of VALID_PAGES) assert.ok($(`page-${p}`), `#page-${p}`);
  });

  test('visibleTypeSet() is empty without a user', () => {
    assert.equal(auth.CURRENT_USER, null);
    assert.equal(visibleTypeSet().size, 0);
  });
});

describe('restricted non-admin user', () => {
  test('visibleTypeSet = globally enabled ∩ personal allowlist', () => {
    login({
      is_admin: false,
      allowed_automations: ['web_scraper', 'hacker_news_digest', 'email_sender', 'not_a_type'],
      enabled_automations: cat.ALL_TYPES.filter((t) => t !== 'email_sender'),
    });
    assert.deepEqual([...visibleTypeSet()].sort(), ['hacker_news_digest', 'web_scraper']);
  });

  test('navigate("landing") activates the page and renders only visible automations', async () => {
    scrolls.length = 0;
    navigate('landing');
    assertOn('landing');
    assert.deepEqual(scrolls, [{ top: 0 }]);
    const cards = [...$('lp-autos').querySelectorAll('.lp-auto')];
    assert.deepEqual(cards.map((c) => c.dataset.type), ['web_scraper', 'hacker_news_digest']); // ALL_TYPES order
    assert.equal(cards[0].querySelector('h4').textContent, cat.AUTO_CATALOG.web_scraper.name);
    assert.equal(cards[0].querySelector('.type-chip').textContent, 'WEB');
    assert.ok(cards[0].querySelector('.type-chip').classList.contains('chip-web'));
  });

  test('the landing grid is rendered once and not rebuilt on later visits', () => {
    login({ is_admin: true });
    navigate('landing');
    assert.equal($('lp-autos').querySelectorAll('.lp-auto').length, 2);
    login({ is_admin: false, allowed_automations: ['web_scraper', 'hacker_news_digest'] });
  });

  test('clicking a landing card opens the run modal preselected on that type', () => {
    $('modal').classList.add('hidden');
    click($('lp-autos').querySelector('[data-type="hacker_news_digest"]'));
    assert.ok(!$('modal').classList.contains('hidden'));
    assert.equal($('job-type').value, 'hacker_news_digest');
    $('modal').classList.add('hidden');
  });

  test('navigate("admin") falls back to landing for non-admins', () => {
    navigate('run');
    navigate('admin');
    assertOn('landing');
    assert.equal(fetchMock.callsTo('/api/admin/users').length, 0);
  });

  const LOADERS = [
    ['system', '/api/system'],
    ['analytics', '/api/stats'],
    ['overview', '/api/overview'],
    ['schedules', '/api/schedules'],
  ];
  for (const [page, endpoint] of LOADERS) {
    test(`navigate("${page}") switches page and loads ${endpoint}`, async () => {
      const before = fetchMock.callsTo(endpoint).length;
      navigate(page);
      assertOn(page);
      await flush();
      assert.equal(fetchMock.callsTo(endpoint).length, before + 1);
    });
  }

  test('navigate("analytics") requests the default 7-day window', () => {
    const last = fetchMock.callsTo('/api/stats').at(-1);
    assert.equal(last.url.searchParams.get('days'), '7');
  });

  test('navigate("run") renders the automations grid for visible types only', () => {
    navigate('run');
    assertOn('run');
    assert.equal($('auto-grid').querySelectorAll('.auto-card').length, 2);
  });

  test('navigate("activity") switches page without page loaders', async () => {
    fetchMock.reset();
    navigate('activity');
    assertOn('activity');
    await flush();
    assert.deepEqual(fetchMock.calls.map((c) => c.path), []);
  });

  test('an unknown page deactivates every page (callers validate first)', () => {
    navigate('nope');
    assert.deepEqual(activePages(), []);
    assert.deepEqual(activeTabs(), []);
    assert.equal(location.hash, '#nope');
  });
});

describe('wiring', () => {
  test('nav-tab clicks navigate', () => {
    click('.nav-tab[data-page="schedules"]');
    assertOn('schedules');
  });

  test('any [data-page] element (footer link, child of logo) navigates and prevents default', () => {
    const link = document.querySelector('footer a[data-page="system"]') || document.querySelectorAll('a[data-page="system"]')[0];
    const ev = new window.MouseEvent('click', { bubbles: true, cancelable: true });
    link.dispatchEvent(ev);
    assert.ok(ev.defaultPrevented);
    assertOn('system');
    click(document.querySelector('.logo .logo-mark'));
    assertOn('landing');
  });

  test('clicks outside any [data-page] element do not navigate', () => {
    navigate('overview');
    const ev = new window.MouseEvent('click', { bubbles: true, cancelable: true });
    $('toast-banner').dispatchEvent(ev);
    assert.ok(!ev.defaultPrevented);
    assertOn('overview');
  });

  test('landing buttons: Launch → run, View activity → activity', () => {
    click('#lp-launch');
    assertOn('run');
    click('#lp-cta-dash');
    assertOn('activity');
  });

  test('landing "Run an automation" / "+ New Run" open the modal on the first visible type', () => {
    for (const id of ['lp-run', 'lp-cta-run']) {
      $('modal').classList.add('hidden');
      $('job-type').value = '';
      click(`#${id}`);
      assert.ok(!$('modal').classList.contains('hidden'), id);
    }
    $('modal').classList.add('hidden');
  });
});

describe('admin user', () => {
  test('navigate("admin") is allowed and loads the users tab', async () => {
    login({ is_admin: true });
    const before = fetchMock.callsTo('/api/admin/users').length;
    navigate('admin');
    assertOn('admin');
    await flush();
    assert.equal(fetchMock.callsTo('/api/admin/users').length, before + 1);
  });
});

describe('visibleTypeSet matrix', () => {
  test('admin with no enabled list sees every type', () => {
    login({ is_admin: true });
    assert.deepEqual([...visibleTypeSet()], cat.ALL_TYPES);
  });

  test('admin bypasses the allowlist but not the global enabled set', () => {
    login({ is_admin: true, allowed_automations: [], enabled_automations: ['web_scraper', 'pipeline'] });
    assert.deepEqual([...visibleTypeSet()].sort(), ['pipeline', 'web_scraper']);
  });

  test('non-admin with "*" sees every enabled type', () => {
    login({ is_admin: false, allowed_automations: '*', enabled_automations: ['x_scraper', 'pipeline'] });
    assert.deepEqual([...visibleTypeSet()].sort(), ['pipeline', 'x_scraper']);
    login({ is_admin: false, allowed_automations: '*' });
    assert.equal(visibleTypeSet().size, cat.ALL_TYPES.length);
  });

  test('non-admin with a missing or malformed allowlist sees nothing', () => {
    for (const allowed of [undefined, null, 'web_scraper', {}, []]) {
      login({ is_admin: false, allowed_automations: allowed });
      assert.equal(visibleTypeSet().size, 0, JSON.stringify(allowed));
    }
  });

  test('custom: automations from the manifest are always enabled, but still allowlist-gated', async () => {
    fetchMock.on('GET', '/api/automations/manifest', () => ({ automations: [
      { job_type: 'web_scraper' }, { job_type: 'custom:greeter' }, { job_type: 'custom:other' },
    ] }));
    await cat.loadManifest();
    login({ is_admin: true, enabled_automations: ['web_scraper'] });
    assert.deepEqual([...visibleTypeSet()], ['web_scraper', 'custom:greeter', 'custom:other']);
    login({ is_admin: false, enabled_automations: ['web_scraper'], allowed_automations: ['custom:greeter'] });
    assert.deepEqual([...visibleTypeSet()], ['custom:greeter']);
  });
});
