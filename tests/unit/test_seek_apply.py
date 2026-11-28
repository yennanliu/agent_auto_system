"""Seek / JobStreet Quick Apply (seek_apply).

Fixtures under tests/fixtures/seek/ are synthetic. ``job_details_*.json`` follow
the shape of Seek's public GraphQL ``jobDetails`` answer (checked live, Nov
2026) with invented companies and ids. ``wizard_questions.html`` and the inline
wizard pages below are NOT recordings: they model the Quick Apply wizard as
expected, because no logged-in Seek session was available when the adapter was
written. Replace them with trimmed real pages after the first live dry run
(doc/seek_apply.md, "Needs a live check").
"""
import json
from pathlib import Path

import pytest

from src.automation.tools import seek_apply_tool as sk
from src.automation.tools.ats_common import Question, build_profile, load_ledger, with_job_location

FIX = Path(__file__).resolve().parents[1] / "fixtures"


def _load(name):
    return (FIX / "seek" / name).read_text(encoding="utf-8")


def _profile(**kw):
    base = dict(first_name="", last_name="", email="", years_experience=5,
                authorized_countries="Taiwan", notice_period="1 month",
                programming_languages="Python, Java", languages="English")
    base.update(kw)
    return build_profile(**base)


@pytest.fixture(autouse=True)
def _no_default_answers(monkeypatch, tmp_path):
    # The candidate's own data/default_answers.txt must not leak into tests.
    monkeypatch.setenv("ATS_DEFAULT_ANSWERS_FILE", str(tmp_path / "none.txt"))


# ── markets ──────────────────────────────────────────────────────────────────

def test_resolve_site_codes_names_and_sources():
    assert sk.resolve_site("AU") == "au"
    assert sk.resolve_site("New Zealand") == "nz"
    assert sk.resolve_site("jobstreet_sg") == "sg"
    assert sk.resolve_site("jobsdb_hk") is None
    info = sk.site_info("my")
    assert info["base"] == "https://my.jobstreet.com" and info["site_key"] == "MY-Main"
    assert info["session"] == "jobstreet" and info["country"] == "Malaysia"
    assert sk.site_info("nz")["session"] == "seek"


def test_parse_seek_jobs_now_carries_the_job_id():
    from src.automation.tools.ats_sources import parse_seek_jobs
    data = json.loads((FIX / "ats" / "jobstreet_sg.json").read_text(encoding="utf-8"))
    jobs = parse_seek_jobs(data, "https://sg.jobstreet.com", "Singapore")
    assert jobs and all(j["url"].endswith("/job/" + j["job_id"]) for j in jobs)


# ── job details (GraphQL) ────────────────────────────────────────────────────

def test_parse_job_details_quick_apply():
    d = sk.parse_job_details(json.loads(_load("job_details_quick_apply.json")))
    assert d["job_id"] == "90000001" and d["company"] == "Example Optics Pty Ltd"
    assert d["link_out"] is False and d["expired"] is False
    assert len(d["questions"]) == 5 and d["questions"][2].startswith("Which of the following statements")


def test_parse_job_details_link_out_and_unknown():
    d = sk.parse_job_details(json.loads(_load("job_details_link_out.json")))
    assert d["link_out"] is True and d["questions"] == []
    assert sk.parse_job_details(json.loads(_load("job_details_unknown.json"))) is None
    assert sk.parse_job_details({}) is None


def test_inactive_status_counts_as_expired():
    data = json.loads(_load("job_details_quick_apply.json"))
    data["data"]["jobDetails"]["job"]["status"] = "Expired"
    assert sk.parse_job_details(data)["expired"] is True


class _Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_fetch_job_details_is_a_get_with_query_and_variables():
    calls = []

    class C:
        def get(self, url, params=None):
            calls.append((url, params))
            return _Resp(json.loads(_load("job_details_quick_apply.json")))

    d = sk.fetch_job_details(C(), "https://au.seek.com", "90000001")
    assert d["job_id"] == "90000001"
    url, params = calls[0]
    assert url == "https://au.seek.com/graphql"
    assert json.loads(params["variables"]) == {"jobId": "90000001"}
    assert "questionnaire" in params["query"] and "isLinkOut" in params["query"]
    assert "mutation" not in params["query"]


