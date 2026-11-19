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


# ── review fixes (PR #115) ───────────────────────────────────────────────────

@pytest.mark.parametrize("over,text", [
    ({"currency": "EUR", "currency_sign": "€", "budget_min": None, "budget_min_usd": None,
      "budget_max": 500.0, "budget_max_usd": 540.0}, "fixed, €500 EUR (≈ $540)"),
    ({"currency": "EUR", "currency_sign": "€", "budget_min": 100.0, "budget_min_usd": 108.0,
      "budget_max": 500.0, "budget_max_usd": 540.0}, "fixed, €100–€500 EUR (≈ $108–$540)"),
    ({"budget_min": None, "budget_max": None, "budget_min_usd": None, "budget_max_usd": None},
     "fixed, ? USD"),
    ({"type": "hourly", "budget_min": 15.0, "budget_max": 25.0}, "hourly, $15–$25 USD per hour"),
])
def test_budget_text_handles_partial_budgets(over, text):
    assert fbf.budget_text(_p(**over)) == text


def test_relevance_judge_sees_the_description(flow_env):
    json.loads(fbf.FreelancerBidsFlow().kickoff(inputs={
        "keywords": "python", "candidate_background": "Python dev", "task_filter": "no greenfield"}))
    metas = [i["job_meta"] for name, i in flow_env if name == "GreenhouseRelevanceCrew"]
    assert metas and all("Description: d" in m for m in metas)


def test_a_failing_ledger_write_keeps_the_drafts(flow_env, mocker):
    mocker.patch.object(ft, "record_drafted", side_effect=OSError("disk full"))
    out = json.loads(fbf.FreelancerBidsFlow().kickoff(inputs={
        "keywords": "python", "candidate_background": "Python dev"}))
    assert [d["id"] for d in out["drafted"]] == ["1", "3"]
    assert any("could not record project 1" in w and "disk full" in w for w in out["warnings"])


def test_ledger_write_is_atomic(tmp_path, mocker):
    path = tmp_path / "drafted.json"
    ft.record_drafted(str(path), _p(id="1"))
    mocker.patch.object(ft.json, "dump", side_effect=OSError("interrupted"))
    with pytest.raises(OSError):
        ft.record_drafted(str(path), _p(id="2"))
    assert set(ft.load_ledger(str(path))) == {"1"}                  # previous ledger intact
    assert [f.name for f in tmp_path.iterdir()] == ["drafted.json"]  # no temp file left


def test_a_malformed_project_is_skipped_with_a_warning():
    bad = {**RAW["result"]["projects"][0], "id": 1, "bid_stats": {"bid_count": "unknown"}}
    data = {"status": "success", "result": {"projects": [bad, *RAW["result"]["projects"][1:]]}}
    projects, warnings = ft.search(_Client(_Resp(data)), ["python"])
    assert len(projects) == len(RAW["result"]["projects"]) - 1
    assert any("malformed project 1" in w for w in warnings)


# ── placing bids (opt-in) ────────────────────────────────────────────────────

class _ApiResp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data

    def json(self):
        return self._data


class _BidClient:
    """Fake authenticated API: /self and /bids."""
    def __init__(self, *, self_resp=None, bid_resps=()):
        self.self_resp = self_resp or _ApiResp(200, {"status": "success", "result": {"id": 42, "username": "ada"}})
        self.bid_resps, self.posts, self.headers = list(bid_resps), [], []

    def get(self, url, headers=None, params=None):
        self.headers.append(headers)
        return self.self_resp

    def post(self, url, headers=None, json=None):
        self.headers.append(headers)
        self.posts.append(json)
        return self.bid_resps.pop(0) if self.bid_resps else _ApiResp(
            200, {"status": "success", "result": {"id": 1000 + len(self.posts)}})

    def close(self):
        pass


def test_whoami_and_place_bid_send_the_token_and_body():
    client = _BidClient()
    me = ft.whoami(client, "tok")
    assert me == {"id": 42, "username": "ada"} and client.headers[0]["freelancer-oauth-v1"] == "tok"
    res = ft.place_bid(client, "tok", bidder_id=42, project=_p(id="7"), amount=200, period_days=0,
                       milestone_percentage=150, description="x" * 2000)
    body = client.posts[0]
    assert res == {"bid_id": 1001}
    assert body == {"project_id": 7, "bidder_id": 42, "amount": 200.0, "period": 1,
                    "milestone_percentage": 100, "description": "x" * 1500}


def test_api_errors_carry_their_code_and_flag_auth():
    client = _BidClient(self_resp=_ApiResp(401, {"status": "error", "message": "You must be logged in",
                                                 "error_code": "RestExceptionCodes.NOT_AUTHENTICATED"}))
    with pytest.raises(ft.FreelancerAPIError) as e:
        ft.whoami(client, "bad")
    assert e.value.is_auth and "logged in" in str(e.value)
    quota = ft.FreelancerAPIError("no bids left", "ProjectExceptionCodes.NOT_ENOUGH_BIDS", 409)
    assert not quota.is_auth


