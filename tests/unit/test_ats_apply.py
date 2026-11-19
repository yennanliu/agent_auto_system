"""Multi-ATS apply (Phase 1): router, platform adapters, shared runner, flow.

Adapter tests run against trimmed REAL responses captured from each platform
(tests/fixtures/ats/): Lever's server-rendered apply form, Ashby's GraphQL form
definition, Workable's form API, and each public job-listing API. No network.
"""
import json
from datetime import date
from pathlib import Path

import pytest

from src.automation.tools import ats_apply_tool as ats
from src.automation.tools.ats_ashby import AshbyAdapter
from src.automation.tools.ats_common import (
    build_profile,
    rule_answer,
    standard_for_label,
    standard_value,
    start_date_from_notice,
)
from src.automation.tools.ats_greenhouse import GreenhouseAdapter
from src.automation.tools.ats_lever import LeverAdapter, parse_form
from src.automation.tools.ats_router import BoardRef, detect_ats, parse_board_ref
from src.automation.tools.ats_smartrecruiters import SmartRecruitersAdapter
from src.automation.tools.ats_workable import WorkableAdapter

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "ats"


def _fixture(name):
    text = (FIX / name).read_text(encoding="utf-8")
    return json.loads(text) if name.endswith(".json") else text


class _Resp:
    def __init__(self, data=None, status=200, text=""):
        self._data, self.status_code, self.text = data, status, text

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Client:
    """Routes by URL substring → (data | text). Records every call."""
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def _hit(self, method, url, **kw):
        self.calls.append((method, url, kw))
        for key, val in self.routes.items():
            if key in url:
                if isinstance(val, _Resp):
                    return val
                return _Resp(text=val) if isinstance(val, str) else _Resp(val)
        return _Resp({}, 404)

    def get(self, url, params=None):
        return self._hit("GET", url, params=params)

    def post(self, url, json=None):
        return self._hit("POST", url, json=json)

    def close(self):
        pass


PROFILE = build_profile(
    first_name="Ada", last_name="Lovelace", email="ada@example.com", phone="+1 415 555 0100",
    current_location="San Francisco, CA", authorized_countries="United States",
    linkedin_url="https://linkedin.com/in/ada", github_url="https://github.com/ada",
    notice_period="2 weeks", languages="English, Chinese", school="Stanford University",
    resume_path="/tmp/cv.pdf", extra_answers="office three days: Yes",
)


def _by_label(answers):
    return {a["label"]: a["value"] for a in answers}


# ── router ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ref,expected", [
    ("https://jobs.lever.co/palantir", BoardRef("lever", "palantir")),
    ("jobs.lever.co/Palantir/6ED76CE8-4156-4b60-b120-403538bd66cd/apply",
     BoardRef("lever", "palantir", "6ed76ce8-4156-4b60-b120-403538bd66cd")),
    ("https://jobs.eu.lever.co/acme", BoardRef("lever", "acme", "", "eu")),
    ("https://jobs.ashbyhq.com/Notion", BoardRef("ashby", "Notion")),   # case kept: API is case-sensitive
    ("https://jobs.ashbyhq.com/openai/8fb1615c-34bf-47c4-a1d1-b7b2f836bbd3/application",
     BoardRef("ashby", "openai", "8fb1615c-34bf-47c4-a1d1-b7b2f836bbd3")),
    ("https://apply.workable.com/huggingface/", BoardRef("workable", "huggingface")),
    ("https://apply.workable.com/huggingface/j/0bd8c06db3/apply/",
     BoardRef("workable", "huggingface", "0BD8C06DB3")),
    ("https://acme.workable.com/j/ABC123DEF", BoardRef("workable", "acme", "ABC123DEF")),
    ("https://jobs.smartrecruiters.com/Ubisoft2/744000162345149-senior-gameplay-programmer",
     BoardRef("smartrecruiters", "Ubisoft2", "744000162345149")),
    ("https://jobs.smartrecruiters.com/oneclick-ui/company/x/publication/y?dcr_ci=Ubisoft2",
     BoardRef("smartrecruiters", "Ubisoft2")),
    ("https://boards.greenhouse.io/figma/jobs/5426468004", BoardRef("greenhouse", "figma", "5426468004")),
    ("https://acme.wd5.myworkdayjobs.com/en-US/careers", BoardRef("workday", "acme.wd5/careers")),
    ("https://wd1.myworkdaysite.com/recruiting/wf/WellsFargoJobs/job/IRVING-TX/Eng_R-531729",
     BoardRef("workday", "wf.wd1/WellsFargoJobs", "Eng_R-531729")),
    ("workday:ngc.wd1/Northrop_Grumman_External_Site", BoardRef("workday", "ngc.wd1/Northrop_Grumman_External_Site")),
    ("lever:palantir", BoardRef("lever", "palantir")),
    ("greenhouse:figma/123", BoardRef("greenhouse", "figma", "123")),
    ("workable:huggingface", BoardRef("workable", "huggingface")),
    ("smartrecruiters:BoschGroup", BoardRef("smartrecruiters", "BoschGroup")),
])
def test_parse_board_ref(ref, expected):
    assert parse_board_ref(ref) == expected


