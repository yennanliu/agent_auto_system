"""The platform-neutral ATS core (``ats_common``) used by every apply adapter.

The answer rules themselves are exercised in depth through Greenhouse's
``plan_answers`` in ``test_greenhouse_apply.py``; these tests pin the
adapter-facing API: ``Question`` → ``answer_question`` / ``plan_questions``,
title matching, the keyed ledger and the job-location context.
"""
import json

import pytest

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
    # "Chinese, Cantonese" is the shorter "Chinese" hit, so only the
    # never-Cantonese rule keeps it out.
    q = Question("q3", "Which languages do you speak?", "multiselect",
                 options=["English", "Chinese, Cantonese", "Mandarin Chinese (Putonghua)", "Japanese"])
    assert ac.answer_question(q, PROFILE)["value"] == ["English", "Mandarin Chinese (Putonghua)"]


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


# ── option matching ───────────────────────────────────────────────────────────

def test_match_option_exact_then_prefix_then_substring_shortest_wins():
    opts = ["National Taiwan University of Science And Technology",
            "National Taiwan University (NTU)", "No", "Not now"]
    assert ac.match_option(opts, "no") == "No"
    assert ac.match_option(["No, I am not", "Not now"], "no") == "No, I am not"
    assert ac.match_option(["I know", "Not now"], "no") is None          # never inside "know"/"now"
    assert ac.match_option(opts, "National Taiwan University") == "National Taiwan University (NTU)"
    assert ac.match_option(opts, "") is None


def test_years_option_ranges_open_ended_and_exact():
    opts = ["Less than 1", "1-3", "4-7", "8 or more"]
    assert ac.years_option(opts, 2) == "1-3"
    assert ac.years_option(opts, 8) == "8 or more"
    assert ac.years_option(["5+ years"], 12) == "5+ years"
    assert ac.years_option(["0", "1", "2"], 1) == "1"
    assert ac.years_option(["0-2", "3-5"], 9) is None


# ── country / location context ───────────────────────────────────────────────

def test_countries_in_and_job_countries():
    assert ac.countries_in("Are you eligible to work in the US?") == {"united states"}
    assert ac.countries_in("eligible to work for us?") == set()          # "us" the company
    assert ac.job_countries("San Francisco, CA") == {"united states"}
    assert ac.job_countries("Toronto") == {"canada"}
    assert ac.job_countries("Remote - Worldwide") == {"*"}
    assert ac.job_countries("Starbase") == set()


def test_with_job_location_does_not_mutate_profile():
    prof = {"location": "Taipei"}
    out = ac.with_job_location(prof, "London")
    assert out["job_countries"] == {"united kingdom"} and "job_countries" not in prof


# ── rule answers ─────────────────────────────────────────────────────────────

def _rule(label, **over):
    return ac.rule_answer(label, {**PROFILE, **over})


def test_extra_answers_win_over_rules():
    assert _rule("What is your expected salary?", extra_answers=[("salary", "negotiable")]) == "negotiable"


def test_sponsorship_and_authorization_are_answered_independently():
    p = {"authorized_countries": set()}
    assert _rule("Will you now or in the future require visa sponsorship?", **p) == "No"
    assert _rule("Will you require visa sponsorship?", needs_sponsorship=True, **p) == "Yes"
    assert _rule("Are you legally authorized to work in this country?", **p) == "Yes"
    # "authorized … without sponsorship" is an authorization question.
    assert _rule("Are you authorized to work without sponsorship?", needs_sponsorship=True, **p) == "No"


def test_named_country_authorization_uses_authorized_countries():
    assert _rule("Are you authorized to work in Taiwan?") == "Yes"
    assert _rule("Are you authorized to work in the United States?") == "No"
    assert _rule("Will you require sponsorship to work in the United States?") == "Yes"


def test_salary_current_vs_expected_never_mixed():
    assert _rule("What is your expected salary?") == "70000 SGD / year"
    assert _rule("What is your current salary?") is None                 # unset → not the expectation
    assert _rule("Last drawn salary", current_salary="60000") == "60000"


def test_misc_rules():
    assert _rule("Have you ever worked at Acme before?") == "No"
    assert _rule("Were you referred by an employee?") == "No"
    assert _rule("How did you hear about this job?") == "__source__"
    assert _rule("How many years of experience do you have?") == "4"
    assert _rule("Preferred name") == "Ada"
    assert _rule("Full legal name") == "Ada Lovelace"
    assert _rule("I acknowledge the privacy notice") == "__agree__"
    assert _rule("Have you ever been convicted of a criminal offence?") == "No"
    assert _rule("Have you ever been convicted of a criminal offence?", has_criminal_record=True) == "Yes"
    assert _rule("Tell us about a time you failed") is None


def test_location_text_questions_split_the_profile_location():
    assert _rule("City") == "Taipei"
    assert _rule("Country") == "Taiwan"
    assert _rule("State") == "Taipei"                                     # no state: first division
    assert _rule("State", location="San Francisco, CA") == "CA"
    assert _rule("Country", location="San Francisco, CA") == "United States"
    assert _rule("Are you located in Singapore?") == "No"
    assert _rule("Are you located in Taiwan?") == "Yes"


def test_spoken_language_yes_no_only_for_yes_no_phrasing():
    assert _rule("Can you speak Chinese fluently?") == "Yes"
    assert _rule("Can you speak Japanese fluently?") == "No"
    assert _rule("Describe your English language skills") is None


# ── mapping answers onto options ─────────────────────────────────────────────

