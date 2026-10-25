"""Unit tests for the Greenhouse auto-apply automation: board parsing, job
matching, discovery (fake API client), answer planning, ledger, validator and
flow wiring. No network, no browser."""
import pytest

from src.automation.tools import greenhouse_apply_tool as gh

# ── board parsing ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ref, expected", [
    ("figma", ("figma", "")),
    ("Figma", ("figma", "")),
    ("https://boards.greenhouse.io/figma", ("figma", "")),
    ("job-boards.greenhouse.io/figma/", ("figma", "")),
    ("https://job-boards.greenhouse.io/figma/jobs/5426468004", ("figma", "5426468004")),
    ("https://boards.greenhouse.io/figma/jobs/5426468004?gh_jid=5426468004", ("figma", "5426468004")),
    ("https://boards.greenhouse.io/embed/job_board?for=stripe", ("stripe", "")),
    ("https://job-boards.greenhouse.io/embed/job_app?for=stripe&token=8172510", ("stripe", "8172510")),
])
def test_parse_board_ref_accepts_tokens_and_urls(ref, expected):
    assert gh.parse_board_ref(ref) == expected


@pytest.mark.parametrize("ref", ["", "  ", "https://stripe.com/jobs", "not a token!", "https://evil.com/figma"])
def test_parse_board_ref_rejects_non_greenhouse(ref):
    assert gh.parse_board_ref(ref) is None


# ── matching / discovery ──────────────────────────────────────────────────────

def _job(title, loc="Remote", jid=1):
    return {"id": jid, "title": title, "location": {"name": loc}, "company_name": "Acme"}


def test_match_job_keyword_phrases_need_every_word():
    j = _job("Senior Backend Engineer, Payments")
    assert gh.match_job(j, ["backend engineer"], [])
    assert gh.match_job(j, ["frontend", "payments"], [])     # any phrase
    assert not gh.match_job(j, ["backend designer"], [])     # every word of a phrase
    assert gh.match_job(j, [], [])                           # empty = all


def test_match_job_location_substring():
    j = _job("Engineer", loc="San Francisco, CA • Remote (US)")
    assert gh.match_job(j, [], ["remote"])
    assert not gh.match_job(j, [], ["taipei"])


