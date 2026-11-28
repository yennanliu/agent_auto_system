"""tailor_resume end to end: flow → LLM crew → guard → runner → planner →
browser filler, and every fallback to the plain file upload.

The browser tests drive real Chromium against a local copy of Greenhouse's
résumé widget ("Attach" file input + an "Enter manually" button that reveals
the ``#resume_text`` textarea) and skip when Chromium isn't installed."""
import pytest

from src.automation.tools import ats_apply_tool as ats
from src.automation.tools import greenhouse_apply_tool as gh
from src.automation.tools.ats_greenhouse import GreenhouseAdapter

RESUME = """Ada Lovelace
ada@example.com

EXPERIENCE
Senior Backend Engineer, Acme (2019-2024)
- Built Go services handling 20000 requests per second
- Led migration to Kubernetes

SKILLS
Go, Python, Kubernetes"""

TAILORED = """Ada Lovelace
ada@example.com

SKILLS
Kubernetes, Go, Python

EXPERIENCE
Senior Backend Engineer, Acme (2019-2024)
- Led migration to Kubernetes
- Built Go services handling 20000 requests per second"""

JOB_DATA = {"title": "Platform Engineer", "company_name": "Figma",
            "content": "&lt;p&gt;Kubernetes, Go&lt;/p&gt;"}


# ── flow → crew → guard ──────────────────────────────────────────────────────

def _kickoff(mocker, flow_path, flow_cls, **inputs):
    captured = {}
    mocker.patch(f"{flow_path}.run_{'greenhouse' if 'greenhouse' in flow_path else 'ats'}_apply",
                 side_effect=lambda **kw: captured.update(kw) or {
                     "applied": [], "skipped": [], "jobs_found": 0, "summary": "none"})
    resolve = mocker.patch("src.automation.harness.provider.resolve",
                           return_value=(object(), "gemini", "gemini/gemini-2.5-flash"))
    flow_cls().kickoff(inputs={"boards": "figma", "first_name": "Ada", "last_name": "L",
                               "email": "a@x.io", **inputs})
    return captured, resolve


def _crew_returns(mocker, raw=None, exc=None):
    crew = mocker.patch("src.automation.flows.greenhouse_apply_flow.ResumeTailorCrew")
    kickoff = crew.return_value.crew.return_value.kickoff
    if exc:
        kickoff.side_effect = exc
    else:
        kickoff.return_value = mocker.Mock(raw=raw, usage_metrics=None)
    return kickoff


def _gh_flow(mocker, **inputs):
    from src.automation.flows.greenhouse_apply_flow import GreenhouseApplyFlow
    return _kickoff(mocker, "src.automation.flows.greenhouse_apply_flow", GreenhouseApplyFlow,
                    **inputs)


def test_flow_off_by_default_makes_no_llm_call(mocker):
    captured, resolve = _gh_flow(mocker)
    assert captured["tailor_resume_fn"] is None
    resolve.assert_not_called()


def test_flow_tailors_through_the_crew(mocker):
    kickoff = _crew_returns(mocker, raw=f"```\n{TAILORED}\n```")
    captured, _ = _gh_flow(mocker, tailor_resume=True)
    assert captured["tailor_resume_fn"](RESUME, JOB_DATA) == TAILORED
    inputs = kickoff.call_args.kwargs["inputs"]
    assert inputs["job_title"] == "Platform Engineer" and inputs["company"] == "Figma"
    assert inputs["job_description"] == "Kubernetes, Go" and inputs["resume"] == RESUME


@pytest.mark.parametrize("raw, exc", [
    (TAILORED + "\n- 10 years of Rust", None),      # invented a number → rejected
    ("Ada", None),                                  # truncated answer → rejected
    (None, RuntimeError("provider 500")),           # LLM call failed
])
def test_flow_tailor_returns_empty_so_the_file_is_uploaded(mocker, raw, exc):
    _crew_returns(mocker, raw=raw, exc=exc)
    captured, _ = _gh_flow(mocker, tailor_resume=True)
    assert captured["tailor_resume_fn"](RESUME, JOB_DATA) == ""


def test_flow_no_job_description_skips_the_llm(mocker):
    kickoff = _crew_returns(mocker, raw=TAILORED)
    captured, _ = _gh_flow(mocker, tailor_resume=True)
    assert captured["tailor_resume_fn"](RESUME, {"title": "X"}) == ""
    kickoff.assert_not_called()


def test_flow_no_llm_available_disables_tailoring(mocker):
    from src.automation.flows.greenhouse_apply_flow import GreenhouseApplyFlow
    captured = {}
    mocker.patch("src.automation.flows.greenhouse_apply_flow.run_greenhouse_apply",
                 side_effect=lambda **kw: captured.update(kw) or {
                     "applied": [], "skipped": [], "jobs_found": 0, "summary": "none"})
    mocker.patch("src.automation.harness.provider.resolve", side_effect=RuntimeError("no key"))
    GreenhouseApplyFlow().kickoff(inputs={"boards": "figma", "first_name": "Ada",
                                          "last_name": "L", "email": "a@x.io",
                                          "tailor_resume": True})
    assert captured["tailor_resume_fn"] is None


