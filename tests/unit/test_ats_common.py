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


@pytest.mark.parametrize("label,years,answer", [
    ("Do you have seven (7) years of full-stack or embedded device software development?", 5, "No"),
    ("Do you have 7 years of professional software development experience?", 8, "Yes"),
    ("Do you have at least 3 years of experience with AWS?", 2, "No"),
])
def test_do_you_have_n_years_compares_with_the_profile(label, years, answer):
    """Found in the Workday e2e (3M asks 'seven (7) years'): answered truthfully
    from the profile's years instead of being left unanswered."""
    assert ac.rule_answer(label, {**PROFILE, "years": years}) == answer
    q = Question("q", label, "select", required=True, options=["Yes", "No"])
    assert ac.answer_question(q, {**PROFILE, "years": years})["value"] == answer


@pytest.mark.parametrize("label,years", [
    # The profile has total years only. Enough of them says nothing about one
    # skill (Experian, live: "8-12 years" of C# would have been sent).
    ("Do you have seven (7) years of full-stack or embedded device software development?", 8),
    ("Do you have 5+ years of Python experience?", 5),
    ("Do you possess ten years or more of experience leading teams?", 12),
    ("How many years of experience with C# do you have?", 10),
    ("How many years have you used SQL?", 10),
    ("How many years of experience do you have in .NET development, including building REST APIs?", 10),
])
def test_years_of_one_skill_are_never_taken_from_the_total(label, years):
    assert ac.rule_answer(label, {**PROFILE, "years": years}) is None


def test_years_of_general_software_work_are_the_total():
    p = {**PROFILE, "years": 10}
    assert ac.rule_answer("How many years of Software Engineering/Product Development experience "
                          "do you have?", p) == "10"


def test_citizenship_is_never_read_from_a_visa_remark():
    """Western Digital, live: the question's "(…temporary U.S. visa, check No)"
    made the sponsorship rule answer "No"."""
    p = {**PROFILE, "authorized_countries": {"united states"}, "job_country": "United States"}
    assert ac.rule_answer("Are you a citizen of the U.S., a lawful permanent resident of the U.S., or a "
                          "non-U.S. citizen who is in the U.S. as an asylee or a refugee? (if you only hold "
                          "a temporary work/travel/student U.S. visa, please check No)", p) is None


def test_an_agreement_restricting_your_right_to_work_is_not_authorization():
    """Western Digital, live: "right to work" made it a work-authorization "Yes"."""
    assert ac.rule_answer("Are you currently party to any employment, consulting or personal services "
                          "agreement that (i) restricts your right to terminate such agreements or (ii) "
                          "restricts your right to work for WD immediately upon hire?", PROFILE) is None


def test_prior_employment_by_the_companys_initials():
    wd = {**PROFILE, "job_company": "Western Digital"}
    assert ac.rule_answer("Have you ever been employed by WD or any of its subsidiaries?", wd) == "No"
    assert ac.rule_answer("Have you ever been employed by IBM?", wd) is None


def test_how_did_you_first_hear():
    assert ac.rule_answer("How did you first hear about the position you applied for?", PROFILE) == "__source__"


def test_how_many_years_still_returns_the_number():
    assert ac.rule_answer("How many years of experience do you have?", {**PROFILE, "years": 4}) == "4"


def test_required_sex_question_declines():
    """Found in the Workday e2e: 3M asks 'Please select your sex.' — a voluntary
    self-identification question, answered only with the decline option."""
    q = Question("q", "Please select your sex.", "select", required=True,
                 options=["Female", "Male", "I do not wish to answer"])
    assert ac.answer_question(q, PROFILE)["value"] == "I do not wish to answer"
    q = Question("q", "Essex office location", "select", required=True, options=["Yes", "No"])
    assert ac.answer_question(q, PROFILE) is None                        # 'sex' only as a word


@pytest.mark.parametrize("decline", ["Not declared", "Undeclared", "I choose not to disclose",
                                     "I'd rather not say", "Not specified", "Preferred not to Answer"])
def test_decline_option_wordings(decline):
    assert ac.decline_option(["Female", "Male", decline]) == decline
    assert ac.decline_option(["Female", "Male"]) is None


