"""scripts/ats_region_report.py — title filter, location matching, collection, rendering (no network)."""
import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ats_region_report.py"
_spec = importlib.util.spec_from_file_location("ats_region_report", _PATH)
arr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(arr)

EUROPE = [arr.compile_country(c) for c in arr.EUROPE]
ASIA = [arr.compile_country(c) for c in arr.ASIA]


def _job(title, location, url=None):
    return {"title": title, "location": location, "url": url or f"https://x/{title}/{location}"}


# ── title filter ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("title", [
    "Senior Software Engineer", "Backend Developer", "Staff SRE", "Data Engineer II",
    "AI Engineer", "Softwareentwickler (m/w/d)", "Full-Stack Engineer",
])
def test_swe_titles_are_kept(title):
    assert arr.is_swe(title)


@pytest.mark.parametrize("title", [
    "Software Engineering Intern", "Software Sales Executive", "Engineering Manager, Software",
    "Werkstudent Softwareentwicklung", "Director of Software Engineering", "Mechanical Engineer",
    "Real Estate Developer",
])
def test_non_swe_titles_are_dropped(title):
    assert not arr.is_swe(title)


# ── location matching ────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("London, United Kingdom", ["United Kingdom"]),
    ("GBR-London-5 Canada Square", ["United Kingdom"]),
    ("PB_Cen_Katowice ul Sokolska 34", ["Poland"]),      # "_" is a word break
    ("PL Warsaw Workspace", ["Poland"]),                 # alpha-2 as the first token
    ("Dublin, IE", ["Ireland"]),                         # ... or the last
    ("POL > Wrocław > Swobodna 1", ["Poland"]),
    ("DE - Glinde", ["Germany"]),
    ("Braşov", ["Romania"]),
    ("Belfast, Northern Ireland", ["United Kingdom"]),   # not the Republic
    ("United Kingdom; Denmark", ["United Kingdom", "Denmark"]),
])
def test_european_locations(text, expected):
    assert arr.countries_of(text, EUROPE) == expected


@pytest.mark.parametrize("text", [
    "Wilmington DE", "Wilmington, DE", "DE - Work from home",   # Delaware
    "Indianapolis, IN", "Annapolis Junction, MD",              # ...napoli...
    "Immenstaad am Bodensee",                                  # ...odense...
    "Gateway Manufacturing Php",                               # ...turin...
    "Rochester", "Dublin, OH / Hybrid", "Paris, TX", "London, CAN",
    "CA - LONDON 100 KELLOGG LANE", "New Orleans - Louisiana", "TRC IT HUB - Torreon, Mexico",
    "Cambridge MA", "Hoover AL   Riverchase Operations Center Birmingham AL",
    "New York, New York", "Toronto - 16 York St", "Remote", "",
])
def test_lookalikes_outside_europe_are_rejected(text):
    assert arr.countries_of(text, EUROPE, eu_dc=True) == []


def test_shared_city_names_need_the_eu_datacenter():
    assert arr.countries_of("Cambridge", EUROPE) == []
    assert arr.countries_of("Cambridge", EUROPE, eu_dc=True) == ["United Kingdom"]
    assert arr.countries_of("Cambridge, UK", EUROPE) == ["United Kingdom"]
    assert arr.is_eu_datacenter("workday", "barclays.wd3/External")
    assert not arr.is_eu_datacenter("workday", "nike.wd1/nke")
    assert not arr.is_eu_datacenter("lever", "acme.wd3/x")


@pytest.mark.parametrize("text,expected", [
    ("Taipei, Taipei City, Taiwan Region", ["Taiwan"]),
    ("IND - Chennai, Neville Towers", ["India"]),
    ("IN - Bengaluru - Office", ["India"]),
    ("Indianapolis, IN", []),
    ("Carmel, IN", []),
    ("Singapore-CapitaSky", ["Singapore"]),
])
def test_asian_locations(text, expected):
    assert arr.countries_of(text, ASIA) == expected


