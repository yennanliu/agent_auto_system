// Bespoke run form: email_collect (lead sources, presets, language warnings).

import { escHtml } from '../helpers.js';

// ── email_collect run form ────────────────────────────────────────────────────
// The Taiwan sources (公會 名錄, 經濟部 登記) match Chinese company names, so an
// English query or region silently discovers nothing. Presets, a real directory
// picker and a live language warning exist to keep that from happening.

const LEAD_PRESETS = {
  'tw-marketing': { query: '行銷', region: '台北', tw: true,
                    offer: 'an AI agent / automation consulting proposal for small businesses' },
  'tw-tech':      { query: '科技', region: '台北', tw: true,
                    offer: 'an AI agent / automation consulting proposal for small businesses' },
  'tw-ecom':      { query: '電商', region: '台北', tw: true,
                    offer: 'an AI agent that automates customer support & order follow-up' },
  'tw-design':    { query: '設計', region: '台北', tw: true,
                    offer: 'an AI agent / automation consulting proposal for small businesses' },
  'global-maps':  { query: 'marketing agency', region: '', tw: false, offer: '' },
};

const hasChinese = (s) => /[㐀-鿿]/.test(s || '');
// 經濟部 routes an F###### 營業項目 code to the business-item dataset, so a query
// with no Chinese in it is still perfectly valid for that one source.
const isRegistryCode = (s) => /^F\d{6}$/i.test((s || '').trim());

// Whether a source can do anything with this query at all. Both Taiwan sources
// match registered Chinese company names; only 經濟部 also takes an F-code.
export function leadSourceAcceptsQuery(source, query) {
  if (source === 'maps') return true;
  if (source === 'govbiz') return hasChinese(query) || isRegistryCode(query);
  return hasChinese(query);   // association
}

function leadSelectedSources() {
  const ids = { maps: 'lead-src-maps', association: 'lead-src-assoc', govbiz: 'lead-src-gov' };
  return Object.keys(ids).filter(s => document.getElementById(ids[s]).checked);
}

// Built-in 公會 slugs come from the server (src/automation/tools/tw_association_tool.py)
// so the picker can never drift from the slugs the tool actually accepts.
export async function loadAssociationDirectories() {
  const box = document.getElementById('lead-assoc-builtin');
  if (!box) return;
  let dirs = [];
  try {
    const resp = await fetch('/api/associations');
    if (resp.ok) dirs = (await resp.json()).associations || [];
  } catch { /* fall through to the built-in default below */ }
  if (!dirs.length) dirs = [{ slug: 'tca', name: '台北市電腦商業同業公會 (TCA)', url: '' }];
  box.innerHTML = dirs.map((d, i) => `
    <div class="lead-check">
      <input type="checkbox" class="lead-assoc-slug" value="${escHtml(d.slug)}" ${i === 0 ? 'checked' : ''}
             id="lead-assoc-${escHtml(d.slug)}" />
      <label for="lead-assoc-${escHtml(d.slug)}">${escHtml(d.name)}
        <span class="lead-check-sub">slug <code>${escHtml(d.slug)}</code> — 會員e名錄, searched with your Business Query</span></label>
    </div>`).join('');
}

export function selectedAssociations() {
  const slugs = [...document.querySelectorAll('.lead-assoc-slug:checked')].map(c => c.value);
  const urls = document.getElementById('lead-associations').value
    .split('\n').map(s => s.trim()).filter(Boolean);
  return [...slugs, ...urls];
}

// The sources that are on but cannot use the current query — each of these
// contributes exactly 0 businesses to the run.
function leadDeadSources() {
  const query = document.getElementById('lead-query').value.trim();
  if (!query) return [];
  const labels = { association: '公會 名錄', govbiz: '經濟部 登記' };
  return leadSelectedSources()
    .filter(s => !leadSourceAcceptsQuery(s, query))
    .map(s => labels[s]);
}

function syncLeadLangWarning() {
  const warn = document.getElementById('lead-lang-warn');
  const query = document.getElementById('lead-query').value.trim();
  const region = document.getElementById('lead-region').value.trim();
  const dead = leadDeadSources();
  const msgs = [];
  if (dead.length) {
    msgs.push(`Business Query <b>“${escHtml(query)}”</b> has no Chinese characters, so ${dead.join(' and ')} ` +
              'will find 0 businesses — those sources search registered Chinese company names. ' +
              'Try 行銷 / 科技 / 設計' +
              (dead.includes('經濟部 登記') ? ', or an F###### 營業項目 code such as F401010.' : '.'));
  }
  if (document.getElementById('lead-src-gov').checked && region && !hasChinese(region)) {
    msgs.push(`Region <b>“${escHtml(region)}”</b> is matched against the registered address, which is written in ` +
              'Chinese — use 台北 / 台中 / 高雄 instead, or leave it blank.');
  }
  warn.innerHTML = msgs.join('<br><br>');
  warn.classList.toggle('show', msgs.length > 0);
}

// email_collect: reveal each discovery source's own options only when it's on.
export function syncLeadSourceOptions() {
  document.getElementById('lead-assoc-wrap').style.display =
    document.getElementById('lead-src-assoc').checked ? '' : 'none';
  document.getElementById('lead-gov-wrap').style.display =
    document.getElementById('lead-src-gov').checked ? '' : 'none';
  syncLeadLangWarning();
}
['lead-src-assoc', 'lead-src-gov', 'lead-src-maps'].forEach(id =>
  document.getElementById(id).addEventListener('change', syncLeadSourceOptions));
['lead-query', 'lead-region'].forEach(id =>
  document.getElementById(id).addEventListener('input', syncLeadLangWarning));

document.getElementById('lead-preset').addEventListener('change', (e) => {
  const preset = LEAD_PRESETS[e.target.value];
  if (!preset) return;
  document.getElementById('lead-query').value = preset.query;
  document.getElementById('lead-region').value = preset.region;
  document.getElementById('lead-offer').value = preset.offer;
  document.getElementById('lead-src-maps').checked = !preset.tw;
  document.getElementById('lead-src-assoc').checked = preset.tw;
  document.getElementById('lead-src-gov').checked = preset.tw;
  document.getElementById('lead-gcis-enrich').checked = preset.tw;
  document.getElementById('lead-resolve-sites').checked = true;
  // A Taiwan preset is useless without a directory to read, so make sure the
  // first built-in slug is on even if the user had cleared it.
  const slugs = [...document.querySelectorAll('.lead-assoc-slug')];
  if (preset.tw && slugs.length && !slugs.some(c => c.checked)) slugs[0].checked = true;
  syncLeadSourceOptions();
});
