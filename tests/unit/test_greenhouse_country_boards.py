"""Filters + rendering of scripts/greenhouse_country_boards.py (no network)."""
import importlib.util
import re
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "greenhouse_country_boards.py"
_spec = importlib.util.spec_from_file_location("greenhouse_country_boards", _PATH)
gcb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gcb)


def _job(title, location, url=None):
    return {"title": title, "location": location, "url": url or f"https://x/{title}/{location}"}


@pytest.mark.parametrize("title", [
    "Senior Software Engineer", "Backend Engineer (Java)", "Site Reliability Engineer",
    "Machine Learning Engineer", "Forward Deployed Engineer", "C++ Developer", "Data Scientist",
])
def test_engineering_titles_are_kept(title):
    assert gcb.is_engineering_title(title)


@pytest.mark.parametrize("title", [
    "Sales Engineer", "Solutions Architect", "Software Engineering Manager",
    "Software Engineer Intern", "Security Engineer", "QA Engineer", "Business Developer",
    "Leather Material Developer", "Account Executive",
])
def test_non_engineering_titles_are_dropped(title):
    assert not gcb.is_engineering_title(title)


@pytest.mark.parametrize("country,location,expected", [
    ("netherlands", "Amsterdam, Netherlands; Berlin, Germany", True),
    ("netherlands", "NL- Remote", True),
    ("netherlands", "St. John's, NL, Canada", False),   # Newfoundland
    ("netherlands", "Amsterdam, NY", False),
    ("spain", "Remote, Spain", True),
    ("spain", "Valencia, CA", False),
    ("spain", "Málaga", True),
    ("hong_kong", "Hong Kong, Hong Kong SAR; Singapore", True),
    ("hong_kong", "Singapore", False),
])
def test_country_location_matching(country, location, expected):
    rows = gcb.rank_boards({"acme": [_job("Software Engineer", location)]}, gcb.COUNTRIES[country])
    assert bool(rows) is expected


def test_every_country_preset_compiles_and_has_a_location_line():
    for key, c in gcb.COUNTRIES.items():
        re.compile(c["match"])
        re.compile(c.get("reject") or "x")
        assert c["name"] and c["location"], key


def test_rank_boards_sorts_by_role_count_and_dedupes_urls():
    boards = {
        "small": [_job("Software Engineer", "Madrid")],
        "big": [_job("Backend Engineer", "Madrid"), _job("Data Engineer", "Barcelona"),
                _job("Data Engineer", "Barcelona")],          # same url twice
        "none": [_job("Software Engineer", "Paris"), _job("Sales Engineer", "Madrid")],
    }
    rows = gcb.rank_boards(boards, gcb.COUNTRIES["spain"])
    assert [(b, len(r)) for b, r in rows] == [("big", 2), ("small", 1)]


def test_boards_in_text_reads_hosted_and_embed_urls():
    text = ("https://job-boards.greenhouse.io/Stripe/jobs/1 "
            "https://boards.greenhouse.io/embed/job_app?for=gemini&token=2")
    assert gcb.boards_in_text(text) == {"stripe", "gemini"}


def test_render_issue_has_input_block_and_escaped_table():
    cache = {"date": "2026-11-15", "candidates": 100, "boards": {"acme": []}}
    rows = [("acme", [_job("Engineer | [Platform]", "Madrid", "https://x/1")])]
    title, body = gcb.render_issue(gcb.COUNTRIES["spain"], rows, cache)
    assert title == "Spain: 1 Greenhouse companies with software / AI engineering openings (2026-11-15)"
    assert "boards:   acme" in body and "location: Spain, Madrid" in body
    assert "[Engineer \\| (Platform)](https://x/1)" in body
    assert "**Only 1 companies:**" in body