class _Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    """Stands in for httpx.Client: maps URL suffixes to JSON payloads."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, params=None):
        self.calls.append(url)
        for suffix, data in self.routes.items():
            if url.endswith(suffix):
                return data if isinstance(data, _Resp) else _Resp(data)
        return _Resp({}, 404)

    def close(self):
        pass


def test_discover_filters_and_interleaves_boards():
    client = FakeClient({
        "/a/jobs": {"jobs": [_job("Backend Engineer", jid=1), _job("Backend Lead", jid=2),
                             _job("Recruiter", jid=3)]},
        "/b/jobs": {"jobs": [_job("Backend Engineer", jid=9)]},
    })
    warnings: list = []
    jobs, total = gh.discover_jobs(client, ["a", "b", "https://stripe.com/x"], ["backend"], [],
                                   gh._noop, warnings)
    assert total == 4
    assert [(j["token"], j["job_id"]) for j in jobs] == [("a", "1"), ("b", "9"), ("a", "2")]
    assert jobs[0]["url"] == "https://job-boards.greenhouse.io/a/jobs/1"
    assert any("not a Greenhouse board" in w for w in warnings)


def test_discover_job_url_bypasses_filters_and_missing_board_warns():
    client = FakeClient({"/a/jobs": {"jobs": [_job("Recruiter", jid=3), _job("Other", jid=4)]}})
    warnings: list = []
    jobs, _ = gh.discover_jobs(client, ["https://job-boards.greenhouse.io/a/jobs/3", "nope"],
                               ["backend"], ["taipei"], gh._noop, warnings)
    assert [j["job_id"] for j in jobs] == ["3"]
    assert any("no such Greenhouse board" in w for w in warnings)


# ── answer planning ───────────────────────────────────────────────────────────

PROFILE = {
    "first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.com",
    "phone": "+886 900 000 000", "location": "Taipei, Taiwan",
    "linkedin_url": "https://linkedin.com/in/ada", "github_url": "", "website_url": "",
    "years": 4, "salary": "", "work_authorized": True, "needs_sponsorship": False,
    "resume_path": "/tmp/resume.pdf", "cover_letter_path": None, "extra_answers": [],
}


def _q(label, name, ftype="input_text", required=True, values=()):
    return {"label": label, "required": required,
            "fields": [{"name": name, "type": ftype,
                        "values": [{"label": v, "value": i} for i, v in enumerate(values)]}]}


def _plan(questions, profile=PROFILE, answer_fn=None, **extra):
    return gh.plan_answers({"questions": questions, **extra}, profile, answer_fn)


def _by_field(answers):
    return {a["field"]: a["value"] for a in answers}


def test_plan_standard_fields_and_resume():
    answers, unanswered = _plan([
        _q("First Name", "first_name"), _q("Last Name", "last_name"),
        _q("Email", "email"), _q("Phone", "phone"),
        {"label": "Resume/CV", "required": True, "fields": [
            {"name": "resume", "type": "input_file"}, {"name": "resume_text", "type": "textarea"}]},
    ])
    assert unanswered == []
    got = _by_field(answers)
    assert got["first_name"] == "Ada" and got["email"] == "ada@example.com"
    assert got["resume"] == "/tmp/resume.pdf"
    assert "resume_text" not in got
    assert next(a for a in answers if a["field"] == "resume")["kind"] == "file"


def test_plan_select_rules_map_onto_options():
    answers, unanswered = _plan([
        _q("Are you authorized to work in the country for which you applied?", "q1",
           "multi_value_single_select", values=["Yes", "No"]),
        _q("Will you now or in the future require visa sponsorship?", "q2",
           "multi_value_single_select", values=["Yes", "No"]),
        _q("Have you ever worked for Figma before, as an employee or a contractor?", "q3",
           "multi_value_single_select", values=["Yes", "No"]),
        _q("How many years of professional experience do you have?", "q4",
           "multi_value_single_select", values=["0-2 years", "3-5 years", "6+ years"]),
        _q("I acknowledge the privacy notice", "q5",
           "multi_value_single_select", values=["I acknowledge", "No"]),
    ])
    assert unanswered == []
    assert _by_field(answers) == {"q1": "Yes", "q2": "No", "q3": "No",
                                  "q4": "3-5 years", "q5": "I acknowledge"}


def test_plan_authorized_without_sponsorship_is_yes_not_no():
    """Regression: 'without sponsorship' must not be read as a sponsorship ask."""
    answers, _ = _plan([_q("Are you legally authorized to work in the US without sponsorship?",
                           "q1", "multi_value_single_select", values=["Yes", "No"])])
    assert _by_field(answers) == {"q1": "Yes"}
    needs = {**PROFILE, "needs_sponsorship": True}
    answers, _ = _plan([_q("Are you legally authorized to work in the US without sponsorship?",
                           "q1", "multi_value_single_select", values=["Yes", "No"])], needs)
    assert _by_field(answers) == {"q1": "No"}


def test_plan_text_rules_and_extra_answers_win():
    profile = {**PROFILE, "extra_answers": [("notice period", "2 weeks"), ("linkedin", "override")]}
    answers, unanswered = _plan([
        _q("LinkedIn Profile", "q1", required=False),
        _q("What is your notice period?", "q2"),
        _q("From where do you intend to work?", "q3"),
    ], profile)
    assert unanswered == []
    assert _by_field(answers) == {"q1": "override", "q2": "2 weeks", "q3": "Taipei, Taiwan"}


def test_plan_required_unknown_is_unanswered_optional_is_blank():
    answers, unanswered = _plan([
        _q("Why do you want to join Figma?", "q1", "textarea"),
        _q("Additional Information", "q2", "textarea", required=False),
        _q("Pronouns", "q3", "multi_value_single_select", required=False,
           values=["she/her", "he/him", "they/them"]),
    ])
    assert unanswered == ["Why do you want to join Figma?"]
    assert answers == []


def test_plan_answer_fn_only_for_required_and_select_must_match_option():
    calls = []

    def fake(label, kind, options):
        calls.append((label, kind))
        return "Because of the product" if kind == "text" else "Maybe"

    answers, unanswered = _plan([
        _q("Why do you want to join?", "q1", "textarea"),
        _q("Optional essay", "q2", "textarea", required=False),
        _q("Do you enjoy pairing?", "q3", "multi_value_single_select", values=["Yes", "No"]),
    ], answer_fn=fake)
    assert _by_field(answers) == {"q1": "Because of the product"}
    assert unanswered == ["Do you enjoy pairing?"]          # "Maybe" isn't an option
    assert [c[0] for c in calls] == ["Why do you want to join?", "Do you enjoy pairing?"]


def test_plan_answer_fn_failure_leaves_unanswered():
    def boom(*_a):
        raise RuntimeError("llm down")

    _, unanswered = _plan([_q("Why us?", "q1", "textarea")], answer_fn=boom)
    assert unanswered == ["Why us?"]


def test_plan_location_and_compliance_decline():
    data = {
        "location_questions": [
            {"label": "Latitude", "required": True, "fields": [{"name": "latitude", "type": "input_hidden"}]},
            {"label": "Location", "required": True, "fields": [{"name": "location", "type": "input_text"}]},
        ],
        "compliance": [{"type": "eeoc", "questions": [
            _q("VeteranStatus", "veteran_status", "multi_value_single_select",
               values=["I am not a protected veteran", "I don't wish to answer"]),
            _q("Gender", "gender", "multi_value_single_select", required=False,
               values=["Male", "Female", "Decline To Self Identify"]),
        ]}],
    }
    answers, unanswered = _plan([], **data)
    assert unanswered == []
    got = {a["field"]: (a["kind"], a["value"]) for a in answers}
    assert got == {"location": ("location", "Taipei, Taiwan"),
                   "veteran_status": ("select", "I don't wish to answer")}


def test_plan_required_location_without_profile_location_is_unanswered():
    data = {"location_questions": [{"label": "Location", "required": True,
                                    "fields": [{"name": "location", "type": "input_text"}]}]}
    _, unanswered = _plan([], {**PROFILE, "location": ""}, **data)
    assert unanswered == ["Location"]


def test_plan_cover_letter_uploads_file_only_when_given():
    q = {"label": "Cover Letter", "required": True, "fields": [
        {"name": "cover_letter", "type": "input_file"}, {"name": "cover_letter_text", "type": "textarea"}]}
    _, unanswered = _plan([q])
    assert unanswered == ["Cover Letter"]
    answers, unanswered = _plan([q], {**PROFILE, "cover_letter_path": "/tmp/cl.txt"})
    assert unanswered == [] and _by_field(answers) == {"cover_letter": "/tmp/cl.txt"}


def test_match_option_and_years_option():
    assert gh.match_option(["Yes", "No"], "yes") == "Yes"
    assert gh.match_option(["Yes, I am", "No"], "Yes") == "Yes, I am"
    assert gh.match_option(["Yes", "No"], "") is None
    assert gh._years_option(["Less than 1", "1-3", "4-7", "8 or more"], 10) == "8 or more"
    assert gh._years_option(["0-2", "3-5"], 9) is None


def test_parse_extra_answers():
    assert gh._parse_extra_answers("notice period: 2 weeks\nbad line\n : x\nurl: https://a.b") == [
        ("notice period", "2 weeks"), ("url", "https://a.b")]


# ── ledger ────────────────────────────────────────────────────────────────────

def test_ledger_roundtrip(tmp_path):
    path = str(tmp_path / "sub" / "ledger.json")
    assert gh.load_ledger(path) == {}
    gh.record_applied(path, {"token": "a", "job_id": "1", "title": "Eng", "company": "A"})
    led = gh.load_ledger(path)
    assert "a/1" in led and led["a/1"]["title"] == "Eng"


# ── run (no browser reached) ──────────────────────────────────────────────────

_ARGS = {"first_name": "Ada", "last_name": "L", "email": "a@x.io"}


def test_run_missing_resume_returns_error_not_raise(monkeypatch):
    monkeypatch.delenv("GREENHOUSE_RESUME_PATH", raising=False)
    out = gh.run_greenhouse_apply(boards="a", **_ARGS)
    assert "Resume file not found" in out["error"]


def test_run_no_boards_returns_error():
    assert "No Greenhouse boards" in gh.run_greenhouse_apply(boards=" , ", **_ARGS)["error"]


def test_run_no_matches_returns_summary(tmp_path):
    resume = tmp_path / "cv.pdf"
    resume.write_bytes(b"%PDF")
    client = FakeClient({"/a/jobs": {"jobs": [_job("Recruiter")]}})
    out = gh.run_greenhouse_apply(boards="a", keywords="backend", resume_path=str(resume),
                                  client=client, ledger_path=str(tmp_path / "l.json"), **_ARGS)
    assert out["jobs_found"] == 0 and out["total_listed"] == 1
    assert "No matching" in out["summary"]
    assert "error" not in out


def test_run_skips_ledger_filtered_and_unanswerable_without_opening_browser(tmp_path, mocker):
    resume = tmp_path / "cv.pdf"
    resume.write_bytes(b"%PDF")
    ledger = str(tmp_path / "l.json")
    gh.record_applied(ledger, {"token": "a", "job_id": "1"})
    client = FakeClient({
        "/a/jobs": {"jobs": [_job("Backend A", jid=1), _job("Backend B", jid=2),
                             _job("Backend C", jid=3)]},
        "/a/jobs/3": {"questions": [_q("Why us?", "q1", "textarea")]},
    })
    launch = mocker.patch.object(gh, "_launch_browser")
    out = gh.run_greenhouse_apply(
        boards="a", keywords="backend", resume_path=str(resume), client=client,
        ledger_path=ledger, relevance_fn=lambda t, _m: (t != "Backend B", "not B"), **_ARGS)
    reasons = {s["job_id"]: s["reason"] for s in out["skipped"]}
    assert reasons["1"] == "already applied (ledger)"
    assert reasons["2"] == "filtered out: not B"
    assert reasons["3"].startswith("unanswered required question(s): Why us?")
    assert out["filtered_count"] == 1 and out["applied"] == []
    launch.assert_not_called()                  # nothing was worth a browser


def test_basetool_wraps_exceptions(mocker):
    mocker.patch.object(gh, "run_greenhouse_apply", side_effect=RuntimeError("boom"))
    out = gh.GreenhouseApplyTool()._run(boards="a", **_ARGS)
    assert "boom" in out["error"]


# ── validator ─────────────────────────────────────────────────────────────────

def test_validator_accepts_processed_and_zero_runs():
    from src.automation.harness.validator import validate

    assert validate("greenhouse_apply", {"applied": [{"job_id": "1"}], "skipped": [],
                                         "jobs_found": 1, "summary": "1 job scanned, 1 prepared."}).valid
    assert validate("greenhouse_apply", {"applied": [], "skipped": [], "jobs_found": 0,
                                         "summary": "No matching Greenhouse jobs on figma."}).valid


def test_validator_rejects_error():
    from src.automation.harness.validator import validate

    assert not validate("greenhouse_apply", {"applied": [], "error": "no resume"}).valid


# ── flow ──────────────────────────────────────────────────────────────────────

def test_parse_answer_requires_confidence():
    from src.automation.flows.greenhouse_apply_flow import _parse_answer, _parse_verdict

    assert _parse_answer('```json\n{"answer": "Yes", "confident": true}\n```') == "Yes"
    assert _parse_answer('{"answer": "Yes", "confident": false}') == ""
    assert _parse_answer('{"answer": "Yes"}') == ""
    assert _parse_answer("nope") == ""
    assert _parse_verdict('x {"relevant": "yes", "reason": "ok"} y')["relevant"] is True


def test_flow_raises_on_missing_required():
    from src.automation.flows.greenhouse_apply_flow import GreenhouseApplyFlow

    with pytest.raises(Exception):
        GreenhouseApplyFlow().kickoff(inputs={"boards": "figma", "first_name": "A"})


def test_flow_passes_args_and_no_llm_gates_by_default(mocker):
    captured = {}
    mocker.patch("src.automation.flows.greenhouse_apply_flow.run_greenhouse_apply",
                 side_effect=lambda **kw: captured.update(kw) or {
                     "applied": [], "skipped": [], "jobs_found": 0, "summary": "none"})
    resolve = mocker.patch("src.automation.harness.provider.resolve",
                           return_value=(object(), "gemini", "gemini/gemini-2.5-flash"))

    from src.automation.flows.greenhouse_apply_flow import GreenhouseApplyFlow

    GreenhouseApplyFlow().kickoff(inputs={
        "boards": "figma", "first_name": "Ada", "last_name": "L", "email": "a@x.io",
        "keywords": "backend", "max_applications": 3, "dry_run": False,
    })
    assert captured["boards"] == "figma" and captured["keywords"] == "backend"
    assert captured["max_applications"] == 3 and captured["dry_run"] is False
    assert captured["relevance_fn"] is None and captured["answer_fn"] is None
    resolve.assert_not_called()                 # no gate requested → no LLM


def test_flow_wires_gates_when_filter_and_background_set(mocker):
    captured = {}
    mocker.patch("src.automation.flows.greenhouse_apply_flow.run_greenhouse_apply",
                 side_effect=lambda **kw: captured.update(kw) or {
                     "applied": [], "skipped": [], "jobs_found": 0, "summary": "none"})
    mocker.patch("src.automation.harness.provider.resolve",
                 return_value=(object(), "gemini", "gemini/gemini-2.5-flash"))

    from src.automation.flows.greenhouse_apply_flow import GreenhouseApplyFlow

    GreenhouseApplyFlow().kickoff(inputs={
        "boards": "figma", "first_name": "Ada", "last_name": "L", "email": "a@x.io",
        "task_filter": "backend only", "candidate_background": "8 years of Go at Acme.",
    })
    assert callable(captured["relevance_fn"]) and callable(captured["answer_fn"])


def test_plan_required_single_option_select_is_an_acknowledgement():
    answers, unanswered = _plan([
        _q("Candidate AI Usage Attestation:", "q1", "multi_value_single_select", values=["I agree"]),
        _q("Aviso de Privacidade (Data Privacy Notice)", "q2", "multi_value_multi_select",
           values=["Aceito os termos do Aviso de Privacidade"]),
        _q("Optional newsletter", "q3", "multi_value_single_select", required=False, values=["Subscribe"]),
    ])
    assert unanswered == []
    assert _by_field(answers) == {"q1": "I agree", "q2": ["Aceito os termos do Aviso de Privacidade"]}


def test_plan_long_option_labels_and_source_question():
    answers, _ = _plan([
        _q("Will you now or in the future require visa sponsorship?", "q1", "multi_value_single_select",
           values=["Yes - I will require visa sponsorship.", "No - I do not require visa sponsorship."]),
        _q("How did you hear about this job?", "q2", "multi_value_single_select",
           values=["Company Website", "Social media (Facebook, LinkedIn, Twitter)"]),
    ])
    assert _by_field(answers) == {"q1": "No - I do not require visa sponsorship.",
                                  "q2": "Company Website"}
    answers, _ = _plan([_q("How did you hear about us?", "q1", "multi_value_single_select",
                           values=["Referral", "Social media (LinkedIn)", "Other"]),
                        _q("How did you hear about this job?", "q2")])
    assert _by_field(answers) == {"q1": "Other", "q2": "Company careers page"}


def test_plan_required_demographic_questions_decline_by_flag():
    demo = {"questions": [
        {"id": 4033064002, "label": "Gender", "required": True, "type": "multi_value_single_select",
         "answer_options": [{"label": "Male"}, {"label": "I don't wish to answer", "decline_to_answer": True}]},
        {"id": 4033070002, "label": "Optional survey", "required": False, "type": "multi_value_single_select",
         "answer_options": [{"label": "A"}]},
        {"id": 4033099002, "label": "Race", "required": True, "type": "multi_value_single_select",
         "answer_options": [{"label": "White"}, {"label": "Asian"}]},
    ]}
    answers, unanswered = _plan([], demographic_questions=demo)
    assert _by_field(answers) == {"4033064002": "I don't wish to answer"}
    assert unanswered == ["Race (demographic)"]          # no decline option → never guess


def test_run_result_leads_with_summary_and_strips_skipped_answers(tmp_path):
    resume = tmp_path / "cv.pdf"
    resume.write_bytes(b"%PDF")
    client = FakeClient({"/a/jobs": {"jobs": [_job("Backend A", jid=1)]},
                         "/a/jobs/1": {"questions": [_q("Why us?", "q1", "textarea")]}})
    out = gh.run_greenhouse_apply(boards="a", resume_path=str(resume), client=client,
                                  ledger_path=str(tmp_path / "l.json"), **_ARGS)
    assert next(iter(out)) == "summary"
    assert all("answers" not in s for s in out["skipped"])


def test_answer_guard_blocks_company_questions_background_doesnt_cover():
    from src.automation.flows.greenhouse_apply_flow import _names_unmentioned_employer

    job = {"company": "Figma", "token": "figma"}
    assert _names_unmentioned_employer("Why do you want to join Figma?", job, "I admire Discord.")
    assert not _names_unmentioned_employer("Why do you want to join Figma?", job, "I use Figma daily.")
    assert not _names_unmentioned_employer("Describe a project you led.", job, "I admire Discord.")


def test_plan_authorized_countries_answer_named_country_questions():
    tw = {**PROFILE, "authorized_countries": {"taiwan"}}
    yn = ["Yes", "No"]
    answers, unanswered = _plan([
        _q("Are you legally authorized to work in the United States?", "q1", "multi_value_single_select", values=yn),
        _q("Will you require visa sponsorship to work in the US?", "q2", "multi_value_single_select", values=yn),
        _q("Are you authorized to work in Taiwan?", "q3", "multi_value_single_select", values=yn),
        _q("Are you authorized to work in the country for which you applied?", "q4",
           "multi_value_single_select", values=yn),
        _q("Will you now or in the future require sponsorship?", "q5", "multi_value_single_select", values=yn),
    ], tw)
    assert unanswered == []
    assert _by_field(answers) == {"q1": "No", "q2": "Yes", "q3": "Yes", "q4": "Yes", "q5": "No"}


def test_countries_in_matches_aliases_not_substrings():
    assert gh.countries_in("authorized to work in the U.S.?") == {"united states"}
    assert gh.countries_in("Taiwan or UK") == {"taiwan", "united kingdom"}
    assert gh.countries_in("Are you eligible to work for us?") == set()
    assert gh.countries_in("Are you authorized to work in the US?") == {"united states"}
    assert gh.countries_in("Why do you want to join?") == set()


def test_match_job_expands_role_shorthand_and_matches_word_starts():
    assert gh.match_job(_job("Senior Software Engineer, Backend"), ["SWE"], [])
    assert gh.match_job(_job("Site Reliability Engineer"), ["sre"], [])
    assert gh.match_job(_job("Senior Backend Engineer"), ["BE"], [])
    assert not gh.match_job(_job("Account Executive"), ["swe"], [])
    assert gh.match_job(_job("Engineering Manager"), ["engineer"], [])     # word start
    assert not gh.match_job(_job("Maintenance Lead"), ["ai"], [])          # not mid-word
    assert gh.expand_keywords(["swe", "sde"])[:2] == ["software engineer", "software developer"]
