"""End-to-end: the Recruitee adapter drives real Chromium through a local
stand-in of a hosted Recruitee application form.

Hermetic. The page is generated from a real captured offer
(tests/fixtures/ats/recruitee_offer.json, Channable) and reproduces what was
seen live: inputs named ``candidate.*``; per-question names keyed by the FORM's
own ids, which differ from the API's (the question text sits in the hidden
``….openQuestionId`` input's parent); Yes/No and choice radios with labels; a
``type=number`` salary box; a required consent box next to an optional one; and
a Cookiebot banner that appears late, offers only "Manage preferences" / "Allow
all", and covers the page until dismissed.

    uv run pytest tests/e2e/test_recruitee_apply_browser.py -v
"""
import html
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest

from src.automation.tools import ats_recruitee as rc
from src.automation.tools.ats_common import build_profile

pytestmark = pytest.mark.e2e

OFFER = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "ats" / "recruitee_offer.json")
                   .read_text(encoding="utf-8"))["offer"]
FACTS = ("EU citizenship or a valid work permit: EU citizenship\n"
         "availability for at least 32 hours: Yes\nmonthly gross salary: 5,500 EUR")


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


def _question(q: dict, form_id: int) -> str:
    """One question as the hosted form renders it: a parent whose first line is
    the question, a hidden openQuestionId, then the control."""
    name = f"candidate.openQuestionAnswers.{form_id}"
    text = rc.plain(q["body"]).split(". ")[0] if q["kind"] == "video" else rc.plain(q["body"])
    star = " *" if q["required"] else ""
    head = (f'<div class="q"><div>{html.escape(text)}{star}</div>'
            f'<input type="hidden" name="{name}.openQuestionId" value="{form_id}">')
    req = " required" if q["required"] else ""
    if q["kind"] == "boolean":
        body = "".join(f'<input type="radio" id="r{form_id}{i}" name="{name}.flag" value="{v.lower()}"{req}>'
                       f'<label for="r{form_id}{i}">{v}</label>' for i, v in enumerate(("Yes", "No")))
    elif q["kind"] == "single_choice":
        body = "".join(f'<input type="radio" id="r{form_id}{i}" name="{name}.content" value="{o["id"]}"{req}>'
                       f'<label for="r{form_id}{i}">{html.escape(rc.plain(o["body"]))}</label>'
                       for i, o in enumerate(q["open_question_options"]))
    elif q["kind"] == "salary":
        body = f'<input type="number" name="{name}.content"{req}> EUR'
    elif q["kind"] in ("string", "text"):
        body = f'<input type="text" name="{name}.content"{req}>'
    else:  # video / file
        body = '<input type="file">'
    return head + body + "</div>"


def _page(rename: str = "") -> str:
    qs = "".join(_question({**q, "body": rename or q["body"]} if i == 0 and rename else q, 7000 + i)
                 for i, q in enumerate(OFFER["open_questions"]))
    return f"""<!doctype html><html><head><meta charset=utf-8><title>Apply</title>
<style>#CybotCookiebotDialog {{ position: fixed; inset: 0; background: rgba(0,0,0,.4); }}
#CybotCookiebotDialog .box {{ background: #fff; margin: 30vh auto; width: 400px; padding: 10px; }}</style></head><body>
<form id="f">
<label for="n">Full name *</label><input id="n" name="candidate.name" required>
<label for="e">Email address *</label><input id="e" type="email" name="candidate.email" required>
<button type="button" id="country-select-input-candidate.phone">Netherlands</button>
<label for="p">Phone number *</label><input id="p" type="tel" name="candidate.phone" required>
<label for="cv">CV or resume *</label><input id="cv" type="file" name="candidate.cv" required>
<label for="cl">Cover letter</label><input id="cl" type="file" name="candidate.coverLetterFile">
<fieldset><legend>Questions</legend>{qs}</fieldset>
<input type="hidden" name="candidate.agreements.0.agreementId" value="1">
<input type="checkbox" id="a0" name="candidate.agreements.0.consent" required><label for="a0">I consent to the privacy policy *</label>
<input type="checkbox" id="a1" name="candidate.agreements.1.consent"><label for="a1">Keep me in your talent pool</label>
<button type="submit">Send</button>
</form>
<script>
window.__cookie = 'none';
setTimeout(() => {{   // the banner comes late, over the whole page
  document.body.insertAdjacentHTML('beforeend', '<div id="CybotCookiebotDialog"><div class="box">' +
    '<button id="CybotCookiebotDialogBodyLevelButtonCustomize">Manage preferences</button>' +
    '<button id="CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll">Allow all</button>' +
    '<div id="prefs" hidden><input type="checkbox" id="CybotCookiebotDialogBodyLevelButtonMarketingInline" checked>' +
    '<button id="CybotCookiebotDialogBodyLevelButtonLevelOptinAllowallSelection">Allow selection</button></div></div></div>');
  const d = document.getElementById('CybotCookiebotDialog');
  document.getElementById('CybotCookiebotDialogBodyLevelButtonCustomize').onclick = () => {{ document.getElementById('prefs').hidden = false; }};
  document.getElementById('CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll').onclick = () => {{ window.__cookie = 'all'; d.remove(); }};
  document.getElementById('CybotCookiebotDialogBodyLevelButtonLevelOptinAllowallSelection').onclick = () => {{
    window.__cookie = document.getElementById('CybotCookiebotDialogBodyLevelButtonMarketingInline').checked ? 'marketing' : 'necessary';
    d.remove(); }};
}}, 1500);
document.getElementById('f').addEventListener('submit', async (ev) => {{
  ev.preventDefault();
  const out = {{}};
  for (const el of document.querySelectorAll('#f input')) {{
    if (!el.name) continue;
    if (el.type === 'file') {{ if (el.files.length) out[el.name] = el.files[0].name; continue; }}
    if (el.type === 'radio' || el.type === 'checkbox') {{ if (el.checked) out[el.name] = el.labels[0] ? el.labels[0].innerText : el.value; continue; }}
    if (el.value) out[el.name] = el.value;
  }}
  out.__cookie = window.__cookie;
  await fetch('/submit', {{ method: 'POST', body: JSON.stringify(out) }});
  document.body.innerHTML = '<h1>Thank you for applying!</h1>';
}});
</script></body></html>"""