def test_offsite_ats_url_takes_the_apply_link_not_the_policy_link():
    content = sk.parse_job_details(json.loads(_load("job_details_link_out.json")))["content"]
    assert sk.offsite_ats_url(content) == "https://jobs.lever.co/exampleuni/0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"
    # A board link is every opening at the company, not this job.
    assert sk.offsite_ats_url('<p>Apply at <a href="https://jobs.lever.co/exampleuni">our board</a></p>') == ""
    assert sk.offsite_ats_url("<p>No links here.</p>") == ""


# ── step classification ──────────────────────────────────────────────────────

def _page(h1, extra=""):
    return f"<html><head><title>SEEK</title></head><body><h1>{h1}</h1>{extra}</body></html>"


@pytest.mark.parametrize("url,html,step", [
    ("https://au.seek.com/job/1/apply", _page("Choose documents"), "documents"),
    ("https://au.seek.com/job/1/apply/role-requirements", _page("x"), "questions"),
    ("https://au.seek.com/job/1/apply/profile", _page("x"), "profile"),
    ("https://au.seek.com/job/1/apply/review", _page("x"), "review"),
    ("https://au.seek.com/job/1/apply/success", _page("x"), "success"),
    # Path not recognised → the step heading decides.
    ("https://sg.jobstreet.com/job/1/apply/step-2", _page("Answer employer questions"), "questions"),
    ("https://sg.jobstreet.com/job/1/apply/step-4", _page("Review and submit"), "review"),
    ("https://login.seek.com/login?x=1", _page("Sign in"), "login"),
    ("https://au.seek.com/oauth/login?returnUrl=/job/1/apply", _page("Sign in"), "login"),
    ("https://au.seek.com/job/1/apply",
     "<html><head><title>Just a moment...</title></head><body></body></html>", "challenge"),
    ("https://au.seek.com/job/1/apply", _page("You've already applied for this job"), "already_applied"),
    ("https://au.seek.com/job/1", _page("Oops", "<p>This job is no longer advertised.</p>"), "closed"),
    ("https://au.seek.com/job/1/apply/step-x", _page("Tell us about your weekend"), "unknown"),
])
def test_classify_step(url, html, step):
    assert sk.classify_step(url, html) == step


def test_questions_fixture_is_classified_by_its_heading_too():
    assert sk.classify_step("https://au.seek.com/job/1/apply/next", _load("wizard_questions.html")) == "questions"


# ── reading employer questions ───────────────────────────────────────────────

def test_read_questions_from_the_wizard_page():
    qs = {q.key: q for q in sk.read_questions(_load("wizard_questions.html"))}
    assert set(qs) == {"select|id=q-years", "radio|name=q-rtw", "checkbox|name=q-lang",
                       "select|id=q-salary", "select|id=q-notice", "text|id=q-proud"}
    years = qs["select|id=q-years"]
    assert years.kind == "select" and years.required
    assert years.options[0] == "No experience" and "Select an option" not in years.options
    rtw = qs["radio|name=q-rtw"]
    assert rtw.label.startswith("Which of the following statements best describes your right to work")
    assert rtw.kind == "select" and len(rtw.options) == 5 and rtw.required
    lang = qs["checkbox|name=q-lang"]
    assert lang.kind == "multiselect" and lang.options == ["C#", "Python", "Java", "Go"]
    proud = qs["text|id=q-proud"]
    assert proud.label == "Tell us about a project you are proud of" and proud.required is False


def test_lone_required_checkbox_is_a_one_option_question():
    html = ('<form><label><input type="checkbox" name="ok" required> I confirm my answers are true'
            '</label></form>')
    (q,) = sk.read_questions(html)
    assert q.key == "checkbox|name=ok" and q.kind == "select" and q.required
    assert q.options == ["I confirm my answers are true"]


