"""End-to-end: ``run_greenhouse_apply`` drives a real browser through a form.

Hermetic. A local HTTP server serves ``greenhouse_fixture/job_app.html``, a
stand-in for Greenhouse's embedded application form that keeps the DOM contract
the tool relies on (input ids = API field names, react-select option ids, a
checkbox-group multi-select, async autocompletes, consent checkbox, and a decoy
role=option list). The Job Board API is a fake httpx client. Nothing leaves
this machine and nothing is submitted anywhere real.

This exercises the whole funnel through the shared ``ats_common`` core:
discover → relevance gate → plan (with the LLM ``answer_fn`` called off the
Playwright loop) → fill in Chromium → submit → confirmation → ledger.

Needs a Playwright Chromium (``uv run playwright install chromium``); skipped
otherwise. Run with:

    uv run pytest tests/e2e/test_greenhouse_apply_browser.py -v
"""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from src.automation.tools import greenhouse_apply_tool as gh

pytestmark = pytest.mark.e2e

FIXTURE = Path(__file__).parent / "greenhouse_fixture" / "job_app.html"


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


# ── local "Greenhouse" ───────────────────────────────────────────────────────

class _Site:
    def __init__(self):
        self.submissions: list[dict] = []
        site = self

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
                if self.path.startswith("/embed/job_app"):
                    self._send(FIXTURE.read_text(encoding="utf-8"))
                elif self.path.startswith("/confirmation"):
                    self._send("<h1>Thank you for applying to Acme!</h1>")
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


@pytest.fixture
def site(monkeypatch):
    s = _Site()
    monkeypatch.setattr(gh, "_EMBED_APP", s.base + "/embed/job_app?for={token}&token={job_id}")
    monkeypatch.setenv("GREENHOUSE_HEADLESS", "1")
    yield s
    s.close()


def _sel(name, *labels):
    return {"name": name, "type": "multi_value_single_select", "values": [{"label": v} for v in labels]}


_FORM = {
    "location": {"name": "Taipei, Taiwan"},
    "questions": [
        {"label": "First Name", "required": True, "fields": [{"name": "first_name", "type": "input_text"}]},
        {"label": "Last Name", "required": True, "fields": [{"name": "last_name", "type": "input_text"}]},
        {"label": "Email", "required": True, "fields": [{"name": "email", "type": "input_text"}]},
        {"label": "Phone", "required": False, "fields": [{"name": "phone", "type": "input_text"}]},
        {"label": "Resume/CV", "required": True, "fields": [{"name": "resume", "type": "input_file"},
                                                           {"name": "resume_text", "type": "textarea"}]},
        {"label": "LinkedIn Profile", "required": False,
         "fields": [{"name": "question_101", "type": "input_text"}]},
        {"label": "Are you legally authorized to work in Taiwan?", "required": True,
         "fields": [_sel("question_102", "Yes", "No")]},
        {"label": "Will you now or in the future require visa sponsorship?", "required": True,
         "fields": [_sel("question_103", "Yes", "No")]},
        {"label": "How many years of experience do you have?", "required": True,
         "fields": [_sel("question_104", "0-2 years", "3-5 years", "6+ years")]},
        {"label": "Which languages do you speak?", "required": False,
         "fields": [{"name": "question_105[]", "type": "multi_value_multi_select",
                     # "Chinese, Cantonese" is shorter than the Mandarin option, so only
                     # the never-Cantonese rule keeps it from being picked for "Chinese".
                     "values": [{"label": "English"}, {"label": "Chinese, Cantonese"},
                                {"label": "Mandarin Chinese (Putonghua)"}]}]},
        {"label": "Why do you want to join Acme?", "required": True,
         "fields": [{"name": "question_106", "type": "textarea"}]},
    ],
    "location_questions": [{"label": "Location (City)", "required": True,
                            "fields": [{"name": "location", "type": "input_text"}]}],
    "education": "education_optional",
    "compliance": [{"questions": [{"label": "Gender", "required": True,
                                   "fields": [{"name": "gender", "values": [
                                       {"label": "Male"}, {"label": "Female"},
                                       {"label": "I don't wish to answer"}]}]}]}],
}
_UNANSWERABLE = {**_FORM, "questions": _FORM["questions"] + [
    {"label": "Favourite colour?", "required": True, "fields": [_sel("question_999", "Red", "Blue")]}]}

