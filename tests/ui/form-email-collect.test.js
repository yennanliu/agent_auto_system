// forms/email-collect.js: source/query compatibility, 公會 directory picker, the
// language warning, per-source option panels and presets.
import { test, describe, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { setupDom, importUi, setValue, jsonResponse, $ } from './harness.js';

const { fetchMock, window } = setupDom({
  routes: [['GET', '/api/associations', () => ({ associations: [
    { slug: 'tca', name: '台北市電腦公會 (TCA)', url: '' },
    { slug: 'a"b', name: '<b>Evil</b>', url: '' },
  ] })]],
});
const ec = await importUi('forms/email-collect.js');

const warn = $('lead-lang-warn');
const slugBoxes = () => [...document.querySelectorAll('.lead-assoc-slug')];
function setSources({ maps = false, assoc = false, gov = false }) {
  $('lead-src-maps').checked = maps;
  $('lead-src-assoc').checked = assoc;
  $('lead-src-gov').checked = gov;
}

beforeEach(async () => {
  $('run-form').reset();
  fetchMock.reset();
  await ec.loadAssociationDirectories();
  ec.syncLeadSourceOptions();
});

describe('leadSourceAcceptsQuery', () => {
  const cases = [
    ['maps', 'marketing agency', true],
    ['maps', '', true],
    ['association', '行銷', true],
    ['association', 'marketing 行銷', true],
    ['association', 'marketing', false],
    ['association', 'F401010', false],
    ['govbiz', '科技', true],
    ['govbiz', 'F401010', true],
    ['govbiz', ' f401010 ', true],
    ['govbiz', 'F40101', false],
    ['govbiz', 'F4010100', false],
    ['govbiz', 'design', false],
    ['govbiz', '', false],
    ['govbiz', undefined, false],
  ];
  for (const [source, query, expected] of cases) {
    test(`${source} × ${JSON.stringify(query)} → ${expected}`, () => {
      assert.equal(ec.leadSourceAcceptsQuery(source, query), expected);
    });
  }
});

describe('loadAssociationDirectories', () => {
  test('renders one escaped checkbox per server directory, first one ticked', () => {
    assert.equal(fetchMock.callsTo('/api/associations').length, 1);
    const boxes = slugBoxes();
    assert.deepEqual(boxes.map((c) => [c.value, c.checked]), [['tca', true], ['a"b', false]]);
    const label = document.querySelector('label[for="lead-assoc-tca"]');
    assert.match(label.textContent, /台北市電腦公會 \(TCA\)/);
    const evil = $('lead-assoc-builtin').querySelectorAll('.lead-check')[1];
    assert.equal(evil.querySelector('label b'), null, 'directory names must be escaped');
    assert.match(evil.querySelector('label').textContent, /<b>Evil<\/b>/);
  });

  for (const [label, handler] of [
    ['a non-OK response', () => 500],
    ['an empty list', () => ({ associations: [] })],
    ['a network error', () => { throw new Error('down'); }],
    ['a body without associations', () => jsonResponse({})],
  ]) {
    test(`falls back to the built-in TCA slug on ${label}`, async () => {
      fetchMock.on('GET', '/api/associations', handler);
      try {
        await ec.loadAssociationDirectories();
        assert.deepEqual(slugBoxes().map((c) => [c.value, c.checked]), [['tca', true]]);
      } finally {
        fetchMock.on('GET', '/api/associations', () => ({ associations: [{ slug: 'tca', name: 'T', url: '' }, { slug: 'a"b', name: 'X', url: '' }] }));
      }
    });
  }

  test('does nothing when the picker is not on the page', async () => {
    const box = $('lead-assoc-builtin');
    const parent = box.parentNode;
    const next = box.nextSibling;
    box.remove();
    try {
      await ec.loadAssociationDirectories();
      assert.equal(fetchMock.callsTo('/api/associations').length, 1, 'only the beforeEach call');
    } finally {
      parent.insertBefore(box, next);
    }
  });
});

describe('selectedAssociations', () => {
  test('ticked slugs first, then one URL per non-blank line', () => {
    slugBoxes()[1].checked = true;
    $('lead-associations').value = '  https://a.example/m  \n\n https://b.example \n';
    assert.deepEqual(ec.selectedAssociations(), ['tca', 'a"b', 'https://a.example/m', 'https://b.example']);
  });

  test('empty when nothing is ticked or typed', () => {
    slugBoxes().forEach((c) => { c.checked = false; });
    assert.deepEqual(ec.selectedAssociations(), []);
  });
});

describe('syncLeadSourceOptions and the language warning', () => {
  test('each source panel is shown only while its source is on', () => {
    setSources({ maps: true });
    ec.syncLeadSourceOptions();
    assert.equal($('lead-assoc-wrap').style.display, 'none');
    assert.equal($('lead-gov-wrap').style.display, 'none');
    setSources({ assoc: true, gov: true });
    ec.syncLeadSourceOptions();
    assert.equal($('lead-assoc-wrap').style.display, '');
    assert.equal($('lead-gov-wrap').style.display, '');
  });

  test('toggling a source checkbox re-syncs the panels', () => {
    setValue('#lead-src-assoc', true);
    assert.equal($('lead-assoc-wrap').style.display, '');
    setValue('#lead-src-assoc', false);
    assert.equal($('lead-assoc-wrap').style.display, 'none');
  });

  test('no warning for Maps with an English query, or for a blank query', () => {
    setSources({ maps: true });
    $('lead-query').value = 'marketing';
    ec.syncLeadSourceOptions();
    assert.equal(warn.innerHTML, '');
    assert.ok(!warn.classList.contains('show'));
    setSources({ assoc: true });
    $('lead-query').value = '  ';
    ec.syncLeadSourceOptions();
    assert.ok(!warn.classList.contains('show'));
  });

  test('English query with 公會 on warns about that source only', () => {
    setSources({ maps: true, assoc: true });
    setValue('#lead-query', 'marketing');
    assert.ok(warn.classList.contains('show'));
    assert.match(warn.textContent, /“marketing” has no Chinese characters, so 公會 名錄 will find 0 businesses/);
    assert.doesNotMatch(warn.textContent, /F401010/);
  });

  test('both Taiwan sources dead → both named, plus the F-code hint', () => {
    setSources({ assoc: true, gov: true });
    setValue('#lead-query', 'design');
    assert.match(warn.textContent, /公會 名錄 and 經濟部 登記 will find 0/);
    assert.match(warn.textContent, /F###### 營業項目 code such as F401010\./);
  });

  test('an F-code satisfies 經濟部 but not 公會', () => {
    setSources({ assoc: true, gov: true });
    setValue('#lead-query', 'F401010');
    assert.match(warn.textContent, /so 公會 名錄 will find 0/);
    assert.doesNotMatch(warn.textContent, /經濟部 登記 will find/);
  });

  test('an English region with 經濟部 on gets its own warning', () => {
    setSources({ gov: true });
    $('lead-query').value = '行銷';
    setValue('#lead-region', 'Taipei');
    assert.ok(warn.classList.contains('show'));
    assert.match(warn.textContent, /Region “Taipei” is matched against the registered address/);
    setValue('#lead-region', '台北');
    assert.ok(!warn.classList.contains('show'));
  });

  test('the query is escaped in the warning', () => {
    setSources({ assoc: true });
    setValue('#lead-query', '<img src=x onerror=alert(1)>');
    assert.equal(warn.querySelector('img'), null);
    assert.match(warn.textContent, /“<img src=x onerror=alert\(1\)>”/);
  });
});

describe('presets', () => {
  test('a Taiwan preset fills Chinese query/region and switches to the Taiwan sources', () => {
    setValue('#lead-preset', 'tw-ecom');
    assert.equal($('lead-query').value, '電商');
    assert.equal($('lead-region').value, '台北');
    assert.equal($('lead-offer').value, 'an AI agent that automates customer support & order follow-up');
    assert.deepEqual(
      ['lead-src-maps', 'lead-src-assoc', 'lead-src-gov', 'lead-gcis-enrich', 'lead-resolve-sites'].map((id) => $(id).checked),
      [false, true, true, true, true]);
    assert.equal($('lead-assoc-wrap').style.display, '');
    assert.ok(!warn.classList.contains('show'));
  });

  test('a Taiwan preset re-ticks the first directory if all were cleared', () => {
    slugBoxes().forEach((c) => { c.checked = false; });
    setValue('#lead-preset', 'tw-tech');
    assert.deepEqual(slugBoxes().map((c) => c.checked), [true, false]);
  });

  test('a Taiwan preset keeps the user’s directory choice when one is ticked', () => {
    slugBoxes()[0].checked = false;
    slugBoxes()[1].checked = true;
    setValue('#lead-preset', 'tw-design');
    assert.deepEqual(slugBoxes().map((c) => c.checked), [false, true]);
  });

  test('the global preset goes back to Maps only', () => {
    setSources({ assoc: true, gov: true });
    setValue('#lead-preset', 'global-maps');
    assert.equal($('lead-query').value, 'marketing agency');
    assert.equal($('lead-region').value, '');
    assert.equal($('lead-offer').value, '');
    assert.deepEqual(['lead-src-maps', 'lead-src-assoc', 'lead-src-gov', 'lead-gcis-enrich'].map((id) => $(id).checked),
      [true, false, false, false]);
    assert.equal($('lead-gov-wrap').style.display, 'none');
  });

  test('an unknown preset value changes nothing', () => {
    $('lead-query').value = 'keep';
    const sel = $('lead-preset');
    sel.add(new window.Option('bogus', 'bogus'));
    setValue(sel, 'bogus');
    assert.equal($('lead-query').value, 'keep');
    sel.remove(sel.options.length - 1);
  });
});