# ── answer planning ──────────────────────────────────────────────────────────

_RTW = Question("radio|name=q-rtw",
                "Which of the following statements best describes your right to work in Australia?",
                "select", True,
                ["I'm an Australian citizen", "I'm a permanent resident and/or NZ citizen",
                 "I have a family/partner visa with no restrictions",
                 "I require sponsorship to work for a new employer (e.g. 482, 457)"])


def test_right_to_work_statement_not_authorized_picks_sponsorship():
    p = with_job_location(_profile(), "Melbourne VIC, Australia")
    answers, unanswered = sk.plan_seek_questions([_RTW], p, "Australia")
    assert unanswered == []
    assert answers[0]["value"].startswith("I require sponsorship")


def test_right_to_work_statement_never_falls_into_the_no_restrictions_trap():
    # The shared planner alone would map an inferred "No" onto "…visa with NO
    # restrictions"; Seek's rule must keep this question unanswered instead.
    p = with_job_location(_profile(authorized_countries="Australia"), "Sydney NSW, Australia")
    answers, unanswered = sk.plan_seek_questions([_RTW], p, "Australia")
    assert answers == [] and unanswered == [_RTW.label]


def test_right_to_work_statement_from_the_users_extra_answer():
    p = with_job_location(_profile(authorized_countries="Australia",
                                   extra_answers="right to work: Australian citizen"), "Sydney, Australia")
    answers, _ = sk.plan_seek_questions([_RTW], p, "Australia")
    assert answers[0]["value"] == "I'm an Australian citizen"


def test_yes_no_right_to_work_is_left_to_the_shared_planner():
    q = Question("select|id=w", "Do you have the right to work in New Zealand?", "select", True, ["Yes", "No"])
    p = with_job_location(_profile(), "Auckland, New Zealand")
    answers, unanswered = sk.plan_seek_questions([q], p, "New Zealand")
    assert unanswered == [] and answers[0]["value"] == "No"


def test_notice_and_salary_bands():
    notice = Question("select|id=n", "How much notice are you required to give your current employer?",
                      "select", True, ["None, I'm ready to go now", "2 weeks", "1 month", "2 months"])
    salary = Question("select|id=s", "What's your expected annual base salary?", "select", True,
                      ["$80k", "$100k", "$120k", "$140k+"])
    answers, unanswered = sk.plan_seek_questions([notice, salary], _profile(salary_expectation="105000"), "Australia")
    assert unanswered == []
    assert [a["value"] for a in answers] == ["1 month", "$100k"]
    # No salary expectation → nothing to pick from → unanswered (job skipped).
    _, unanswered = sk.plan_seek_questions([salary], _profile(), "Australia")
    assert unanswered == [salary.label]


def test_salary_option_parsing():
    assert sk.salary_option(["$80k", "$90k", "$100k"], "92000 AUD / year") == "$90k"
    assert sk.salary_option(["$80,000 – $90,000", "$100,000+"], "120000") == "$100,000+"
    assert sk.salary_option(["Yes", "No"], "100000") is None
    assert sk.salary_option(["$80k"], "") is None


def test_unanswerable_required_question_is_reported_and_llm_only_when_given():
    q = Question("select|id=d", "Do you have a current Australian driver's licence?", "select", True, ["Yes", "No"])
    _, unanswered = sk.plan_seek_questions([q], _profile(), "Australia")
    assert unanswered == [q.label]
    answers, unanswered = sk.plan_seek_questions([q], _profile(), "Australia",
                                                 answer_fn=lambda _l, _k, _o: "Yes")
    assert unanswered == [] and answers[0]["value"] == "Yes"