_JOBS = [
    {"id": 1, "title": "Backend Engineer", "location": {"name": "Taipei, Taiwan"}, "company_name": "Acme"},
    {"id": 2, "title": "Backend Engineer II", "location": {"name": "Taipei, Taiwan"}, "company_name": "Acme"},
    {"id": 3, "title": "Data Engineer", "location": {"name": "Taipei, Taiwan"}, "company_name": "Acme"},
    {"id": 4, "title": "Frontend Engineer", "location": {"name": "Taipei, Taiwan"}, "company_name": "Acme"},
]


class _Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Api:
    """Fake Job Board API (boards-api.greenhouse.io/v1/boards/acme/...)."""
    def get(self, url, params=None):
        if url.endswith("/acme/jobs"):
            return _Resp({"jobs": _JOBS})
        jid = url.rsplit("/", 1)[-1]
        if url.endswith(f"/acme/jobs/{jid}") and (params or {}).get("questions") == "true":
            return _Resp(_UNANSWERABLE if jid == "3" else _FORM)
        return _Resp({}, 404)

    def close(self):
        pass


@pytest.fixture
def resume(tmp_path):
    p = tmp_path / "ada_cv.pdf"
    p.write_bytes(b"%PDF-1.4 fixture")
    return str(p)


_PROFILE = dict(first_name="Ada", last_name="Lovelace", email="ada@example.com",
                phone="+886 912 345 678", linkedin_url="https://linkedin.com/in/ada",
                current_location="Taipei, Taiwan", years_experience=4,
                authorized_countries="Taiwan", languages="English, Chinese",
                school="National Taiwan University", degree="Master's Degree")


class _Recorder:
    """answer_fn / relevance_fn that record calls and whether they ran with an
    event loop on their thread (crewai's sync kickoff refuses to run there)."""
    def __init__(self):
        self.asked: list[str] = []
        self.on_loop: list[bool] = []

    def _loop(self) -> bool:
        try:
            asyncio.get_running_loop()
            return True
        except RuntimeError:
            return False

    def answer(self, label, kind, options, job):
        self.on_loop.append(self._loop())
        self.asked.append(label)
        return "I love compilers." if "Why do you want" in label else ""

    def relevance(self, title, meta):
        self.on_loop.append(self._loop())
        return ("Frontend" not in title, "not a backend role" if "Frontend" in title else "")


def _run(site, resume, tmp_path, rec, **kw):
    return gh.run_greenhouse_apply(
        **{"boards": "acme", **_PROFILE, **kw}, resume_path=resume, client=_Api(),
        ledger_path=str(tmp_path / "ledger.json"), relevance_fn=rec.relevance,
        answer_fn=rec.answer, log=lambda _m: None)


# ── tests ────────────────────────────────────────────────────────────────────

