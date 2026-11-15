// forms/pipeline.js: add / remove / retype / renumber steps and collect the
// {job_type, payload} list (with {{steps.N.result}} templates passed through).
import { test, describe, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { setupDom, importUi, click, setValue, $ } from './harness.js';

setupDom();
const pl = await importUi('forms/pipeline.js');

const list = () => $('pipeline-steps-list');
const cards = () => [...list().querySelectorAll('.pipeline-step-card')];
const fieldsOf = (card) => [...card.querySelectorAll('.ps-field')].map((el) => el.dataset.field);
const nums = () => cards().map((c) => c.querySelector('.pipeline-step-num').textContent);
const hints = () => cards().map((c) => c.querySelector('.pipeline-step-hint code').textContent);
const arrows = () => list().querySelectorAll('[data-arrow]').length;
const f = (card, name) => card.querySelector(`[data-field="${name}"]`);

beforeEach(() => { list().innerHTML = ''; });

describe('addPipelineStep', () => {
  test('defaults to an X Scraper step with no arrow and no prev-output tip', () => {
    pl.addPipelineStep();
    const [card] = cards();
    assert.equal(card.dataset.stepType, 'x_scraper');
    assert.equal(card.querySelector('.pipeline-step-type-sel').value, 'x_scraper');
    assert.deepEqual(fieldsOf(card), ['username', 'limit']);
    assert.equal(f(card, 'limit').value, '5');
    assert.equal(arrows(), 0);
    assert.doesNotMatch(card.querySelector('.pipeline-step-fields').textContent, /Prev output/);
    assert.deepEqual(nums(), ['Step 1']);
    assert.deepEqual(hints(), ['{{steps.0.result}}']);
  });

  test('later steps get an arrow and a tip pointing at the previous step', () => {
    pl.addPipelineStep('web_scraper');
    pl.addPipelineStep('email_sender');
    pl.addPipelineStep('hacker_news_digest');
    assert.equal(arrows(), 2);
    assert.deepEqual(nums(), ['Step 1', 'Step 2', 'Step 3']);
    assert.deepEqual(hints(), ['{{steps.0.result}}', '{{steps.1.result}}', '{{steps.2.result}}']);
    const tip = cards()[2].querySelector('.pipeline-step-fields').textContent;
    assert.match(tip, /\{\{steps\.1\.result\}\}/);
    assert.match(tip, /\{\{steps\.1\.result\.summary\}\}/);
    assert.match(tip, /\{\{steps\.1\.result\.leads\[\]\.email\}\}/);
    // Children alternate card / arrow / card / arrow / card.
    assert.deepEqual([...list().children].map((el) => (el.hasAttribute('data-arrow') ? '↓' : 'card')),
      ['card', '↓', 'card', '↓', 'card']);
  });

  test('email_sender suggests a prior-step token only when a prior step exists', () => {
    pl.addPipelineStep('email_sender');
    pl.addPipelineStep('email_sender');
    const [first, second] = cards();
    assert.equal(f(first, 'to').placeholder, 'recipient@example.com, another@example.com');
    assert.equal(f(first, 'body').placeholder, 'Email body');
    assert.match(f(second, 'to').placeholder, /\{\{steps\.0\.result\.leads\[\]\.email\}\}/);
    assert.equal(f(second, 'body').placeholder, 'Email body or {{steps.0.result.summary}}');
  });

  const FIELDS = {
    google_form_fill: ['company_name', 'company_size', 'ai_problem'],
    web_scraper: ['url'],
    hacker_news_digest: ['limit'],
    x_scraper: ['username', 'limit'],
    email_sender: ['to', 'cc', 'subject', 'body'],
    google_sheet_reader: ['url', 'limit'],
    shopee_seller_scraper: ['keyword', 'limit'],
    email_collect: ['query', 'region', 'limit'],
  };
  for (const [type, fields] of Object.entries(FIELDS)) {
    test(`${type} step renders fields ${fields.join(', ')}`, () => {
      pl.addPipelineStep(type);
      assert.deepEqual(fieldsOf(cards()[0]), fields);
      assert.equal(cards()[0].querySelector('.pipeline-step-type-sel').value, type);
    });
  }

  test('an unknown type renders no fields (and has no matching option)', () => {
    pl.addPipelineStep('mystery');
    assert.deepEqual(fieldsOf(cards()[0]), []);
    assert.equal(cards()[0].dataset.stepType, 'mystery');
  });

  test('the "+ Add Step" button appends an Email Sender step', () => {
    click('#pipeline-add-step');
    click('#pipeline-add-step');
    assert.deepEqual(cards().map((c) => c.dataset.stepType), ['email_sender', 'email_sender']);
    assert.equal(arrows(), 1);
  });
});

describe('changing a step type', () => {
  test('re-renders that step’s fields using its current position', () => {
    pl.addPipelineStep('x_scraper');
    pl.addPipelineStep('x_scraper');
    const second = cards()[1];
    setValue(second.querySelector('.pipeline-step-type-sel'), 'email_sender');
    assert.equal(second.dataset.stepType, 'email_sender');
    assert.deepEqual(fieldsOf(second), ['to', 'cc', 'subject', 'body']);
    assert.match(f(second, 'to').placeholder, /steps\.0\.result/);
    assert.deepEqual(fieldsOf(cards()[0]), ['username', 'limit'], 'other steps untouched');
  });
});

describe('removing a step', () => {
  test('removing a middle step drops it and its arrow, then renumbers', () => {
    pl.addPipelineStep('web_scraper');
    pl.addPipelineStep('x_scraper');
    pl.addPipelineStep('email_sender');
    click(cards()[1].querySelector('.pipeline-remove-step'));
    assert.deepEqual(cards().map((c) => c.dataset.stepType), ['web_scraper', 'email_sender']);
    assert.equal(arrows(), 1);
    assert.deepEqual(nums(), ['Step 1', 'Step 2']);
    assert.deepEqual(hints(), ['{{steps.0.result}}', '{{steps.1.result}}']);
  });

  test('removing the last step leaves the others intact', () => {
    pl.addPipelineStep('web_scraper');
    pl.addPipelineStep('email_sender');
    click(cards()[1].querySelector('.pipeline-remove-step'));
    assert.deepEqual(cards().map((c) => c.dataset.stepType), ['web_scraper']);
    assert.equal(arrows(), 0);
  });

  test('removing the first step removes it and renumbers the rest', {
    todo: 'pipeline.js:117 — `prev?.getAttribute(...) !== null` is true when prev is null, so prev.remove() throws and the first step can never be removed',
  }, () => {
    pl.addPipelineStep('web_scraper');
    pl.addPipelineStep('email_sender');
    click(cards()[0].querySelector('.pipeline-remove-step'));
    assert.deepEqual(cards().map((c) => c.dataset.stepType), ['email_sender']);
    assert.deepEqual(nums(), ['Step 1']);
    assert.equal(list().firstElementChild.hasAttribute('data-arrow'), false, 'no leading arrow');
  });
});

describe('collectPipelineSteps', () => {
  test('returns {job_type, payload} per card in order, with templates passed through', () => {
    pl.addPipelineStep('email_collect');
    pl.addPipelineStep('email_sender');
    const [a, b] = cards();
    f(a, 'query').value = '  行銷 ';
    f(a, 'region').value = '';
    f(b, 'to').value = '{{steps.0.result.leads[].email}}';
    f(b, 'subject').value = 'Hi';
    f(b, 'body').value = '{{steps.0.result.summary}}';
    assert.deepEqual(pl.collectPipelineSteps(), [
      { job_type: 'email_collect', payload: { query: '行銷', limit: 15 } },
      { job_type: 'email_sender', payload: {
        to: '{{steps.0.result.leads[].email}}', subject: 'Hi', body: '{{steps.0.result.summary}}' } },
    ]);
  });

  test('canonical integers become numbers; other numeric-looking text stays a string', () => {
    pl.addPipelineStep('google_sheet_reader');
    pl.addPipelineStep('google_sheet_reader');
    pl.addPipelineStep('google_sheet_reader');
    const [a, b, c] = cards();
    f(a, 'url').value = '42'; f(a, 'limit').value = ' 200 ';
    f(b, 'url').value = 'x'; f(b, 'limit').value = '007';
    f(c, 'url').value = 'x'; f(c, 'limit').value = '1e3';
    const out = pl.collectPipelineSteps().map((s) => s.payload);
    assert.deepEqual(out, [{ url: 42, limit: 200 }, { url: 'x', limit: '007' }, { url: 'x', limit: '1e3' }]);
  });

  test('select values are kept (not trimmed/coerced) and empty fields are omitted', () => {
    pl.addPipelineStep('google_form_fill');
    const [card] = cards();
    f(card, 'company_size').value = '200 up';
    assert.deepEqual(pl.collectPipelineSteps(), [{ job_type: 'google_form_fill', payload: { company_size: '200 up' } }]);
  });

  test('follows a changed step type and returns [] with no steps', () => {
    assert.deepEqual(pl.collectPipelineSteps(), []);
    pl.addPipelineStep('x_scraper');
    setValue(cards()[0].querySelector('.pipeline-step-type-sel'), 'web_scraper');
    f(cards()[0], 'url').value = 'https://a.test';
    assert.deepEqual(pl.collectPipelineSteps(), [{ job_type: 'web_scraper', payload: { url: 'https://a.test' } }]);
  });
});