def test_whole_fixture_page_plans_except_the_right_to_work_statement():
    qs = sk.read_questions(_load("wizard_questions.html"))
    p = with_job_location(_profile(salary_expectation="118000"), "Melbourne VIC, Australia")
    answers, unanswered = sk.plan_seek_questions(qs, p, "Australia")
    by = {a["field"]: a["value"] for a in answers}
    assert by["radio|name=q-rtw"].startswith("I require sponsorship")    # Taiwan only → sponsorship
    assert by["checkbox|name=q-lang"] == ["Python", "Java"]
    assert by["select|id=q-salary"] == "$120k" and by["select|id=q-notice"] == "1 month"
    # "years as a software engineer" is skill-specific: the shared rules don't guess it.
    assert unanswered == ["How many years' experience do you have as a software engineer?"]


def test_prescreen_flags_questions_the_rules_refuse_to_guess():
    p = with_job_location(_profile(), "")  # job country unknown
    assert sk.prescreen(["Are you authorized to work in the country this job is in?"], p)
    assert sk.prescreen(["How much notice do you need?"], p) == []
    known = with_job_location(_profile(), "Sydney NSW, Australia")
    assert sk.prescreen(["Are you authorized to work in the country this job is in?"], known) == []


# ── the wizard walker ────────────────────────────────────────────────────────

URL = "https://au.seek.com/job/90000001/apply"
DOCS = (URL, _page("Choose documents"))
QS_EASY = (URL + "/role-requirements", """<html><body><h1>Answer employer questions</h1><form>
  <label for="n">How much notice are you required to give your current employer?</label>
  <select id="n"><option value="">Select</option><option value="1">2 weeks</option>
  <option value="2">1 month</option></select></form></body></html>""")
QS_HARD = (URL + "/role-requirements", """<html><body><h1>Answer employer questions</h1><form>
  <label for="d">Do you have a current Australian driver's licence?</label>
  <select id="d"><option value="">Select</option><option value="y">Yes</option>
  <option value="n">No</option></select></form></body></html>""")
PROFILE = (URL + "/profile", _page("Update SEEK Profile"))
REVIEW = (URL + "/review", _page("Review and submit"))


class FakeDriver:
    """Serves wizard pages in order; ``continue`` advances one page."""

    def __init__(self, pages, success=True, errors_on=None):
        self.pages, self.i, self.success = list(pages), 0, success
        self.clicks, self.filled, self.documents, self.opened = [], [], [], []

    def open(self, job):
        self.opened.append(job["job_id"])
        self.i = 0

    def snapshot(self):
        return self.pages[min(self.i, len(self.pages) - 1)]

    def choose_documents(self, *, resume_name, cover_letter):
        self.documents.append((resume_name, cover_letter))
        return ["cover letter: written"] if cover_letter else []

    def fill(self, answers):
        self.filled.extend(answers)

    def click(self, what):
        self.clicks.append(what)
        if what == "continue":
            self.i += 1
        return True

    def wait_success(self):
        return self.success


def _plan(qs):
    return sk.plan_seek_questions(qs, _profile(), "Australia")


JOB = {"job_id": "90000001", "url": "https://au.seek.com/job/90000001", "title": "Python Engineer",
       "company": "Example Optics", "location": "Melbourne VIC, Australia"}


def test_dry_run_walks_to_review_and_never_submits():
    d = FakeDriver([DOCS, QS_EASY, PROFILE, REVIEW])
    e = sk.walk_wizard(d, JOB, _plan, cover_letter="Hi {company}, re {title}", dry_run=True)
    assert e["status"] == "prepared" and e["submitted"] is False
    assert "submit" not in d.clicks and d.clicks == ["continue"] * 3
    assert d.documents == [("", "Hi Example Optics, re Python Engineer")]
    assert d.filled[0]["value"] == "1 month"
    assert e["answers"] == [{"label": "How much notice are you required to give your current employer?",
                             "value": "1 month"}]


