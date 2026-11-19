"""Role filter, date window and report shape of scripts/daily_new_jobs.py (no network)."""
import importlib.util
from datetime import date
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "daily_new_jobs.py"
_spec = importlib.util.spec_from_file_location("daily_new_jobs", _PATH)
dnj = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dnj)

DAY = date(2026, 11, 19)


@pytest.mark.parametrize("title", [
    "Software Engineer", "Senior Software Engineer, Payments", "SWE II", "Software Developer",
    "Backend Engineer", "Back-End Software Engineer", "Full Stack Engineer", "Fullstack Developer",
    "AI Engineer", "Senior AI Infrastructure Engineer", "AI/ML Engineer",
    "Forward Deployed Engineer", "Forward-Deployed Software Engineer", "FDE, Public Sector",
])
def test_target_roles_match(title):
    assert dnj.is_target_role(title)


@pytest.mark.parametrize("title", [
    "Software Engineering Manager", "Director, Backend Engineering", "Product Designer",
    "Physical Design Backend Engineer", "Senior ASIC Low-Power Integration and AI Engineer",
    "Forward Deployed Process Consultant", "Technical Recruiter, Software Engineering",
    "Data Scientist",
])
def test_other_roles_rejected(title):
    assert not dnj.is_target_role(title)


@pytest.mark.parametrize("stamp, expected", [
    ("2026-11-19T09:00:00-05:00", True),
    ("2026-11-19T21:00:00-05:00", False),     # 02:00 UTC on the 20th
    ("2026-11-19T00:10:00.123+00:00", True),
    ("2026-11-18T23:59:59Z", False),
    (1795050585, True),                        # epoch seconds
    (1795050585000, True),                     # epoch milliseconds
    (None, False), ("", False),
])
def test_on_day_is_utc(stamp, expected):
    assert dnj.on_day(stamp, DAY) is expected


def test_scan_keeps_todays_target_roles_once(monkeypatch):
    def fake(board, day):
        yield "Backend Engineer", "https://x/1", "2026-11-19T10:00:00Z"
        yield "Backend Engineer", "https://x/1", "2026-11-19T10:00:00Z"   # duplicate URL
        yield "Software Engineer", "https://x/2", "2026-11-01T10:00:00Z"  # not today
        yield "Account Executive", "https://x/3", "2026-11-19T10:00:00Z"  # not a target role
        yield "  AI Engineer ", "https://x/4", True                         # source pre-filtered
    monkeypatch.setitem(dnj.SOURCES, "fake", fake)
    assert dnj.scan("Acme", "fake", "", DAY) == [
        ("Acme", "Backend Engineer", "https://x/1"),
        ("Acme", "AI Engineer", "https://x/4"),
    ]


def test_collect_sorts_and_isolates_failures(monkeypatch):
    def ok(board, day):
        yield "Software Engineer", f"https://x/{board}", True

    def boom(board, day):
        raise TimeoutError("slow board")
        yield  # pragma: no cover

    monkeypatch.setitem(dnj.SOURCES, "ok", ok)
    monkeypatch.setitem(dnj.SOURCES, "boom", boom)
    monkeypatch.setattr(dnj, "COMPANIES", [("beta", "ok", "b"), ("Down", "boom", ""), ("Alpha", "ok", "a")])
    rows, warnings = dnj.collect(DAY)
    assert [r[0] for r in rows] == ["Alpha", "beta"]
    assert warnings == ["Down: TimeoutError: slow board"]


def test_every_company_uses_a_known_source():
    assert {s for _, s, _ in dnj.COMPANIES} <= set(dnj.SOURCES)