def test_to_option_special_answers():
    assert ac.to_option("__agree__", ["No", "I agree"]) == "I agree"
    assert ac.to_option("__source__", ["LinkedIn", "Company website", "Other"]) == "Company website"
    assert ac.to_option("__source__", ["Referral", "Other"]) == "Other"
    assert ac.to_option("No", ["I currently work at X", "I have never worked at X"]) == "I have never worked at X"
    assert ac.to_option("No", ["Citizen", "PR", "Others"]) == "Others"
    assert ac.to_option("Taipei, Taiwan", ["Japan", "Taiwan"]) == "Taiwan"
    assert ac.to_option("5 years", ["0-2 years", "3-5 years"]) == "3-5 years"


def test_programming_options_never_guess_a_level():
    plain = ["Python", "Java", "JavaScript"]
    assert ac.to_options(["Java"], plain, "Which programming languages do you know?") == ["Java"]
    leveled = ["Java Beginner", "Java Advanced", "Python Advanced"]
    label = "Which programming languages are you proficient in?"
    assert ac.to_options(["Java", "Python: Advanced"], leveled, label) == ["Python Advanced"]


def test_text_answers_render_special_values():
    q = Question("s", "How did you hear about us?", "text", required=True)
    assert ac.answer_question(q, PROFILE)["value"] == "Company careers page"
    consent = Question("c", "I acknowledge the terms", "text")
    assert ac.answer_question(consent, PROFILE) is None                   # a text box is no checkbox
    langs = Question("l", "Which languages do you speak?", "text")
    assert ac.answer_question(langs, PROFILE)["value"] == "English, Chinese"


def test_misleading_extra_answer_falls_back_to_rules_for_selects():
    q = Question("q", "Will you require sponsorship in the country where you currently reside?",
                 "select", required=True, options=["Yes", "No"])
    prof = {**PROFILE, "extra_answers": [("currently reside", "Taiwan")]}
    assert ac.answer_question(q, prof)["value"] == "No"


def test_question_options_default_is_not_shared():
    a, b = Question("a", "A", "select"), Question("b", "B", "select")
    a.options.append("x")
    assert b.options == []


# ── threading / browser helpers ──────────────────────────────────────────────

async def test_off_loop_moves_calls_off_a_running_event_loop():
    import asyncio
    import threading

    def where():
        try:
            asyncio.get_running_loop()
            return "loop"
        except RuntimeError:
            return threading.current_thread().name

    assert ac.off_loop(where) != "loop"
    assert ac.off_loop(lambda a, b: a + b, 2, 3) == 5


def test_off_loop_is_direct_without_a_loop():
    import threading
    assert ac.off_loop(lambda: threading.current_thread()) is threading.current_thread()


class _Loc:
    def __init__(self, n=1, visible=True, boom=False):
        self.n, self.visible, self.boom = n, visible, boom
        self.first = self

    def count(self):
        if self.boom:
            raise RuntimeError("detached")
        return self.n

    def is_visible(self):
        return self.visible


class _Page:
    def __init__(self, locs, texts=()):
        self.locs, self.texts = locs, set(texts)

    def locator(self, sel):
        return self.locs.get(sel, _Loc(0))

    def get_by_text(self, txt, exact=False):
        return _Loc(1, visible=txt in self.texts)


def test_first_visible_skips_missing_hidden_and_erroring_selectors():
    page = _Page({"a": _Loc(boom=True), "b": _Loc(visible=False), "c": _Loc()})
    assert ac.first_visible(page, ["a", "b", "missing", "c"]) is page.locs["c"]
    assert ac.first_visible(page, ["a", "b"]) is None


def test_visible_text_returns_the_first_shown_text():
    page = _Page({}, texts={"Thank you for applying"})
    assert ac.visible_text(page, ("Application submitted", "Thank you for applying")) == "Thank you for applying"
    assert ac.visible_text(page, ("nope",)) is None


def test_launch_browser_falls_back_to_bundled_chromium():
    calls = []

    class Chromium:
        def launch(self, **kw):
            calls.append(kw)
            if kw.get("channel") == "chrome":
                raise RuntimeError("Chrome not installed")
            return "browser"

    pw = type("PW", (), {"chromium": Chromium()})()
    assert ac.launch_browser(pw, headless=True) == "browser"
    assert calls[0]["channel"] == "chrome" and "channel" not in calls[1]
    assert calls[1]["args"] == ac.STEALTH_ARGS and calls[1]["headless"] is True


@pytest.mark.parametrize("label", [
    "Have you signed an agreement with a current/previous employer containing a provision related to non-competition?",
    "Did you enter into a non-disclosure agreement with your current employer?",
    "Are you bound by any agreement that restricts your employment?",
    "Have you ever agreed to an arbitration clause with a previous employer?",
])
def test_factual_agreement_questions_are_not_consent(label):
    """Found in the Workday e2e: 'Have you signed an agreement…' was answered
    'Yes' by the consent rule. It's a fact about the candidate, never assumed."""
    assert ac.rule_answer(label, PROFILE) is None
    q = Question("q", label, "select", required=True, options=["Yes", "No"])
    assert ac.answer_question(q, PROFILE) is None


@pytest.mark.parametrize("label", [
    "I acknowledge the privacy notice", "Applicant Arbitration Agreement Acknowledgement",
    "I certify that the information provided is true", "Do you consent to the processing of your data?",
])
def test_real_consent_boxes_still_agree(label):
    assert ac.rule_answer(label, PROFILE) == "__agree__"