def test_live_run_submits_after_marking_pending():
    order = []
    d = FakeDriver([DOCS, QS_EASY, PROFILE, REVIEW], success=True)
    orig_click = d.click
    d.click = lambda what: order.append(what) or orig_click(what)
    e = sk.walk_wizard(d, JOB, _plan, dry_run=False, on_submit=lambda: order.append("pending") or True)
    assert e["status"] == "submitted" and e["submitted"] is True
    assert order[-2:] == ["pending", "submit"]           # ledgered BEFORE the click


def test_lost_claim_never_clicks_submit():
    d = FakeDriver([DOCS, REVIEW])
    e = sk.walk_wizard(d, JOB, _plan, dry_run=False, on_submit=lambda: False)
    assert e["status"] == "skipped" and "submit" not in d.clicks


def test_submit_without_success_page_is_unconfirmed():
    e = sk.walk_wizard(FakeDriver([DOCS, REVIEW], success=False), JOB, _plan, dry_run=False,
                       on_submit=lambda: True)
    assert e["status"] == "unconfirmed" and e["submitted"] is False


def test_unanswerable_question_skips_before_filling():
    d = FakeDriver([DOCS, QS_HARD, PROFILE, REVIEW])
    e = sk.walk_wizard(d, JOB, _plan, dry_run=False, on_submit=lambda: True)
    assert e["status"] == "skipped" and e["unanswered"] == ["Do you have a current Australian driver's licence?"]
    assert d.filled == [] and "submit" not in d.clicks


@pytest.mark.parametrize("page,status", [
    (("https://login.seek.com/u/login", _page("Sign in")), "session_expired"),
    ((URL, "<html><head><title>Just a moment...</title></head></html>"), "blocked"),
    ((URL + "/weird", _page("Upload your selfie")), "skipped"),
    ((URL, _page("You have already applied for this role")), "skipped"),
])
def test_wizard_stops_on_login_challenge_unknown_and_already_applied(page, status):
    d = FakeDriver([page])
    e = sk.walk_wizard(d, JOB, _plan, dry_run=False, on_submit=lambda: True)
    assert e["status"] == status and "submit" not in d.clicks


def test_unknown_step_is_flagged_for_a_warning():
    e = sk.walk_wizard(FakeDriver([(URL + "/weird", _page("Upload your selfie"))]), JOB, _plan)
    assert e["unknown_step"] is True and "layout changed" in e["reason"]


def test_questions_page_that_will_not_advance_is_skipped_with_its_errors():
    stuck = (QS_EASY[0], QS_EASY[1].replace("</form>", '<p role="alert">Please select an option</p></form>'))

    class Stuck(FakeDriver):
        def click(self, what):
            self.clicks.append(what)
            if what == "continue" and self.i == 0:
                self.i += 1
            return True

    e = sk.walk_wizard(Stuck([DOCS, stuck]), JOB, _plan, dry_run=True)
    assert e["status"] == "skipped" and "Please select an option" in e["reason"]


# ── the run (search → details → wizard), with fakes ──────────────────────────

def _search_payload(jobs):
    return {"data": [{"id": j[0], "title": j[1], "companyName": "Example Co",
                      "locations": [{"label": "Sydney NSW"}], "teaser": "t"} for j in jobs]}


def _details(job_id, *, link_out=False, expired=False, questions=(), content=""):
    return {"data": {"jobDetails": {"job": {
        "id": job_id, "title": "x", "isExpired": expired, "isLinkOut": link_out, "status": "Active",
        "advertiser": {"name": "Example Co"}, "location": {"label": "Sydney NSW"}, "content": content,
        "products": {"questionnaire": {"questions": list(questions)}}}}}}


class FakeClient:
    def __init__(self, jobs, details=None):
        self.jobs, self.details, self.calls = jobs, details or {}, []

    def get(self, url, params=None):
        self.calls.append((url, params))
        if url.endswith("/api/jobsearch/v5/search"):
            return _Resp(_search_payload(self.jobs) if params["page"] == 1 else {"data": []})
        if url.endswith("/graphql"):
            jid = json.loads(params["variables"])["jobId"]
            return _Resp(self.details.get(jid) or _details(jid))
        return _Resp({}, 404)

    def close(self):
        pass


