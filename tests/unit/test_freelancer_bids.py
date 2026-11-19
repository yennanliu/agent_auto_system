"""freelancer_bids: the Freelancer.com search/filter tool and the draft-only flow.

The tool runs against a trimmed REAL API response (tests/fixtures/ats/
freelancer_projects.json). The flow's LLM is mocked. No network.
"""
import json
from pathlib import Path

import pytest

from src.automation.flows import freelancer_bids_flow as fbf
from src.automation.tools import freelancer_tool as ft

RAW = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "ats" / "freelancer_projects.json")
                 .read_text(encoding="utf-8"))


class _Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Client:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, params=None):
        self.calls.append(dict(params or {}))
        return self.responses.pop(0) if self.responses else _Resp(RAW)

    def close(self):
        pass


def _projects():
    return [ft.normalize(p) for p in RAW["result"]["projects"]]


# ── tool ─────────────────────────────────────────────────────────────────────

def test_normalize_converts_budget_to_usd_and_builds_the_link():
    inr = next(p for p in _projects() if p["currency"] == "INR")
    raw = next(r for r in RAW["result"]["projects"] if r["currency"]["code"] == "INR")
    assert inr["budget_max_usd"] == round(raw["budget"]["maximum"] * raw["currency"]["exchange_rate"], 2)
    assert inr["url"] == f"https://www.freelancer.com/projects/{raw['seo_url']}"
    assert inr["skills"] and inr["bid_count"] == raw["bid_stats"]["bid_count"]


def _p(**over):
    base = {"id": "1", "title": "Python scraper", "type": "fixed", "currency": "USD", "currency_sign": "$",
            "budget_min": 100.0, "budget_max": 300.0, "budget_min_usd": 100.0, "budget_max_usd": 300.0,
            "bid_count": 5, "bid_avg": 200.0, "skills": ["Python"], "description": "d", "url": "u",
            "submitted_at": 1, "nonpublic": False, "status": "open"}
    return {**base, **over}


@pytest.mark.parametrize("project,kw,expect", [
    (_p(), {}, ""),
    (_p(budget_max_usd=150.0), {"min_budget_usd": 200}, "budget up to $150 < $200"),
    (_p(budget_min_usd=500.0), {"max_budget_usd": 400}, "budget from $500 > $400"),
    (_p(type="hourly"), {"project_type": "fixed"}, "hourly project (wanted fixed)"),
    (_p(bid_count=80), {"max_bids": 50}, "80 bids already (max 50)"),
    (_p(status="closed"), {}, "not open (closed)"),
    (_p(nonpublic=True), {}, "not public"),
    (_p(title="Logo design", skills=["Graphic Design"]), {"keywords": ["python"]},
     "title / skills don't match the keywords"),
])
def test_skip_reason(project, kw, expect):
    args = {"min_budget_usd": 0, "max_budget_usd": 0, "project_type": "any", "max_bids": 0, "keywords": []}
    assert ft.skip_reason(project, **{**args, **kw}) == expect


def test_keyword_match_uses_skills_and_role_shorthand():
    assert ft.matches_keywords(_p(title="Need help", skills=["Python"]), ["python"])
    assert ft.matches_keywords(_p(title="Senior Software Engineer needed"), ["swe"])
    assert not ft.matches_keywords(_p(title="Logo", skills=["Design"]), ["swe"])


@pytest.mark.parametrize("project,bid", [
    (_p(bid_avg=200.0), 200),                         # average inside the budget
    (_p(bid_avg=900.0), 200),                         # outside → midpoint of 100–300
    (_p(budget_min=None, budget_max=None, bid_avg=None), None),
])
def test_suggested_bid(project, bid):
    assert ft.suggested_bid(project) == bid


def test_search_one_query_per_keyword_merged_and_failures_are_warnings():
    client = _Client(_Resp(RAW), _Resp({"status": "error", "message": "rate limited"}), _Resp(RAW))
    projects, warnings = ft.search(client, ["python", "django", "flask"])
    assert [c["query"] for c in client.calls] == ["python", "django", "flask"]
    assert all(c["full_description"] == "true" and c["limit"] == 50 for c in client.calls)
    assert len(projects) == len(RAW["result"]["projects"])          # merged by id
    assert any("django" in w and "rate limited" in w for w in warnings)


def test_search_caps_keywords():
    client = _Client()
    _, warnings = ft.search(client, [f"k{i}" for i in range(7)])
    assert len(client.calls) == 5 and any("first 5 keywords" in w for w in warnings)