@pytest.fixture(scope="module")
def site():
    submissions: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a):
            pass

        def do_GET(self):  # noqa: N802
            path = urlparse(self.path).path
            body = _page(rename="Something nobody asked about, long enough to compare" if "renamed" in path else "")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())

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


def _job(url):
    return {"platform": "recruitee", "token": "channable", "job_id": OFFER["slug"], "title": OFFER["title"],
            "company": "Channable", "location": "Utrecht, Netherlands", "url": url, "apply_url": url}


def _run(page, url, resume, dry_run, extra=FACTS):
    ad = rc.RecruiteeAdapter()
    job = _job(url)
    profile = build_profile(first_name="Ada", last_name="Lovelace", email="ada@example.com",
                            phone="+31 6 12345678", current_location="Utrecht, Netherlands",
                            authorized_countries="Netherlands", resume_path=resume, extra_answers=extra)
    answers, unanswered = ad.plan(OFFER, job, profile, None)
    assert unanswered == []
    return ad.apply(page, job, answers, dry_run, print)


def test_submit_fills_by_question_text_and_refuses_optional_cookies(site, page, resume):
    base, submissions = site
    before = len(submissions)
    entry = _run(page, f"{base}/o/haskell/c/new", resume, dry_run=False)
    assert entry["status"] == "submitted" and entry["submitted"] is True, entry
    assert len(submissions) == before + 1
    sent = submissions[-1]
    q = "candidate.openQuestionAnswers."
    assert sent["candidate.name"] == "Ada Lovelace" and sent["candidate.email"] == "ada@example.com"
    assert sent["candidate.phone"] == "+31612345678" and sent["candidate.cv"] == "ada_cv.pdf"
    assert "candidate.coverLetterFile" not in sent                          # optional: left alone
    assert sent[f"{q}7000.content"] == "Company careers page"                # by text, form id 7000
    assert sent[f"{q}7001.content"] == "EU citizenship"
    assert f"{q}7002.content" not in sent                                    # "specify which type": not "Yes"
    assert sent[f"{q}7004.flag"] == "Yes"
    assert sent[f"{q}7005.content"] == "5500"                                # number box: just the amount
    assert sent["candidate.agreements.0.consent"] and "candidate.agreements.1.consent" not in sent
    assert sent["__cookie"] == "necessary"                                   # never "Allow all"


def test_dry_run_never_sends(site, page, resume):
    base, submissions = site
    before = len(submissions)
    entry = _run(page, f"{base}/o/haskell/c/new", resume, dry_run=True)
    assert entry["status"] == "prepared" and len(submissions) == before


def test_a_question_missing_from_the_form_fails_the_job(site, page, resume):
    base, submissions = site
    before = len(submissions)
    entry = _run(page, f"{base}/renamed/c/new", resume, dry_run=False)
    assert entry["status"] == "failed" and "How did you find out about Channable" in entry["reason"]
    assert len(submissions) == before
