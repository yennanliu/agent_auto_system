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


@pytest.mark.parametrize("loc, expected", [
    ("San Francisco, CA", {"united states"}),
    ("Remote - USA", {"united states"}),
    ("Bangkok, Thailand", {"thailand"}),
    ("Toronto", {"canada"}),
    ("Office Based - Taipei, Taiwan", {"taiwan"}),
    ("Home Based - APAC", {"*"}),
    ("Home based - Worldwide", {"*"}),
    ("Starbase", set()),
])
def test_job_countries(loc, expected):
    assert gh.job_countries(loc) == expected


def _generic_auth_plan(loc):
    yn = ["Yes", "No"]
    tw = {**PROFILE, "authorized_countries": {"taiwan"}}
    return gh.plan_answers({"location": {"name": loc}, "questions": [
        _q("Are you authorized to work in the country for which you applied?", "q1",
           "multi_value_single_select", values=yn),
        _q("Will you now or in the future require sponsorship?", "q2", "multi_value_single_select", values=yn),
    ]}, tw, lambda *_a: "Yes")


def test_generic_authorization_follows_job_location():
    answers, unanswered = _generic_auth_plan("Bangkok, Thailand")
    assert unanswered == [] and _by_field(answers) == {"q1": "No", "q2": "Yes"}
    answers, _ = _generic_auth_plan("Taipei, Taiwan")
    assert _by_field(answers) == {"q1": "Yes", "q2": "No"}
    answers, _ = _generic_auth_plan("Home Based - APAC")
    assert _by_field(answers) == {"q1": "Yes", "q2": "No"}


def test_generic_authorization_unknown_job_country_is_never_guessed():
    answers, unanswered = _generic_auth_plan("Starbase")        # answer_fn would say "Yes"
    assert answers == []
    assert len(unanswered) == 2


def test_run_caps_applications_per_board(tmp_path, mocker):
    resume = tmp_path / "cv.pdf"
    resume.write_bytes(b"%PDF")
    jobs = {"jobs": [_job("Backend A", jid=1), _job("Backend B", jid=2), _job("Backend C", jid=3)]}
    routes = {"/a/jobs": jobs, "/b/jobs": {"jobs": [_job("Backend Z", jid=9)]}}
    for jid in (1, 2, 3, 9):
        routes[f"/jobs/{jid}"] = {"questions": []}
    mocker.patch.object(gh, "_launch_browser")
    mocker.patch.object(gh, "_apply_to_job", side_effect=lambda _p, job, *_a: {**job, "status": "prepared"})
    out = gh.run_greenhouse_apply(boards="a, b", resume_path=str(resume), client=FakeClient(routes),
                                  ledger_path=str(tmp_path / "l.json"), max_applications=10,
                                  max_per_board=1, **_ARGS)
    assert sorted((e["token"], e["job_id"]) for e in out["applied"]) == [("a", "1"), ("b", "9")]


def test_no_answer_maps_onto_sentence_style_options():
    answers, _ = _plan([_q("Have you ever worked for Robinhood as an employee, intern or contractor?", "q1",
                           "multi_value_single_select",
                           values=["I currently work at Robinhood as a full-time employee",
                                   "I have previously worked at Robinhood", "I have never worked at Robinhood"])])
    assert _by_field(answers) == {"q1": "I have never worked at Robinhood"}


def test_match_option_short_answers_never_substring_match():
    assert gh.match_option(["I know the team", "Not now"], "no") is None
    assert gh.match_option(["Yes, I am", "No, I am not"], "no") == "No, I am not"


def test_run_caps_jobs_examined_per_board(tmp_path, mocker):
    resume = tmp_path / "cv.pdf"
    resume.write_bytes(b"%PDF")
    client = FakeClient({"/a/jobs": {"jobs": [_job("Backend A", jid=i) for i in range(1, 6)]},
                         **{f"/a/jobs/{i}": {"questions": [_q("Why us?", "q", "textarea")]} for i in range(1, 6)}})
    mocker.patch.object(gh, "_launch_browser")
    out = gh.run_greenhouse_apply(boards="a", resume_path=str(resume), client=client,
                                  ledger_path=str(tmp_path / "l.json"), max_scans_per_board=2, **_ARGS)
    assert out["jobs_found"] == 2