@pytest.mark.parametrize("ref", ["", "palantir", "https://example.com/jobs/1",
                                 "https://jobs.workable.com/view/abc123/backend-engineer",
                                 "https://apply.workable.com/api/v3/x", "foo:bar"])
def test_parse_board_ref_rejects_ambiguous_or_foreign(ref):
    assert parse_board_ref(ref) is None


def test_bare_token_needs_a_default_platform():
    assert parse_board_ref("palantir", default_platform="lever") == BoardRef("lever", "palantir")


def test_workday_is_a_discovery_platform():
    ref = detect_ats("https://acme.wd1.myworkdayjobs.com/External")
    assert ref == BoardRef("workday", "acme.wd1/External") and ref.supported
    assert parse_board_ref("workday:acme") is None                       # tenant.wdN/site needed


# ── shared core additions ────────────────────────────────────────────────────

def test_start_date_from_notice():
    today = date(2026, 11, 19)
    assert start_date_from_notice("2 weeks", today) == date(2026, 12, 3)
    assert start_date_from_notice("1 month", today) == date(2026, 12, 19)
    assert start_date_from_notice("Immediately", today) == today
    assert start_date_from_notice("", today) is None
    assert start_date_from_notice("negotiable", today) is None


@pytest.mark.parametrize("label,name", [
    ("First name", "first_name"), ("Last Name*", "last_name"), ("Legal Name", "full_name"),
    ("Email address", "email"), ("Phone Number", "phone"), ("Mobile", "phone"),
    ("Linkedin", "linkedin"), ("GitHub profile", "github"), ("Portfolio URL", "website"),
    ("Why us?", None), ("Phone interview availability", None),
    ("Share 2-3 of your open-source contributions with links (GitHub PRs)", None),
])
def test_standard_for_label(label, name):
    assert standard_for_label(label) == name


def test_standard_value_and_profile():
    assert standard_value("full_name", PROFILE) == "Ada Lovelace"
    assert standard_value("city", PROFILE) == "San Francisco"
    assert standard_value("website", PROFILE) == "https://github.com/ada"  # falls back to GitHub
    assert standard_value("nope", PROFILE) == ""
    assert build_profile(first_name="a", last_name="b", email="c", years_experience="x")["years"] == 3


def test_new_shared_rules():
    p = {**PROFILE, "education": {"school": "Stanford University", "degree": "BSc"}}
    assert rule_answer("Please tell us how you heard about this opportunity.", p) == "__source__"
    assert rule_answer("Which university are you currently attending?", p) == "Stanford University"
    assert rule_answer("What is your highest level of education?", p) == "BSc"
    # A content question that mentions GitHub is not the profile-URL field.
    assert rule_answer("Share 2-3 open-source contributions with links (GitHub PRs)", p) is None
    assert rule_answer("GitHub profile", p) == "https://github.com/ada"
    # US tech cities resolve the job's country for generic authorization questions.
    sf = {**p, "job_countries": {"united states"}}
    assert rule_answer("Are you authorized to work in the country where the job is located?", sf) == "Yes"


# ── Lever ────────────────────────────────────────────────────────────────────

def test_lever_discover_normalizes_postings():
    client = _Client({"api.lever.co/v0/postings/palantir": _fixture("lever_postings.json")})
    jobs = LeverAdapter().discover(client, BoardRef("lever", "palantir"))
    assert jobs[0]["job_id"] == "0ccbe620-a3ef-41d1-a5c4-68e56b3c91d0"
    assert jobs[0]["apply_url"].endswith("/apply") and jobs[0]["platform"] == "lever"
    assert all(j["title"] and j["location"] is not None for j in jobs)
    assert client.calls[0][2]["params"] == {"mode": "json"}


def test_lever_eu_site_uses_eu_api():
    client = _Client({"api.eu.lever.co": []})
    LeverAdapter().discover(client, BoardRef("lever", "acme", region="eu"))
    assert client.calls[0][1].startswith("https://api.eu.lever.co/")


