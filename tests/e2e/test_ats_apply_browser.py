"""End-to-end: the Lever / Ashby / Workable adapters drive real Chromium.

Hermetic. Each platform's application form is served locally from a capture of
its REAL DOM (tests/e2e/ats_fixture/: Lever's server-rendered form, and the
hydrated Ashby / Workable forms with scripts stripped). A small injected shim
stands in for each site's JavaScript — the widgets the adapters drive (Lever's
location dropdown, Ashby's Yes/No buttons and location combobox, Workable's
combobox and cookie banner) — and posts what was filled to /submit, then shows
the platform's confirmation. Nothing leaves this machine.

Per adapter: plan from the real form definition → fill in Chromium → submit →
confirmation, asserting the exact values the form received. Plus one full
``run_ats_apply`` funnel through Lever (discover → plan → apply → ledger).

    uv run pytest tests/e2e/test_ats_apply_browser.py -v
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from src.automation.tools import ats_apply_tool as ats
from src.automation.tools.ats_ashby import AshbyAdapter
from src.automation.tools.ats_common import build_profile, load_ledger
from src.automation.tools.ats_lever import LeverAdapter, parse_form
from src.automation.tools.ats_smartrecruiters import SmartRecruitersAdapter
from src.automation.tools.ats_workable import WorkableAdapter

pytestmark = pytest.mark.e2e

HERE = Path(__file__).parent
FIXTURE = HERE / "ats_fixture"
API_FIX = HERE.parent / "fixtures" / "ats"


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            pw.chromium.launch(headless=True).close()
        return True
    except Exception:  # noqa: BLE001
        return False


if not _chromium_available():
    pytest.skip("Playwright Chromium not installed", allow_module_level=True)


SHIM = r"""
<script>
(function () {
  const P = window.__PLATFORM__, OPTIONS = window.__OPTIONS__ || {};
  document.addEventListener('submit', (e) => e.preventDefault(), true);

  function collect() {
    const out = {};
    document.querySelectorAll('input, textarea, select').forEach((e) => {
      const k = e.getAttribute('name') || e.dataset.ui || e.id;
      if (!k) return;
      if (e.type === 'file') { if (e.files && e.files.length) out[k] = e.files[0].name; return; }
      if (e.type === 'checkbox' || e.type === 'radio') { if (e.checked) (out[k] = out[k] || []).push(e.value); return; }
      if (e.value) out[k] = e.value;
    });
    document.querySelectorAll('[data-field-path]').forEach((c) => {
      const path = c.getAttribute('data-field-path');
      const b = c.querySelector('button[aria-pressed="true"]');
      if (b) out[path] = b.getAttribute('data-option');
      const i = c.querySelector('input:not([name]):not([type=file])');
      if (i && i.value) out[path] = i.value;
    });
    out.__cookieDeclined = !!window.__cookieDeclined;
    return out;
  }
  async function submit(e) {
    if (e) { e.preventDefault(); e.stopPropagation(); }
    await fetch('/submit', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                             body: JSON.stringify({ platform: P, data: collect() }) });
    if (P === 'lever') { location.href = '/lever/thanks'; return; }
    document.body.innerHTML = '<h1>' + (P === 'ashby' ? 'Thank you for applying' : 'Application submitted') + '</h1>';
  }
  function menu(after, items, onPick, attrs) {
    document.querySelectorAll('.shim-menu').forEach((m) => m.remove());
    const m = document.createElement('div');
    m.className = 'shim-menu';
    Object.entries(attrs || {}).forEach(([k, v]) => m.setAttribute(k, v));
    items.forEach((t, i) => {
      const d = document.createElement('div');
      d.setAttribute('role', 'option'); d.textContent = t; d.id = (attrs && attrs.id ? attrs.id : 'opt') + '-' + i;
      d.addEventListener('click', () => { onPick(t); m.remove(); });
      m.appendChild(d);
    });
    after.after(m);
    return m;
  }
  const CITIES = ['San Francisco, California, United States', 'San Francisco Bay Area, California, United States'];

  if (P === 'lever') {
    const box = document.querySelector('#location-input');
    const results = document.querySelector('.dropdown-results');
    box.addEventListener('input', () => setTimeout(() => {
      results.innerHTML = '';
      ['San Francisco, CA, USA', 'San Francisco, Petén, GTM'].forEach((t, i) => {
        const d = document.createElement('div');
        d.className = 'dropdown-location'; d.id = 'location-' + i; d.textContent = t;
        d.addEventListener('click', () => {
          box.value = t; results.innerHTML = '';
          document.querySelector('#selected-location').value = JSON.stringify({ name: t });
        });
        results.appendChild(d);
      });
    }, 100));
    document.querySelector('#btn-submit').addEventListener('click', submit);
  }

  if (P === 'ashby') {
    document.addEventListener('click', (e) => {
      const b = e.target.closest('button[data-option]');
      if (b) {
        e.preventDefault();
        b.parentElement.querySelectorAll('button[data-option]').forEach((x) => x.setAttribute('aria-pressed', 'false'));
        b.setAttribute('aria-pressed', 'true');
      }
      if (e.target.closest('button') && /Submit Application/i.test(e.target.closest('button').innerText)) submit(e);
    }, true);
    const loc = document.querySelector("[data-field-path='_systemfield_location'] input");
    loc.addEventListener('input', () => setTimeout(() => menu(loc, CITIES, (t) => { loc.value = t; }), 100));
  }

  if (P === 'workable') {
    // The captured page carries Workable's real cookie dialog + backdrop.
    document.querySelectorAll("[data-ui^='cookie-consent-']").forEach((btn) => {
      btn.addEventListener('click', (e) => {
        e.preventDefault();
        window.__cookieDeclined = /decline/.test(btn.getAttribute('data-ui'));
        document.querySelectorAll("[data-ui='backdrop'], [data-ui='cookie-consent']").forEach((x) => x.remove());
      });
    });
    Object.entries(OPTIONS).forEach(([id, opts]) => {
      const combo = document.getElementById('input_' + id + '_input');
      if (!combo) return;
      combo.setAttribute('readonly', 'readonly');
      combo.addEventListener('click', () => {
        if (document.querySelector('[data-ui=backdrop]')) return;   // the banner swallows clicks
        menu(combo, opts, (t) => {
          combo.value = t;
          const hidden = document.querySelector("input[name='" + id + "']");
          if (hidden) hidden.value = t;
        }, { id: 'input_' + id + '_listbox', role: 'listbox' });
      });
    });
    // The phone widget's hidden country list also uses role=option.
    const decoy = document.createElement('ul');
    decoy.innerHTML = '<li role="option">Afghanistan</li><li role="option">Albania</li>';
    document.body.appendChild(decoy);
    document.querySelector("button[data-ui='apply-button']").addEventListener('click', submit);
  }
})();
</script>
"""


class _Site:
    def __init__(self, workable_options: dict):
        self.submissions: list[dict] = []
        site = self
        pages = {
            "/lever/apply": ("lever", FIXTURE / "lever_form.html"),
            "/ashby/application": ("ashby", FIXTURE / "ashby_form.html"),
            "/workable/apply/": ("workable", FIXTURE / "workable_form.html"),
        }

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def _send(self, body: str, ctype="text/html"):
                data = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", f"{ctype}; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                path = self.path.split("?")[0]
                if path in pages:
                    platform, file = pages[path]
                    head = (f"<script>window.__PLATFORM__ = {json.dumps(platform)};"
                            f"window.__OPTIONS__ = {json.dumps(workable_options)};</script>")
                    html = file.read_text(encoding="utf-8")
                    self._send(html.replace("</body>", head + SHIM + "</body>", 1))
                elif path == "/lever/thanks":
                    self._send("<h2>Application submitted!</h2>")
                else:
                    self.send_error(404)

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                site.submissions.append(json.loads(self.rfile.read(n) or b"{}"))
                self._send("{}", "application/json")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


class _Resp:
    def __init__(self, data=None, text=""):
        self._data, self.text, self.status_code = data, text, 200

    def json(self):
        return self._data

    def raise_for_status(self):
        pass


class _Client:
    def __init__(self, routes):
        self.routes = routes

    def get(self, url, params=None):
        return next((v for k, v in self.routes.items() if k in url), _Resp({}))

    post = get

    def close(self):
        pass


def _api(name):
    return json.loads((API_FIX / name).read_text(encoding="utf-8"))


WORKABLE_FORM = _api("workable_form.json")
WORKABLE_OPTIONS = {f["id"]: [o["value"].strip() for o in f.get("options", [])]
                    for sec in WORKABLE_FORM for f in sec["fields"] if f.get("type") == "dropdown"}


@pytest.fixture(scope="module")
def site():
    s = _Site(WORKABLE_OPTIONS)
    yield s
    s.close()


@pytest.fixture
def page():
    # Per test: a live sync_playwright would stop run_ats_apply starting its own.
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        yield browser.new_context(locale="en-US").new_page()
        browser.close()


@pytest.fixture(scope="module")
def resume(tmp_path_factory):
    p = tmp_path_factory.mktemp("cv") / "ada_cv.pdf"
    p.write_bytes(b"%PDF-1.4 fixture")
    return str(p)


def _profile(resume):
    return build_profile(
        first_name="Ada", last_name="Lovelace", email="ada@example.com", phone="+1 415 555 0100",
        current_location="San Francisco, CA", authorized_countries="United States",
        linkedin_url="https://linkedin.com/in/ada", github_url="https://github.com/ada",
        notice_period="2 weeks", languages="English, Chinese", school="Stanford University",
        resume_path=resume, extra_answers="office three days: Yes")


def _essay(label, kind, options):
    return "" if options else "Because I love the product."


def _last(site, platform):
    return [s["data"] for s in site.submissions if s["platform"] == platform][-1]


# ── per-adapter: plan → fill → submit → confirmation ─────────────────────────

def test_lever_fills_real_form_and_confirms(site, page, resume):
    adapter = LeverAdapter()
    job = {"platform": "lever", "token": "palantir", "job_id": "6ed76ce8", "title": "Admin BP",
           "company": "palantir", "location": "Singapore, Singapore", "url": "",
           "apply_url": f"{site.base}/lever/apply"}
    form = parse_form((FIXTURE / "lever_form.html").read_text(encoding="utf-8"))
    answers, unanswered = adapter.plan(form, job, _profile(resume), _essay)
    assert unanswered == []
    entry = adapter.apply(page, job, answers, dry_run=False, log=print)
    assert entry["status"] == "submitted", entry.get("reason")

    sent = _last(site, "lever")
    assert sent["resume"] == "ada_cv.pdf"
    assert (sent["name"], sent["email"], sent["phone"]) == ("Ada Lovelace", "ada@example.com", "+1 415 555 0100")
    assert sent["location"] == "San Francisco, CA, USA"                 # picked from the dropdown
    assert json.loads(sent["selectedLocation"])["name"] == "San Francisco, CA, USA"
    assert sent["urls[LinkedIn]"] == "https://linkedin.com/in/ada"
    radios = {k: v for k, v in sent.items() if k.startswith("cards[") and isinstance(v, list)}
    assert ["English (ENG)", "Mandarin (MAN)"] in radios.values()       # checkbox group, never Cantonese
    # Singapore job, US-only authorization — each answer under its own question:
    work_auth = "cards[1c719ca9-5069-4afe-9e82-39ca420e0edb]"
    assert sent[f"{work_auth}[field0]"] == ["No"]                       # authorized to work there?
    assert sent[f"{work_auth}[field1]"] == ["Yes"]                      # will you need sponsorship?
    assert ["Yes, I consent"] in radios.values()
    assert "Stanford University" in sent.values()                       # <select> by label


def test_ashby_fills_real_form_and_confirms(site, page, resume):
    adapter = AshbyAdapter()

    class C:
        def post(self, url, json=None):
            return _Resp(_api("ashby_form.json"))

    job = {"platform": "ashby", "token": "openai", "job_id": "8fb1615c", "title": "TPM",
           "company": "openai", "location": "San Francisco", "url": "",
           "apply_url": f"{site.base}/ashby/application"}
    answers, unanswered = adapter.plan(adapter.load_form(C(), job), job, _profile(resume), _essay)
    assert unanswered == []
    entry = adapter.apply(page, job, answers, dry_run=False, log=print)
    assert entry["status"] == "submitted", entry.get("reason")

    sent = _last(site, "ashby")
    assert sent["_systemfield_name"] == "Ada Lovelace"
    assert sent["_systemfield_email"] == "ada@example.com"
    assert sent["_systemfield_resume"] == "ada_cv.pdf"
    assert sent["_systemfield_location"] == "San Francisco, California, United States"
    assert sent["bed95633-1b6e-4cd0-9eaf-c5a9f75ac35d"] == "yes"        # authorized where the job is
    assert sent["bda78810-f000-4f1c-99c5-f783e161f2f7"] == "no"         # needs sponsorship
    assert sent["d33e2a8f-3742-4e5e-b91c-b7607d3cdf63"] == "yes"        # office 3 days (extra answer)
    date = next(v for k, v in sent.items() if isinstance(v, str) and v.count("/") == 2)
    assert len(date) == 10                                              # MM/DD/YYYY from the notice period
    acks = [k for k, v in sent.items() if v == ["on"]]
    assert any(k.startswith("I acknowledge") for k in acks) and "I confirm I have read the above." in acks


def test_workable_fills_real_form_and_confirms(site, page, resume):
    adapter = WorkableAdapter()

    class C:
        def get(self, url, params=None):
            return _Resp(WORKABLE_FORM)

    job = {"platform": "workable", "token": "huggingface", "job_id": "0BD8C06DB3", "title": "Wild Card",
           "company": "Hugging Face", "location": "United States (Remote)", "url": "",
           "apply_url": f"{site.base}/workable/apply/"}
    answers, unanswered = adapter.plan(adapter.load_form(C(), job), job, _profile(resume), _essay)
    assert unanswered == []
    entry = adapter.apply(page, job, answers, dry_run=False, log=print)
    assert entry["status"] == "submitted", entry.get("reason")

    sent = _last(site, "workable")
    assert sent["__cookieDeclined"] is True                             # banner dismissed first
    assert (sent["firstname"], sent["lastname"], sent["email"]) == ("Ada", "Lovelace", "ada@example.com")
    assert sent["phone"] == "+1 415 555 0100"
    assert sent["resume"] == "ada_cv.pdf"
    assert sent["CA_47143"] == "https://github.com/ada"
    assert sent["CA_10627"] == "2 weeks"                                # scoped listbox, not the phone decoy
    assert sent["CA_10629"] == "Company careers page"
    assert sum(1 for v in sent.values() if v == "Because I love the product.") == 2


def test_dry_run_never_submits(site, page, resume):
    before = len(site.submissions)
    adapter = LeverAdapter()
    job = {"platform": "lever", "token": "palantir", "job_id": "x", "title": "t", "company": "c",
           "location": "Singapore", "url": "", "apply_url": f"{site.base}/lever/apply"}
    form = parse_form((FIXTURE / "lever_form.html").read_text(encoding="utf-8"))
    answers, _ = adapter.plan(form, job, _profile(resume), _essay)
    entry = adapter.apply(page, job, answers, dry_run=True, log=print)
    assert entry["status"] == "prepared" and entry["submitted"] is False
    assert len(site.submissions) == before


def test_missing_form_fails_only_that_job(site, page, resume):
    job = {"platform": "ashby", "token": "o", "job_id": "x", "title": "t", "company": "c",
           "location": "", "url": "", "apply_url": f"{site.base}/ashby/missing"}
    entry = AshbyAdapter().apply(page, job, [], dry_run=False, log=print)
    assert entry["status"] == "failed" and "did not load" in entry["reason"]


# ── the whole funnel through run_ats_apply ───────────────────────────────────

def test_run_ats_apply_full_funnel_through_lever(site, resume, tmp_path, monkeypatch):
    monkeypatch.setenv("ATS_HEADLESS", "1")
    postings = _api("lever_postings.json")[:1]
    postings[0]["applyUrl"] = f"{site.base}/lever/apply"
    client = _Client({
        "api.lever.co/v0/postings/palantir": _Resp(postings),
        f"{site.base}/lever/apply": _Resp(text=(FIXTURE / "lever_form.html").read_text(encoding="utf-8")),
        # One page whose totalFound matches its contents (the fake ignores offset).
        "api.smartrecruiters.com": _Resp({**_api("smartrecruiters_postings.json"),
                                          "totalFound": len(_api("smartrecruiters_postings.json")["content"])}),
    })
    ledger = str(tmp_path / "ledger.json")

    class ManualSmartRecruiters(SmartRecruitersAdapter):
        """A platform that only lists jobs (manual[]): SmartRecruiters' real
        adapter applies in a live browser, which a hermetic test must never do."""
        can_apply = False
        plans_in_browser = False

        def apply_url(self, job):  # pragma: no cover — must never be reached
            raise AssertionError("hermetic test reached the live apply page")

    kw = dict(boards="lever:palantir, smartrecruiters:Ubisoft2",
              adapters={**ats._adapters(), "smartrecruiters": ManualSmartRecruiters()}, first_name="Ada", last_name="Lovelace",
              email="ada@example.com", phone="+1 415 555 0100", current_location="San Francisco, CA",
              authorized_countries="United States", languages="English, Chinese",
              school="Stanford University", resume_path=resume, ledger_path=ledger,
              client=client, dry_run=False, answer_fn=lambda label, kind, options, job: _essay(label, kind, options),
              log=print)
    out = ats.run_ats_apply(**kw)
    assert out["submitted_count"] == 1, out
    assert out["manual_count"] == len(_api("smartrecruiters_postings.json")["content"])
    assert set(load_ledger(ledger)) == {f"lever:palantir/{postings[0]['id']}"}

    again = ats.run_ats_apply(**kw)
    assert again["submitted_count"] == 0
    assert again["skipped"][0]["reason"] == "already applied (ledger)"