def test_require_work_permit_is_a_sponsorship_question():
    tw = {**PROFILE, "authorized_countries": {"taiwan"}}
    answers, _ = gh.plan_answers({"location": {"name": "Ljubljana, Slovenia"}, "questions": [
        _q("Will you now (or in the future) require a work permit in order to work in this country?", "q1",
           "multi_value_single_select", values=["Yes", "No"]),
        _q("Do you have a valid work permit for this country?", "q2", "multi_value_single_select",
           values=["Yes", "No"]),
    ]}, tw)
    assert _by_field(answers) == {"q1": "Yes", "q2": "No"}


def test_plan_education_and_employment_blocks():
    prof = {**PROFILE,
            "education": {"school": "National Taiwan University", "degree": "Master's Degree", "discipline": "Physics"},
            "employment": {"company": "Self-employed", "title": "Sr. Backend Engineer",
                           "start_month": "June", "start_year": "2025"}}
    answers, unanswered = gh.plan_answers(
        {"education": "education_required", "employment": "employment_required", "questions": []}, prof)
    assert unanswered == []
    got = {a["field"]: (a["kind"], a["value"]) for a in answers}
    assert got["school--0"] == ("combo", "National Taiwan University")
    assert got["degree--0"] == ("select", "Master's Degree")
    assert got["start-date-month-0"] == ("select", "June")
    assert got["current-role-0_1"] == ("check", True)
    assert next(a for a in answers if a["field"] == "discipline--0")["optional"] is True


def test_plan_required_blocks_without_data_are_unanswered():
    _, unanswered = gh.plan_answers(
        {"education": "education_required", "employment": "employment_optional", "questions": []}, PROFILE)
    assert unanswered == ["Education (school / degree)"]


@pytest.mark.parametrize("text, expected", [
    ("2025-06", {"start_month": "June", "start_year": "2025"}),
    ("06/2025", {"start_month": "June", "start_year": "2025"}),
    ("Jan 2023", {"start_month": "January", "start_year": "2023"}),
    ("", {}),
])
def test_parse_start(text, expected):
    assert gh._parse_start(text) == expected


def test_match_option_prefers_shortest_prefix_hit():
    opts = ["National Taiwan Normal University", "National Taiwan University of Science And Technology",
            "National Taiwan University (NTU)"]
    assert gh.match_option(opts, "National Taiwan University") == "National Taiwan University (NTU)"


# ── configurable default answers ──────────────────────────────────────────────

DEFAULTS = {**PROFILE, "salary": "70000 SGD / year", "current_salary": "", "notice_period": "1 month",
            "languages": "English, Chinese", "programming_languages": "Python, Java, JavaScript",
            "ai_experience": "5 years", "reason_for_leaving": "Looking for more challenging opportunities.",
            "has_criminal_record": False, "authorized_countries": {"taiwan"},
            "employment": {"company": "Self-employed"}}
SELECT = "multi_value_single_select"
MULTI = "multi_value_multi_select"


def test_default_answers_text_questions():
    answers, unanswered = _plan([
        _q("Please provide your salary expectations.", "q1"),
        _q("If in case considered, how soon can you start working with us?", "q2"),
        _q("Reason for leaving your current/last position.", "q3"),
        _q("Please describe your experience with AI and large language models (LLMs).", "q4"),
        _q("Have you ever been convicted of a criminal offence?", "q5"),
        _q("Your residential area.", "q6"),
        _q("Where are you currently employed or where were you last employed?", "q7"),
    ], DEFAULTS)
    assert unanswered == []
    assert _by_field(answers) == {
        "q1": "70000 SGD / year", "q2": "1 month",
        "q3": "Looking for more challenging opportunities.", "q4": "5 years",
        "q5": "No", "q6": "Taipei, Taiwan", "q7": "Self-employed"}


def test_last_drawn_salary_is_not_the_expectation():
    """Regression: 'last drawn salary' used to be answered with the expected salary."""
    _, unanswered = _plan([_q("What was your last drawn salary?", "q1")], DEFAULTS)
    assert unanswered == ["What was your last drawn salary?"]
    answers, _ = _plan([_q("What was your last drawn salary?", "q1")],
                       {**DEFAULTS, "current_salary": "60000 SGD / year"})
    assert _by_field(answers) == {"q1": "60000 SGD / year"}


def test_notice_period_maps_onto_options():
    answers, _ = _plan([_q("What is your notice period to your current employer?", "q1", SELECT,
                           values=["Immediate", "1 month", "2 months", "3 months and above"])], DEFAULTS)
    assert _by_field(answers) == {"q1": "1 month"}