def test_lever_form_parsing_reads_every_question():
    form = parse_form(_fixture("lever_form.html"))
    by_name = {f["name"]: f for f in form}
    assert by_name["resume"]["control"] == "file" and by_name["resume"]["required"]
    assert by_name["location"]["control"] == "location"
    assert by_name["phone"]["required"] is False
    langs = next(f for f in form if f["label"].startswith("Language Skill"))
    assert langs["control"] == "checkbox" and langs["required"] and "English (ENG)" in langs["options"]
    auth = next(f for f in form if f["label"].startswith("Are you legally authorized"))
    assert auth["control"] == "radio" and auth["options"] == ["Yes", "No"]
    assert all("✱" not in f["label"] for f in form)


def test_lever_plan_answers_everything_and_puts_resume_first():
    form = parse_form(_fixture("lever_form.html"))
    answers, unanswered = LeverAdapter().plan(form, {"location": "Singapore, Singapore"}, PROFILE, None)
    assert unanswered == []
    assert answers[0]["kind"] == "file"                           # resume before text fields
    got = _by_label(answers)
    assert got["Full name"] == "Ada Lovelace"
    assert got["Language Skill(s) (Check all that apply)"] == ["English (ENG)", "Mandarin (MAN)"]
    # A Singapore job for a US-authorized candidate: not authorized, needs sponsorship.
    assert got["Are you legally authorized to work in the country for which you are applying?"] == "No"
    assert any(v == "Yes" for k, v in got.items() if k.startswith("Will you now or in the future"))
    assert got["Are you a Singapore citizen?"] == "No"
    assert next(v for k, v in got.items() if k.startswith("Which university")) == "Stanford University"
    assert "Palantir Website" in got.values()                     # how-did-you-hear → careers site


def test_lever_required_question_without_answer_skips_job():
    form = parse_form(_fixture("lever_form.html"))
    bare = build_profile(first_name="Ada", last_name="L", email="a@b.c", resume_path="/cv.pdf")
    _, unanswered = LeverAdapter().plan(form, {"location": "Singapore"}, bare, None)
    assert "Current location" in unanswered
    assert any(u.startswith("Which university") for u in unanswered)


def test_lever_required_eeo_select_declines():
    html = """<form id="application-form"><li class="application-question">
      <div class="application-label">Gender ✱</div>
      <select name="eeo[gender]" required><option value="">Select</option>
        <option value="Male">Male</option><option value="Female">Female</option>
        <option value="Decline">Decline to self-identify</option></select></li></form>"""
    answers, unanswered = LeverAdapter().plan(parse_form(html), {}, PROFILE, None)
    assert unanswered == [] and answers[0]["value"] == "Decline to self-identify"


# ── Ashby ────────────────────────────────────────────────────────────────────

def test_ashby_discover_skips_unlisted_and_marks_remote():
    board = _fixture("ashby_board.json")
    board["jobs"].append({"id": "hidden", "title": "Secret", "isListed": False})
    board["jobs"].append({"id": "r1", "title": "Remote SWE", "location": "US", "isRemote": True})
    jobs = AshbyAdapter().discover(_Client({"job-board/openai": board}), BoardRef("ashby", "openai"))
    ids = [j["job_id"] for j in jobs]
    assert "hidden" not in ids and "8fb1615c-34bf-47c4-a1d1-b7b2f836bbd3" in ids
    assert next(j for j in jobs if j["job_id"] == "r1")["location"] == "US (Remote)"


def test_ashby_form_and_plan_from_real_graphql():
    client = _Client({"non-user-graphql": _fixture("ashby_form.json")})
    form = AshbyAdapter().load_form(client, {"token": "openai", "job_id": "8fb1615c"})
    assert client.calls[0][2]["json"]["variables"]["organizationHostedJobsPageName"] == "openai"
    answers, unanswered = AshbyAdapter().plan(form, {"location": "San Francisco"}, PROFILE, None)
    assert unanswered == []
    kinds = {a["label"]: (a["kind"], a["value"]) for a in answers}
    assert kinds["Legal Name"] == ("text", "Ada Lovelace")
    assert kinds["Phone Number"] == ("text", "+1 415 555 0100")
    assert kinds["Where are you currently located?"][0] == "location"
    assert kinds["Are you authorized to work in the country where the job is located?"] == ("yesno", "Yes")
    assert kinds["Applicant Arbitration Agreement Acknowledgement"][0] == "multiselect"
    date_kind, date_val = kinds["When can you start a new role?"]
    assert date_kind == "date" and len(date_val.split("/")) == 3


