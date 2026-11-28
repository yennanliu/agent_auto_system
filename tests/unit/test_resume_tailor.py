"""Tailored plain-text résumé (tailor_resume): text extraction, the safety
check on the LLM's rewrite, planner integration and — above all — that every
failure falls back to uploading the résumé file. No network, no browser."""
import pytest

from src.automation.tools import greenhouse_apply_tool as gh
from src.automation.tools import resume_tailor as rt

RESUME = """Ada Lovelace
ada@example.com

EXPERIENCE
Senior Backend Engineer, Acme (2019-2024)
- Built Go services handling 20000 requests per second
- Led migration to Kubernetes

Data Analyst, Initech (2016-2019)
- SQL reporting and dashboards

SKILLS
Go, Python, Kubernetes, PostgreSQL"""

TAILORED = """Ada Lovelace
ada@example.com

SKILLS
Kubernetes, Go, PostgreSQL, Python

EXPERIENCE
Senior Backend Engineer, Acme (2019-2024)
- Led the migration of backend services to Kubernetes
- Built Go services handling 20000 requests per second

Data Analyst, Initech (2016-2019)
- SQL reporting"""


def _pdf_with_text(text: str) -> bytes:
    """A minimal one-page PDF whose content stream draws ``text``."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


# ── load_resume_text ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("ext", [".txt", ".md"])
def test_load_text_resume(tmp_path, ext):
    p = tmp_path / f"cv{ext}"
    p.write_text("Ada   Lovelace\n\n\n\nGo  engineer  \n", encoding="utf-8")
    assert rt.load_resume_text(str(p)) == "Ada Lovelace\n\nGo engineer"


def test_load_pdf_resume(tmp_path):
    p = tmp_path / "cv.pdf"
    p.write_bytes(_pdf_with_text("Ada Lovelace Backend Engineer"))
    assert "Ada Lovelace Backend Engineer" in rt.load_resume_text(str(p))


@pytest.mark.parametrize("name, data", [
    ("cv.pdf", b"%PDF"),            # corrupt PDF
    ("cv.docx", b"PK\x03\x04"),     # unsupported format
])
def test_load_unreadable_resume_is_empty(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    assert rt.load_resume_text(str(p)) == ""


def test_load_missing_resume_is_empty(tmp_path):
    assert rt.load_resume_text(str(tmp_path / "nope.txt")) == ""
    assert rt.load_resume_text("") == ""


# ── job description ───────────────────────────────────────────────────────────

def test_job_description_unescapes_greenhouse_html():
    content = ("&lt;p&gt;We need a &lt;strong&gt;Go&lt;/strong&gt; engineer.&lt;/p&gt;"
               "&lt;ul&gt;&lt;li&gt;Kubernetes&lt;/li&gt;&lt;li&gt;R&amp;amp;D&lt;/li&gt;&lt;/ul&gt;")
    assert rt.job_description({"content": content}) == \
        "We need a Go engineer.\n\n- Kubernetes\n- R&D"
    assert rt.job_description({}) == ""


# ── accept(): the guard on the LLM's rewrite ──────────────────────────────────

def test_accept_good_rewrite_and_strips_fences():
    assert rt.accept(TAILORED, RESUME) == (TAILORED, "")
    assert rt.accept(f"```text\n{TAILORED}\n```", RESUME) == (TAILORED, "")


@pytest.mark.parametrize("bad, why", [
    ("", "empty"),
    ("Ada Lovelace\nGo engineer", "too short"),
    (TAILORED + "\n- 8+ years of Rust", "numbers not in the original résumé: 8"),
    (TAILORED.replace("2016", "2014"), "2014"),
])
def test_accept_rejects_empty_short_and_invented_numbers(bad, why):
    text, reason = rt.accept(bad, RESUME)
    assert text == "" and why in reason


# ── bind_tailor / tailored_text ───────────────────────────────────────────────

def test_bind_tailor_off_returns_none(tmp_path):
    assert rt.bind_tailor(None, str(tmp_path / "cv.txt"), lambda _m: None, []) is None


def test_bind_tailor_unreadable_resume_warns_and_disables(tmp_path):
    p = tmp_path / "cv.pdf"
    p.write_bytes(b"%PDF")
    warnings, logs = [], []
    assert rt.bind_tailor(lambda r, j: "x", str(p), logs.append, warnings) is None
    assert "uploading the file" in warnings[0] and logs


def test_bind_tailor_binds_resume_text(tmp_path):
    p = tmp_path / "cv.txt"
    p.write_text(RESUME, encoding="utf-8")
    seen = {}
    fn = rt.bind_tailor(lambda r, j: seen.update(resume=r, job=j) or "out", str(p),
                        lambda _m: None, [])
    assert fn({"title": "SRE"}) == "out"
    assert seen == {"resume": RESUME, "job": {"title": "SRE"}}


def test_tailored_text_never_raises():
    def boom(_job):
        raise RuntimeError("LLM down")
    assert rt.tailored_text({}, {}) == ""
    assert rt.tailored_text({"tailor_resume": None}, {}) == ""
    assert rt.tailored_text({"tailor_resume": boom}, {}) == ""
    assert rt.tailored_text({"tailor_resume": lambda _j: None}, {}) == ""
    assert rt.tailored_text({"tailor_resume": lambda _j: "  cv  "}, {}) == "cv"


# ── plan_answers: paste when offered, file otherwise ─────────────────────────

_PROFILE = {"first_name": "Ada", "last_name": "L", "email": "a@x.io", "phone": "",
            "location": "", "resume_path": "/tmp/cv.pdf", "cover_letter_path": None,
            "extra_answers": []}
_RESUME_Q = {"label": "Resume/CV", "required": True, "fields": [
    {"name": "resume", "type": "input_file"}, {"name": "resume_text", "type": "textarea"}]}


def _plan(tailor, questions=(_RESUME_Q,)):
    profile = {**_PROFILE, "tailor_resume": tailor}
    return gh.plan_answers({"title": "SRE", "content": "Go", "questions": list(questions)}, profile)


def test_plan_pastes_tailored_resume_when_form_offers_text():
    answers, unanswered = _plan(lambda job: f"tailored for {job['title']}")
    assert unanswered == []
    [a] = answers
    assert (a["field"], a["kind"], a["value"]) == ("resume_text", "paste", "tailored for SRE")
    # The original upload rides along so the filler can fall back to it.
    assert a["fallback"] == {"field": "resume", "kind": "file", "value": "/tmp/cv.pdf",
                             "label": "Resume/CV"}


@pytest.mark.parametrize("tailor", [
    None,                                   # tailoring off
    lambda _j: "",                          # LLM abstained / output rejected
    lambda _j: (_ for _ in ()).throw(RuntimeError("LLM down")),  # LLM raised
])
def test_plan_falls_back_to_file_upload(tailor):
    answers, unanswered = _plan(tailor)
    assert unanswered == []
    assert [(a["field"], a["kind"], a["value"]) for a in answers] == \
        [("resume", "file", "/tmp/cv.pdf")]


def test_plan_without_paste_option_never_calls_tailor():
    calls = []
    only_file = {"label": "Resume/CV", "required": True,
                 "fields": [{"name": "resume", "type": "input_file"}]}
    answers, _ = _plan(lambda j: calls.append(j) or "x", [only_file])
    assert calls == [] and answers[0]["kind"] == "file"


def test_plan_skipped_job_costs_no_llm_call():
    calls = []
    why_us = {"label": "Why us?", "required": True,
              "fields": [{"name": "q1", "type": "textarea"}]}
    _answers, unanswered = _plan(lambda j: calls.append(j) or "x", [_RESUME_Q, why_us])
    assert unanswered == ["Why us?"] and calls == []


# ── _fill_form: the paste box, and the fallback when it isn't there ──────────

class _Loc:
    def __init__(self, page, sel):
        self.page, self.sel = page, sel
        self.first = self

    def count(self):
        return 1 if self.page.exists(self.sel) else 0

    def is_visible(self):
        return self.page.exists(self.sel) and self.sel not in self.page.hidden

    def click(self, timeout=None):
        self.page.actions.append(("click", self.sel))
        self.page.hidden.discard(self.page.reveals.get(self.sel))

    def wait_for(self, state=None, timeout=None):
        if not self.is_visible():
            raise TimeoutError(self.sel)

    def fill(self, value, timeout=None):
        self.page.actions.append(("fill", self.sel, value))

    def set_input_files(self, value, timeout=None):
        self.page.actions.append(("upload", self.sel, value))


class _Page:
    """Just enough of a Playwright page for the résumé field."""
    def __init__(self, present, hidden=(), reveals=None):
        self.present, self.hidden = set(present), set(hidden)
        self.reveals, self.actions = reveals or {}, []

    def exists(self, sel):
        return sel in self.present

    def locator(self, sel):
        return _Loc(self, sel)

    def wait_for_timeout(self, _ms):
        pass

    def evaluate(self, *_a):
        return 0


_BOX, _BTN, _FILE = "[id='resume_text']", "[data-testid='resume-text']", \
    "input[type='file'][id='resume']"


def _paste_answers():
    return [{"field": "resume_text", "kind": "paste", "value": "TAILORED", "label": "Resume/CV",
             "fallback": {"field": "resume", "kind": "file", "value": "/tmp/cv.pdf",
                          "label": "Resume/CV"}}]


def test_fill_clicks_enter_manually_then_pastes():
    page = _Page({_BOX, _BTN, _FILE}, hidden={_BOX}, reveals={_BTN: _BOX})
    answers = _paste_answers()
    assert gh._fill_form(page, answers, lambda _m: None) == []
    assert page.actions == [("click", _BTN), ("fill", _BOX, "TAILORED")]
    assert answers[0]["kind"] == "paste"


def test_fill_pastes_into_an_already_visible_box():
    page = _Page({_BOX, _FILE})
    assert gh._fill_form(page, _paste_answers(), lambda _m: None) == []
    assert page.actions == [("fill", _BOX, "TAILORED")]


@pytest.mark.parametrize("present, hidden", [
    ({_FILE}, set()),                        # no paste option on the live form
    ({_BOX, _BTN, _FILE}, {_BOX}),           # the button doesn't reveal the box
])
def test_fill_falls_back_to_file_upload(present, hidden):
    page = _Page(present, hidden=hidden)   # no `reveals`: clicking does nothing
    answers, logs = _paste_answers(), []
    assert gh._fill_form(page, answers, logs.append) == []
    assert page.actions[-1] == ("upload", _FILE, "/tmp/cv.pdf")
    assert not any(a[0] == "fill" for a in page.actions)
    assert answers[0]["kind"] == "file"     # the result reports what was really sent
    assert any("uploading the file instead" in m for m in logs)


# ── runner: tailoring is bound once per run, from the résumé's text ──────────

class _Resp:
    def __init__(self, data):
        self._data, self.status_code = data, 200

    def json(self):
        return self._data

    def raise_for_status(self):
        pass


class _Client:
    def __init__(self, routes):
        self.routes = routes

    def get(self, url, params=None):
        return _Resp(next(v for k, v in self.routes.items() if url.endswith(k)))

    def close(self):
        pass


def _gh_routes():
    return {"/a/jobs": {"jobs": [{"id": 1, "title": "Backend SRE", "location": {"name": "Remote"},
                                  "company_name": "A", "absolute_url": "u"}]},
            "/a/jobs/1": {"title": "Backend SRE", "company_name": "A",
                          "content": "&lt;p&gt;Go, Kubernetes&lt;/p&gt;", "questions": [_RESUME_Q]}}


@pytest.fixture
def gh_run(tmp_path, mocker):
    sent = []
    mocker.patch.object(gh, "_launch_browser")
    mocker.patch.object(gh, "_apply_to_job", side_effect=lambda _p, job, answers, *_a: sent.append(
        answers) or {**job, "status": "prepared"})

    def run(resume_name="cv.txt", resume_bytes=RESUME.encode(), **kw):
        resume = tmp_path / resume_name
        resume.write_bytes(resume_bytes)
        out = gh.run_greenhouse_apply(boards="a", first_name="Ada", last_name="L", email="a@x.io",
                                      resume_path=str(resume), client=_Client(_gh_routes()),
                                      ledger_path=str(tmp_path / "l.json"), **kw)
        return out, sent
    return run


def test_runner_pastes_tailored_resume(gh_run):
    seen = {}

    def tailor(resume_text, job_data):
        seen.update(resume=resume_text, title=job_data["title"])
        return "TAILORED CV"

    out, sent = gh_run(tailor_resume_fn=tailor)
    assert seen == {"resume": RESUME, "title": "Backend SRE"}
    assert [(a["field"], a["value"]) for a in sent[0]] == [("resume_text", "TAILORED CV")]
    assert len(out["applied"]) == 1


def test_runner_without_tailoring_uploads_file(gh_run):
    out, sent = gh_run()
    assert [a["kind"] for a in sent[0]] == ["file"]
    assert len(out["applied"]) == 1


def test_runner_unreadable_resume_uploads_file_and_warns(gh_run):
    calls = []
    out, sent = gh_run(resume_name="cv.pdf", resume_bytes=b"%PDF",
                       tailor_resume_fn=lambda r, j: calls.append(1) or "x")
    assert calls == [] and [a["kind"] for a in sent[0]] == ["file"]
    assert any("tailor_resume" in w for w in out["warnings"])


def test_runner_tailor_crash_still_applies_with_file(gh_run):
    def tailor(_r, _j):
        raise RuntimeError("provider 500")
    out, sent = gh_run(tailor_resume_fn=tailor)
    assert [a["kind"] for a in sent[0]] == ["file"]
    assert len(out["applied"]) == 1