def test_location_text_falls_back_to_the_url_segment():
    job = _job("x", "2 Locations", "https://a.wd1.myworkdayjobs.com/s/job/Krak%C3%B3w-Poland/Dev_R1")
    assert arr.location_text(job) == "Kraków Poland"
    assert arr.location_text(_job("x", "Paris")) == "Paris"
    assert arr.location_text({"title": "x", "location": "", "url": "https://x/none"}) == ""


# ── collect / render ─────────────────────────────────────────────────────────

def _cache():
    return {"date": "2026-11-24", "platforms": {
        "workday": {"candidates": 10, "dead": [], "boards": {
            "acme.wd3/Careers": [
                _job("Software Engineer", "London, UK", "https://acme/1"),
                _job("Software Engineer", "London, UK", "https://acme/1"),      # duplicate URL
                _job("Data Engineer", "2 Locations", "https://acme.wd3.x/s/job/Berlin-Germany/D_2"),
                _job("Sales Manager", "London, UK", "https://acme/3"),
                _job("Backend Developer", "Austin, TX", "https://acme/4"),
            ],
            "zeta.wd1/Jobs": [_job("AI Engineer", "Remote - United Kingdom", "https://zeta/1")],
        }},
        "lever": {"candidates": 5, "boards": {
            "beta": [_job("DevOps Engineer", "Warsaw, Poland", "https://beta/1")]}},
    }}


def test_collect_groups_boards_by_country():
    out = arr.collect(_cache(), arr.EUROPE)
    assert [(p, b, len(r)) for p, b, r in out["United Kingdom"]] == [
        ("workday", "acme.wd3/Careers", 1), ("workday", "zeta.wd1/Jobs", 1)]
    assert [(p, b) for p, b, _ in out["Germany"]] == [("workday", "acme.wd3/Careers")]
    assert [(p, b) for p, b, _ in out["Poland"]] == [("lever", "beta")]
    assert out["France"] == []


def test_collect_platform_filter():
    out = arr.collect(_cache(), arr.EUROPE, ["workday"])
    assert out["Poland"] == []


def test_render_issue():
    cache = _cache()
    title, parts = arr.render("Europe", arr.collect(cache, arr.EUROPE), cache)
    assert title == "Lever / Workday employers with SWE openings in Europe (3 boards, 2026-11-24 scan)"
    assert len(parts) == 1
    body = parts[0]
    assert "**3 boards from 3 companies, 4 SWE roles**" in body
    assert "| United Kingdom | 2 | 2 |" in body
    assert "### Poland — 1 boards, 1 SWE roles" in body
    assert "| acme | 1 | `workday:acme.wd3/Careers` | [Software Engineer](https://acme/1) |" in body
    assert "| beta | 1 | `lever:beta` |" in body
    assert "France" not in body.split("### How this was made")[0]      # empty countries are left out


def test_render_splits_a_long_report_into_comments():
    cache = {"date": "2026-11-24", "platforms": {"workday": {"candidates": 400, "boards": {
        f"co{i}.wd3/Careers": [_job("Software Engineer", "Paris, France", f"https://co{i}/1")]
        for i in range(400)}}}}
    title, parts = arr.render("Europe", arr.collect(cache, arr.EUROPE), cache, max_body=8000)
    assert len(parts) > 2 and all(len(p) <= 8000 for p in parts)
    assert parts[1].startswith("### France (continued)")
    rows = [line for p in parts for line in p.splitlines() if line.startswith("| co")]
    assert len(rows) == 400 and len(set(rows)) == 400                   # nothing lost or repeated


def test_main_writes_the_report_files(tmp_path, capsys):
    import json
    cache = tmp_path / "boards.json"
    cache.write_text(json.dumps(_cache()))
    arr.main(["europe", "--cache", str(cache), "--out-dir", str(tmp_path / "out")])
    assert (tmp_path / "out" / "europe_2026-11-24.md").read_text().startswith("Lever / Workday employers")
    assert "United Kingdom" in capsys.readouterr().out


def test_main_without_a_cache_exits(tmp_path):
    with pytest.raises(SystemExit, match="no scan cache"):
        arr.main(["europe", "--cache", str(tmp_path / "missing.json")])