def test_ashby_graphql_error_raises():
    client = _Client({"non-user-graphql": {"errors": [{"message": "Cannot query field"}]}})
    with pytest.raises(RuntimeError, match="Cannot query field"):
        AshbyAdapter().load_form(client, {"token": "x", "job_id": "y"})


def test_ashby_date_from_explicit_extra_answer():
    form = [{"path": "d1", "label": "Earliest start date", "type": "Date", "required": True, "options": []}]
    prof = {**PROFILE, "extra_answers": [("start date", "2027-01-15")]}
    answers, _ = AshbyAdapter().plan(form, {}, prof, None)
    assert answers[0]["value"] == "01/15/2027"


def test_ashby_unknown_required_type_is_unanswered():
    form = [{"path": "s1", "label": "Rate yourself", "type": "Score", "required": True, "options": []}]
    assert AshbyAdapter().plan(form, {}, PROFILE, None) == ([], ["Rate yourself"])


# ── Workable ─────────────────────────────────────────────────────────────────

def test_workable_discover_uses_account_name_and_paginates():
    page1 = {"results": _fixture("workable_jobs.json")["results"][:2], "nextPage": "tok"}
    page2 = {"results": _fixture("workable_jobs.json")["results"][2:]}

    class Paged(_Client):
        def post(self, url, json=None):
            self.calls.append(("POST", url, {"json": json}))
            return _Resp(page2 if json.get("token") else page1)

    client = Paged({"api/v1/accounts/huggingface": {"name": "Hugging Face"}})
    jobs = WorkableAdapter().discover(client, BoardRef("workable", "huggingface"))
    assert len(jobs) == len(_fixture("workable_jobs.json")["results"])
    assert jobs[0]["company"] == "Hugging Face" and jobs[0]["apply_url"].endswith("/apply/")
    assert [c[2]["json"] for c in client.calls if c[0] == "POST"] == [{}, {"token": "tok"}]


def test_workable_plan_from_real_form():
    client = _Client({"/form": _fixture("workable_form.json")})
    form = WorkableAdapter().load_form(client, {"job_id": "0BD8C06DB3"})
    answers, unanswered = WorkableAdapter().plan(form, {"location": "United States (Remote)"}, PROFILE, None)
    got = {a["field"]: a["value"] for a in answers}
    assert got["firstname"] == "Ada" and got["phone"] == "+1 415 555 0100"
    assert got["CA_47143"] == "https://github.com/ada"            # "Github profile" custom field
    assert got["CA_10627"] == "2 weeks"                           # notice-period dropdown
    assert len(unanswered) == 2                                   # two essays → need the LLM


def test_workable_trap_questions_are_never_guessed():
    """Hugging Face asks whether the first answer starts with a phrase from the
    job ad; nothing in the profile can answer that, so the job is skipped."""
    client = _Client({"/form": _fixture("workable_form_trap.json")})
    form = WorkableAdapter().load_form(client, {"job_id": "DB4D7C0EC8"})
    answers, unanswered = WorkableAdapter().plan(form, {"location": "France (Remote)"}, PROFILE, None)
    assert any("exact phrase" in u for u in unanswered)
    contrib = next(f["id"] for f in form if f["label"].startswith("Share 2"))
    assert contrib not in {a["field"] for a in answers}           # not the GitHub profile URL


def test_workable_llm_answers_required_essays():
    client = _Client({"/form": _fixture("workable_form.json")})
    form = WorkableAdapter().load_form(client, {"job_id": "x"})
    answers, unanswered = WorkableAdapter().plan(form, {"location": "US"}, PROFILE,
                                                 lambda label, kind, options: "Because of the Hub.")
    assert unanswered == []
    assert sum(1 for a in answers if a["value"] == "Because of the Hub.") == 2


# ── SmartRecruiters (discovery only) ─────────────────────────────────────────

def test_smartrecruiters_discover_and_is_manual():
    client = _Client({"companies/Ubisoft2/postings": _fixture("smartrecruiters_postings.json")})
    adapter = SmartRecruitersAdapter()
    jobs = adapter.discover(client, BoardRef("smartrecruiters", "Ubisoft2"))
    assert jobs and jobs[0]["company"] == "Ubisoft" and jobs[0]["url"].startswith("https://jobs.smartrecruiters.com/Ubisoft2/")
    assert adapter.can_apply is False


