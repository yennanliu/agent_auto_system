// jobs.js: run-form submit → payload per job type, POST /api/jobs → /run, scheduled
// jobs, error handling, re-run prefill (RERUN_FIELD_MAP) and run-now.
import { test, describe, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import {
  setupDom, importUi, flush, setValue, jsonResponse, FakeEventSource, $,
} from './harness.js';

const ADMIN = { username: 'admin', is_admin: true, allowed_automations: '*' };
const bespoke = (job_type, name) => ({ job_type, name, icon: '•', desc: '', custom_ui: true, fields: [], steps: [] });
const MANIFEST = [
  { job_type: 'web_scraper', name: 'Web Scraper', icon: '🌐', desc: '', custom_ui: false,
    name_template: 'Scrape: {url}', steps: [],
    fields: [{ name: 'url', type: 'url', label: 'URL to Scrape', required: true, default: null, options: [] }] },
  bespoke('google_form_fill', 'Form Fill'),
  bespoke('x_scraper', 'X Scraper'),
  bespoke('email_sender', 'Email Sender'),
  bespoke('profit_health_check', '利潤健檢'),
  bespoke('tasker_apply', 'Tasker'),
  bespoke('tw104_apply', '104'),
  bespoke('linkedin_apply', 'LinkedIn Easy Apply'),
  bespoke('email_collect', 'Email Collector'),
  bespoke('pipeline', 'Pipeline'),
  bespoke('greenhouse_apply', 'Greenhouse'),
];
const CACHED_RUN = {
  id: 1, job_id: 5, job_name: 'Cached job', job_type: 'x_scraper', status: 'success',
  started_at: '2026-01-01T00:00:00Z', finished_at: '2026-01-01T00:00:05Z',
};

const { fetchMock, window } = setupDom({
  routes: [
    ['GET', '/api/auth/me', () => ADMIN],
    ['GET', '/api/automations/manifest', () => ({ automations: MANIFEST })],
    ['GET', '/api/runs', () => jsonResponse([CACHED_RUN], { headers: { 'X-Total-Count': '1' } })],
    ['GET', '/api/associations', () => ({ associations: [{ slug: 'tca', name: 'TCA' }, { slug: 'tpa', name: 'TPA' }] })],
    ['GET', '/api/cover-letters', () => []],
    ['GET', '/api/schedules', () => []],
    ['POST', '/api/jobs', () => ({ id: 42 })],
    ['POST', /^\/api\/jobs\/\d+\/run$/, () => ({ run_id: 99 })],
  ],
});

await importUi('main.js');
await flush(10);
const rm = await importUi('run-modal.js');
const jobs = await importUi('jobs.js');
const runForm = $('run-form');

const toast = () => $('toast-banner').textContent;
const jobPosts = () => fetchMock.callsTo('/api/jobs', 'POST');
const runPosts = () => fetchMock.callsTo(/^\/api\/jobs\/\d+\/run$/, 'POST');
const LLM = { llm_provider: 'openai', llm_model: 'gpt-4o-mini' };

async function submit() {
  runForm.dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
  await flush(15);
}
/** Select a type, run `fill`, submit, and return the created-job request body. */
async function submitAs(type, fill = () => {}) {
  rm.selectJobType(type);
  fill();
  await submit();
  const posts = jobPosts();
  return posts.length ? posts[posts.length - 1].body : null;
}
const val = (id, v) => { $(id).value = v; };
const chk = (id, v) => { $(id).checked = v; };

/** Mock GET /api/jobs/{id} and call rerun(id). */
async function rerunWith(id, job) {
  fetchMock.on('GET', `/api/jobs/${id}`, () => job);
  await jobs.rerun(id);
  await flush();
}

beforeEach(() => {
  jobs.setRerunMode(null);
  runForm.reset();
  $('pipeline-steps-list').innerHTML = '';
  rm.updateModelOptions();
  rm.closeModalFn();
  $('toast-banner').textContent = '';
  fetchMock.reset();
});

// ── Submit: payload per job type ──────────────────────────────────────────────

describe('submit builds the payload per job type', () => {
  test('generic (manifest) type collects data-field inputs and uses name_template', async () => {
    const body = await submitAs('web_scraper', () => {
      document.querySelector('#generic-fields [data-field="url"]').value = ' https://x.test ';
    });
    assert.deepEqual(body, {
      name: 'Scrape: https://x.test', job_type: 'web_scraper', payload: { url: 'https://x.test', ...LLM },
    });
  });

  test('generic type with an empty required field posts nothing', async () => {
    assert.equal(await submitAs('web_scraper'), null);
    assert.equal(toast(), 'URL to Scrape is required');
  });

  test('google_form_fill', async () => {
    const body = await submitAs('google_form_fill', () => {
      val('company-name', ' Acme '); val('company-size', '11-100'); val('ai-problem', ' triage ');
    });
    assert.deepEqual(body, {
      name: 'Form: Acme', job_type: 'google_form_fill',
      payload: { company_name: 'Acme', company_size: '11-100', ai_problem: 'triage', ...LLM },
    });
    assert.equal(await submitAs('google_form_fill'), body, 'no new POST without a company');
    assert.equal(toast(), 'Company name is required');
  });

  test('x_scraper strips a leading @ and defaults a blank limit to 5', async () => {
    const body = await submitAs('x_scraper', () => { val('x-username', ' @jack '); val('x-limit', ''); });
    assert.equal(body.name, 'X: @jack');
    assert.deepEqual(body.payload, { username: 'jack', limit: 5, ...LLM });
    fetchMock.reset();
    assert.equal(await submitAs('x_scraper', () => val('x-username', '@')), null);
    assert.equal(toast(), 'X username is required');
  });

  test('email_sender: cc optional, recipient count in the name, required fields', async () => {
    let body = await submitAs('email_sender', () => {
      val('email-to', 'a@x.io, b@x.io,'); val('email-subject', 'Hi'); val('email-body', 'Body');
    });
    assert.equal(body.name, 'Email: Hi → 2 recipients');
    assert.deepEqual(body.payload, { to: 'a@x.io, b@x.io,', subject: 'Hi', body: 'Body', ...LLM });

    body = await submitAs('email_sender', () => {
      val('email-to', 'a@x.io'); val('email-cc', 'c@x.io'); val('email-subject', 'S'); val('email-body', 'B');
    });
    assert.equal(body.name, 'Email: S → 1 recipient');
    assert.equal(body.payload.cc, 'c@x.io');

    for (const [fill, msg] of [
      [() => {}, 'Recipients are required'],
      [() => val('email-to', 'a@x.io'), 'Subject is required'],
      [() => { val('email-to', 'a@x.io'); val('email-subject', 'S'); }, 'Body is required'],
    ]) {
      fetchMock.reset();
      assert.equal(await submitAs('email_sender', fill), null);
      assert.equal(toast(), msg);
    }
  });

  test('tasker_apply payload and charge validation', async () => {
    const body = await submitAs('tasker_apply', () => {
      val('tasker-categories', '110,101001'); val('tasker-min', '2000'); val('tasker-max', '3000');
      val('tasker-max-cases', ''); val('tasker-template', ''); val('tasker-filter', ' python ');
    });
    assert.equal(body.name, 'Tasker 提案: 110,101001 (dry-run)');
    assert.deepEqual(body.payload, {
      category_ids: '110,101001', min_charge: 2000, max_charge: 3000, max_cases: 5, dry_run: true,
      task_filter: 'python', ...LLM,
    });

    const cases = [
      [() => val('tasker-categories', ''), '分類 ID 為必填 (e.g. 110)'],
      [() => { val('tasker-categories', '1'); val('tasker-min', ''); }, '請填寫初次估價最低與最高金額'],
      [() => { val('tasker-categories', '1'); val('tasker-min', '999'); }, '最低金額不可小於 1000 元'],
      [() => { val('tasker-categories', '1'); val('tasker-min', '5000'); val('tasker-max', '4000'); }, '最低金額不可大於最高金額'],
    ];
    for (const [fill, msg] of cases) {
      fetchMock.reset();
      assert.equal(await submitAs('tasker_apply', fill), null);
      assert.equal(toast(), msg);
    }
  });

  test('tw104_apply payload', async () => {
    const body = await submitAs('tw104_apply', () => {
      val('tw104-keyword', ' python '); val('tw104-area', '台北'); val('tw104-max', '7');
      val('tw104-max-pages', ''); chk('tw104-remote', true); chk('tw104-dry-run', false);
      val('tw104-cover', ' 您好 '); val('tw104-filter', '');
    });
    assert.equal(body.name, '104 應徵: python');
    assert.deepEqual(body.payload, {
      keyword: 'python', max_applications: 7, max_pages: 10, remote: true, part_time: false,
      dry_run: false, area: '台北', cover_letter: '您好', ...LLM,
    });
    fetchMock.reset();
    assert.equal(await submitAs('tw104_apply'), null);
    assert.equal(toast(), '職缺關鍵字為必填 (e.g. 軟體工程師)');
  });

  test('linkedin_apply sends expand_keywords from #li-expand (default off)', async () => {
    const body = await submitAs('linkedin_apply', () => val('li-keywords', 'FDE'));
    assert.equal(body.name, 'LinkedIn 應徵: FDE (dry-run)');
    assert.deepEqual(body.payload, {
      keywords: 'FDE', years_experience: 3, max_applications: 5, max_pages: 10, remote: false,
      expand_keywords: false, dry_run: true, location: 'Taiwan', nationality: 'Taiwanese',
      task_filter: 'AI, software, backend, python, cloud, infra', ...LLM,
    });
  });

  test('linkedin_apply with #li-expand ticked, explicit 0 years, blank optionals omitted', async () => {
    const body = await submitAs('linkedin_apply', () => {
      val('li-keywords', 'Forward Deployed Engineer, AI Engineer'); chk('li-expand', true);
      val('li-years', '0'); val('li-location', ''); val('li-nationality', ''); val('li-filter', '');
      val('li-phone', '0912'); chk('li-remote', true); chk('li-dry-run', false);
      val('li-max', 'x'); val('li-max-pages', '3');
    });
    assert.equal(body.name, 'LinkedIn 應徵: Forward Deployed Engineer, AI Engineer');
    assert.deepEqual(body.payload, {
      keywords: 'Forward Deployed Engineer, AI Engineer', years_experience: 0, max_applications: 5,
      max_pages: 3, remote: true, expand_keywords: true, dry_run: false, phone: '0912', ...LLM,
    });
    fetchMock.reset();
    assert.equal(await submitAs('linkedin_apply', () => val('li-years', '')), null);
    assert.equal(toast(), 'Job keywords are required (e.g. Software Engineer)');
  });

  test('linkedin_apply: blank years falls back to 3', async () => {
    const body = await submitAs('linkedin_apply', () => { val('li-keywords', 'x'); val('li-years', ''); });
    assert.equal(body.payload.years_experience, 3);
  });

  test('email_collect: Maps only, English query', async () => {
    const body = await submitAs('email_collect', () => {
      val('lead-query', ' dentist '); val('lead-region', 'Austin'); val('lead-limit', '');
      chk('lead-social', true);
    });
    assert.equal(body.name, 'Leads: dentist @ Austin');
    // The default-checked built-in slug is sent even though the 公會 source is off.
    assert.deepEqual(body.payload, {
      query: 'dentist', limit: 15, sources: ['maps'], smtp_check: true, include_social: true,
      gcis_enrich: false, associations: ['tca'], region: 'Austin', ...LLM,
    });
  });

  test('email_collect: Taiwan sources, extra association URLs, resolve_missing_websites only with govbiz', async () => {
    let body = await submitAs('email_collect', () => {
      val('lead-query', '行銷'); chk('lead-src-maps', false); chk('lead-src-assoc', true); chk('lead-src-gov', true);
      document.querySelector('.lead-assoc-slug[value="tpa"]').checked = true;
      val('lead-associations', ' https://g.example/m \n\n'); chk('lead-resolve-sites', false);
      val('lead-industry', 'B2B'); val('lead-offer', 'AI');
    });
    assert.equal(body.name, 'Leads: 行銷');
    assert.deepEqual(body.payload, {
      query: '行銷', limit: 15, sources: ['association', 'govbiz'], smtp_check: true, include_social: false,
      gcis_enrich: false, associations: ['tca', 'tpa', 'https://g.example/m'],
      resolve_missing_websites: false, industry: 'B2B', offer: 'AI', ...LLM,
    });

    body = await submitAs('email_collect', () => val('lead-query', 'x'));
    assert.ok(!('resolve_missing_websites' in body.payload));
  });

  test('email_collect: an F###### code is accepted by 經濟部 alone', async () => {
    const body = await submitAs('email_collect', () => {
      val('lead-query', 'F401010'); chk('lead-src-maps', false); chk('lead-src-gov', true);
    });
    assert.deepEqual(body.payload.sources, ['govbiz']);
  });

  test('email_collect validation', async () => {
    const cases = [
      [() => {}, 'Business query is required'],
      [() => { val('lead-query', 'x'); chk('lead-src-maps', false); }, 'Pick at least one discovery source'],
      [() => {
        val('lead-query', '行銷'); chk('lead-src-maps', false); chk('lead-src-assoc', true);
        document.querySelectorAll('.lead-assoc-slug').forEach((c) => { c.checked = false; });
      }, 'Pick at least one 公會 directory (or add one by URL)'],
    ];
    for (const [fill, msg] of cases) {
      fetchMock.reset();
      assert.equal(await submitAs('email_collect', fill), null);
      assert.equal(toast(), msg);
    }
    fetchMock.reset();
    assert.equal(await submitAs('email_collect', () => {
      val('lead-query', 'marketing'); chk('lead-src-maps', false); chk('lead-src-assoc', true); chk('lead-src-gov', true);
      document.querySelector('.lead-assoc-slug').checked = true;
    }), null);
    assert.match(toast(), /^"marketing" has no Chinese characters/);
  });

  test('pipeline posts ordered steps with {{steps.N.result}} references', async () => {
    const body = await submitAs('pipeline', () => {
      const cards = document.querySelectorAll('.pipeline-step-card');
      const f = (card, name) => card.querySelector(`[data-field="${name}"]`);
      f(cards[0], 'username').value = 'jack';
      f(cards[1], 'to').value = '{{steps.0.result.leads[].email}}';
      f(cards[1], 'subject').value = 'Digest';
      f(cards[1], 'body').value = '{{steps.0.result.summary}}';
    });
    assert.equal(body.name, 'Pipeline: X Scraper → Email Sender');
    assert.deepEqual(body.payload, {
      steps: [
        { job_type: 'x_scraper', payload: { username: 'jack', limit: 5 } },
        { job_type: 'email_sender', payload: {
          to: '{{steps.0.result.leads[].email}}', subject: 'Digest', body: '{{steps.0.result.summary}}' } },
      ],
      ...LLM,
    });
  });

  test('pipeline validation names the failing step', async () => {
    assert.equal(await submitAs('pipeline', () => {
      document.querySelector('.pipeline-step-card [data-field="username"]').value = 'jack';
    }), null);
    assert.equal(toast(), 'Step 2: Recipients are required');

    fetchMock.reset();
    $('pipeline-steps-list').innerHTML = '';  // reseed fresh, empty steps
    assert.equal(await submitAs('pipeline'), null);
    assert.equal(toast(), 'Step 1: X Username is required');

    fetchMock.reset();
    assert.equal(await submitAs('pipeline', () => { $('pipeline-steps-list').innerHTML = ''; }), null);
    assert.equal(toast(), 'Add at least one step');
  });

  test('the chosen provider/model are attached to every payload', async () => {
    const body = await submitAs('x_scraper', () => {
      val('x-username', 'a');
      setValue('#llm-provider', 'anthropic');
      val('llm-model', 'claude-opus-5-5');
    });
    assert.equal(body.payload.llm_provider, 'anthropic');
    assert.equal(body.payload.llm_model, 'claude-opus-5-5');
  });
});

// ── Trigger / schedule / errors ───────────────────────────────────────────────

describe('triggering a run', () => {
  test('POST /api/jobs, then /api/jobs/{id}/run, then stream the run', async () => {
    const before = FakeEventSource.instances.length;
    rm.openModal('x_scraper');
    val('x-username', 'jack');
    await submit();
    assert.deepEqual(fetchMock.calls.filter((c) => c.method === 'POST').map((c) => c.path),
      ['/api/jobs', '/api/jobs/42/run']);
    assert.ok(!('schedule' in jobPosts()[0].body));
    const es = FakeEventSource.instances.at(-1);
    assert.equal(FakeEventSource.instances.length, before + 1);
    assert.equal(es.url, '/api/runs/99/stream');
    assert.ok($('modal').classList.contains('hidden'));
    assert.ok(!$('progress-panel').classList.contains('hidden'));
    assert.equal($('progress-job-name').textContent, 'X: @jack');
    assert.ok(location.hash.endsWith('#activity'));
    // The form is reset back to the default type after submit.
    assert.equal($('job-type').value, 'google_form_fill');
    assert.equal($('x-username').value, '');
  });

  for (const status of [422, 429, 500]) {
    test(`POST /api/jobs → ${status} shows an error and never triggers a run`, async () => {
      fetchMock.on('POST', '/api/jobs', () => jsonResponse({ detail: 'nope' }, { status }));
      try {
        await submitAs('x_scraper', () => val('x-username', 'jack'));
        assert.equal(toast(), 'Failed to create job');
        assert.equal(runPosts().length, 0);
      } finally {
        fetchMock.on('POST', '/api/jobs', () => ({ id: 42 }));
      }
    });
  }

  test('POST /run → 429 shows "Failed to trigger run" and opens no stream', async () => {
    fetchMock.on('POST', '/api/jobs/42/run', () => jsonResponse({ detail: 'busy' }, { status: 429 }));
    const before = FakeEventSource.instances.length;
    try {
      await submitAs('x_scraper', () => val('x-username', 'jack'));
      assert.equal(toast(), 'Failed to trigger run');
      assert.equal(FakeEventSource.instances.length, before);
    } finally {
      fetchMock.on('POST', '/api/jobs/42/run', () => ({ run_id: 99 }));
    }
  });

  test('a network error is surfaced as a toast', async () => {
    fetchMock.on('POST', '/api/jobs', () => { throw new Error('offline'); });
    try {
      await submitAs('x_scraper', () => val('x-username', 'jack'));
      assert.equal(toast(), 'offline');
    } finally {
      fetchMock.on('POST', '/api/jobs', () => ({ id: 42 }));
    }
  });
});

describe('scheduled jobs', () => {
  test('a cron expression saves the job with `schedule` and does not run it', async () => {
    const body = await submitAs('x_scraper', () => { val('x-username', 'jack'); val('job-schedule', ' @daily '); });
    assert.equal(body.schedule, '@daily');
    assert.equal(body.name, 'X: @jack');
    assert.equal(runPosts().length, 0);
    assert.equal(toast(), 'Scheduled "X: @jack" (@daily)');
    assert.ok(location.hash.endsWith('#schedules'));
  });

  test('400 shows the server detail (bad cron)', async () => {
    fetchMock.on('POST', '/api/jobs', () => jsonResponse({ detail: 'Invalid cron: "nope"' }, { status: 400 }));
    try {
      await submitAs('x_scraper', () => { val('x-username', 'jack'); val('job-schedule', 'nope'); });
      assert.equal(toast(), 'Invalid cron: "nope"');
    } finally {
      fetchMock.on('POST', '/api/jobs', () => ({ id: 42 }));
    }
  });

  test('other failures show a generic message', async () => {
    fetchMock.on('POST', '/api/jobs', () => 500);
    try {
      await submitAs('x_scraper', () => { val('x-username', 'jack'); val('job-schedule', '@hourly'); });
      assert.equal(toast(), 'Failed to save scheduled job');
    } finally {
      fetchMock.on('POST', '/api/jobs', () => ({ id: 42 }));
    }
  });
});

// ── setRerunMode ──────────────────────────────────────────────────────────────

describe('setRerunMode', () => {
  test('re-run mode retitles the modal, hides the type grid and escapes the job name', () => {
    jobs.setRerunMode({ jobId: 1, jobName: '<img src=x onerror=1> & co', jobType: 'x_scraper', extras: {} });
    assert.equal(jobs.rerunCtx.jobId, 1);
    assert.equal($('modal-title').textContent, 'Re-run Automation');
    assert.equal($('run-submit-btn').textContent, 'Execute ▶');
    assert.ok($('type-grid').classList.contains('hidden'));
    const banner = $('rerun-banner');
    assert.ok(!banner.classList.contains('hidden'));
    assert.equal(banner.querySelector('img'), null);
    assert.equal(banner.querySelector('strong').textContent, '<img src=x onerror=1> & co');

    jobs.setRerunMode(null);
    assert.equal(jobs.rerunCtx, null);
    assert.equal($('modal-title').textContent, 'New Automation Run');
    assert.equal($('run-submit-btn').textContent, 'Run ▶');
    assert.ok(banner.classList.contains('hidden'));
    assert.equal(banner.innerHTML, '');
    assert.ok(!$('type-grid').classList.contains('hidden'));
  });
});

// ── Re-run prefill ────────────────────────────────────────────────────────────

describe('rerun', () => {
  test('linkedin_apply: prefills via RERUN_FIELD_MAP incl. expand_keywords → #li-expand', async () => {
    await rerunWith(11, {
      id: 11, name: 'LI <old>', job_type: 'linkedin_apply',
      payload: JSON.stringify({
        keywords: 'FDE', expand_keywords: true, years_experience: 0, max_applications: 9,
        remote: true, dry_run: false, nationality: 'Japanese', resume_path: '/r.pdf',
        llm_provider: 'anthropic', llm_model: 'claude-sonnet-5-5',
      }),
    });
    assert.ok(!$('modal').classList.contains('hidden'));
    assert.equal($('job-type').value, 'linkedin_apply');
    assert.equal($('li-keywords').value, 'FDE');
    assert.equal($('li-expand').checked, true);
    assert.equal($('li-years').value, '0');
    assert.equal($('li-max').value, '9');
    assert.equal($('li-remote').checked, true);
    assert.equal($('li-dry-run').checked, false);
    assert.equal($('li-nationality').value, 'Japanese');
    // Absent optional text keys mean "was blank": the HTML defaults are cleared.
    assert.equal($('li-location').value, '');
    assert.equal($('li-filter').value, '');
    // Number inputs absent from the payload keep their HTML default.
    assert.equal($('li-max-pages').value, '10');
    assert.equal($('llm-provider').value, 'anthropic');
    assert.equal($('llm-model').value, 'claude-sonnet-5-5');
    assert.deepEqual(jobs.rerunCtx, { jobId: 11, jobName: 'LI <old>', jobType: 'linkedin_apply', extras: { resume_path: '/r.pdf' } });
    assert.equal($('rerun-banner').querySelector('strong').textContent, 'LI <old>');

    // Submitting carries the non-form key over; form-owned keys reflect the form.
    val('li-keywords', 'AI Engineer');
    chk('li-expand', false);
    await submit();
    const body = jobPosts().at(-1).body;
    assert.equal(body.payload.resume_path, '/r.pdf');
    assert.equal(body.payload.keywords, 'AI Engineer');
    assert.equal(body.payload.expand_keywords, false);
    assert.equal(body.payload.llm_provider, 'anthropic');
    assert.equal(jobs.rerunCtx, null, 'submit leaves re-run mode');
  });

  test('linkedin_apply: expand_keywords false/absent leaves #li-expand unticked', async () => {
    chk('li-expand', true);
    await rerunWith(12, { id: 12, name: 'n', job_type: 'linkedin_apply', payload: { keywords: 'a', expand_keywords: false } });
    assert.equal($('li-expand').checked, false);
    chk('li-expand', true);
    await rerunWith(13, { id: 13, name: 'n', job_type: 'linkedin_apply', payload: { keywords: 'a' } });
    assert.equal($('li-expand').checked, false);
  });

  test('a form-owned key the user cleared stays cleared (form wins over extras)', async () => {
    await rerunWith(14, { id: 14, name: 'n', job_type: 'email_sender',
      payload: { to: 'a@x.io', cc: 'c@x.io', subject: 'S', body: 'B', attachments: ['f'] } });
    assert.equal($('email-cc').value, 'c@x.io');
    val('email-cc', '');
    await submit();
    const { payload } = jobPosts().at(-1).body;
    assert.ok(!('cc' in payload));
    assert.deepEqual(payload.attachments, ['f']);
  });

  test('a select value missing from the options is added so it survives', async () => {
    await rerunWith(15, { id: 15, name: 'n', job_type: 'google_form_fill',
      payload: { company_name: 'A', company_size: '5000+' } });
    assert.equal($('company-size').value, '5000+');
    assert.equal($('ai-problem').value, '');
  });

  test('generic type prefills data-field inputs; unknown keys become extras', async () => {
    await rerunWith(16, { id: 16, name: 'n', job_type: 'web_scraper', payload: { url: 'https://p.test', depth: 2 } });
    assert.equal(document.querySelector('#generic-fields [data-field="url"]').value, 'https://p.test');
    assert.deepEqual(jobs.rerunCtx.extras, { depth: 2 });
  });

  test('email_collect: sources and associations (known slugs ticked, others into the textarea)', async () => {
    await rerunWith(17, { id: 17, name: 'n', job_type: 'email_collect',
      payload: { query: '行銷', sources: ['association', 'govbiz'], associations: ['tpa', 'https://g.example/m'], smtp_check: false } });
    assert.equal($('lead-src-maps').checked, false);
    assert.equal($('lead-src-assoc').checked, true);
    assert.equal($('lead-src-gov').checked, true);
    const slugs = Object.fromEntries([...document.querySelectorAll('.lead-assoc-slug')].map((c) => [c.value, c.checked]));
    assert.deepEqual(slugs, { tca: false, tpa: true });
    assert.equal($('lead-associations').value, 'https://g.example/m');
    assert.equal($('lead-smtp').checked, false);
    assert.equal($('lead-assoc-wrap').style.display, '');
    assert.deepEqual(jobs.rerunCtx.extras, {});
  });

  test('email_collect: missing sources default to maps and an empty list clears the default slug', async () => {
    await rerunWith(18, { id: 18, name: 'n', job_type: 'email_collect', payload: { query: 'q' } });
    assert.equal($('lead-src-maps').checked, true);
    assert.equal($('lead-src-assoc').checked, false);
    assert.ok([...document.querySelectorAll('.lead-assoc-slug')].every((c) => !c.checked));
    await submit();
    assert.ok(!('associations' in jobPosts().at(-1).body.payload));
  });

  test('tw104_apply refreshes the cover-letter counter', async () => {
    await rerunWith(19, { id: 19, name: 'n', job_type: 'tw104_apply', payload: { keyword: 'k', cover_letter: '12345' } });
    assert.equal($('tw104-cover').value, '12345');
    assert.equal($('tw104-cover-count').textContent, '5');
  });

  test('pipeline rebuilds each step card with its values', async () => {
    await rerunWith(20, { id: 20, name: 'n', job_type: 'pipeline', payload: { steps: [
      { job_type: 'web_scraper', payload: { url: 'https://a.test' } },
      { job_type: 'email_sender', payload: { to: '{{steps.0.result.leads[].email}}', subject: 's', body: 'b' } },
    ] } });
    const cards = [...document.querySelectorAll('.pipeline-step-card')];
    assert.deepEqual(cards.map((c) => c.dataset.stepType), ['web_scraper', 'email_sender']);
    assert.equal(cards[0].querySelector('[data-field="url"]').value, 'https://a.test');
    assert.equal(cards[1].querySelector('[data-field="to"]').value, '{{steps.0.result.leads[].email}}');
    assert.deepEqual(jobs.rerunCtx.extras, {});
  });

  test('profit_health_check re-run without new files reuses the stored upload_id', async () => {
    await rerunWith(21, { id: 21, name: '利潤健檢：s.csv', job_type: 'profit_health_check', payload: { upload_id: 'u-1' } });
    assert.deepEqual(jobs.rerunCtx.extras, { upload_id: 'u-1' });
    await submit();
    assert.equal(fetchMock.callsTo('/api/uploads').length, 0);
    assert.deepEqual(jobPosts().at(-1).body, {
      name: '利潤健檢：s.csv', job_type: 'profit_health_check', payload: { upload_id: 'u-1', ...LLM },
    });
  });

  test('an unparseable payload string prefills nothing but still opens the form', async () => {
    await rerunWith(22, { id: 22, name: 'n', job_type: 'x_scraper', payload: '{not json' });
    assert.equal($('job-type').value, 'x_scraper');
    assert.equal($('x-username').value, '');
    assert.deepEqual(jobs.rerunCtx.extras, {});
  });

  test('a failed load shows an error and does not open the modal', async () => {
    fetchMock.on('GET', '/api/jobs/404', () => 404);
    await jobs.rerun(404);
    assert.equal(toast(), 'Could not load the previous run settings');
    assert.ok($('modal').classList.contains('hidden'));
    assert.equal(jobs.rerunCtx, null);
  });

  test('a type the user may not run is refused', async () => {
    await rerunWith(23, { id: 23, name: 'n', job_type: 'not_enabled', payload: {} });
    assert.equal(toast(), 'This automation is not enabled for your account');
    assert.ok($('modal').classList.contains('hidden'));
  });
});

// ── Run now ───────────────────────────────────────────────────────────────────

describe('runJobNow', () => {
  test('runs the stored job, names the progress panel from the cached run, and streams', async () => {
    fetchMock.on('POST', '/api/jobs/5/run', () => ({ run_id: 77 }));
    await jobs.runJobNow(5);
    await flush();
    assert.equal(fetchMock.callsTo('/api/jobs/5/run', 'POST').length, 1);
    assert.equal(jobPosts().length, 0, 'run-now must not create a new job');
    assert.equal($('progress-job-name').textContent, 'Cached job');
    assert.equal(FakeEventSource.instances.at(-1).url, '/api/runs/77/stream');
  });

  test('falls back to "job N" for an uncached job', async () => {
    fetchMock.on('POST', '/api/jobs/8/run', () => ({ run_id: 78 }));
    await jobs.runJobNow(8);
    assert.equal($('progress-job-name').textContent, 'job 8');
  });

  test('a failed run shows an error', async () => {
    fetchMock.on('POST', '/api/jobs/9/run', () => 429);
    const before = FakeEventSource.instances.length;
    await jobs.runJobNow(9);
    assert.equal(toast(), 'Re-run failed');
    assert.equal(FakeEventSource.instances.length, before);
  });
});

test('the submit handler prevents the native form submission', () => {
  rm.selectJobType('google_form_fill');
  const ev = new window.Event('submit', { bubbles: true, cancelable: true });
  runForm.dispatchEvent(ev);
  assert.equal(ev.defaultPrevented, true);
});