def _run(tmp_path, jobs, driver=None, **kw):
    driver = driver if driver is not None else FakeDriver([DOCS, QS_EASY, PROFILE, REVIEW])
    client = kw.pop("client", None) or FakeClient(jobs, kw.pop("details", None))
    kw.setdefault("profile", _profile())
    out = sk.run_seek_apply(keywords=kw.pop("keywords", "python engineer"), site=kw.pop("site", "au"),
                            client=client, wizard_factory=kw.pop("wizard_factory", lambda: driver),
                            ledger_path=str(tmp_path / "seek_applied.json"), **kw)
    return out, driver, client


def test_run_dry_run_prepares_and_leaves_no_ledger(tmp_path):
    out, d, _ = _run(tmp_path, [("1", "Python Engineer"), ("2", "Senior Python Engineer")])
    assert out["applied_count"] == 2 and all(e["status"] == "prepared" for e in out["applied"])
    assert out["jobs_found"] == 2 and d.opened == ["1", "2"]
    assert load_ledger(str(tmp_path / "seek_applied.json")) == {}


def test_run_filters_titles_and_caps_applications(tmp_path):
    jobs = [("1", "Python Engineer"), ("2", "Marketing Manager"), ("3", "Python Engineer II"),
            ("4", "Lead Python Engineer")]
    out, d, _ = _run(tmp_path, jobs, max_applications=2)
    assert out["jobs_found"] == 3                    # the marketing job's title doesn't match
    assert d.opened == ["1", "3"] and out["applied_count"] == 2


def test_run_live_submit_is_ledgered_and_never_repeated(tmp_path):
    out, _, _ = _run(tmp_path, [("7", "Python Engineer")], dry_run=False)
    assert out["submitted_count"] == 1
    assert load_ledger(str(tmp_path / "seek_applied.json"))["seek:7"]["status"] == "applied"
    again, d2, _ = _run(tmp_path, [("7", "Python Engineer")], dry_run=False)
    assert again["applied_count"] == 0 and d2.opened == []
    assert again["skipped"][0]["reason"] == "already applied (ledger)"


def test_run_unconfirmed_submit_is_pending_and_never_resent(tmp_path):
    out, _, _ = _run(tmp_path, [("8", "Python Engineer")], dry_run=False,
                     driver=FakeDriver([DOCS, REVIEW], success=False))
    assert out["unconfirmed_count"] == 1 and out["submitted_count"] == 0
    assert load_ledger(str(tmp_path / "seek_applied.json"))["seek:8"]["status"] == "pending"
    again, d2, _ = _run(tmp_path, [("8", "Python Engineer")], dry_run=False)
    assert d2.opened == [] and "never confirmed" in again["skipped"][0]["reason"]


def test_run_exception_after_submit_click_stays_pending(tmp_path):
    class Boom(FakeDriver):
        def click(self, what):
            if what == "submit":
                raise RuntimeError("browser crashed")
            return super().click(what)

    out, _, _ = _run(tmp_path, [("9", "Python Engineer")], dry_run=False, driver=Boom([DOCS, REVIEW]))
    assert out["skipped"][0]["status"] == "failed"
    assert load_ledger(str(tmp_path / "seek_applied.json"))["seek:9"]["status"] == "pending"


