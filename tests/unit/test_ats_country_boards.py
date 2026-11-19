"""scripts/ats_country_boards.py — candidates, board checks, ranking, rendering (no network)."""
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from src.automation.tools.ats_router import parse_board_ref

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ats_country_boards.py"
_spec = importlib.util.spec_from_file_location("ats_country_boards", _PATH)
acb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(acb)


def _job(title, location, url=None):
    return {"title": title, "location": location, "url": url or f"https://x/{title}/{location}"}


# ── candidates ───────────────────────────────────────────────────────────────

def test_candidates_from_aggregator_list():
    out = acb.candidates_from("json_list:ashby", json.dumps(["openai", " ", "Notion"]))
    assert out["ashby"] == {"openai", "Notion"} and out["lever"] == set()


def test_candidates_from_any_text_uses_the_router():
    text = json.dumps([
        {"url": "https://jobs.lever.co/palantir/11111111-1111-1111-1111-111111111111"},
        {"url": "https://jobs.ashbyhq.com/openai/application"},
        {"url": "https://apply.workable.com/huggingface/j/ABC123DEF/"},
        {"url": "https://boards.greenhouse.io/figma"},          # not this scanner's platform
        {"url": "https://jobs.workable.com/view/x"},            # Workable's own site, not an account
        {"url": "https://ngc.wd1.myworkdayjobs.com/Ext/job/US/Engineer_R1"},
    ])
    out = acb.candidates_from("urls", text)
    assert out == {"lever": {"palantir"}, "ashby": {"openai"}, "workable": {"huggingface"},
                   "workday": {"ngc.wd1/Ext"}}


def test_clean_drops_junk_names():
    assert acb.clean(["a", "ok", "x" * 90, "has/slash", "ok"]) == ["ok"]
    # Workday tokens are tenant.wdN/site: the slash is part of the name.
    assert acb.clean(["ngc.wd1/Northrop_Grumman_External_Site"], "workday") == [
        "ngc.wd1/Northrop_Grumman_External_Site"]


# ── checking one board ───────────────────────────────────────────────────────

def _status_error(code):
    req = httpx.Request("GET", "https://x")
    return httpx.HTTPStatusError("boom", request=req, response=httpx.Response(code, request=req))


class _Adapter:
    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), 0

    def discover(self, client, ref):
        self.calls += 1
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


@pytest.fixture
def adapters(monkeypatch):
    table = {}
    monkeypatch.setattr(acb, "ADAPTERS", table)
    monkeypatch.setattr(acb.time, "sleep", lambda _s: None)
    return table


def test_live_board_keeps_title_location_url(adapters):
    adapters["lever"] = _Adapter([{"title": "SWE", "location": "Taipei", "url": "u", "job_id": "1",
                                   "platform": "lever", "token": "x"}])
    assert acb.check_board(None, "lever", "x", acb.Gate(0)) == (
        "live", [{"title": "SWE", "location": "Taipei", "url": "u"}])


@pytest.mark.parametrize("error", [RuntimeError("no such Lever site"), _status_error(404)])
def test_404_is_dead(adapters, error):
    adapters["lever"] = _Adapter(error)
    assert acb.check_board(None, "lever", "x", acb.Gate(0)) == ("dead", None)


@pytest.mark.parametrize("code", [403, 429, 503])
def test_throttling_pauses_and_retries_never_dead(adapters, code):
    adapters["ashby"] = _Adapter(_status_error(code), [])
    gate = acb.Gate(0)
    assert acb.check_board(None, "ashby", "x", gate) == ("live", [])
    assert adapters["ashby"].calls == 2 and gate.open.is_set()


def test_a_board_that_never_answers_is_unknown(adapters):
    adapters["workable"] = _Adapter(*[httpx.ConnectError("down")] * 6)
    assert acb.check_board(None, "workable", "x", acb.Gate(0)) == ("unknown", None)


# ── report ───────────────────────────────────────────────────────────────────

CACHE = {"date": "2026-11-19", "platforms": {
    "lever": {"candidates": 10, "dead": [], "boards": {
        "acme": [_job("Software Engineer", "Taipei, Taiwan"), _job("Sales Manager", "Taipei, Taiwan")]}},
    "ashby": {"candidates": 5, "dead": [], "boards": {
        "beta": [_job("Backend Engineer", "Taipei"), _job("ML Engineer", "Hsinchu, Taiwan"),
                 _job("Backend Engineer", "Tokyo, Japan")]}},
    "workable": {"candidates": 3, "dead": [], "boards": {"gamma": [_job("Designer", "Taipei")]}},
}}


def test_rank_across_platforms_filters_titles_and_country():
    rows = acb.rank(CACHE, acb.COUNTRIES["taiwan"])
    assert [(p, b, len(r)) for p, b, r in rows] == [("ashby", "beta", 2), ("lever", "acme", 1)]


def test_render_issue_input_block_pastes_into_ats_apply():
    rows = acb.rank(CACHE, acb.COUNTRIES["taiwan"])
    title, body = acb.render_issue(acb.COUNTRIES["taiwan"], rows, CACHE)
    assert title.startswith("Taiwan: 2 Lever/Ashby companies")
    boards_line = next(ln for ln in body.splitlines() if ln.startswith("boards:"))
    tokens = [t.strip() for t in boards_line.split(":", 1)[1].split(",")]
    assert tokens == ["ashby:beta", "lever:acme"]
    assert all(parse_board_ref(t) is not None for t in tokens)          # ats_apply accepts them
    assert "[beta](https://jobs.ashbyhq.com/beta) | ashby | 2 |" in body
    assert "about 18 candidate board names" in body and "3 were live boards" in body


def test_report_without_cache_exits(tmp_path):
    args = type("A", (), {"list": False, "name": None, "countries": ["taiwan"],
                          "cache": str(tmp_path / "none.json"), "out_dir": str(tmp_path)})()
    with pytest.raises(SystemExit, match="run `scan` first"):
        acb.report(args)


def test_workday_rows_link_to_the_board_and_say_manual():
    cache = {"date": "2026-11-19", "platforms": {"workday": {"candidates": 1, "dead": [], "boards": {
        "ngc.wd1/Ext": [_job("Software Engineer", "Taipei, Taiwan")]}}}}
    rows = acb.rank(cache, acb.COUNTRIES["taiwan"])
    title, body = acb.render_issue(acb.COUNTRIES["taiwan"], rows, cache)
    assert title.startswith("Taiwan: 1 Workday companies")
    assert "[ngc.wd1/Ext](https://ngc.wd1.myworkdayjobs.com/Ext) | workday |" in body
    assert "listed for manual apply" in body
    assert parse_board_ref("workday:ngc.wd1/Ext") is not None


def test_scanner_searches_workday_and_passes_no_hint_elsewhere(adapters):
    seen = {}

    class Hinted:
        def discover(self, client, ref, search=()):
            seen[ref.platform] = search
            return []

    class Old:                                                   # pre-hint signature
        def discover(self, client, ref):
            seen[ref.platform] = "no hint"
            return []
    adapters["workday"], adapters["lever"] = Hinted(), Old()
    acb.check_board(None, "workday", "ngc.wd1/Ext", acb.Gate(0))
    acb.check_board(None, "lever", "x", acb.Gate(0))
    assert seen == {"workday": ("engineer",), "lever": "no hint"}