def test_prior_employment_is_about_the_hiring_company_only():
    at = {"job_company": "figma"}
    assert ac.rule_answer("Have you ever worked for Figma before?", at) == "No"
    assert ac.rule_answer("Have you ever worked for us before?", at) == "No"
    assert ac.rule_answer("Are you a former employee?", at) == "No"
    # Another employer is a fact about the person: never guessed.
    assert ac.rule_answer("Have you ever been employed by PricewaterhouseCoopers?", at) is None
    assert ac.rule_answer("Have you ever worked for Epic Systems in the past?", at) is None
    # Never a prefix match: "a state government" is not "acme" (PR review).
    acme = {"job_company": "acme"}
    assert ac.rule_answer("Have you ever been employed by a State government?", acme) is None
    assert ac.rule_answer("Have you ever worked for Armanino before?", {"job_company": "arm"}) is None
    assert ac.rule_answer("Have you previously worked for Acme's subsidiaries?", acme) == "No"
    # A leading determiner isn't the name (Micron, Workday, live 2026-11-21).
    micron = {"job_company": "micron"}
    assert ac.rule_answer("Have you previously been employed by any Micron company?", micron) == "No"
    assert ac.rule_answer("Have you ever worked for the Micron group?", micron) == "No"
    assert ac.rule_answer("Have you previously been employed by any Epic Systems company?", micron) is None
    assert ac.rule_answer("Have you ever worked for Metropolitan Hospital before?",
                          {"job_company": "metropolitan-hospital"}) == "No"
    # Without the job's company (legacy callers) the old answer stands.
    assert ac.rule_answer("Have you ever worked at Acme before?", {}) == "No"


def test_with_job_location_attaches_the_company():
    p = ac.with_job_location({}, None, "3m")
    assert p == {"job_company": "3m"}
    assert ac.with_job_location({}, None) == {}


def test_have_you_agreed_to_the_terms_is_consent():
    # PR review: "agreed to" is the wording of consent boxes, not of a factual question.
    assert ac.rule_answer("Have you read and agreed to the terms and conditions?", {}) == "__agree__"
    assert ac.rule_answer("Have you signed an agreement containing a non-compete?", {}) != "__agree__"


def test_explicit_extra_answers_survive_the_yes_no_guard():
    """PR #122 review: the guard drops only INFERRED Yes/No on non yes/no text
    questions; the user's own Extra Answer is kept."""
    label = "Please confirm your willingness to relocate"
    mine = {"extra_answers": [("willingness to relocate", "Yes")]}
    assert ac.answer_question(ac.Question("k", label, "text", True, []), mine)["value"] == "Yes"
    inferred = {"authorized_countries": {"netherlands"}}
    assert ac.answer_question(ac.Question(
        "k", "If you selected valid work permit, please specify which type", "text", False, []), inferred) is None


def test_availability_after_an_offer_is_the_notice_period():
    """Micron (Workday), live 2026-11-21: the options are "Within 1 month after
    offer acceptance", …; a notice period that maps onto none stays unanswered."""
    q = "When would you be available if an offer was accepted?"
    opts = ["Immediately after offer acceptance", "Within 1 month after offer acceptance",
            "Within 2 months after offer acceptance", "More than 2 months after offer acceptance"]
    assert ac.rule_answer(q, {"notice_period": "1 month"}) == "1 month"
    assert ac.rule_answer(q, {}) is None
    ans = ac.answer_question(ac.Question(q, q, "select", True, opts), {"notice_period": "1 month"}, None)
    assert ans["value"] == "Within 1 month after offer acceptance"


@pytest.mark.parametrize("label", [
    "Were you referred by an employee?", "Referral source",
    "Are you referred to this job by a Micron employee?",   # Micron (Workday), live 2026-11-21
])
def test_referral_questions_answer_no(label):
    assert ac.rule_answer(label, {}) == "No"


def test_saved_default_answers_apply_after_the_runs_own(tmp_path, monkeypatch):
    f = tmp_path / "default_answers.txt"
    f.write_text("# personal facts\nbonded: No\nacademic ranking: 11%-20%\n\nno colon line\n")
    monkeypatch.setenv("ATS_DEFAULT_ANSWERS_FILE", str(f))
    p = ac.build_profile(first_name="A", last_name="B", email="a@b.c", extra_answers="bonded: Yes, TWD 1")
    assert p["extra_answers"] == [("bonded", "Yes, TWD 1"), ("bonded", "No"), ("academic ranking", "11%-20%")]
    assert ac.rule_answer("Are you bonded by your current company/school?", p) == "Yes, TWD 1"
    q = "Please select your academic ranking:"
    ans = ac.answer_question(ac.Question(q, q, "select", True, ["Top 10%", "11%-20%", "21%-50%"]), p, None)
    assert ans["value"] == "11%-20%"
    monkeypatch.setenv("ATS_DEFAULT_ANSWERS_FILE", str(tmp_path / "missing.txt"))
    assert ac.default_answers() == []