def test_ats_flow_wires_tailoring_too(mocker):
    from src.automation.flows.ats_apply_flow import ATSApplyFlow
    _crew_returns(mocker, raw=TAILORED)
    captured, _ = _kickoff(mocker, "src.automation.flows.ats_apply_flow", ATSApplyFlow,
                           tailor_resume=True)
    assert captured["tailor_resume_fn"](RESUME, JOB_DATA) == TAILORED


# ── runner → planner, through the multi-ATS Greenhouse adapter ───────────────

class _Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Client:
    def __init__(self, routes):
        self.routes = routes

    def get(self, url, **_kw):
        for key, val in self.routes.items():
            if url.split("?")[0].endswith(key):
                return _Resp(val)
        return _Resp({}, 404)

    def close(self):
        pass


_RESUME_Q = {"label": "Resume/CV", "required": True, "fields": [
    {"name": "resume", "type": "input_file"}, {"name": "resume_text", "type": "textarea"}]}


@pytest.fixture
def ats_run(tmp_path, mocker):
    resume = tmp_path / "cv.txt"
    resume.write_text(RESUME, encoding="utf-8")
    sent = []
    mocker.patch("playwright.sync_api.sync_playwright")
    mocker.patch.object(ats, "launch_browser")
    mocker.patch.object(gh, "_apply_to_job", side_effect=lambda _p, job, answers, *_a: sent.append(
        answers) or {**job, "status": "prepared", "submitted": False})
    client = _Client({
        "/figma/jobs": {"jobs": [{"id": 7, "title": "Platform Engineer",
                                  "company_name": "Figma", "location": {"name": "Remote"}}]},
        "/figma/jobs/7": {**JOB_DATA, "location": {"name": "Remote"}, "questions": [_RESUME_Q]},
    })

    def run(**kw):
        out = ats.run_ats_apply(boards="greenhouse:figma", first_name="Ada", last_name="L",
                                email="a@x.io", resume_path=str(resume), client=client,
                                ledger_path=str(tmp_path / "ats.json"),
                                adapters={"greenhouse": GreenhouseAdapter()}, **kw)
        return out, sent
    mocker.patch.dict("os.environ", {"GREENHOUSE_LEDGER": str(tmp_path / "gh.json")})
    return run


def test_ats_runner_pastes_tailored_resume_on_greenhouse(ats_run):
    out, sent = ats_run(tailor_resume_fn=lambda r, j: f"{r}\n(for {j['title']})")
    assert [(a["field"], a["kind"]) for a in sent[0]] == [("resume_text", "paste")]
    assert sent[0][0]["value"].endswith("(for Platform Engineer)")
    assert len(out["applied"]) == 1


def test_ats_runner_falls_back_when_tailoring_fails(ats_run):
    def boom(_r, _j):
        raise RuntimeError("provider 500")
    out, sent = ats_run(tailor_resume_fn=boom)
    assert [(a["field"], a["kind"]) for a in sent[0]] == [("resume", "file")]
    assert len(out["applied"]) == 1


# ── the browser filler, against a real page ──────────────────────────────────

_FORM = """<!doctype html><form>
<div role="group">
  <input id="resume" type="file" class="visually-hidden">
  <button type="button" data-testid="resume-text"
          onclick="document.getElementById('resume_text').hidden = false">Enter manually</button>
  <textarea id="resume_text" hidden></textarea>
</div></form>"""
_FORM_NO_PASTE = '<!doctype html><form><input id="resume" type="file"></form>'


@pytest.fixture(scope="module")
def browser_page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001 — CI has no Chromium
            pytest.skip(f"Chromium unavailable: {exc}")
        yield browser.new_page()
        browser.close()


def _answers(resume_file):
    return [{"field": "resume_text", "kind": "paste", "value": TAILORED, "label": "Resume/CV",
             "fallback": {"field": "resume", "kind": "file", "value": str(resume_file),
                          "label": "Resume/CV"}}]


def test_browser_enter_manually_then_paste(browser_page, tmp_path):
    cv = tmp_path / "cv.txt"
    cv.write_text(RESUME, encoding="utf-8")
    browser_page.set_content(_FORM)
    answers = _answers(cv)
    assert gh._fill_form(browser_page, answers, lambda _m: None) == []
    assert browser_page.locator("#resume_text").input_value() == TAILORED
    assert browser_page.locator("#resume").evaluate("e => e.files.length") == 0
    assert answers[0]["kind"] == "paste"


def test_browser_without_paste_option_uploads_file(browser_page, tmp_path):
    cv = tmp_path / "cv.txt"
    cv.write_text(RESUME, encoding="utf-8")
    browser_page.set_content(_FORM_NO_PASTE)
    answers = _answers(cv)
    assert gh._fill_form(browser_page, answers, lambda _m: None) == []
    assert browser_page.locator("#resume").evaluate("e => e.files[0].name") == "cv.txt"
    assert answers[0]["kind"] == "file"