def test_smartrecruiters_unknown_company_raises():
    client = _Client({"postings": {"content": [], "totalFound": 0}})
    with pytest.raises(RuntimeError, match="unknown company"):
        SmartRecruitersAdapter().discover(client, BoardRef("smartrecruiters", "nope"))


# ── Greenhouse adapter shares the greenhouse_apply ledger ────────────────────

def test_greenhouse_adapter_reads_and_writes_both_ledgers(tmp_path, monkeypatch):
    from src.automation.tools import greenhouse_apply_tool as gh
    gh_ledger = tmp_path / "gh.json"
    monkeypatch.setenv("GREENHOUSE_LEDGER", str(gh_ledger))
    job = {"platform": "greenhouse", "token": "figma", "job_id": "1", "title": "Eng", "company": "Figma"}
    gh.record_applied(str(gh_ledger), job)                        # applied via greenhouse_apply
    adapter = GreenhouseAdapter()
    assert adapter.already_applied(job, {}) is True
    job2 = {**job, "job_id": "2"}
    adapter.record(str(tmp_path / "ats.json"), job2)
    assert "greenhouse:figma/2" in ats.load_ledger(str(tmp_path / "ats.json"))
    assert "figma/2" in ats.load_ledger(str(gh_ledger))


# ── shared runner (fake adapters, no browser) ────────────────────────────────

class _FakeAdapter(ats.Adapter):
    def __init__(self, platform, jobs, *, can_apply=True, unanswered=(), apply_status="prepared"):
        self.platform, self.label, self.can_apply = platform, platform.title(), can_apply
        self._jobs, self._unanswered, self._status = jobs, list(unanswered), apply_status
        self.applied: list[str] = []

    def discover(self, client, ref):
        if ref.company == "boom":
            raise RuntimeError("API down")
        return [{"platform": self.platform, "token": ref.company, "job_id": j[0], "title": j[1],
                 "company": ref.company, "location": j[2], "url": f"u/{j[0]}", "apply_url": f"a/{j[0]}"}
                for j in self._jobs]

    def load_form(self, client, job):
        return {}

    def plan(self, form, job, profile, answer_fn):
        return ([{"field": "x", "kind": "text", "value": "v", "label": "X"}],
                self._unanswered if job["job_id"] == "bad" else [])

    def apply(self, page, job, answers, dry_run, log):
        self.applied.append(job["job_id"])
        submitted = self._status == "submitted"
        return {**ats.entry_for(job, answers), "status": self._status, "submitted": submitted}


@pytest.fixture
def runner(tmp_path, mocker):
    resume = tmp_path / "cv.pdf"
    resume.write_bytes(b"%PDF")
    page = mocker.MagicMock()
    browser = mocker.MagicMock()
    browser.new_context.return_value.new_page.return_value = page
    launch = mocker.patch.object(ats, "launch_browser", return_value=browser)
    mocker.patch("playwright.sync_api.sync_playwright")

    def run(adapters, **kw):
        args = dict(first_name="Ada", last_name="L", email="a@b.c", resume_path=str(resume),
                    ledger_path=str(tmp_path / "ledger.json"), client=_Client({}),
                    adapters=adapters, max_applications=10)
        return ats.run_ats_apply(**{**args, **kw})
    run.launch = launch
    return run


def test_runner_routes_boards_filters_and_lists_manual(runner):
    lever = _FakeAdapter("lever", [("1", "Backend Engineer", "Taipei"), ("2", "Designer", "Taipei")])
    sr = _FakeAdapter("smartrecruiters", [("9", "Backend Engineer", "Berlin")], can_apply=False)
    out = runner({"lever": lever, "smartrecruiters": sr},
                 boards="lever:acme, smartrecruiters:Beta, https://example.com/x",
                 keywords="backend")
    assert lever.applied == ["1"]                                  # Designer filtered by keyword
    assert [m["job_id"] for m in out["manual"]] == ["9"] and out["manual_count"] == 1
    assert sr.applied == []
    assert any("not a recognised ATS board" in w for w in out["warnings"])
    assert "listed for manual apply" in out["summary"]


def test_runner_job_url_bypasses_filters(runner):
    lever = _FakeAdapter("lever", [("11111111-1111-1111-1111-111111111111", "Designer", "Paris")])
    runner({"lever": lever}, boards="https://jobs.lever.co/acme/11111111-1111-1111-1111-111111111111",
           keywords="backend", location="Taipei")
    assert lever.applied == ["11111111-1111-1111-1111-111111111111"]


