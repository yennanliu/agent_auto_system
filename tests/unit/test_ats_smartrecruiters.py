"""SmartRecruiters apply: planning one screening question (no browser).

Question shapes and option wordings are the ones read from a live Wise posting
(Senior Staff Software Engineer, 2026-11-20): TEXT, SELECT (incl. multi-select),
RADIO, LANGUAGES and INFO blocks, with a "prefer not to answer" option on every
diversity question.
"""
import pytest

from src.automation.tools import ats_smartrecruiters as sr
from src.automation.tools.ats_common import build_profile

PROFILE = build_profile(first_name="Ada", last_name="Lovelace", email="ada@example.com",
                        phone="+44 7700 900123", current_location="London, United Kingdom",
                        authorized_countries="United Kingdom", years_experience=8,
                        notice_period="1 month")
DECLINE = "I prefer not to answer"
ELIGIBLE = ["I hold citizenship or permanent residency (No visa/sponsorship required)",
            "I hold a temporary work visa / permit that allows me to work full-time",
            "I hold a student or temporary visa with work-hour restrictions",
            "I do not currently have work authorization and will require visa sponsorship", "Other"]


def _q(label, type_="SELECT", required=True, options=(), multiple=False):
    return {"type": type_, "label": label, "required": required, "options": list(options),
            "multiple": multiple, "value": "", "id": "question_x", "ordinal": 0}


def test_text_question_from_the_profile():
    assert sr.plan_question(_q("What is your notice period?", "TEXT"), PROFILE) == {"value": "1 month"}


def test_self_describe_text_is_left_empty():
    assert sr.plan_question(_q("If you have chosen to self describe, please specify below", "TEXT",
                               required=False), PROFILE) is None


@pytest.mark.parametrize("label", [
    "What's your age?",
    "Are you part of the first generation of your family to study further education? e.g. college or university?",
    "Do you have, or have you previously had, a disability or neurodiversity?",
    "What gender do you most closely identify with?",
    "Do you identify with a gender that is different to the gender assigned to you at birth?",
    "Are you Hispanic/Latin American? (https://wise.jobs/definitions)",
    "If you're applying from the US, please let us know your veteran status.",
])
def test_diversity_questions_only_ever_decline(label):
    opts = ["18-24", "25-34", "Yes", "No", "Man", "Woman", DECLINE]
    assert sr.plan_question(_q(label, options=opts), PROFILE) == {"value": DECLINE}


def test_multi_select_diversity_declines_as_a_list():
    q = _q("I identify my sexual orientation as (mark all that apply)", options=["Bisexual", DECLINE],
           multiple=True)
    assert sr.plan_question(q, PROFILE) == {"value": [DECLINE]}


def test_diversity_without_a_decline_option_is_unanswered():
    assert sr.plan_question(_q("What gender do you most closely identify with?",
                               options=["Man", "Woman"]), PROFILE) is None


def test_eligibility_wording_is_not_guessed():
    """'Yes, authorized' does not say citizen vs visa holder: left to the user."""
    assert sr.plan_question(_q("How are you eligible to work in this location?", options=ELIGIBLE),
                            PROFILE) is None


def test_extra_answers_pick_the_matching_option():
    p = {**PROFILE, "extra_answers": [("eligible to work in this location",
                                        "I hold citizenship or permanent residency")]}
    assert sr.plan_question(_q("How are you eligible to work in this location?", options=ELIGIBLE),
                            p)["value"] == ELIGIBLE[0]


def test_personal_radio_facts_are_not_guessed():
    for label in ("Are you currently a Wise customer?",
                  "Does the advertised salary band meet your expectations?"):
        assert sr.plan_question(_q(label, "RADIO", options=["Yes", "No"]), PROFILE) is None


def test_info_and_languages_blocks_are_skipped():
    assert sr.plan_question(_q("", "INFO", required=False), PROFILE) is None
    assert sr.plan_question(_q("Languages", "LANGUAGES", required=False), PROFILE) is None


def test_city_suggestion_country_match():
    assert sr.t_has("London, England, United Kingdom", "united kingdom")
    assert not sr.t_has("London, Ontario, Canada", "united kingdom")
    assert sr.t_has("Taipei, Taipei City, Taiwan", "taiwan")


class _Boom:
    def evaluate_all(self, _js):
        raise RuntimeError("detached")


def test_option_labels_survives_a_detached_menu():
    # The label-reading itself (property, not attribute) runs in a real
    # browser: tests/e2e/test_smartrecruiters_apply_browser.py
    # ::test_option_labels_reads_the_label_property.
    assert sr.option_labels(_Boom()) == []


def test_city_country_match_is_whole_part_only():
    assert sr.t_has("San Francisco, CA, US", "CA")
    assert not sr.t_has("Kingston, Jamaica", "CA")                      # not a substring
    assert not sr.t_has("London, Ontario, Canada", "United Kingdom")


def test_apply_url_falls_back_to_the_job_page(monkeypatch):
    import httpx

    def down(*_a, **_k):
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(httpx, "get", down)
    job = {"token": "Wise", "job_id": "1", "url": "https://jobs.smartrecruiters.com/Wise/1"}
    assert sr.SmartRecruitersAdapter().apply_url(job) == "https://jobs.smartrecruiters.com/Wise/1?oga=true"