def test_spoken_languages_multiselect_and_yes_no():
    answers, unanswered = _plan([
        _q("What language(s) can you speak at a native or business level?", "q1", MULTI,
           values=["Arabic", "Cantonese", "English", "French", "Mandarin Chinese"]),
        _q("Can you speak Chinese fluently?", "q2"),
        _q("Can you speak Japanese fluently?", "q3", SELECT, values=["Yes", "No"]),
    ], DEFAULTS)
    assert unanswered == []
    assert _by_field(answers) == {"q1": ["English", "Mandarin Chinese"], "q2": "Yes", "q3": "No"}


def test_programming_languages_need_a_level_when_options_have_one():
    q = _q("Please select up to 3 programming languages you are familiar with and indicate "
           "your level of proficiency.", "q1", MULTI,
           values=["Java Beginner", "Java Advanced", "JavaScript Intermediate", "JavaScript Advanced",
                   "Python Intermediate", "Python Advanced"])
    _, unanswered = _plan([q], DEFAULTS)          # no levels given → never a guessed proficiency
    assert len(unanswered) == 1
    answers, _ = _plan([q], {**DEFAULTS, "programming_languages":
                             "Python: Advanced, Java: Advanced, JavaScript: Intermediate"})
    assert _by_field(answers) == {"q1": ["Python Advanced", "Java Advanced", "JavaScript Intermediate"]}
    answers, _ = _plan([_q("Which programming languages are you proficient in?", "q2")], DEFAULTS)
    assert _by_field(answers) == {"q2": "Python, Java, JavaScript"}   # "Java" never matched "JavaScript"


def test_residency_and_location_questions_follow_the_profile():
    answers, unanswered = _plan([
        _q("Are you a Singapore Citizen or Singapore Permanent Resident?", "q1", SELECT, values=["Yes", "No"]),
        _q("What is your residency status in Singapore?", "q2", SELECT,
           values=["Singapore Citizen", "Singapore PR", "Others"]),
        _q("Are you located in Singapore?", "q3", SELECT, values=["Yes", "No"]),
        _q("City", "q4"), _q("State", "q5"), _q("Country", "q6"),
        _q("Where do you currently live?", "q7", SELECT, values=["Singapore", "Taiwan", "Thailand"]),
        _q("What is your work eligibility in Singapore?", "q8", SELECT,
           values=["Singaporean", "Singapore PR", "Work Visa", "I'm not eligible to work in Singapore"]),
    ], DEFAULTS)
    assert unanswered == []
    assert _by_field(answers) == {"q1": "No", "q2": "Others", "q3": "No", "q4": "Taipei",
                                  "q5": "Taipei", "q6": "Taiwan", "q7": "Taiwan",
                                  "q8": "I'm not eligible to work in Singapore"}
    # A country you ARE authorized in is not assumed to be your citizenship.
    _, unanswered = _plan([_q("Are you a Taiwan citizen?", "q1", SELECT, values=["Yes", "No"])], DEFAULTS)
    assert unanswered == ["Are you a Taiwan citizen?"]


def test_extra_answers_still_beat_the_defaults():
    profile = {**DEFAULTS, "extra_answers": [("notice period", "2 weeks")]}
    answers, _ = _plan([_q("What is your notice period?", "q1")], profile)
    assert _by_field(answers) == {"q1": "2 weeks"}


def test_flow_passes_default_answers(mocker):
    captured = {}
    mocker.patch("src.automation.flows.greenhouse_apply_flow.run_greenhouse_apply",
                 side_effect=lambda **kw: captured.update(kw) or {
                     "applied": [], "skipped": [], "jobs_found": 0, "summary": "none"})
    from src.automation.flows.greenhouse_apply_flow import GreenhouseApplyFlow

    GreenhouseApplyFlow().kickoff(inputs={
        "boards": "figma", "first_name": "Ada", "last_name": "L", "email": "a@x.io",
        "notice_period": "2 months"})
    assert captured["notice_period"] == "2 months"
    assert captured["languages"] == "English, Chinese"
    assert captured["salary_expectation"] == "70000 SGD / year"
    assert captured["has_criminal_record"] is False


def test_sponsorship_where_you_reside_is_judged_on_your_location_not_the_job():
    q = _q("Will you require employer sponsorship for a work visa or work permit in the country "
           "where you currently reside or are currently based?", "q1", SELECT, values=["Yes", "No"])
    answers, unanswered = _plan([q], DEFAULTS, location={"name": "Singapore"})
    assert unanswered == [] and _by_field(answers) == {"q1": "No"}
    # The generic form of the question still follows the job's country.
    answers, _ = _plan([_q("Will you require visa sponsorship for this role?", "q2", SELECT,
                           values=["Yes", "No"])], DEFAULTS, location={"name": "Singapore"})
    assert _by_field(answers) == {"q2": "Yes"}


