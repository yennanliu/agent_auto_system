// run-modal.js: open/close, type cards from the manifest, generic (manifest-driven)
// vs bespoke (#fields-<type>) forms, provider → model dropdown, schedule preset.
import { test, describe, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { setupDom, importUi, flush, click, setValue, jsonResponse, $ } from './harness.js';

const ADMIN = { username: 'admin', is_admin: true, allowed_automations: '*' };

// A manifest shaped like spec.manifest(): a few bespoke (custom_ui) types plus
// generic ones exercising every field type the generic renderer supports.
const MANIFEST = [
  { job_type: 'web_scraper', name: 'Web Scraper', icon: '🌐', desc: 'Scrape a page', custom_ui: false,
    name_template: 'Scrape: {url}', help_note: null, steps: [['Start', 'Starting']],
    fields: [{ name: 'url', type: 'url', label: 'URL to Scrape', required: true, default: null,
      min: null, max: null, placeholder: 'https://example.com', help: '', options: [] }] },
  { job_type: 'hacker_news_digest', name: 'HN Digest', icon: '🔶', desc: 'Top HN', custom_ui: false,
    name_template: 'HN Digest (top {limit})', help_note: null, steps: [],
    fields: [{ name: 'limit', type: 'number', label: 'Number of Stories (1–10)', required: false,
      default: 5, min: 1, max: 10, placeholder: '', help: '', options: [] }] },
  { job_type: 'custom:kitchen', name: 'Kitchen <Sink>', icon: '<i>', desc: 'All "field" types & more',
    custom_ui: false, name_template: '{title} / {mode}', help_note: 'Note <b>bold</b>', steps: [],
    fields: [
      { name: 'title', type: 'text', label: 'Title (short)', required: true, default: 'Hello "x"',
        placeholder: 'a <title>', help: 'Shown <first>', options: [] },
      { name: 'mode', type: 'select', label: 'Mode', required: false, default: null,
        options: [['fast', 'Fast'], ['slow', 'Slow & steady']] },
      { name: 'notes', type: 'textarea', label: 'Notes', required: false, default: '<pre-filled>',
        placeholder: 'notes…', options: [] },
      { name: 'loud', type: 'checkbox', label: 'Loud', required: false, default: true, options: [] },
      { name: 'quiet', type: 'checkbox', label: 'Quiet', required: false, default: false, options: [] },
      { name: 'count', type: 'number', label: 'Count', required: false, default: 3, min: 0, max: null, options: [] },
      { name: 'opt', type: 'text', label: 'Optional', required: false, default: null, options: [] },
    ] },
  { job_type: 'google_form_fill', name: 'Form Fill', icon: '📋', desc: 'Fill a form', custom_ui: true, fields: [], steps: [] },
  { job_type: 'linkedin_apply', name: 'LinkedIn', icon: '🔗', desc: 'Easy Apply', custom_ui: true, fields: [], steps: [] },
  { job_type: 'tw104_apply', name: '104', icon: '💼', desc: '104', custom_ui: true, fields: [], steps: [] },
  { job_type: 'profit_health_check', name: '利潤健檢', icon: '🧾', desc: 'csv', custom_ui: true, fields: [], steps: [] },
  { job_type: 'pipeline', name: 'Pipeline', icon: '🔗', desc: 'Chain', custom_ui: true, fields: [], steps: [] },
];

const { fetchMock, window } = setupDom({
  routes: [
    ['GET', '/api/auth/me', () => ADMIN],
    ['GET', '/api/automations/manifest', () => ({ automations: MANIFEST })],
    ['GET', '/api/runs', () => jsonResponse([], { headers: { 'X-Total-Count': '0' } })],
    ['GET', '/api/associations', () => ({ associations: [] })],
    ['GET', '/api/cover-letters', () => [{ name: 'Default', text: 'Hi' }]],
  ],
});

await importUi('main.js');
await flush(10);
const rm = await importUi('run-modal.js');
const jobs = await importUi('jobs.js');
const auth = await importUi('auth.js');

const modal = $('modal');
const cardTypes = () => [...document.querySelectorAll('#type-grid .type-card')].map((c) => c.dataset.type);
const visibleFieldsets = () => [...document.querySelectorAll('#run-form [id^="fields-"]')]
  .filter((el) => !el.classList.contains('hidden')).map((el) => el.id);
const generic = () => $('generic-fields');
const field = (name) => generic().querySelector(`[data-field="${name}"]`);
const toast = () => $('toast-banner').textContent;

beforeEach(() => {
  rm.closeModalFn();
  jobs.setRerunMode(null);
  $('toast-banner').textContent = '';
});

describe('renderTypeCards', () => {
  test('renders one card per manifest entry, in manifest order', () => {
    assert.deepEqual(cardTypes(), MANIFEST.map((a) => a.job_type));
  });

  test('escapes name, icon, desc and job_type from the manifest', () => {
    const card = document.querySelector('.type-card[data-type="custom:kitchen"]');
    assert.equal(card.querySelector('.type-card-name').textContent, 'Kitchen <Sink>');
    assert.equal(card.querySelector('.type-card-icon').textContent, '<i>');
    assert.equal(card.querySelector('.type-card-desc').textContent, 'All "field" types & more');
    assert.equal(card.querySelector('i'), null, 'icon must not be parsed as markup');
  });

  test('re-rendering replaces (does not append to) the grid', () => {
    rm.renderTypeCards();
    rm.renderTypeCards();
    assert.equal(cardTypes().length, MANIFEST.length);
  });
});

describe('openModal / closeModalFn', () => {
  test('openModal() shows the modal and selects the first visible type', () => {
    rm.openModal();
    assert.ok(!modal.classList.contains('hidden'));
    assert.equal($('job-type').value, 'web_scraper');
    assert.deepEqual([...document.querySelectorAll('.type-card.active')].map((c) => c.dataset.type), ['web_scraper']);
  });

  test('openModal(preselect) selects that type', () => {
    rm.openModal('linkedin_apply');
    assert.equal($('job-type').value, 'linkedin_apply');
    assert.deepEqual(visibleFieldsets(), ['fields-linkedin_apply']);
  });

  test('openModal hides types the user may not run and ignores a non-visible preselect', () => {
    const user = auth.CURRENT_USER;
    const saved = { ...user };
    Object.assign(user, { is_admin: false, allowed_automations: ['hacker_news_digest', 'pipeline'] });
    try {
      rm.openModal('linkedin_apply');
      const shown = [...document.querySelectorAll('#type-grid .type-card:not(.hidden)')].map((c) => c.dataset.type);
      assert.deepEqual(shown, ['hacker_news_digest', 'pipeline']);
      // Preselect not allowed → falls back to the first visible card.
      assert.equal($('job-type').value, 'hacker_news_digest');
    } finally {
      Object.keys(user).forEach((k) => delete user[k]);
      Object.assign(user, saved);
      rm.openModal(); // restore card visibility
    }
  });

  test('the header and landing buttons open the modal', () => {
    for (const id of ['new-run-btn', 'run-new-btn']) {
      rm.closeModalFn();
      click($(id));
      assert.ok(!modal.classList.contains('hidden'), `${id} should open the modal`);
    }
  });

  test('close button, cancel button and overlay click close it; clicks inside do not', () => {
    rm.openModal();
    click(document.querySelector('#modal .modal-header'));
    assert.ok(!modal.classList.contains('hidden'), 'click inside the dialog keeps it open');
    click(modal);
    assert.ok(modal.classList.contains('hidden'));
    for (const id of ['modal-close', 'cancel-btn']) {
      rm.openModal();
      click($(id));
      assert.ok(modal.classList.contains('hidden'), `${id} should close the modal`);
    }
  });

  test('opening after a cancelled re-run drops the pre-filled values and re-run mode', () => {
    rm.openModal('linkedin_apply');
    jobs.setRerunMode({ jobId: 7, jobName: 'Old job', jobType: 'linkedin_apply', extras: {} });
    $('li-keywords').value = 'stale keywords';
    $('llm-provider').value = 'gemini';
    rm.updateModelOptions();
    $('pipeline-steps-list').innerHTML = '<div class="pipeline-step-card"></div>';

    rm.openModal('linkedin_apply');
    assert.equal(jobs.rerunCtx, null);
    assert.equal($('li-keywords').value, '');
    assert.equal($('llm-provider').value, 'openai');
    assert.equal($('llm-model').options[0].value, 'gpt-4o-mini');
    assert.equal($('pipeline-steps-list').children.length, 0);
    assert.ok($('rerun-banner').classList.contains('hidden'));
    assert.equal($('modal-title').textContent, 'New Automation Run');
    assert.ok(!$('type-grid').classList.contains('hidden'));
  });
});

describe('selectJobType', () => {
  test('custom_ui type shows only its bespoke #fields-<type> and empties the generic form', () => {
    rm.selectJobType('web_scraper');
    assert.ok(generic().innerHTML.length > 0);
    rm.selectJobType('google_form_fill');
    assert.deepEqual(visibleFieldsets(), ['fields-google_form_fill']);
    assert.ok(generic().classList.contains('hidden'));
    assert.equal(generic().innerHTML, '');
  });

  test('a type missing from the manifest falls back to its bespoke form', () => {
    rm.selectJobType('email_collect'); // not in this test manifest
    assert.deepEqual(visibleFieldsets(), ['fields-email_collect']);
    assert.equal($('job-type').value, 'email_collect');
  });

  test('generic type hides all bespoke forms and renders data-field inputs', () => {
    rm.selectJobType('web_scraper');
    assert.deepEqual(visibleFieldsets(), []);
    assert.ok(!generic().classList.contains('hidden'));
    const url = field('url');
    assert.equal(url.tagName, 'INPUT');
    assert.equal(url.type, 'url');
    assert.equal(url.placeholder, 'https://example.com');
    assert.match(generic().querySelector('label').innerHTML, /URL to Scrape <span[^>]*>\*<\/span>/);
  });

  test('generic renderer handles every field type, defaults, help and help_note (escaped)', () => {
    rm.selectJobType('custom:kitchen');
    const title = field('title');
    assert.equal(title.type, 'text');
    assert.equal(title.value, 'Hello "x"');
    assert.equal(title.placeholder, 'a <title>');
    assert.equal(title.closest('.field').querySelector('div').textContent, 'Shown <first>');

    const mode = field('mode');
    assert.equal(mode.tagName, 'SELECT');
    assert.deepEqual([...mode.options].map((o) => [o.value, o.textContent]), [['fast', 'Fast'], ['slow', 'Slow & steady']]);

    const notes = field('notes');
    assert.equal(notes.tagName, 'TEXTAREA');
    assert.equal(notes.value, '<pre-filled>');

    assert.equal(field('loud').type, 'checkbox');
    assert.equal(field('loud').checked, true);
    assert.equal(field('quiet').checked, false);

    const count = field('count');
    assert.equal(count.type, 'number');
    assert.equal(count.value, '3');
    assert.equal(count.min, '0');
    assert.equal(count.getAttribute('max'), null);

    // Optional fields get no required star.
    assert.equal(mode.closest('.field').querySelector('label span'), null);
    // help_note is trusted server HTML (rendered as markup).
    assert.equal(generic().querySelector('b')?.textContent, 'bold');
  });

  test('pipeline widens the modal and seeds two steps exactly once', () => {
    rm.selectJobType('pipeline');
    assert.ok(document.querySelector('#modal .modal').classList.contains('modal-wide'));
    const types = () => [...document.querySelectorAll('.pipeline-step-card')].map((c) => c.dataset.stepType);
    assert.deepEqual(types(), ['x_scraper', 'email_sender']);
    rm.selectJobType('pipeline');
    assert.deepEqual(types(), ['x_scraper', 'email_sender'], 'reselecting must not add more steps');

    rm.selectJobType('google_form_fill');
    assert.ok(!document.querySelector('#modal .modal').classList.contains('modal-wide'));
    assert.equal($('pipeline-steps-list').innerHTML, '');
  });

  test('tw104_apply loads the saved cover letters', async () => {
    const before = fetchMock.callsTo('/api/cover-letters').length;
    rm.selectJobType('tw104_apply');
    await flush();
    assert.equal(fetchMock.callsTo('/api/cover-letters').length, before + 1);
    assert.deepEqual([...$('tw104-cover-saved').options].map((o) => o.value), ['', 'Default']);
  });

  test('clicking anywhere inside a type card selects it', () => {
    rm.openModal('web_scraper');
    click(document.querySelector('.type-card[data-type="linkedin_apply"] .type-card-name'));
    assert.equal($('job-type').value, 'linkedin_apply');
    assert.ok(document.querySelector('.type-card[data-type="linkedin_apply"]').classList.contains('active'));
    assert.ok(!document.querySelector('.type-card[data-type="web_scraper"]').classList.contains('active'));
    // A click on the grid gap (not a card) changes nothing.
    click($('type-grid'));
    assert.equal($('job-type').value, 'linkedin_apply');
  });
});

describe('collectGenericPayload', () => {
  const kitchen = MANIFEST.find((a) => a.job_type === 'custom:kitchen');
  const web = MANIFEST.find((a) => a.job_type === 'web_scraper');
  const hn = MANIFEST.find((a) => a.job_type === 'hacker_news_digest');

  test('collects typed values and fills name_template', () => {
    rm.selectJobType('custom:kitchen');
    field('title').value = '  Report  ';
    field('mode').value = 'slow';
    field('notes').value = ' n ';
    field('quiet').checked = true;
    field('count').value = '12';
    field('opt').value = 'x';
    const out = rm.collectGenericPayload(kitchen);
    assert.deepEqual(out, {
      payload: { title: 'Report', mode: 'slow', notes: 'n', loud: true, quiet: true, count: 12, opt: 'x' },
      jobName: 'Report / slow',
    });
  });

  test('required empty field → null and an error toast without the "(…)" label suffix', () => {
    rm.selectJobType('custom:kitchen');
    field('title').value = '   ';
    assert.equal(rm.collectGenericPayload(kitchen), null);
    assert.equal(toast(), 'Title is required');
  });

  test('cleared field with a default is sent as "", empty optionals are omitted, bad number → default', () => {
    rm.selectJobType('custom:kitchen');
    field('title').value = 'T';
    field('notes').value = '';   // has default → ""
    field('opt').value = '';     // no default → omitted
    field('count').value = '';   // number → falls back to default 3
    const { payload } = rm.collectGenericPayload(kitchen);
    assert.equal(payload.notes, '');
    assert.ok(!('opt' in payload));
    assert.equal(payload.count, 3);
  });

  test('a number field with no default and no value is omitted', () => {
    const entry = { name: 'N', fields: [{ name: 'n', type: 'number', label: 'N', default: null }] };
    generic().innerHTML = '<input type="number" data-field="n" value="abc">';
    assert.deepEqual(rm.collectGenericPayload(entry), { payload: {}, jobName: 'N' });
  });

  test('name_template with a missing key renders blank; no template → entry name', () => {
    rm.selectJobType('web_scraper');
    field('url').value = 'https://a.example';
    assert.deepEqual(rm.collectGenericPayload(web), { payload: { url: 'https://a.example' }, jobName: 'Scrape: https://a.example' });
    rm.selectJobType('hacker_news_digest');
    assert.deepEqual(rm.collectGenericPayload(hn), { payload: { limit: 5 }, jobName: 'HN Digest (top 5)' });
    assert.equal(rm.collectGenericPayload({ name: 'Plain', fields: [] }).jobName, 'Plain');
    assert.equal(rm.collectGenericPayload({ name: 'X', name_template: 'Run {nope}', fields: [] }).jobName, 'Run');
  });
});

describe('updateModelOptions', () => {
  test('changing the provider swaps the model list', () => {
    setValue('#llm-provider', 'anthropic');
    const values = [...$('llm-model').options].map((o) => o.value);
    assert.ok(values.length > 0 && values.every((v) => v.startsWith('claude-')), values.join());
    setValue('#llm-provider', 'gemini');
    assert.ok([...$('llm-model').options].every((o) => o.value.startsWith('gemini/')));
  });

  test('an unknown provider falls back to the OpenAI models', () => {
    const sel = $('llm-provider');
    sel.add(new window.Option('Mystery', 'mystery'));
    sel.value = 'mystery';
    rm.updateModelOptions();
    assert.equal($('llm-model').options[0].value, 'gpt-4o-mini');
    sel.remove(sel.options.length - 1);
    setValue('#llm-provider', 'openai');
  });
});

describe('schedule preset', () => {
  test('picking a preset copies it into the cron field and resets the picker', () => {
    $('job-schedule').value = '';
    setValue('#job-schedule-preset', '@daily');
    assert.equal($('job-schedule').value, '@daily');
    assert.equal($('job-schedule-preset').value, '');
  });

  test('the blank "Presets…" option leaves a typed cron untouched', () => {
    $('job-schedule').value = '5 4 * * *';
    setValue('#job-schedule-preset', '');
    assert.equal($('job-schedule').value, '5 4 * * *');
    $('job-schedule').value = '';
  });
});

test('picking profit-health CSVs renders the classification preview', () => {
  const input = $('ph-files');
  Object.defineProperty(input, 'files', { configurable: true, value: [new window.File(['x'], 'sales.csv')] });
  input.dispatchEvent(new window.Event('change', { bubbles: true }));
  assert.match($('ph-classified').textContent, /sales\.csv→ 銷售/);
  delete input.files;
});