def test_runner_skips_unanswerable_and_never_opens_browser_for_it(runner):
    lever = _FakeAdapter("lever", [("bad", "Backend", "X")], unanswered=["Why us?"])
    out = runner({"lever": lever}, boards="lever:acme")
    assert lever.applied == [] and out["skipped"][0]["unanswered"] == ["Why us?"]
    runner.launch.assert_not_called()


def test_runner_ledger_records_only_confirmed_and_skips_next_time(runner, tmp_path):
    lever = _FakeAdapter("lever", [("1", "Backend", "X")], apply_status="submitted")
    out = runner({"lever": lever}, boards="lever:acme", dry_run=False)
    assert out["submitted_count"] == 1
    assert "lever:acme/1" in ats.load_ledger(str(tmp_path / "ledger.json"))
    again = runner({"lever": lever}, boards="lever:acme", dry_run=False)
    assert again["skipped"][0]["reason"] == "already applied (ledger)" and lever.applied == ["1"]


def test_runner_dry_run_prepared_is_not_ledgered(runner, tmp_path):
    lever = _FakeAdapter("lever", [("1", "Backend", "X")])
    out = runner({"lever": lever}, boards="lever:acme")
    assert out["applied"][0]["status"] == "prepared"
    assert ats.load_ledger(str(tmp_path / "ledger.json")) == {}


def test_runner_caps_per_board_and_interleaves(runner):
    lever = _FakeAdapter("lever", [("1", "Backend A", "X"), ("2", "Backend B", "X"), ("3", "Backend C", "X")])
    out = runner({"lever": lever}, boards="lever:a, lever:b", max_per_board=1)
    assert sorted((e["token"], e["job_id"]) for e in out["applied"]) == [("a", "1"), ("b", "1")]


def test_runner_relevance_gate_and_failing_discovery(runner):
    lever = _FakeAdapter("lever", [("1", "Backend", "X"), ("2", "Manager", "X")])
    out = runner({"lever": lever}, boards="lever:acme, lever:boom",
                 relevance_fn=lambda title, meta: ("Manager" not in title, "no managers"))
    assert lever.applied == ["1"] and out["filtered_count"] == 1
    assert any("boom" in w and "API down" in w for w in out["warnings"])


def test_runner_errors_without_resume_or_boards(runner, tmp_path):
    out = runner({}, boards="", resume_path=str(tmp_path / "cv.pdf"))
    assert "No usable boards" in out["error"]
    out = runner({"lever": _FakeAdapter("lever", [("1", "Backend", "X")])}, boards="lever:x",
                 resume_path=str(tmp_path / "missing.pdf"))
    assert "Resume file not found" in out["error"]
    # Nothing to auto-apply to (the board has no matching job): no résumé needed.
    out = runner({"lever": _FakeAdapter("lever", [("1", "Sales", "X")])}, boards="lever:x",
                 keywords="backend", resume_path=str(tmp_path / "missing.pdf"))
    assert "error" not in out and out["jobs_found"] == 0


def test_runner_unsupported_platform_warns(runner):
    out = runner({"lever": _FakeAdapter("lever", [])}, boards="https://acme.wd1.myworkdayjobs.com/x")
    assert any("workday is not supported" in w for w in out["warnings"])


def test_tool_wrapper_never_raises(mocker):
    mocker.patch.object(ats, "run_ats_apply", side_effect=RuntimeError("boom"))
    out = ats.ATSApplyTool()._run(boards="lever:x", first_name="a", last_name="b", email="c")
    assert out["error"] == "RuntimeError: boom" and out["applied"] == []


# ── flow wiring ──────────────────────────────────────────────────────────────

def test_flow_passes_profile_and_llm_callbacks(mocker):
    from src.automation.flows.ats_apply_flow import ATSApplyFlow
    run = mocker.patch("src.automation.flows.ats_apply_flow.run_ats_apply",
                       return_value={"applied": [], "jobs_found": 0})
    mocker.patch("src.automation.flows.ats_apply_flow.append_log")
    mocker.patch("src.automation.flows.greenhouse_apply_flow.append_log")
    mocker.patch("src.automation.harness.provider.resolve", return_value=(object(), "p", "m"))
    flow = ATSApplyFlow()
    out = flow.kickoff(inputs={"boards": "lever:acme", "first_name": "Ada", "last_name": "L",
                               "email": "a@b.c", "task_filter": "backend only",
                               "candidate_background": "10 years Python", "dry_run": True})
    kw = run.call_args.kwargs
    assert kw["boards"] == "lever:acme" and kw["dry_run"] is True
    assert callable(kw["relevance_fn"]) and callable(kw["answer_fn"])
    assert json.loads(out) == {"applied": [], "jobs_found": 0}


