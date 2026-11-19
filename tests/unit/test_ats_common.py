"""The platform-neutral ATS core (``ats_common``) used by every apply adapter.

The answer rules themselves are exercised in depth through Greenhouse's
``plan_answers`` in ``test_greenhouse_apply.py``; these tests pin the
adapter-facing API: ``Question`` → ``answer_question`` / ``plan_questions``,
title matching, the keyed ledger and the job-location context.
"""
import json

from src.automation.tools import ats_common as ac
from src.automation.tools.ats_common import Question

PROFILE = {
    "first_name": "Ada", "last_name": "Lovelace", "location": "Taipei, Taiwan",
    "years": 4, "salary": "70000 SGD / year", "linkedin_url": "https://linkedin.com/in/ada",
    "work_authorized": True, "needs_sponsorship": False, "authorized_countries": {"taiwan"},
    "languages": "English, Chinese", "programming_languages": "Python, Java",
    "extra_answers": [],
}


def test_text_question_answered_from_profile():
    a = ac.answer_question(Question("q1", "LinkedIn Profile", "text"), PROFILE)
    assert a == {"field": "q1", "kind": "text", "value": "https://linkedin.com/in/ada",
                 "label": "LinkedIn Profile"}


def test_select_maps_years_onto_range_option():
    q = Question("q2", "How many years of experience do you have?", "select",
                 required=True, options=["0-2 years", "3-5 years", "6+ years"])
    assert ac.answer_question(q, PROFILE)["value"] == "3-5 years"


def test_multiselect_picks_every_spoken_language_never_cantonese():
    q = Question("q3", "Which languages do you speak?", "multiselect",
                 options=["English", "Cantonese", "Mandarin Chinese", "Japanese"])
    assert ac.answer_question(q, PROFILE)["value"] == ["English", "Mandarin Chinese"]


def test_unknown_job_country_never_guesses_authorization_even_with_llm():
    asked = []
    profile = ac.with_job_location(PROFILE, "Somewhere")  # no recognisable country
    q = Question("q4", "Are you authorized to work in the country for which you applied?",
                 "select", required=True, options=["Yes", "No"])
    assert ac.answer_question(q, profile, lambda *a: asked.append(a) or "Yes") is None
    assert asked == []


def test_job_location_decides_generic_authorization_question():
    q = Question("q5", "Are you authorized to work in the country for which you applied?",
                 "select", required=True, options=["Yes", "No"])
    assert ac.answer_question(q, ac.with_job_location(PROFILE, "Taipei"), None)["value"] == "Yes"
    assert ac.answer_question(q, ac.with_job_location(PROFILE, "New York, NY"), None)["value"] == "No"
    assert ac.with_job_location(PROFILE, None) is PROFILE


def test_required_single_option_select_is_acknowledged():
    q = Question("q6", "Aceito os termos", "select", required=True, options=["Sim, aceito"])
    assert ac.answer_question(q, PROFILE)["value"] == "Sim, aceito"


def test_required_demographic_select_declines():
    q = Question("q7", "Gender", "select", required=True,
                 options=["Male", "Female", "I don't wish to answer"])
    assert ac.answer_question(q, PROFILE)["value"] == "I don't wish to answer"
    assert ac.decline_option(["Yes", "Prefer not to say"]) == "Prefer not to say"
    assert ac.decline_option(["Yes", "No"]) is None


def test_llm_only_asked_for_required_questions():
    asked = []

    def fn(label, kind, options):
        asked.append(label)
        return "Because I love compilers"

    optional = Question("o", "Anything else you'd like to share?", "text")
    required = Question("r", "Why do you want to work here?", "text", required=True)
    assert ac.answer_question(optional, PROFILE, fn) is None
    assert ac.answer_question(required, PROFILE, fn)["value"] == "Because I love compilers"
    assert asked == ["Why do you want to work here?"]


def test_llm_failure_leaves_question_unanswered():
    def boom(*_a):
        raise RuntimeError("LLM down")

    q = Question("r", "Why do you want to work here?", "text", required=True)
    assert ac.answer_question(q, PROFILE, boom) is None


def test_plan_questions_reports_only_required_gaps():
    qs = [Question("a", "LinkedIn", "text", required=True),
          Question("b", "Favourite colour?", "text", required=True),
          Question("c", "Hobbies", "text"),
          Question("d", "", "text", required=True)]
    answers, unanswered = ac.plan_questions(qs, PROFILE)
    assert [a["field"] for a in answers] == ["a"]
    assert unanswered == ["Favourite colour?", "(unlabelled question)"]


def test_file_questions_are_left_to_the_adapter():
    assert ac.answer_question(Question("f", "Portfolio upload", "file", required=True), PROFILE) is None


def test_match_title_expands_role_shorthand():
    assert ac.match_title("Senior Software Engineer, Backend", "Taipei, Taiwan", ["swe"], ["taipei"])
    assert not ac.match_title("Maintenance Technician", "Taipei", ["ai"], [])
    assert not ac.match_title("Backend Engineer", "Remote - US", ["backend"], ["taipei"])
    assert ac.match_title("Anything", "", [], [])


def test_interleave_round_robins_groups():
    assert ac.interleave([[1, 2, 3], ["a"], [10, 20]]) == [1, "a", 10, 2, 20, 3]


def test_ledger_round_trip_with_adapter_key(tmp_path):
    path = str(tmp_path / "sub" / "ledger.json")
    assert ac.load_ledger(path) == {}
    ac.record_applied(path, "lever:acme/abc-123", {"title": "Eng", "company": "Acme"})
    led = ac.load_ledger(path)
    assert set(led) == {"lever:acme/abc-123"}
    assert led["lever:acme/abc-123"]["company"] == "Acme"
    assert "applied_at" in led["lever:acme/abc-123"]


def test_corrupt_ledger_reads_as_empty(tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(["not", "a", "dict"]))
    assert ac.load_ledger(str(path)) == {}


def test_profile_field_parsers():
    assert ac.parse_start("2025-06") == {"start_month": "June", "start_year": "2025"}
    assert ac.parse_start("nope") == {}
    assert ac.parse_extra_answers("notice: 2 weeks\nbad\n") == [("notice", "2 weeks")]
    assert ac.split_list("a, b;c\n d") == ["a", "b", "c", "d"]


def test_greenhouse_reexports_stay_compatible():
    """Greenhouse is the first adapter on the shared core; its old names must
    keep resolving to the shared implementations."""
    from src.automation.tools import greenhouse_apply_tool as gh
    assert gh.match_option is ac.match_option
    assert gh._years_option is ac.years_option
    assert gh.load_ledger is ac.load_ledger
    assert gh._launch_browser is ac.launch_browser
