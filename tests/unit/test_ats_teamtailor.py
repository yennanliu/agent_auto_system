"""Teamtailor adapter against real captured responses (Anyfin, 2026-11-21). No network."""
import json
from pathlib import Path

import pytest

from src.automation.tools import ats_teamtailor as tt
from src.automation.tools.ats_common import build_profile, rule_answer
from src.automation.tools.ats_router import BoardRef, detect_ats, parse_board_ref

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "ats"
FEED = json.loads((FIX / "teamtailor_jobs.json").read_text(encoding="utf-8"))
FORM_HTML = (FIX / "teamtailor_form.html").read_text(encoding="utf-8")


class _Resp:
    def __init__(self, status=200, data=None, text=""):
        self.status_code, self._data, self.text = status, data, text

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Client:
    def __init__(self, routes):
        self.routes = routes

    def get(self, url, **_kw):
        return self.routes.get(url, _Resp(404))


def _jobs():
    return tt.TeamtailorAdapter().discover(
        _Client({"https://anyfin.teamtailor.com/jobs.json": _Resp(data=FEED)}), BoardRef("teamtailor", "anyfin"))


def _profile(extra=""):
    return build_profile(first_name="Ada", last_name="Lovelace", email="ada@example.com", phone="+46 70 123 4567",
                         current_location="Stockholm, Sweden", authorized_countries="Sweden",
                         resume_path="/tmp/cv.pdf", extra_answers=extra)


# ── routing ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ref, want", [
    ("teamtailor:anyfin", BoardRef("teamtailor", "anyfin")),
    ("teamtailor:anyfin/8651426", BoardRef("teamtailor", "anyfin", "8651426")),
    ("https://anyfin.teamtailor.com/jobs", BoardRef("teamtailor", "anyfin")),
    ("https://anyfin.teamtailor.com/jobs/8651426-site-reliability-engineer", BoardRef("teamtailor", "anyfin", "8651426")),
])
def test_routing(ref, want):
    assert parse_board_ref(ref) == want


@pytest.mark.parametrize("url", ["https://www.teamtailor.com/en/pricing", "https://app.teamtailor.com/x",
                                 "https://anyfin.teamtailor.com/jobs/abc", "https://evilteamtailor.com/jobs/1"])
def test_not_a_board(url):
    assert detect_ats(url) is None


# ── discover / form ──────────────────────────────────────────────────────────

def test_discover_reads_the_json_feed():
    jobs = _jobs()
    assert len(jobs) == len(FEED["items"])
    j = next(j for j in jobs if j["job_id"] == "8651426")
    assert j["title"] == "Site Reliability Engineer" and j["company"] == "Anyfin"
    assert j["location"] == "Stockholm, Sweden"
    assert j["url"] == "https://anyfin.teamtailor.com/jobs/8651426-site-reliability-engineer"


def test_unknown_company():
    with pytest.raises(RuntimeError, match="no such Teamtailor company"):
        tt.TeamtailorAdapter().discover(_Client({}), BoardRef("teamtailor", "nope"))


def test_parse_form_reads_builtins_and_questions():
    form = tt.parse_form(FORM_HTML)
    assert set(form["fields"]) == {"candidate[first_name]", "candidate[last_name]", "candidate[email]", "candidate[phone]"}
    assert all(f["required"] for f in form["fields"].values())
    assert form["fields"]["candidate[first_name]"]["label"] == "First name"
    assert form["resume_required"] is True
    assert form["cover_letter"] == "candidate[job_applications_attributes][0][cover_letter]"
    qs = [(q["kind"], q["required"], q["label"][:30], q["options"]) for q in form["questions"]]
    assert qs == [("choice", True, "Do you have a work permit for ", ["Yes", "No"]),
                  ("choice", True, "What is your citizenship?", ["Swedish", "Other European Union country",
                                                                  "The rest of the world"]),
                  ("boolean", True, "Background check? Due to the n", [])]
    assert all(q["id"].isdigit() for q in form["questions"])


def test_labels_lose_the_required_marker_anywhere():
    assert tt._clean("First name * Required") == "First name"
    assert tt._clean("Background check? * Required Due to our business") == "Background check? Due to our business"


def test_location_from_jobposting():
    jp = {"jobLocation": [{"address": {"addressLocality": "Copenhagen", "addressRegion": "Denmark"}}],
          "jobLocationType": "TELECOMMUTE"}
    assert tt.posting_location(jp) == "Copenhagen, Denmark (Remote)"
    assert tt.posting_location({}) == ""


# ── plan ─────────────────────────────────────────────────────────────────────

def _plan(extra=""):
    job = next(j for j in _jobs() if j["job_id"] == "8651426")
    return tt.TeamtailorAdapter().plan(tt.parse_form(FORM_HTML), job, _profile(extra), None)


def test_personal_questions_are_not_guessed():
    answers, unanswered = _plan()
    assert unanswered[0] == "What is your citizenship?"
    # The background check asks for CONSENT; the criminal-record rule must not
    # answer it "No" (that would refuse the check).
    assert unanswered[1].startswith("Background check?")
    by = {a["label"]: a["value"] for a in answers}
    assert by["Do you have a work permit for Sweden?"] == "Yes"            # authorized_countries
    assert by["First name"] == "Ada" and by["Resume"] == "/tmp/cv.pdf"


def test_extra_answers_complete_it():
    answers, unanswered = _plan("What is your citizenship: The rest of the world\naccept this step: Yes")
    assert unanswered == []
    def got(start):
        return next((a["kind"], a["value"]) for a in answers if a["label"].startswith(start))
    assert got("What is your citizenship") == ("q_choice", "The rest of the world")
    assert got("Background check?") == ("q_boolean", "Yes")


def test_criminal_rule_answers_only_a_question_about_having_a_record():
    assert rule_answer("Have you ever been convicted of a criminal offence?", {}) == "No"
    assert rule_answer("We review any criminal record. Do you accept this step?", {}) is None
    # A consent wording falls to the shared consent rule like any consent box;
    # what matters is that the has-a-record flag never answers it "No".
    assert rule_answer("Do you consent to a criminal record check?", {}) != "No"


# ── PR #126 review: a [number] answer keeps its value ───────────────────────

@pytest.mark.parametrize("answer, want", [("70,000", "70000"), ("5,500 SEK / month", "5500"),
                                          ("8 years", "8"), ("5,5", "5.5"), ("negotiable", None)])
def test_number_questions_keep_the_value(answer, want):
    form = {"fields": {}, "resume": False, "resume_required": False, "cover_letter": "",
            "questions": [{"id": "1", "kind": "text", "label": "What is your salary expectation?", "required": True,
                           "multiple": False, "number": True, "options": []}]}
    job = next(j for j in _jobs() if j["job_id"] == "8651426")
    answers, unanswered = tt.TeamtailorAdapter().plan(form, job, _profile(f"salary expectation: {answer}"), None)
    if want is None:
        assert answers == [] and unanswered == ["What is your salary expectation?"]
    else:
        assert answers[0]["value"] == want and unanswered == []
