"""Recruitee adapter against real captured API responses (Channable, 2026-11-20). No network."""
import json
from pathlib import Path

import pytest

from src.automation.tools import ats_recruitee as rc
from src.automation.tools.ats_common import build_profile
from src.automation.tools.ats_router import BoardRef, detect_ats, parse_board_ref

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "ats"
OFFERS = json.loads((FIX / "recruitee_offers.json").read_text(encoding="utf-8"))
OFFER = json.loads((FIX / "recruitee_offer.json").read_text(encoding="utf-8"))
SLUG = OFFER["offer"]["slug"]


class _Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Client:
    def __init__(self, routes):
        self.routes, self.urls = routes, []

    def get(self, url, **_kw):
        self.urls.append(url)
        for frag, resp in self.routes.items():
            if url.endswith(frag):
                return resp
        return _Resp({}, 404)


def _profile(extra=""):
    return build_profile(first_name="Ada", last_name="Lovelace", email="ada@example.com",
                         phone="+31 6 12345678", current_location="Utrecht, Netherlands",
                         authorized_countries="Netherlands", resume_path="/tmp/cv.pdf", extra_answers=extra)


def _job():
    return rc.RecruiteeAdapter().discover(_Client({"/api/offers/": _Resp(OFFERS)}),
                                          BoardRef("recruitee", "channable"))[0]


# ── routing ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ref, want", [
    ("recruitee:channable", BoardRef("recruitee", "channable")),
    ("recruitee:Channable/Haskell-Engineer", BoardRef("recruitee", "channable", "haskell-engineer")),
    ("https://channable.recruitee.com/", BoardRef("recruitee", "channable")),
    ("https://channable.recruitee.com/o/haskell-engineer/c/new", BoardRef("recruitee", "channable", "haskell-engineer")),
])
def test_routing(ref, want):
    assert parse_board_ref(ref) == want


@pytest.mark.parametrize("url", ["https://www.recruitee.com/pricing", "https://app.recruitee.com/x",
                                 "https://evilrecruitee.com/o/x", "https://a.b.recruitee.com/o/x"])
def test_not_an_employer_board(url):
    assert detect_ats(url) is None


# ── discover / load_form ─────────────────────────────────────────────────────

def test_discover_lists_offers():
    job = _job()
    assert job["platform"] == "recruitee" and job["token"] == "channable" and job["job_id"] == SLUG
    assert job["company"] == "Channable" and "Utrecht" in job["location"]
    assert job["apply_url"].endswith("/c/new") and job["apply_url"].startswith("https://jobs.channable.com/")


def test_unknown_company_is_an_error():
    with pytest.raises(RuntimeError, match="no such Recruitee company"):
        rc.RecruiteeAdapter().discover(_Client({}), BoardRef("recruitee", "nope"))


def test_load_form_reads_the_single_offer():
    client = _Client({f"/api/offers/{SLUG}": _Resp(OFFER)})
    form = rc.RecruiteeAdapter().load_form(client, _job())
    assert form["slug"] == SLUG and len(form["open_questions"]) == 8
    assert client.urls == [f"https://channable.recruitee.com/api/offers/{SLUG}"]


# ── plan ─────────────────────────────────────────────────────────────────────

def _plan(extra=""):
    return rc.RecruiteeAdapter().plan(OFFER["offer"], _job(), _profile(extra), None)


def test_personal_questions_are_not_guessed():
    answers, unanswered = _plan()
    assert unanswered == [
        "Do you have EU citizenship or a valid work permit for the Netherlands? Or do you need sponsorship from us?",
        "I confirm my availability for at least 32 hours per week, with a minimum of 2 days spent in the Utrecht office",
        "Please provide your monthly gross salary expectations"]
    by = {a["label"]: a for a in answers}
    assert by["Full name"]["value"] == "Ada Lovelace" and by["Email"]["value"] == "ada@example.com"
    assert by["Phone number"]["kind"] == "phone" and by["CV or resume"]["kind"] == "file"
    assert by["How did you find out about Channable?"]["value"] == "Company careers page"
    # "please specify which type" is free text: no rule's bare Yes/No in it.
    assert "If you selected valid work permit, please specify which type" not in by
    assert "Cover letter" not in by                                   # optional and a file: left out


def test_extra_answers_complete_the_form():
    answers, unanswered = _plan("EU citizenship or a valid work permit: EU citizenship\n"
                                "availability for at least 32 hours: Yes\nmonthly gross salary: 5500 EUR")
    assert unanswered == []
    def got(start):
        return next((a["kind"], a["value"]) for a in answers if a["label"].startswith(start))
    assert got("Do you have EU citizenship") == ("choice", "EU citizenship")
    assert got("I confirm my availability") == ("flag", "Yes")
    assert got("Please provide your monthly gross salary") == ("content", "5500 EUR")


def test_required_builtins_without_a_value_block_the_job():
    form = {**OFFER["offer"], "open_questions": [], "options_photo": "required"}
    profile = {**_profile(), "resume_path": "", "phone": ""}
    _, unanswered = rc.RecruiteeAdapter().plan(form, _job(), profile, None)
    assert unanswered == ["Phone number", "CV or resume", "Photo"]


def test_off_builtins_are_not_filled():
    form = {**OFFER["offer"], "open_questions": [], "options_phone": "off", "options_cv": "off"}
    answers, _ = rc.RecruiteeAdapter().plan(form, _job(), _profile(), None)
    assert [a["label"] for a in answers] == ["Full name", "Email"]


def test_infobox_file_and_video_kinds_are_skipped():
    form = {**OFFER["offer"], "open_questions": [
        {"id": 1, "kind": "infobox", "required": False, "body": "<p>Info</p>"},
        {"id": 2, "kind": "file", "required": True, "body": "Attach your motivational letter"},
        {"id": 3, "kind": "video", "required": False, "body": "Introduce yourself"}]}
    _, unanswered = rc.RecruiteeAdapter().plan(form, _job(), _profile(), None)
    assert unanswered == ["Attach your motivational letter"]


# ── matching API questions to the form's own ids (by text) ──────────────────

def test_plain_strips_html():
    assert rc.plain("<p>Payments &amp; <strong>AI</strong></p>") == "Payments & AI"


def test_find_prefix_matches_by_text_and_refuses_ambiguity():
    prefixes = {rc.norm("How did you find out about Channable?\xa0*"): "candidate.openQuestionAnswers.11",
                rc.norm("Where do you live? (City)\xa0*"): "candidate.openQuestionAnswers.12",
                rc.norm("Which one of our company values resonates the most with you"):
                    "candidate.openQuestionAnswers.13"}
    assert rc.find_prefix(prefixes, "How did you find out about Channable?") == "candidate.openQuestionAnswers.11"
    # The page shows only a long body's first paragraph: still matched.
    assert rc.find_prefix(prefixes, "Which one of our company values resonates the most with you "
                                    "Tell us in a few sentences.") == "candidate.openQuestionAnswers.13"
    assert rc.find_prefix(prefixes, "Something else entirely, long enough") is None
    twins = {rc.norm("Please describe your experience"): "a",
             rc.norm("Please describe your experience with Haskell"): "b"}
    assert rc.find_prefix(twins, "Please describe your experience with Go") is None   # ambiguous
