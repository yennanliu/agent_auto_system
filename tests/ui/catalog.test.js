// catalog.js — static tables + loadManifest() overriding them from the server manifest.
// Module state is shared across the tests in this file, so they run in a fixed order:
// static checks → failed loads (no change) → successful load → re-load (merge).
import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { setupDom, importUi, jsonResponse } from './harness.js';

const { fetchMock } = setupDom();
const cat = await importUi('catalog.js');
const STATIC_TYPES = [...cat.ALL_TYPES];

let manifestHandler = () => 500;
fetchMock.on('GET', '/api/automations/manifest', (...a) => manifestHandler(...a));

describe('static tables', () => {
  test('every built-in type has TYPE_META and AUTO_CATALOG entries', () => {
    assert.ok(STATIC_TYPES.length > 0);
    assert.equal(new Set(STATIC_TYPES).size, STATIC_TYPES.length, 'no duplicate types');
    for (const t of STATIC_TYPES) {
      assert.ok(cat.TYPE_META[t]?.chip && cat.TYPE_META[t]?.cls, `TYPE_META.${t}`);
      const a = cat.AUTO_CATALOG[t];
      assert.ok(a, `AUTO_CATALOG.${t}`);
      for (const k of ['icon', 'name', 'desc', 'crew', 'flow', 'agent']) assert.equal(typeof a[k], 'string', `${t}.${k}`);
      assert.ok(Array.isArray(a.inputs) && a.inputs.length, `${t}.inputs`);
      assert.ok(Array.isArray(a.tools), `${t}.tools`);
    }
    assert.deepEqual(Object.keys(cat.TYPE_META).sort(), [...STATIC_TYPES].sort());
    assert.deepEqual(Object.keys(cat.AUTO_CATALOG).sort(), [...STATIC_TYPES].sort());
  });

  test('LLM_MODELS maps each provider to unique [id, label] pairs', () => {
    assert.deepEqual(Object.keys(cat.LLM_MODELS).sort(), ['anthropic', 'gemini', 'openai']);
    for (const [provider, models] of Object.entries(cat.LLM_MODELS)) {
      assert.ok(models.length > 0, provider);
      for (const pair of models) {
        assert.equal(pair.length, 2);
        assert.ok(pair[0] && pair[1], `${provider}: ${pair}`);
      }
      const ids = models.map(([id]) => id);
      assert.equal(new Set(ids).size, ids.length, `${provider} has duplicate ids`);
    }
    assert.ok(cat.LLM_MODELS.gemini.every(([id]) => id.startsWith('gemini/')), 'gemini ids are litellm-prefixed');
  });

  test('manifest-derived state starts empty', () => {
    assert.deepEqual(cat.FLOW_STEPS, {});
    assert.deepEqual(cat.MANIFEST, []);
    assert.deepEqual(cat.MANIFEST_BY_TYPE, {});
  });
});

describe('loadManifest — failures keep the static tables', () => {
  const unchanged = () => {
    assert.deepEqual(cat.ALL_TYPES, STATIC_TYPES);
    assert.deepEqual(cat.MANIFEST, []);
    assert.deepEqual(cat.FLOW_STEPS, {});
  };

  for (const status of [401, 429, 500]) {
    test(`HTTP ${status} → no change, no throw`, async () => {
      manifestHandler = () => jsonResponse({ detail: 'nope' }, { status });
      await cat.loadManifest();
      unchanged();
    });
  }

  test('invalid JSON body → swallowed', async () => {
    manifestHandler = () => new Response('<html>oops', { status: 200 });
    await assert.doesNotReject(cat.loadManifest());
    unchanged();
  });

  test('network error → swallowed', async () => {
    manifestHandler = () => { throw new TypeError('Failed to fetch'); };
    await assert.doesNotReject(cat.loadManifest());
    unchanged();
  });

  test('missing / empty automations list → ALL_TYPES kept', async () => {
    manifestHandler = () => ({});
    await cat.loadManifest();
    unchanged();
    manifestHandler = () => ({ automations: [] });
    await cat.loadManifest();
    unchanged();
  });

  test('requests GET /api/automations/manifest', () => {
    const calls = fetchMock.callsTo('/api/automations/manifest');
    assert.ok(calls.length >= 1);
    assert.ok(calls.every((c) => c.method === 'GET'));
  });
});

describe('loadManifest — success', () => {
  const MANIFEST_V1 = [
    { job_type: 'web_scraper', name: 'Web Scraper', steps: [['Fetch', 'Fetching'], ['Summarize', 'Summarizing']] },
    { job_type: 'custom:greeter', name: 'Greeter', steps: [['Think', 'Thinking']] },
    { job_type: 'pipeline', name: 'Pipeline', steps: [] },
    { job_type: 'email_sender', name: 'Email' },
  ];

  test('populates MANIFEST, MANIFEST_BY_TYPE, FLOW_STEPS and replaces ALL_TYPES in manifest order', async () => {
    manifestHandler = () => ({ automations: MANIFEST_V1 });
    await cat.loadManifest();
    assert.deepEqual(cat.MANIFEST, MANIFEST_V1);
    assert.deepEqual(cat.ALL_TYPES, ['web_scraper', 'custom:greeter', 'pipeline', 'email_sender']);
    assert.equal(cat.MANIFEST_BY_TYPE['custom:greeter'], cat.MANIFEST[1]);
    assert.deepEqual(Object.keys(cat.MANIFEST_BY_TYPE).sort(), ['custom:greeter', 'email_sender', 'pipeline', 'web_scraper']);
    assert.deepEqual(cat.FLOW_STEPS.web_scraper, [
      { label: 'Fetch', trigger: 'Fetching' },
      { label: 'Summarize', trigger: 'Summarizing' },
    ]);
    assert.deepEqual(cat.FLOW_STEPS['custom:greeter'], [{ label: 'Think', trigger: 'Thinking' }]);
  });

  test('entries without steps (absent or empty) get no FLOW_STEPS entry', () => {
    assert.ok(!('pipeline' in cat.FLOW_STEPS));
    assert.ok(!('email_sender' in cat.FLOW_STEPS));
  });

  test('static tables are not mutated by the manifest', () => {
    assert.ok(!('custom:greeter' in cat.AUTO_CATALOG));
    assert.ok(!('custom:greeter' in cat.TYPE_META));
  });

  test('a later failed load keeps the previously loaded manifest', async () => {
    manifestHandler = () => 500;
    await cat.loadManifest();
    assert.deepEqual(cat.ALL_TYPES, ['web_scraper', 'custom:greeter', 'pipeline', 'email_sender']);
    assert.equal(cat.MANIFEST.length, 4);
  });

  test('a re-load replaces MANIFEST/ALL_TYPES but merges into FLOW_STEPS / MANIFEST_BY_TYPE', async () => {
    manifestHandler = () => ({ automations: [
      { job_type: 'web_scraper', steps: [['Only', 'Only step']] },
    ] });
    await cat.loadManifest();
    assert.deepEqual(cat.ALL_TYPES, ['web_scraper']);
    assert.equal(cat.MANIFEST.length, 1);
    assert.deepEqual(cat.FLOW_STEPS.web_scraper, [{ label: 'Only', trigger: 'Only step' }]);
    // Entries dropped from the new manifest are not cleared (current behaviour).
    assert.ok('custom:greeter' in cat.FLOW_STEPS);
    assert.ok('custom:greeter' in cat.MANIFEST_BY_TYPE);
  });
});