def test_find_projects_filters_and_skips_already_drafted(tmp_path):
    ledger = tmp_path / "drafted.json"
    first = _projects()[0]
    ft.record_drafted(str(ledger), first)
    out = ft.find_projects(keywords="python", max_bids=100, ledger_path=str(ledger), client=_Client())
    reasons = {s["id"]: s["reason"] for s in out["skipped"]}
    assert reasons[first["id"]] == "already drafted in an earlier run (ledger)"
    assert out["projects_found"] == len(RAW["result"]["projects"])
    assert all(c["bid_count"] <= 100 for c in out["candidates"])
    ts = [c["submitted_at"] for c in out["candidates"]]
    assert ts == sorted(ts, reverse=True)                            # newest first


# ── flow ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,proposal,why", [
    ("You need X. I would Y. Question?", "You need X. I would Y. Question?", ""),
    ("SKIP: no iOS experience in the background", "", "no iOS experience in the background"),
    ('  "skip: nope"  ', "", "nope"),
    ("", "", ""),
])
def test_parse_proposal(text, proposal, why):
    assert fbf.parse_proposal(text) == (proposal, why)


class _Result:
    def __init__(self, raw):
        self.raw = raw


@pytest.fixture
def flow_env(mocker, monkeypatch, tmp_path):
    mocker.patch.object(fbf, "append_log")
    monkeypatch.setenv("FREELANCER_LEDGER", str(tmp_path / "drafted.json"))
    mocker.patch("src.automation.harness.provider.resolve", return_value=(object(), "p", "m"))
    projects = [_p(id="1", title="Python scraper", submitted_at=3),
                _p(id="2", title="Python iOS app", submitted_at=2),
                _p(id="3", title="Python API", submitted_at=1)]
    mocker.patch.object(ft, "search", return_value=(projects, []))
    seen = []

    def kickoff_for(name):
        def crew(llm=None):
            class C:
                def kickoff(self, inputs):
                    seen.append((name, inputs))
                    if name == "GreenhouseRelevanceCrew":
                        return _Result(json.dumps({"relevant": "API" not in inputs["job_title"], "reason": "no APIs"}))
                    if "iOS" in inputs["project_title"]:
                        return _Result("SKIP: no iOS experience")
                    return _Result(f"Proposal for {inputs['project_title']}. Question?")
            return type("Crew", (), {"crew": lambda self: C()})()
        return crew
    mocker.patch.object(fbf, "FreelancerProposalCrew", side_effect=kickoff_for("FreelancerProposalCrew"))
    mocker.patch.object(fbf, "GreenhouseRelevanceCrew", side_effect=kickoff_for("GreenhouseRelevanceCrew"))
    return seen


def test_flow_drafts_declines_filters_and_never_submits(flow_env, tmp_path):
    out = json.loads(fbf.FreelancerBidsFlow().kickoff(inputs={
        "keywords": "python", "candidate_background": "Python dev", "task_filter": "no APIs"}))
    assert [d["id"] for d in out["drafted"]] == ["1"]
    assert out["drafted"][0]["proposal"] == "Proposal for Python scraper. Question?"
    assert out["drafted"][0]["suggested_bid"] == 200
    reasons = {s["id"]: s["reason"] for s in out["skipped"]}
    assert reasons["2"] == "LLM declined: no iOS experience"
    assert reasons["3"].startswith("filtered out")
    assert "Draft-only" in out["note"] and "not submitted" in out["summary"]
    # The drafter only ever sees the user's own background as facts about them.
    drafts = [i for name, i in flow_env if name != "GreenhouseRelevanceCrew"]
    assert all(i["candidate_background"] == "Python dev" for i in drafts)
    assert set(ft.load_ledger(str(tmp_path / "drafted.json"))) == {"1"}


def test_flow_caps_drafts(flow_env):
    out = json.loads(fbf.FreelancerBidsFlow().kickoff(inputs={
        "keywords": "python", "candidate_background": "Python dev", "max_drafts": 1}))
    assert len(out["drafted"]) == 1


def test_flow_requires_keywords_and_background(mocker):
    mocker.patch.object(fbf, "append_log")
    with pytest.raises(ValueError, match="candidate_background"):
        fbf.FreelancerBidsFlow().kickoff(inputs={"keywords": "python"})


def test_flow_without_llm_drafts_nothing_but_still_reports(mocker, monkeypatch, tmp_path):
    mocker.patch.object(fbf, "append_log")
    monkeypatch.setenv("FREELANCER_LEDGER", str(tmp_path / "d.json"))
    mocker.patch("src.automation.harness.provider.resolve", side_effect=RuntimeError("no key"))
    mocker.patch.object(ft, "search", return_value=([_p()], []))
    out = json.loads(fbf.FreelancerBidsFlow().kickoff(inputs={"keywords": "python", "candidate_background": "x"}))
    assert out["drafted"] == [] and out["projects_found"] == 1
    assert any("no LLM available" in w for w in out["warnings"])