def test_extra_answer_that_fits_no_option_falls_back_to_the_rules():
    """'currently reside: Taiwan' (meant for 'where do you reside?') must not
    block a Yes/No sponsorship question that happens to contain the keyword."""
    profile = {**DEFAULTS, "extra_answers": [("currently reside", "Taiwan")]}
    answers, unanswered = _plan([
        _q("Will you require employer sponsorship for a work visa or work permit in the country "
           "where you currently reside or are currently based?", "q1", SELECT, values=["Yes", "No"]),
        _q("In which country do you currently reside?", "q2", SELECT, values=["Singapore", "Taiwan"]),
    ], profile, location={"name": "Hong Kong; Singapore"})
    assert unanswered == []
    assert _by_field(answers) == {"q1": "No", "q2": "Taiwan"}


# ── PR #80 review fixes ───────────────────────────────────────────────────────

def test_state_and_country_for_a_us_city_state_location():
    profile = {**DEFAULTS, "location": "San Francisco, CA"}
    answers, _ = _plan([_q("City", "q1"), _q("State", "q2"), _q("Country", "q3")], profile)
    assert _by_field(answers) == {"q1": "San Francisco", "q2": "CA", "q3": "United States"}


def test_what_languages_do_you_code_in_is_a_programming_question():
    answers, _ = _plan([_q("What languages do you code in?", "q1")], DEFAULTS)
    assert _by_field(answers) == {"q1": "Python, Java, JavaScript"}


def test_named_language_gets_yes_no_only_for_a_yes_no_question():
    answers, unanswered = _plan([_q("Describe your English language skills.", "q1"),
                                 _q("Are you fluent in English?", "q2")], DEFAULTS)
    assert unanswered == ["Describe your English language skills."]
    assert _by_field(answers) == {"q2": "Yes"}


def test_current_salary_expectation_is_the_expectation():
    answers, _ = _plan([_q("What is your current salary expectation?", "q1")], DEFAULTS)
    assert _by_field(answers) == {"q1": "70000 SGD / year"}


# ── browser fill: checkbox multi-selects and optional blocks ──────────────────

def test_optional_education_block_answers_are_marked_optional():
    profile = {**DEFAULTS, "education": {"school": "NTU", "degree": "Master's Degree"}}
    answers, _ = _plan([], profile, education="education_optional")
    assert {a["field"]: a.get("optional") for a in answers} == {"school--0": True, "degree--0": True}
    answers, _ = _plan([], profile, education="education_required")
    assert {a["field"]: a.get("optional") for a in answers} == {"school--0": False, "degree--0": False}


class _FakeLocator:
    def __init__(self, n):
        self._n = n
        self.first = self

    def count(self):
        return self._n


class _FakePage:
    """Just enough page for _fill_form / _choose_option routing."""
    def __init__(self, checkbox_fields=()):
        self.checkbox_fields = set(checkbox_fields)
        self.keyboard = type("K", (), {"press": lambda *_: None})()

    def locator(self, sel):
        if sel.startswith("input[type='checkbox'][name="):
            name = sel.split("[name='", 1)[1][:-2]  # drop the closing "']" only
            return _FakeLocator(1 if name in self.checkbox_fields else 0)
        return _FakeLocator(1)  # every id "exists" (a <fieldset> has the field's id)

    def evaluate(self, *_):
        return None


def test_fieldset_multiselect_goes_to_the_checkbox_handler(mocker):
    boxes = mocker.patch.object(gh, "_check_boxes", return_value=True)
    page = _FakePage(checkbox_fields={"question_1[]"})
    assert gh._choose_option(page, "question_1[]", ["Acknowledged"]) is True
    boxes.assert_called_once_with(page, "question_1[]", ["Acknowledged"])


def test_optional_answer_that_cannot_be_filled_does_not_fail_the_form(mocker):
    mocker.patch.object(gh, "_choose_option", return_value=False)
    failed = gh._fill_form(_FakePage(), [
        {"field": "school--0", "kind": "combo", "value": "NTU", "label": "School", "optional": True},
        {"field": "question_2", "kind": "select", "value": "Yes", "label": "Required Q"},
    ], log=lambda _m: None)
    assert failed == ["Required Q"]
