"""End-to-end: the Teamtailor adapter drives real Chromium through a locally
served copy of a REAL captured application form (Anyfin,
tests/fixtures/ats/teamtailor_form.html: classes, SVGs and scripts stripped).

A small shim stands in for Teamtailor's own JavaScript: the upload component
gets a Dropzone-style file input, and submitting posts every named field to
/submit and then shows "Thank you for applying". Nothing leaves this machine.

    uv run pytest tests/e2e/test_teamtailor_apply_browser.py -v
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from src.automation.tools import ats_teamtailor as tt
from src.automation.tools.ats_common import build_profile

pytestmark = pytest.mark.e2e

FORM = (Path(__file__).resolve().parents[1] / "fixtures" / "ats" / "teamtailor_form.html").read_text(encoding="utf-8")
FACTS = "What is your citizenship: The rest of the world\naccept this step: Yes"

SHIM = r"""
<script>
(function () {
  const up = document.querySelector("[data-controller~='forms--inputs--upload']");
  const file = document.createElement('input');
  file.type = 'file'; file.className = 'dz-hidden-input'; up.appendChild(file);
  file.addEventListener('change', () => { window.__resume = file.files[0] ? file.files[0].name : null; });
  const form = document.getElementById('job-application-form');
  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const out = {};
    for (const el of form.querySelectorAll('input, textarea')) {
      if (!el.name || el.type === 'hidden' || el.type === 'file' || el.type === 'submit') continue;
      if ((el.type === 'radio' || el.type === 'checkbox')) {
        if (el.checked) out[el.name] = el.labels && el.labels[0] ? el.labels[0].innerText.trim() : el.value;
        continue;
      }
      if (el.value) out[el.name] = el.value;
    }
    out.__resume = window.__resume || null;
    await fetch('/submit', { method: 'POST', body: JSON.stringify(out) });
    document.body.innerHTML = '<h1>Thank you for applying!</h1>';
  });
})();
</script>"""


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


@pytest.fixture(scope="module")
def site():
    submissions: list[dict] = []
    page = f"<!doctype html><html><head><meta charset=utf-8><title>Apply</title></head><body>{FORM}{SHIM}</body></html>"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a):
            pass

        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(page.encode())

        def do_POST(self):  # noqa: N802
            submissions.append(json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}"))
            self.send_response(200)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", submissions
    server.shutdown()


@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        yield b.new_context().new_page()
        b.close()


@pytest.fixture
def resume(tmp_path):
    p = tmp_path / "ada_cv.pdf"
    p.write_bytes(b"%PDF-1.4 test")
    return str(p)


def _run(page, base, resume, dry_run, extra=FACTS):
    ad = tt.TeamtailorAdapter()
    job = {"platform": "teamtailor", "token": "anyfin", "job_id": "8651426", "title": "Site Reliability Engineer",
           "company": "Anyfin", "location": "Stockholm, Sweden", "url": f"{base}/jobs/8651426-sre",
           "apply_url": f"{base}/jobs/8651426-sre"}
    profile = build_profile(first_name="Ada", last_name="Lovelace", email="ada@example.com",
                            phone="+46 70 123 4567", current_location="Stockholm, Sweden",
                            authorized_countries="Sweden", resume_path=resume, extra_answers=extra,
                            cover_letter_text="Hello Anyfin")
    answers, unanswered = ad.plan(tt.parse_form(FORM), job, profile, None)
    assert unanswered == []
    return ad.apply(page, job, answers, dry_run, print)


def test_submit_fills_the_real_form_and_counts_only_on_confirmation(site, page, resume):
    base, submissions = site
    before = len(submissions)
    entry = _run(page, base, resume, dry_run=False)
    assert entry["status"] == "submitted" and entry["submitted"] is True, entry
    assert len(submissions) == before + 1
    sent = submissions[-1]
    assert (sent["candidate[first_name]"], sent["candidate[last_name]"], sent["candidate[email]"]) == \
        ("Ada", "Lovelace", "ada@example.com")
    assert sent["candidate[phone]"] == "+46 70 123 4567" and sent["__resume"] == "ada_cv.pdf"
    assert sent["candidate[job_applications_attributes][0][cover_letter]"] == "Hello Anyfin"
    answers = {k: v for k, v in sent.items() if "answers_attributes" in k}
    assert sorted(answers.values()) == ["The rest of the world", "Yes", "Yes"]   # permit, citizenship, check
    assert sent["candidate[consent_given]"]                                     # the required box only
    assert "candidate[consent_given_future_jobs]" not in sent and "candidate[consent_given_sms]" not in sent


def test_dry_run_never_sends(site, page, resume):
    base, submissions = site
    before = len(submissions)
    entry = _run(page, base, resume, dry_run=True)
    assert entry["status"] == "prepared" and len(submissions) == before