def test_run_skips_expired_and_routes_link_outs(tmp_path):
    content = json.loads(_load("job_details_link_out.json"))["data"]["jobDetails"]["job"]["content"]
    details = {"2": _details("2", expired=True), "3": _details("3", link_out=True, content=content),
               "4": _details("4", link_out=True, content="<p>Apply on our website.</p>")}
    calls = []

    def ats(*, boards, max_applications):
        calls.append((boards, max_applications))
        return {"applied": [{"job_id": "lever-1", "status": "prepared"}], "skipped": [], "manual": [],
                "warnings": []}

    class NoLinkOut(FakeDriver):
        def resolve_link_out(self, job):
            return "https://careers.example.com/jobs/4"

    out, d, _ = _run(tmp_path, [("1", "Python Engineer"), ("2", "Python Engineer"), ("3", "Python Engineer"),
                                ("4", "Python Engineer")], details=details, ats_apply_fn=ats,
                     max_applications=5, driver=NoLinkOut([DOCS, REVIEW]))
    assert d.opened == ["1"]                                     # only the Quick Apply job
    assert [s["reason"] for s in out["skipped"]] == ["job expired"]
    assert calls == [("https://jobs.lever.co/exampleuni/0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b", 4)]
    assert out["applied_count"] == 2 and out["applied"][1]["via"] == "ats_apply"
    assert out["manual_count"] == 1 and out["manual"][0]["apply_url"] == "https://careers.example.com/jobs/4"


def test_run_link_out_without_ats_routing_is_a_manual_lead(tmp_path):
    content = json.loads(_load("job_details_link_out.json"))["data"]["jobDetails"]["job"]["content"]
    out, d, _ = _run(tmp_path, [("3", "Python Engineer")],
                     details={"3": _details("3", link_out=True, content=content)})
    assert d.opened == [] and out["manual_count"] == 1
    assert "run ATS Apply with boards=https://jobs.lever.co/" in out["manual"][0]["reason"]


def test_run_stops_when_the_session_has_expired(tmp_path):
    login = FakeDriver([("https://login.seek.com/u/login", _page("Sign in"))])
    out, d, _ = _run(tmp_path, [("1", "Python Engineer"), ("2", "Python Engineer")], driver=login)
    assert d.opened == ["1"] and out["stopped"].startswith("Seek asked to sign in")
    assert any("run stopped early" in w for w in out["warnings"])


def test_run_browser_that_cannot_open_stops_once(tmp_path):
    opened = []

    def factory():
        opened.append(1)
        raise sk.SessionError("Not logged in to Seek")

    out, _, _ = _run(tmp_path, [("1", "Python Engineer"), ("2", "Python Engineer")], wizard_factory=factory)
    assert opened == [1] and out["stopped"] == "Not logged in to Seek"
    assert len(out["skipped"]) == 1 and out["skipped"][0]["status"] == "failed"


def test_run_relevance_gate_filters_and_fails_open(tmp_path):
    def gate(title, meta):
        if "Senior" in title:
            return False, "too senior"
        raise RuntimeError("judge down")

    out, d, _ = _run(tmp_path, [("1", "Senior Python Engineer"), ("2", "Python Engineer")], relevance_fn=gate)
    assert out["filtered_count"] == 1 and d.opened == ["2"]
    assert out["skipped"][0]["reason"] == "filtered out: too senior"


def test_run_prescreen_skips_without_opening_the_browser(tmp_path):
    # "…where you currently reside" with no current_location: the country can't
    # be told, so the shared rules refuse it — known before any browser opens.
    q = "Are you legally authorized to work in the country where you currently reside?"
    out, d, _ = _run(tmp_path, [("1", "Python Engineer")], details={"1": _details("1", questions=[q])})
    assert d.opened == [] and out["skipped"][0]["reason"].startswith("employer question we won't guess")
    assert out["skipped"][0]["unanswered"] == [q]


def test_run_input_errors_are_results_not_exceptions(tmp_path):
    assert "Unknown site" in sk.run_seek_apply(keywords="python", site="hk")["error"]
    assert "No keywords" in sk.run_seek_apply(keywords=" ", site="au")["error"]
    out = sk.run_seek_apply(keywords="python", site="sg", state_path=str(tmp_path / "missing.json"))
    assert "seek_login.py --site sg" in out["error"]