def test_flow_requires_boards_and_contact(mocker):
    from src.automation.flows.ats_apply_flow import ATSApplyFlow
    mocker.patch("src.automation.flows.ats_apply_flow.append_log")
    with pytest.raises(Exception, match="boards"):
        ATSApplyFlow().kickoff(inputs={"first_name": "a", "last_name": "b", "email": "c"})


def test_ats_apply_registered_with_shared_fields():
    from src.automation.spec import REGISTRY
    spec, gh = REGISTRY["ats_apply"], REGISTRY["greenhouse_apply"]
    names = [f.name for f in spec.fields]
    assert names == [n for f in gh.fields for n in ((f.name, "sources") if f.name == "boards" else (f.name,))]
    assert not next(f for f in spec.fields if f.name == "boards").required   # boards or sources
    assert "lever:palantir" in next(f for f in spec.fields if f.name == "boards").placeholder
    assert spec.canary and spec.canary.source == "src.automation.tools.ats_lever"


def test_smartrecruiters_paginates_by_offset():
    pages = {0: {"content": [{"id": "1", "name": "A"}, {"id": "2", "name": "B"}], "totalFound": 3},
             2: {"content": [{"id": "3", "name": "C"}], "totalFound": 3}}

    class Paged(_Client):
        def get(self, url, params=None):
            self.calls.append(("GET", url, params))
            return _Resp(pages[params["offset"]])

    client = Paged({})
    jobs = SmartRecruitersAdapter().discover(client, BoardRef("smartrecruiters", "Acme"))
    assert [j["job_id"] for j in jobs] == ["1", "2", "3"]
    assert [c[2]["offset"] for c in client.calls] == [0, 2]


# ── review fixes (PR #109) ───────────────────────────────────────────────────

from src.automation.tools.ats_common import profile_url_field, url_path_has  # noqa: E402


@pytest.mark.parametrize("label,site", [
    ("LinkedIn", "linkedin"), ("Linkedin (optional)", "linkedin"), ("GitHub profile", "github"),
    ("What is your LinkedIn URL?", "linkedin"), ("Please share your LinkedIn profile", "linkedin"),
    ("Please provide a link to your LinkedIn", "linkedin"), ("List your GitHub URL", "github"),
    ("Portfolio URL", "website"),
    ("Why GitHub?", None), ("Describe your LinkedIn presence", None), ("Do you have a blog?", None),
    ("Share 2-3 of your open-source contributions with links (GitHub PRs)", None),
])
def test_profile_url_field_is_one_rule_for_both_paths(label, site):
    """standard_for_label and rule_answer must agree on what a profile-URL field is."""
    assert profile_url_field(label) == site
    std = standard_for_label(label)
    assert std == site
    expected = {"linkedin": PROFILE["linkedin_url"], "github": PROFILE["github_url"],
                "website": PROFILE["github_url"], None: None}[site]
    assert rule_answer(label, PROFILE) == expected


def test_why_github_text_field_is_not_filled_with_the_url():
    ashby = [{"path": "q", "label": "Why GitHub?", "type": "String", "required": True, "options": []}]
    answers, unanswered = AshbyAdapter().plan(ashby, {}, PROFILE, None)
    assert answers == [] and unanswered == ["Why GitHub?"]
    workable = [{"id": "q", "label": "Why GitHub?", "type": "text", "required": True,
                 "single": True, "options": []}]
    answers, _ = WorkableAdapter().plan(workable, {}, PROFILE, lambda *a: "Because of Copilot.")
    assert answers[0]["value"] == "Because of Copilot."          # the LLM answers, not the URL


def test_notice_period_parsing_edge_cases():
    today = date(2026, 11, 19)
    assert start_date_from_notice("not sure, let me know", today) is None   # "know" is not "now"
    assert start_date_from_notice("1.5 months", today) is None              # not "5 months"
    assert start_date_from_notice("available now", today) == today


@pytest.mark.parametrize("url", ["https://evilgreenhouse.io/figma", "https://notgreenhouse.io/figma/jobs/1"])
def test_lookalike_greenhouse_hosts_are_rejected(url):
    from src.automation.tools.greenhouse_apply_tool import parse_board_ref as gh_parse
    assert parse_board_ref(url) is None and gh_parse(url) is None


def test_smartrecruiters_oneclick_company_is_validated():
    bad = "https://jobs.smartrecruiters.com/oneclick-ui/company/x?dcr_ci=Evil%2F..%3Fq%3D1"
    assert parse_board_ref(bad) is None


