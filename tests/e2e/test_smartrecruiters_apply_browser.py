"""End-to-end: the SmartRecruiters adapter drives real Chromium through a local
stand-in of the one-click apply page (tests/e2e/smartrecruiters_fixture/app.html).

Hermetic. The stand-in reproduces what was read on a live Wise posting: spl-*
web components with open shadow roots, option / radio text only in the
`label` PROPERTY, spl-radio / spl-checkbox as the controls themselves, menus
scoped by aria-controls, a résumé parser that rewrites the form, Next →
/screening, Submit → a "Thank you for applying" page; plus the DataDome wall.

    uv run pytest tests/e2e/test_smartrecruiters_apply_browser.py -v
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from src.automation.tools.ats_common import build_profile
from src.automation.tools.ats_smartrecruiters import SmartRecruitersAdapter

pytestmark = pytest.mark.e2e

APP = (Path(__file__).parent / "smartrecruiters_fixture" / "app.html").read_bytes()
DECL = "I prefer not to answer"
CITIZEN = "I hold citizenship or permanent residency (No visa/sponsorship required)"
FACTS = ("eligible to work in this location: I hold citizenship or permanent residency\n"
         "Acme customer: No")


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

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a):
            pass

        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(APP)

        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            submissions.append(json.loads(body or b"{}"))
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
        pg = b.new_context().new_page()
        yield pg
        b.close()


@pytest.fixture
def resume(tmp_path):
    p = tmp_path / "cv.pdf"
    p.write_bytes(b"%PDF-1.4 test")
    return str(p)


JOB = {"platform": "smartrecruiters", "token": "Acme", "job_id": "1", "title": "Senior Engineer",
       "company": "Acme", "location": "London, United Kingdom",
       "url": "https://jobs.smartrecruiters.com/Acme/1", "apply_url": "https://jobs.smartrecruiters.com/Acme/1"}


def _profile(resume, extra="", location="London, United Kingdom"):
    return build_profile(first_name="Ada", last_name="Lovelace", email="ada@example.com",
                         phone="+44 7700 900123", current_location=location,
                         authorized_countries="United Kingdom", notice_period="1 month",
                         linkedin_url="https://www.linkedin.com/in/ada", resume_path=resume,
                         extra_answers=extra, cover_letter_text="Hello Acme")


def _apply(page, url, profile, dry_run):
    ad = SmartRecruitersAdapter()
    ad.apply_url = lambda _job: url          # the stand-in instead of the live posting
    return ad.apply_live(page, JOB, profile, None, dry_run, print)


def test_submit_fills_both_pages_and_counts_only_on_the_confirmation(site, page, resume):
    base, submissions = site
    before = len(submissions)
    entry = _apply(page, f"{base}/Acme/1-senior-engineer?oga=true", _profile(resume, FACTS), dry_run=False)
    assert entry["status"] == "submitted" and entry["submitted"] is True, entry
    assert len(submissions) == before + 1
    sent = submissions[-1]
    assert sent["page1"] == {
        "resume": "cv.pdf", "first": "Ada", "last": "Lovelace",
        "email": "ada@example.com", "confirm": "ada@example.com",          # typed over the parse
        "city": "London, England, United Kingdom",                          # not Ontario / OH
        "phone": "+447700900123", "linkedin": "https://www.linkedin.com/in/ada", "message": "Hello Acme"}
    assert sent["answers"] == {
        "notice": "1 month", "eligible": CITIZEN, "customer": "No",
        "gender": DECL, "selfdesc": "", "race": [DECL],
        "salary": "",                                                       # optional, not guessed
        "privacy": True, "alerts": False}                                   # only the consent box


def test_dry_run_stops_before_submit(site, page, resume):
    base, submissions = site
    before = len(submissions)
    entry = _apply(page, f"{base}/Acme/1?oga=true", _profile(resume, FACTS), dry_run=True)
    assert entry["status"] == "prepared" and entry["submitted"] is False, entry
    assert len(submissions) == before
    assert "/screening" in page.url
    labels = {a["label"]: a["value"] for a in entry["answers"]}
    assert labels["How are you eligible to work in this location?"] == CITIZEN
    assert labels["What gender do you most closely identify with?"] == DECL


def test_a_question_it_cannot_answer_skips_the_job(site, page, resume):
    base, submissions = site
    before = len(submissions)
    entry = _apply(page, f"{base}/Acme/1?oga=true", _profile(resume), dry_run=False)
    assert entry["status"] == "skipped"
    assert entry["unanswered"] == ["How are you eligible to work in this location?",
                                   "Are you currently an Acme customer?"]
    assert len(submissions) == before


def test_a_job_without_screening_questions(site, page, resume):
    base, submissions = site
    entry = _apply(page, f"{base}/Acme/1?oga=true&noscreening=1", _profile(resume), dry_run=False)
    assert entry["status"] == "submitted", entry
    assert submissions[-1]["answers"]["privacy"] is True


def test_an_unknown_city_is_never_replaced_by_another(site, page, resume):
    base, _ = site
    entry = _apply(page, f"{base}/Acme/1?oga=true", _profile(resume, FACTS, location="Atlantis"),
                   dry_run=True)
    assert entry["status"] == "skipped" and entry["unanswered"] == ["City (place of residence)"]


def test_a_chat_widget_and_an_ignored_next_click_dont_stop_the_job(site, page, resume):
    """Live: Western Digital's chat widget covered Next (click timed out), and
    Experian / ServiceNow ignored a Next clicked while page 1 was settling."""
    base, _ = site
    entry = _apply(page, f"{base}/Acme/1?oga=true&live=1", _profile(resume, FACTS), dry_run=True)
    assert entry["status"] == "prepared", entry


def test_the_bot_wall_is_reported_not_retried(site, page, resume):
    base, _ = site
    entry = _apply(page, f"{base}/Acme/1?blocked=1", _profile(resume, FACTS), dry_run=True)
    assert entry["status"] == "blocked" and "bot protection" in entry["reason"]


def test_option_labels_reads_the_label_property(page):
    """spl-select-option text lives only in the ``label`` PROPERTY (PR review:
    exercised in a browser, not a fake)."""
    from src.automation.tools.ats_smartrecruiters import option_labels
    page.set_content("<div id=m></div><script>"
                     "for (const t of ['London, England, United Kingdom', 'London, Ontario, Canada']) {"
                     "  const o = document.createElement('spl-select-option'); o.label = t;"
                     "  document.getElementById('m').appendChild(o); }"
                     "const plain = document.createElement('spl-select-option'); plain.textContent = 'Text only';"
                     "document.getElementById('m').appendChild(plain);</script>")
    assert option_labels(page.locator("#m spl-select-option")) == [
        "London, England, United Kingdom", "London, Ontario, Canada", "Text only"]


def test_a_city_only_in_another_country_is_not_picked(site, page, resume):
    """London, UK profile; the menu offers only foreign Londons → no pick."""
    base, _ = site
    entry = _apply(page, f"{base}/Acme/1?oga=true", _profile(resume, FACTS, location="London, Japan"),
                   dry_run=True)
    assert entry["status"] == "skipped" and entry["unanswered"] == ["City (place of residence)"]


def test_the_human_check_is_reported_and_never_touched(site, page, resume, monkeypatch):
    base, _ = site
    monkeypatch.setenv("ATS_HUMAN_CHECK_WAIT", "0")
    entry = _apply(page, f"{base}/Acme/1?captcha=1", _profile(resume, FACTS), dry_run=True)
    assert entry["status"] == "manual" and "human verification" in entry["reason"]
    assert page.frame_locator("#dd").locator("#human-slider").count() == 1   # still there: never clicked


def test_a_person_completing_the_check_lets_the_run_continue(site, page, resume, monkeypatch):
    """Headed + ATS_HUMAN_CHECK_WAIT: the run waits; here the 'person' is a
    timer in the page (the automation itself never clicks the slider)."""
    base, _ = site
    monkeypatch.setenv("ATS_HUMAN_CHECK_WAIT", "20")
    monkeypatch.setenv("ATS_HEADLESS", "0")
    page.add_init_script("setTimeout(() => { if (window.__slider) window.__slider.click(); }, 2500)")
    entry = _apply(page, f"{base}/Acme/1?captcha=1", _profile(resume, FACTS), dry_run=True)
    assert entry["status"] == "prepared", entry