def test_full_funnel_submits_confirms_and_records(site, resume, tmp_path):
    rec = _Recorder()
    out = _run(site, resume, tmp_path, rec, dry_run=False, max_applications=10)

    assert "error" not in out
    by_id = {e["job_id"]: e for e in out["applied"] + out["skipped"]}

    # Job 1: filled in the browser, submitted, confirmation page seen.
    assert by_id["1"]["status"] == "submitted" and by_id["1"]["submitted"] is True
    # Job 2: the board e-mailed a security code → never counted as applied.
    assert by_id["2"]["status"] == "needs_verification" and by_id["2"]["submitted"] is False
    # Job 3: a required question we can't answer → skipped before the browser.
    assert by_id["3"]["status"] == "skipped" and by_id["3"]["unanswered"] == ["Favourite colour?"]
    # Job 4: dropped by the relevance gate.
    assert by_id["4"]["status"] == "skipped" and by_id["4"].get("filtered") is True
    assert out["submitted_count"] == 1 and out["filtered_count"] == 1

    # What the form actually received for job 1.
    sent = {s["job_id"]: s for s in site.submissions}
    assert set(sent) == {"1", "2"}                      # job 3 never reached the form
    s = sent["1"]
    assert (s["first_name"], s["last_name"], s["email"]) == ("Ada", "Lovelace", "ada@example.com")
    assert s["phone"] == "+886 912 345 678"
    assert s["resume"] == "ada_cv.pdf"
    assert s["question_101"] == "https://linkedin.com/in/ada"
    assert s["question_102"] == "Yes"                   # authorized in Taiwan
    assert s["question_103"] == "No"                    # no sponsorship needed
    assert s["question_104"] == "3-5 years"             # 4 years mapped onto a range
    assert s["question_105"] == ["English", "Mandarin Chinese (Putonghua)"]   # never Cantonese
    assert s["question_106"] == "I love compilers."     # required → LLM answer_fn
    assert s["location"] == "Taipei, Taipei City, Taiwan"          # autocomplete suggestion
    assert s["school"] == "National Taiwan University (NTU)"      # shortest matching option
    assert s["degree"] == "Master's Degree"
    assert s["gender"] == "I don't wish to answer"      # EEOC → decline, never a guess
    assert s["consent"] is True

    # The LLM callbacks never ran on the Playwright event-loop thread.
    assert rec.on_loop and not any(rec.on_loop)
    assert "Why do you want to join Acme?" in rec.asked

    # Only the confirmed submission is in the ledger.
    assert set(gh.load_ledger(str(tmp_path / "ledger.json"))) == {"acme/1"}


def test_rerun_skips_ledgered_job_without_resubmitting(site, resume, tmp_path):
    rec = _Recorder()
    _run(site, resume, tmp_path, rec, boards="https://job-boards.greenhouse.io/acme/jobs/1",
         dry_run=False)
    assert len(site.submissions) == 1

    again = _run(site, resume, tmp_path, rec, boards="https://job-boards.greenhouse.io/acme/jobs/1",
                 dry_run=False)
    assert again["applied"] == []
    assert again["skipped"][0]["reason"] == "already applied (ledger)"
    assert len(site.submissions) == 1


def test_dry_run_fills_everything_but_never_submits(site, resume, tmp_path):
    rec = _Recorder()
    out = _run(site, resume, tmp_path, rec, boards="https://job-boards.greenhouse.io/acme/jobs/1",
               dry_run=True)
    assert [e["status"] for e in out["applied"]] == ["prepared"]
    assert out["applied"][0]["submitted"] is False
    # The planned answers are returned for review (file shown by basename only).
    answers = {a["label"]: a["value"] for a in out["applied"][0]["answers"]}
    assert answers["Resume/CV"] == "ada_cv.pdf"
    assert answers["How many years of experience do you have?"] == "3-5 years"
    assert site.submissions == []
    assert gh.load_ledger(str(tmp_path / "ledger.json")) == {}


def test_form_that_never_loads_fails_that_job_only(site, resume, tmp_path, monkeypatch):
    monkeypatch.setattr(gh, "_EMBED_APP", site.base + "/missing?for={token}&token={job_id}")
    rec = _Recorder()
    out = _run(site, resume, tmp_path, rec, boards="https://job-boards.greenhouse.io/acme/jobs/1",
               dry_run=False)
    assert out["applied"] == []
    assert out["skipped"][0]["status"] == "failed"
    assert "did not load" in out["skipped"][0]["reason"]
    assert site.submissions == []