@pytest.mark.parametrize("proposal,amount,why", [
    ("x" * 120, 200, ""),
    ("too short", 200, "proposal shorter than 100 characters"),
    ("x" * 120, None, "no budget to base a bid amount on"),
    ("x" * 120, 999, "bid 999 outside the budget"),
])
def test_bid_problem(proposal, amount, why):
    assert ft.bid_problem(_p(), proposal, amount) == why


@pytest.fixture
def bidding(flow_env, mocker, monkeypatch):
    client = _BidClient()
    mocker.patch("httpx.Client", return_value=client)
    monkeypatch.setenv("FREELANCER_OAUTH_TOKEN", "tok")
    # Long enough to be bid on.
    mocker.patch.object(fbf, "parse_proposal",
                        side_effect=lambda t: ("", "no iOS") if t.startswith("SKIP") else (t + " " + "x" * 120, ""))
    return client


def test_bids_are_placed_only_when_submit_bids_is_on(bidding, tmp_path):
    off = json.loads(fbf.FreelancerBidsFlow().kickoff(inputs={
        "keywords": "python", "candidate_background": "Python dev"}))
    assert bidding.posts == [] and off["bids_placed"] == 0 and "not submitted" in off["summary"]
    assert all("bid" not in d for d in off["drafted"])


def test_submit_bids_places_each_draft_and_records_it(bidding, tmp_path):
    out = json.loads(fbf.FreelancerBidsFlow().kickoff(inputs={
        "keywords": "python", "candidate_background": "Python dev", "submit_bids": True,
        "bid_period_days": 10, "milestone_percentage": 50}))
    assert [b["project_id"] for b in bidding.posts] == [1, 3]                 # drafted ones only
    assert all(b["bidder_id"] == 42 and b["period"] == 10 and b["milestone_percentage"] == 50
               for b in bidding.posts)
    assert [d["bid"]["status"] for d in out["drafted"]] == ["placed", "placed"]
    assert out["bids_placed"] == 2 and "2 bid(s) placed" in out["summary"]
    led = ft.load_ledger(str(tmp_path / "drafted.json"))
    assert led["1"]["status"] == "bid_placed"
    assert "already bid on" in ft.find_projects(keywords="python", ledger_path=str(tmp_path / "drafted.json"),
                                                client=_Client(_Resp({"status": "success", "result": {"projects": [
                                                    {**RAW["result"]["projects"][0], "id": 1}]}})))["skipped"][0]["reason"]


def test_an_auth_error_stops_bidding_but_keeps_the_drafts(bidding):
    bidding.bid_resps = [_ApiResp(401, {"status": "error", "message": "token expired",
                                        "error_code": "RestExceptionCodes.NOT_AUTHENTICATED"})]
    out = json.loads(fbf.FreelancerBidsFlow().kickoff(inputs={
        "keywords": "python", "candidate_background": "Python dev", "submit_bids": True}))
    assert len(bidding.posts) == 1                                           # stopped after the 401
    assert out["drafted"][0]["bid"]["status"] == "failed" and "bid" not in out["drafted"][1]
    assert len(out["drafted"]) == 2 and any("bidding stopped" in w for w in out["warnings"])


def test_a_quota_error_fails_that_bid_and_tries_the_next(bidding):
    bidding.bid_resps = [_ApiResp(409, {"status": "error", "message": "no bids left",
                                        "error_code": "ProjectExceptionCodes.NOT_ENOUGH_BIDS"})]
    out = json.loads(fbf.FreelancerBidsFlow().kickoff(inputs={
        "keywords": "python", "candidate_background": "Python dev", "submit_bids": True}))
    assert [d["bid"]["status"] for d in out["drafted"]] == ["failed", "placed"]


def test_submit_bids_without_a_token_drafts_only(bidding, monkeypatch):
    monkeypatch.delenv("FREELANCER_OAUTH_TOKEN")
    out = json.loads(fbf.FreelancerBidsFlow().kickoff(inputs={
        "keywords": "python", "candidate_background": "Python dev", "submit_bids": True}))
    assert bidding.posts == [] and len(out["drafted"]) == 2
    assert any("FREELANCER_OAUTH_TOKEN is not set" in w for w in out["warnings"])


def test_an_invalid_token_is_caught_before_any_bid(bidding):
    bidding.self_resp = _ApiResp(401, {"status": "error", "message": "invalid token"})
    out = json.loads(fbf.FreelancerBidsFlow().kickoff(inputs={
        "keywords": "python", "candidate_background": "Python dev", "submit_bids": True}))
    assert bidding.posts == [] and any("could not verify the Freelancer token" in w for w in out["warnings"])