def test_search_pages_until_short_page_and_dedupes(tmp_path):
    class Paged(FakeClient):
        def get(self, url, params=None):
            if url.endswith("/api/jobsearch/v5/search"):
                self.calls.append((url, params))
                n = params["page"]
                ids = [(f"{n}{i:02d}", "Python Engineer") for i in range(50 if n == 1 else 3)]
                return _Resp(_search_payload(ids))
            return super().get(url, params)

    client = Paged([])
    out, _, _ = _run(tmp_path, [], client=client, max_pages=5, max_applications=1)
    pages = [p["page"] for u, p in client.calls if u.endswith("/search")]
    assert pages == [1, 2] and out["jobs_found"] == 53
    assert client.calls[0][1]["siteKey"] == "AU-Main"


# ── flow wiring ──────────────────────────────────────────────────────────────

def test_flow_requires_keywords_and_a_known_site():
    from src.automation.flows.seek_apply_flow import SeekApplyFlow
    with pytest.raises(Exception):
        SeekApplyFlow().kickoff(inputs={"keywords": ""})
    with pytest.raises(Exception):
        SeekApplyFlow().kickoff(inputs={"keywords": "python", "site": "hk"})


def test_flow_passes_args_and_routes_link_outs_only_with_contact_details(mocker):
    captured = {}
    mocker.patch("src.automation.flows.seek_apply_flow.run_seek_apply",
                 side_effect=lambda **kw: captured.update(kw) or {
                     "applied": [], "skipped": [], "jobs_found": 0, "summary": "none"})
    from src.automation.flows.seek_apply_flow import SeekApplyFlow

    SeekApplyFlow().kickoff(inputs={"keywords": "python", "site": "nz", "max_applications": 3,
                                    "dry_run": False, "notice_period": "2 weeks"})
    assert captured["site"] == "nz" and captured["dry_run"] is False and captured["max_applications"] == 3
    assert captured["profile"]["notice_period"] == "2 weeks"
    assert captured["relevance_fn"] is None and captured["ats_apply_fn"] is None   # no name / e-mail

    ats = mocker.patch("src.automation.tools.ats_apply_tool.run_ats_apply", return_value={"applied": []})
    SeekApplyFlow().kickoff(inputs={"keywords": "python", "first_name": "Ada", "last_name": "Lee",
                                    "email": "ada@example.com"})
    captured["ats_apply_fn"](boards="https://jobs.lever.co/x/0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b",
                             max_applications=2)
    kw = ats.call_args.kwargs
    assert kw["first_name"] == "Ada" and kw["max_applications"] == 2 and kw["dry_run"] is True


# ── registry / sessions ──────────────────────────────────────────────────────

def test_spec_and_sessions_are_registered():
    from src.automation import browser_session as bs
    from src.automation import spec
    s = spec.REGISTRY["seek_apply"]
    assert s.browser and not s.custom_ui and s.canary.kind == "http"
    fields = {f.name: f for f in s.fields}
    assert fields["dry_run"].default is True
    assert [o[0] for o in fields["site"].options] == list(sk.SITES)
    assert bs.get_spec("seek").default_state_path == "data/seek_state.json"
    assert bs.get_spec("jobstreet").state_env == "JOBSTREET_STORAGE_STATE"


def test_seek_logged_in_rejects_login_hosts_and_challenge():
    from src.automation.browser_session import _seek_logged_in

    class P:
        def __init__(self, url, title="SEEK", header=1, signin=False):
            self.url, self._title, self._header, self._signin = url, title, header, signin

        def title(self):
            return self._title

        def locator(self, sel):
            page = self

            class L:
                first = None

                def count(self):
                    return page._header if sel == "header" else (1 if page._signin else 0)

                def is_visible(self):
                    return page._signin
            loc = L()
            loc.first = loc
            return loc

    assert _seek_logged_in(P("https://login.seek.com/u/login")) is False
    assert _seek_logged_in(P("https://au.seek.com/", title="Just a moment...")) is False
    assert _seek_logged_in(P("https://au.seek.com/", signin=True)) is False
    assert _seek_logged_in(P("https://au.seek.com/", header=0)) is False
    assert _seek_logged_in(P("https://au.seek.com/")) is True
    assert _seek_logged_in(P("https://www.google.com/")) is False
