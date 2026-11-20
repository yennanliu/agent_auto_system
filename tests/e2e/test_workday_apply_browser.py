"""End-to-end: the Workday adapter drives real Chromium through a local stand-in
of a Workday careers site (tests/e2e/workday_fixture/app.html).

Hermetic. The stand-in reproduces the hooks and quirks seen live on 3M: the
data-automation-id attributes, create-account → Sign In, a Sign In button under
a transparent click_filter, a sign-in form that re-mounts, "Apply" turning into
"Continue Application" for a draft (and "Something went wrong" if Apply
Manually is reopened instead), steps that render blank first, listboxes opened
through aria-controls, ids that change on every render, and a Review step.

    uv run pytest tests/e2e/test_workday_apply_browser.py -v
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from src.automation.tools import ats_workday as wd
from src.automation.tools.ats_common import build_profile

pytestmark = pytest.mark.e2e

APP = (Path(__file__).parent / "workday_fixture" / "app.html").read_bytes()
EMAIL, PASSWORD = "ada@example.com", "Corr3ct-Horse!"


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
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture
def resume(tmp_path):
    p = tmp_path / "cv.pdf"
    p.write_bytes(b"%PDF-1.4 test")
    return str(p)


@pytest.fixture(autouse=True)
def _password(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKDAY_PASSWORD_ACME_WD1", PASSWORD)
    # Never read or write the real data/workday_accounts.json (or the Keychain).
    monkeypatch.setenv("WORKDAY_ACCOUNTS_FILE", str(tmp_path / "workday_accounts.json"))
    monkeypatch.setattr(wd.sys, "platform", "linux")


def _job(base):
    url = f"{base}/Search/job/US-Minnesota-Maplewood/Senior-Engineer_R1"
    return {"platform": "workday", "token": "acme.wd1/Search", "job_id": "Senior-Engineer_R1",
            "title": "Senior Engineer", "company": "acme", "location": "US, Minnesota, Maplewood",
            "url": url, "apply_url": url}


def _profile(resume, extra="", email=EMAIL):
    return build_profile(first_name="Ada", last_name="Lovelace", email=email, phone="+1 415 555 0100",
                         current_location="San Francisco, CA", authorized_countries="United States",
                         years_experience=5, resume_path=resume, extra_answers=extra)


FACTS = "Epic Systems: No\nsigned an agreement: No"


def _run(context, base, profile, dry_run=True, create=False):
    page = context.new_page()  # a new tab: sessionStorage (the sign-in) starts empty, like a new run
    adapter = wd.WorkdayAdapter()
    adapter.create_accounts = create
    entry = adapter.apply_live(page, _job(base), profile, None, dry_run, print)
    state = page.evaluate("window.__state ? window.__state() : null")
    page.close()
    return entry, state


def test_dry_run_answers_every_step_and_stops_at_review(site, browser, resume):
    base, submissions = site
    before = len(submissions)
    ctx = browser.new_context()
    entry, state = _run(ctx, base, _profile(resume, FACTS))
    assert entry["status"] == "prepared", entry
    assert entry["submitted"] is False and state["step"] == 4               # on Review
    assert state["values"] == {
        "country": "United States", "source": "Acme Website", "first": "Ada", "last": "Lovelace",
        "device": "Mobile", "phone": "+1 415 555 0100",
        "auth": "Yes", "sponsor": "No", "former": "No", "epic": "No", "noncompete": "No",
        "sex": "Not declared", "terms": True,
    }                                                                          # no middle name, no job alerts
    assert state["files"] == ["cv.pdf"]
    assert len(submissions) == before                                          # dry run: nothing sent
    ctx.close()


def test_a_question_it_cannot_answer_stops_the_job_and_names_it(site, browser, resume):
    base, _ = site
    ctx = browser.new_context()
    entry, state = _run(ctx, base, _profile(resume))
    assert entry["status"] == "skipped"
    assert entry["unanswered"] == [
        "Have you ever been employed by Epic Systems?",
        "Have you signed an agreement with a current or former employer containing a non-compete?"]
    assert state["step"] == 2                                                  # stopped on the questions
    assert "epic" not in state["values"] and "noncompete" not in state["values"]   # never guessed
    assert state["values"]["former"] == "No"                                   # the hiring company: answered
    ctx.close()


def test_the_next_run_resumes_the_draft_without_a_second_upload(site, browser, resume):
    base, _ = site
    ctx = browser.new_context()
    first, _ = _run(ctx, base, _profile(resume))
    assert first["status"] == "skipped"
    second, state = _run(ctx, base, _profile(resume, FACTS))
    assert second["status"] == "prepared", second
    assert state["files"] == ["cv.pdf"]                                        # still one résumé
    ctx.close()


def test_submit_counts_only_on_the_confirmation(site, browser, resume):
    base, submissions = site
    before = len(submissions)
    ctx = browser.new_context()
    entry, _ = _run(ctx, base, _profile(resume, FACTS), dry_run=False)
    assert entry["status"] == "submitted" and entry["submitted"] is True, entry
    assert len(submissions) == before + 1
    assert submissions[-1]["values"]["sex"] == "Not declared"
    ctx.close()


def test_a_wrong_password_fails_cleanly(site, browser, resume, monkeypatch):
    base, _ = site
    monkeypatch.setenv("WORKDAY_PASSWORD_ACME_WD1", "wrong")
    ctx = browser.new_context()
    entry, state = _run(ctx, base, _profile(resume, FACTS))
    assert entry["status"] == "failed" and "could not sign in" in entry["reason"]
    assert "Wrong email address or password" in entry["reason"]
    assert state is None or state.get("values") == {}                          # nothing filled
    ctx.close()


# ── opt-in account creation ──────────────────────────────────────────────────

def test_without_opt_in_no_account_is_created(site, browser, resume, monkeypatch):
    base, _ = site
    monkeypatch.delenv("WORKDAY_PASSWORD_ACME_WD1")
    ctx = browser.new_context()
    entry, _ = _run(ctx, base, _profile(resume, FACTS, email="new@example.com"))
    assert entry["status"] == "manual" and "no Workday account stored for acme.wd1" in entry["reason"]
    assert wd.load_accounts() == {}
    ctx.close()


def test_creates_the_account_saves_it_locally_then_applies(site, browser, resume, monkeypatch):
    import os
    import stat
    base, _ = site
    monkeypatch.delenv("WORKDAY_PASSWORD_ACME_WD1")
    ctx = browser.new_context()
    entry, state = _run(ctx, base, _profile(resume, FACTS, email="new@example.com"), create=True)
    assert entry["status"] == "prepared", entry
    saved = wd.load_accounts()["acme.wd1"]["new@example.com"]
    assert saved["status"] == "active" and len(saved["password"]) == 20
    assert stat.S_IMODE(os.stat(wd.accounts_path()).st_mode) == 0o600          # owner only
    # The next run signs in with the saved password instead of creating another account.
    page = ctx.new_page()
    adapter = wd.WorkdayAdapter()
    adapter.create_accounts = True
    page.goto(_job(base)["url"])
    accounts = page.evaluate("JSON.parse(localStorage.getItem('accounts'))")
    assert accounts == {"new@example.com": saved["password"]}
    second = adapter.apply_live(page, _job(base), _profile(resume, FACTS, email="new@example.com"),
                                None, True, print)
    assert second["status"] == "prepared"
    assert wd.load_accounts()["acme.wd1"]["new@example.com"]["password"] == saved["password"]
    ctx.close()


def test_an_account_needing_e_mail_verification_is_kept_and_reported(site, browser, resume, monkeypatch):
    base, _ = site
    monkeypatch.delenv("WORKDAY_PASSWORD_ACME_WD1")
    ctx = browser.new_context()
    entry, _ = _run(ctx, base, _profile(resume, FACTS, email="ada+verify@example.com"), create=True)
    assert entry["status"] == "needs_verification" and "Click the link" in entry["reason"]
    assert wd.load_accounts()["acme.wd1"]["ada+verify@example.com"]["status"] == "needs_verification"
    ctx.close()


def test_an_existing_account_is_not_overwritten(site, browser, resume, monkeypatch):
    base, _ = site
    monkeypatch.delenv("WORKDAY_PASSWORD_ACME_WD1")
    ctx = browser.new_context()
    entry, _ = _run(ctx, base, _profile(resume, FACTS, email="taken@example.com"), create=True)
    assert entry["status"] == "failed" and "already exists" in entry["reason"]
    assert "taken@example.com" not in wd.load_accounts().get("acme.wd1", {})   # the unused password is dropped
    ctx.close()