def test_existing_employee_of_the_hiring_company():
    """HP (Workday), live 2026-11-23."""
    q = ("To help us understand your preferences related to personal data and uses for future "
         "recruitment processes, are you an existing HP employee?")
    me = {**PROFILE, "job_company": "hp", "employment": {"company": "Self-employed"}}
    assert ac.rule_answer(q, me) == "No"
    # Without a known current employer, or when it IS the company: never guessed.
    assert ac.rule_answer(q, {**PROFILE, "job_company": "hp", "employment": {}}) is None
    assert ac.rule_answer(q, {**me, "employment": {"company": "HP"}}) is None


@pytest.mark.parametrize("company,aliases,options,expected", [
    # Cadence (Workday), live: second level of "Job Board/Social Media Web Site".
    ("cadence", (), ["104 portal", "51job", "Bizreach", "Cadence.com", "DICE", "Facebook"], "Cadence.com"),
    ("amat", ("Applied Materials",), ["I currently work at/for Applied Materials",
                                      "Applied Materials Corporate Website", "Staffing Agency"],
     "Applied Materials Corporate Website"),
    ("amat", ("Applied Materials",), ["I currently work at/for Applied Materials", "Staffing Agency"], None),
    ("cadence", (), ["Agency", "Employee Referral", "Job Fair"], None),
])
def test_company_site_option(company, aliases, options, expected):
    p = ac.with_job_location(PROFILE, None, company, aliases=aliases)
    assert ac.company_site_option(options, p) == expected
    q = "How Did You Hear About Us?"
    if expected:
        assert ac.answer_question(Question(q, q, "select", True, options), p)["value"] == expected


def test_prior_employment_directly_employed_or_contracted():
    """Intel (Workday), live 2026-11-23."""
    q = ("Are you currently or have you previously been directly employed, accepted an offer, "
         "or contracted with Intel or an Intel subsidiary?")
    assert ac.rule_answer(q, {**PROFILE, "job_company": "intel"}) == "No"
    assert ac.rule_answer(q.replace("Intel", "Acme"), {**PROFILE, "job_company": "intel"}) is None


def test_a_sponsorship_question_with_a_residency_note_is_still_sponsorship():
    """Intel (Workday), live: its sponsorship question notes student visas and
    permanent residents; the citizenship guard must not swallow it."""
    p = {**PROFILE, "authorized_countries": {"united states"}, "job_countries": {"united states"},
         "needs_sponsorship": True}
    q = ("Do you now, or in the future, require Intel to sponsor you for a visa to work in the United "
         "States? NOTE: Individuals with work authorization issued pursuant to their student visa "
         "(e.g., F-1, J-1, M-1) or currently a lawful permanent resident should answer accordingly.")
    assert ac.rule_answer(q, p) == "Yes"


def test_intel_sponsorship_with_a_permanent_resident_note():
    """Intel, live 2026-11-23: "…Intel filing employment-based visa and permanent
    resident applications…" made the citizenship rule answer "No" (no
    sponsorship needed) for a candidate not authorized in the U.S."""
    q = ("11)* Do you now, or in the future, require Intel to sponsor you for a visa to work in the "
         "United States? NOTE: … Intel immigration sponsorship refers to Intel filing employment-based "
         "visa and permanent resident applications on behalf of the employee in order for the "
         "individual to have the legal right to work in the U.S.")
    p = ac.with_job_location({**PROFILE, "authorized_countries": {"taiwan"}}, "United States", "intel")
    assert ac.rule_answer(q, p) == "Yes"
    p_us = ac.with_job_location({**PROFILE, "authorized_countries": {"united states"}}, "United States", "intel")
    assert ac.rule_answer(q, p_us) == "No"


def test_sponsorship_note_about_a_current_employer_is_not_the_employer_question():
    """Intel, live 2026-11-24: "…work visas specific to their current employer…"
    made the current-employer rule answer "Self-employed"."""
    q = ("11)* Do you now, or in the future, require Intel to sponsor you for a visa to work in the "
         "United States? NOTE: Individuals … currently on work visas specific to their current "
         "employer (e.g., H-1B, O-1, TN, H-1B1, E-3), should answer “Yes”.")
    p = ac.with_job_location(ac.build_profile(first_name="A", last_name="B", email="a@b.c",
                                              authorized_countries="Taiwan", current_company="Self-employed"),
                             "United States", "intel")
    assert ac.rule_answer(q, p) == "Yes"
    assert ac.rule_answer("Who is your current employer?", p) == "Self-employed"
