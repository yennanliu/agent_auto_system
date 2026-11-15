// main.js — logged-in boot: me 200 → manifest → type cards → bootApp on the hash page.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { setupDom, importUi, flush, $ } from './harness.js';

const MANIFEST = [
  { job_type: 'web_scraper', name: 'Web Scraper', icon: '🌐', desc: 'Scrape a page', steps: [['Fetch', 'Fetching']] },
  { job_type: 'custom:greeter', name: '<img src=x onerror=alert(1)>', desc: 'Say "hi" & <b>wave</b>', steps: [] },
];

const { fetchMock } = setupDom({
  url: 'http://localhost/#schedules',
  routes: [
    ['GET', '/api/auth/me', () => ({ username: 'alice', is_admin: false, allowed_automations: '*' })],
    ['GET', '/api/automations/manifest', () => ({ automations: MANIFEST })],
    ['GET', '/api/runs', () => []],
    ['GET', '/api/schedules', () => []],
    ['GET', '/api/associations', () => ({ associations: [{ slug: 'tca', name: 'TCA' }] })],
  ],
});

await importUi('main.js');
await flush(15);
const cat = await importUi('catalog.js');
const auth = await importUi('auth.js');

test('authenticates from /api/auth/me without showing the login overlay', () => {
  assert.equal(fetchMock.callsTo('/api/auth/me').length, 1);
  assert.ok(!$('login-overlay').classList.contains('show'));
  assert.equal(auth.CURRENT_USER.username, 'alice');
  assert.equal($('user-menu').hidden, false);
  assert.equal($('user-chip').textContent, 'alice');
  assert.equal($('nav-admin').hidden, true);
  assert.equal(fetchMock.callsTo('/api/auth/providers').length, 0, 'no SSO discovery when signed in');
});

test('loads the manifest after the session check', () => {
  const paths = fetchMock.calls.map((c) => c.path);
  const me = paths.indexOf('/api/auth/me');
  const manifest = paths.indexOf('/api/automations/manifest');
  assert.ok(me >= 0 && manifest > me, paths.join(', '));
  assert.deepEqual(cat.ALL_TYPES, ['web_scraper', 'custom:greeter']);
  assert.deepEqual(cat.FLOW_STEPS.web_scraper, [{ label: 'Fetch', trigger: 'Fetching' }]);
});

test('renders one type card per manifest entry, HTML-escaped', () => {
  const cards = [...$('type-grid').querySelectorAll('.type-card')];
  assert.deepEqual(cards.map((c) => c.dataset.type), ['web_scraper', 'custom:greeter']);
  assert.equal(cards[0].querySelector('.type-card-name').textContent, 'Web Scraper');
  assert.equal(cards[0].querySelector('.type-card-icon').textContent, '🌐');
  assert.equal(cards[1].querySelector('.type-card-icon').textContent, '⚙️', 'default icon');
  assert.equal(cards[1].querySelector('.type-card-name').textContent, '<img src=x onerror=alert(1)>');
  assert.equal(cards[1].querySelector('.type-card-desc').textContent, 'Say "hi" & <b>wave</b>');
  assert.equal($('type-grid').querySelectorAll('img,b').length, 0);
});

test('boots to the page in the URL hash and runs its loader', () => {
  assert.ok($('page-schedules').classList.contains('active'));
  assert.equal(document.querySelectorAll('.page.active').length, 1);
  assert.equal(location.hash, '#schedules');
  assert.equal(fetchMock.callsTo('/api/schedules').length, 1);
});

test('loads authenticated side data: run history and association directories', () => {
  assert.ok(fetchMock.callsTo('/api/runs').length >= 1);
  assert.equal(fetchMock.callsTo('/api/associations').length, 1);
  assert.ok($('lead-assoc-builtin').querySelector('input.lead-assoc-slug[value="tca"]'));
});