def test_url_path_has_matches_whole_segments_only():
    assert url_path_has("https://jobs.lever.co/acme/123/thanks", ("thanks",))
    assert not url_path_has("https://jobs.lever.co/thanksgiving-co/123/apply", ("thanks",))
    assert not url_path_has("https://apply.workable.com/success-team/j/ABC/apply/", ("success",))


def test_unconfirmed_submission_is_pending_counts_toward_cap_and_is_never_resent(runner, tmp_path):
    lever = _FakeAdapter("lever", [("1", "Backend A", "X"), ("2", "Backend B", "X")],
                         apply_status="unconfirmed")
    out = runner({"lever": lever}, boards="lever:acme", dry_run=False, max_applications=1)
    assert lever.applied == ["1"]                                  # cap 1: job 2 never attempted
    assert out["submitted_count"] == 0 and out["unconfirmed_count"] == 1
    led = ats.load_ledger(str(tmp_path / "ledger.json"))
    assert led["lever:acme/1"]["status"] == "pending"
    assert "unconfirmed" in out["summary"]

    again = runner({"lever": lever}, boards="lever:acme", dry_run=False, max_applications=1)
    assert lever.applied == ["1", "2"]                             # job 1 not resent
    assert "never confirmed" in again["skipped"][0]["reason"]


def test_dry_run_unconfirmed_is_not_ledgered(runner, tmp_path):
    lever = _FakeAdapter("lever", [("1", "Backend", "X")], apply_status="unconfirmed")
    runner({"lever": lever}, boards="lever:acme", dry_run=True)
    assert ats.load_ledger(str(tmp_path / "ledger.json")) == {}


def test_manual_jobs_are_still_listed_after_the_cap(runner):
    lever = _FakeAdapter("lever", [("1", "Backend", "X"), ("2", "Backend", "X")])
    sr = _FakeAdapter("smartrecruiters", [("9", "Backend", "Y"), ("8", "Backend", "Y")], can_apply=False)
    out = runner({"lever": lever, "smartrecruiters": sr},
                 boards="lever:acme, smartrecruiters:Beta", max_applications=1)
    assert lever.applied == ["1"]
    assert sorted(m["job_id"] for m in out["manual"]) == ["8", "9"]


def test_greenhouse_pending_blocks_both_job_types(tmp_path, monkeypatch):
    from src.automation.tools import greenhouse_apply_tool as gh
    monkeypatch.setenv("GREENHOUSE_LEDGER", str(tmp_path / "gh.json"))
    job = {"platform": "greenhouse", "token": "figma", "job_id": "7", "title": "Eng", "company": "Figma"}
    GreenhouseAdapter().record(str(tmp_path / "ats.json"), job, status="pending")
    assert ats.load_ledger(str(tmp_path / "ats.json"))["greenhouse:figma/7"]["status"] == "pending"
    assert "figma/7" in gh.load_ledger(str(tmp_path / "gh.json"))


class _Opts:
    def __init__(self, texts):
        self.texts, self.clicked = texts, []

    def count(self):
        return len(self.texts)

    def all_inner_texts(self):
        return list(self.texts)

    def nth(self, i):
        outer = self
        return type("O", (), {"click": lambda self, timeout=0: outer.clicked.append(outer.texts[i])})()


class _AcPage:
    def __init__(self, texts):
        self.opts = _Opts(texts)
        self.keyboard = type("K", (), {"press": lambda *_: None})()

    def locator(self, _sel):
        return self.opts

    def wait_for_timeout(self, _ms):
        pass


class _Input:
    def click(self, timeout=0):
        pass

    def is_editable(self, timeout=0):
        return True

    def fill(self, _v):
        pass

    def press_sequentially(self, _v, delay=0):
        pass


def test_pick_autocomplete_never_clicks_a_non_matching_option():
    page = _AcPage(["Taichung, Taiwan", "Tainan, Taiwan"])
    assert ats.pick_autocomplete(page, _Input(), "Taipei", "x", want="Taipei, Taiwan") is False
    assert page.opts.clicked == []


def test_pick_autocomplete_falls_back_to_city_then_first_without_want():
    page = _AcPage(["Taipei, Taipei City, TWN", "Taipei County, TWN"])
    assert ats.pick_autocomplete(page, _Input(), "Taipei", "x", want="Taipei, Taiwan", also=("Taipei",))
    assert page.opts.clicked == ["Taipei, Taipei City, TWN"]
    page = _AcPage(["A", "B"])
    assert ats.pick_autocomplete(page, _Input(), "a", "x") and page.opts.clicked == ["A"]
