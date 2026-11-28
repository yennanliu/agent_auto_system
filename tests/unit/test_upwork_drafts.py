"""upwork_drafts: the read-only Upwork discovery tool and the draft-only flow.

The API fixture (tests/fixtures/upwork/) follows Upwork's documented
``marketplaceJobPostingsSearch`` response shape; it was NOT captured live (no
Upwork token on the build machine). The flow's LLM is mocked. No network.
"""
import inspect
import json
import os
import re
from pathlib import Path

import pytest

from src.automation import spec
from src.automation.flows import upwork_drafts_flow as udf
from src.automation.tools import upwork_tool as ut

RAW = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "upwork" /
                  "marketplace_job_postings_search.json").read_text(encoding="utf-8"))
NODES = [e["node"] for e in RAW["data"]["marketplaceJobPostingsSearch"]["edges"]]


class _Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def json(self):
        return self._data


class _Client:
    """Fake httpx client: records every request; answers GraphQL from a queue."""

    def __init__(self, *responses, token_resp=None):
        self.responses, self.requests = list(responses), []
        self.token_resp = token_resp

    def post(self, url, headers=None, json=None, data=None):
        self.requests.append({"url": url, "headers": headers, "json": json, "data": data})
        if url == ut.TOKEN_URL:
            return self.token_resp or _Resp({}, 400)
        return self.responses.pop(0) if self.responses else _Resp(RAW)

    def get(self, *a, **k):  # pragma: no cover — the tool never GETs
        raise AssertionError("unexpected GET")

    def close(self):
        pass


def _auth(**kw):
    return ut.UpworkAuth(**{"access_token": "tok", **kw})


# ── the draft-only guarantee ─────────────────────────────────────────────────

@pytest.mark.parametrize("doc", [
    "mutation { createJobProposal(input: {}) { newProposalId } }",
    "  # comment\n mutation X { anything }",
    "query { a } mutation { b }",
    "query { vendorProposals { id } }",
    "subscription { jobs { id } }",
    "{ marketplaceJobPostingsSearch { totalCount } }",  # shorthand: must be spelled `query`
])
def test_assert_read_only_refuses_everything_but_plain_queries(doc):
    with pytest.raises(ut.ReadOnlyViolation):
        ut.assert_read_only(doc)


def test_the_search_documents_are_read_only_queries():
    ut.assert_read_only(ut.SEARCH_QUERY)
    ut.assert_read_only(ut.SEARCH_QUERY_MINIMAL)


def test_graphql_refuses_a_mutation_before_touching_the_network():
    client = _Client()
    with pytest.raises(ut.ReadOnlyViolation):
        ut.graphql(client, _auth(), "mutation { createJobProposal(input: {}) { status } }", {})
    assert client.requests == []


def test_no_submit_code_path_exists_in_the_tool_or_flow():
    """Static guard: nothing in upwork_drafts can submit a proposal or spend
    Connects — no proposal mutation, no browser, no click."""
    for mod in (ut, udf):
        src = inspect.getsource(mod)
        code = re.sub(r'"""[\s\S]*?"""', "", src)  # docstrings may *describe* the rule
        for banned in ("createJobProposal", "createContractProposal", "playwright", ".click(",
                       "submit_proposal", "place_bid"):
            assert banned not in code, f"{mod.__name__} contains {banned!r}"
        assert not re.search(r"""["']\s*mutation\b""", code), mod.__name__


def test_a_whole_run_only_sends_read_only_queries(tmp_path):
    client = _Client()
    ut.find_jobs(keywords="python, django", auth=_auth(), client=client, pause=0,
                 ledger_path=str(tmp_path / "l.json"))
    assert client.requests and all(r["url"] == ut.GRAPHQL_URL for r in client.requests)
    for r in client.requests:
        ut.assert_read_only(r["json"]["query"])


def test_spec_says_draft_only_and_has_no_submit_field():
    s = spec.REGISTRY["upwork_drafts"]
    assert "never submits" in s.help_note.lower() and "connects" in s.help_note.lower()
    assert "DRAFT-ONLY" in s.rubric and "Connects" in s.rubric
    assert not [f for f in s.fields if re.search(r"submit|bid|apply", f.name)]
    assert not s.browser and s.canary is None and s.canary_skip
    ok, _ = s.validate({"drafted": [], "jobs_found": 0, "submitted": 0})
    assert ok
    bad, _ = s.validate({"drafted": [], "jobs_found": 0, "submitted": 1})
    assert not bad


# ── normalise ────────────────────────────────────────────────────────────────

def test_normalize_hourly_fixed_and_client():
    hourly, fixed, applied = (ut.normalize(n) for n in NODES)
    assert hourly["type"] == "hourly" and (hourly["hourly_min"], hourly["hourly_max"]) == (35.0, 70.0)
    assert hourly["fixed_budget"] is None
    assert hourly["url"] == "https://www.upwork.com/jobs/~022056488576136638272"
    assert hourly["skills"] == ["Python", "Web Scraping"] and hourly["applicants"] == 4
    assert hourly["client"]["payment_verified"] is True and hourly["client"]["spent"] == 12500.0
    assert fixed["type"] == "fixed" and fixed["fixed_budget"] == 250.0
    assert fixed["client"]["payment_verified"] is False
    assert applied["applied"] is True


def test_normalize_survives_the_minimal_field_set():
    j = ut.normalize({"id": "9", "title": "T", "description": "d", "ciphertext": "~01x",
                      "amount": {"rawValue": "0.0", "currency": "USD"}, "skills": [{"name": "python"}],
                      "client": {"totalFeedback": 0}})
    assert j["type"] == "" and j["applicants"] is None and j["client"]["payment_verified"] is None
    assert j["skills"] == ["python"]


# ── search ───────────────────────────────────────────────────────────────────

def test_search_one_query_per_keyword_merged_and_capped():
    client = _Client()
    jobs, warnings = ut.search(client, _auth(), [f"k{i}" for i in range(7)], pause=0)
    assert len(client.requests) == 5 and any("first 5 keywords" in w for w in warnings)
    assert [r["json"]["variables"]["filter"]["searchExpression_eq"] for r in client.requests] == \
        ["k0", "k1", "k2", "k3", "k4"]
    assert client.requests[0]["headers"]["Authorization"] == "Bearer tok"
    assert len(jobs) == 3                                              # merged by id


def test_tenant_header_is_sent_when_set():
    client = _Client()
    ut.search(client, _auth(tenant_id="org1"), ["python"], pause=0)
    assert client.requests[0]["headers"]["X-Upwork-API-TenantId"] == "org1"


def test_a_failed_search_is_a_warning_and_the_next_one_runs():
    client = _Client(_Resp({}, 500), _Resp(RAW))
    jobs, warnings = ut.search(client, _auth(), ["python", "django"], pause=0)
    assert len(jobs) == 3 and any("search 'python' failed" in w for w in warnings)


def test_a_scope_error_retries_with_the_minimal_fields():
    scope_err = _Resp({"errors": [{"message": "The client or authentication token doesn't have enough "
                                              "oauth2 permissions/scopes to access: [totalSpent]"}]})
    client = _Client(scope_err, _Resp(RAW))
    jobs, warnings = ut.search(client, _auth(), ["python"], pause=0)
    assert len(jobs) == 3 and "totalSpent" not in client.requests[1]["json"]["query"]
    assert any("minimal field set" in w for w in warnings)


def test_an_expired_token_is_refreshed_once_and_saved(tmp_path):
    token_file = tmp_path / "upwork_token.json"
    client = _Client(_Resp({"message": "Authentication failed"}, 401), _Resp(RAW),
                     token_resp=_Resp({"access_token": "new", "refresh_token": "r2", "expires_in": 86400}))
    auth = _auth(refresh_token="r1", client_id="cid", client_secret="sec", token_file=str(token_file))
    jobs, warnings = ut.search(client, auth, ["python"], pause=0)
    assert len(jobs) == 3 and warnings == []
    assert client.requests[1]["data"]["grant_type"] == "refresh_token"
    assert client.requests[2]["headers"]["Authorization"] == "Bearer new"
    saved = json.loads(token_file.read_text())
    assert saved["access_token"] == "new" and saved["refresh_token"] == "r2"
    assert oct(os.stat(token_file).st_mode & 0o777) == "0o600"


def test_a_rejected_token_without_refresh_stops_with_a_hint():
    client = _Client(_Resp({"message": "Authentication failed"}, 401))
    jobs, warnings = ut.search(client, _auth(), ["python", "django"], pause=0)
    assert jobs == [] and len(client.requests) == 1
    assert any("upwork_oauth.py" in w for w in warnings)


def test_auth_from_env_prefers_the_token_file(tmp_path, monkeypatch):
    f = tmp_path / "t.json"
    f.write_text(json.dumps({"access_token": "from-file", "refresh_token": "rf"}))
    monkeypatch.setenv("UPWORK_TOKEN_FILE", str(f))
    monkeypatch.setenv("UPWORK_ACCESS_TOKEN", "from-env")
    a = ut.UpworkAuth.from_env()
    assert a.access_token == "from-file" and a.refresh_token == "rf" and a.configured
    monkeypatch.setenv("UPWORK_TOKEN_FILE", str(tmp_path / "missing.json"))
    assert ut.UpworkAuth.from_env().access_token == "from-env"


# ── pasted jobs ──────────────────────────────────────────────────────────────

PASTED = """https://www.upwork.com/jobs/~01aaaa1111bbbb2222
Airflow migration for nightly jobs
Hourly: $40.00 - $80.00
Proposals: Less than 5
Payment verified
Move cron jobs into Airflow.
---
Logo refresh
Fixed-price - Est. budget: $150
Proposals: 50+
Payment unverified
https://www.upwork.com/jobs/Logo-refresh_~01cccc3333dddd4444/
"""


def test_parse_pasted_reads_type_budget_competition_and_link():
    jobs, warnings = ut.parse_pasted(PASTED)
    a, b = jobs
    assert warnings == []
    assert a["title"] == "Airflow migration for nightly jobs" and a["id"] == "~01aaaa1111bbbb2222"
    assert a["url"] == "https://www.upwork.com/jobs/~01aaaa1111bbbb2222"
    assert (a["type"], a["hourly_min"], a["hourly_max"]) == ("hourly", 40.0, 80.0)
    assert a["applicants"] == 4 and a["client"]["payment_verified"] is True
    assert "Move cron jobs into Airflow." in a["description"]
    assert (b["type"], b["fixed_budget"], b["applicants"]) == ("fixed", 150.0, 50)
    assert b["client"]["payment_verified"] is False and b["id"] == "~01cccc3333dddd4444"


def test_parse_pasted_splits_on_job_links_without_separators():
    text = ("https://www.upwork.com/jobs/~01aaaa00000001\nFirst job\nbody one\n"
            "https://www.upwork.com/jobs/~01aaaa00000002\nSecond job\nbody two")
    jobs, _ = ut.parse_pasted(text)
    assert [j["title"] for j in jobs] == ["First job", "Second job"]


def test_parse_pasted_warns_on_a_job_without_a_link():
    jobs, warnings = ut.parse_pasted("Some job\nFixed-price budget: $500\nDo things.")
    assert jobs[0]["id"].startswith("pasted-") and jobs[0]["fixed_budget"] == 500.0
    assert any("no Upwork link" in w for w in warnings)


# ── filters / rank / rate ────────────────────────────────────────────────────

def _j(**over):
    base = ut.normalize(NODES[0])
    client = {**base["client"], **over.pop("client", {})}
    return {**base, **over, "client": client}


@pytest.mark.parametrize("job,kw,expect", [
    (_j(), {}, ""),
    (_j(applied=True), {}, "already applied on Upwork"),
    (_j(), {"job_type": "fixed"}, "hourly job (wanted fixed)"),
    (_j(type="fixed", fixed_budget=100.0), {"min_fixed_budget": 200}, "fixed budget $100 < $200"),
    (_j(), {"min_hourly_rate": 80}, "hourly rate up to $70 < $80"),
    (_j(applicants=30), {"max_applicants": 20}, "30 proposals already (max 20)"),
    (_j(client={"payment_verified": False}), {"payment_verified_only": True},
     "client's payment method is not verified"),
    (_j(client={"payment_verified": None}), {"payment_verified_only": True}, ""),   # unknown passes
    (_j(), {"keywords": ["logo"]}, "title / skills don't match the keywords"),
    (_j(source="pasted"), {"keywords": ["logo"]}, ""),                               # user picked it
])
def test_skip_reason(job, kw, expect):
    assert ut.skip_reason(job, **kw) == expect


def test_rank_prefers_fresh_uncrowded_verified_jobs():
    now = ut._ts("2026-11-28T00:00:00Z")
    good, why = ut.rank(_j(), ["python"], now)
    crowded, _ = ut.rank(_j(applicants=60, client={"payment_verified": False}), ["python"], now)
    assert good > crowded and "keyword in title" in why and "posted in the last 24 h" in why
    assert 0 <= crowded <= good <= 100


@pytest.mark.parametrize("over,rate,expect", [
    ({}, 0, {"amount": 52.5, "unit": "hour", "basis": "middle of the client's hourly range"}),
    ({}, 50, {"amount": 50, "unit": "hour", "basis": "your hourly rate"}),
    ({}, 90, {"amount": 90, "unit": "hour", "basis": "your hourly rate (above the client's $70/hr max)"}),
    ({"type": "fixed", "fixed_budget": 250.0}, 50, {"amount": 250, "unit": "fixed",
                                                     "basis": "the client's fixed budget"}),
    ({"type": "fixed", "fixed_budget": None}, 0, None),
])
def test_suggested_rate(over, rate, expect):
    assert ut.suggested_rate(_j(**over), rate) == expect


def test_find_jobs_without_a_token_still_drafts_pasted_jobs(tmp_path, monkeypatch):
    monkeypatch.setenv("UPWORK_TOKEN_FILE", str(tmp_path / "none.json"))
    for k in ("UPWORK_ACCESS_TOKEN", "UPWORK_REFRESH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    out = ut.find_jobs(keywords="python", jobs_text=PASTED, ledger_path=str(tmp_path / "l.json"))
    assert out["sources"] == ["pasted"] and out["jobs_found"] == 2
    assert any("not configured" in w for w in out["warnings"])


def test_find_jobs_filters_ranks_and_skips_the_ledger(tmp_path):
    ledger = tmp_path / "l.json"
    ut.record_drafted(str(ledger), {"id": NODES[1]["id"], "title": "x"})
    assert json.loads(ledger.read_text())[NODES[1]["id"]]["title"] == ""   # ids only, no Upwork data
    out = ut.find_jobs(keywords="python, logo", auth=_auth(), client=_Client(), pause=0,
                       ledger_path=str(ledger))
    reasons = {s["id"]: s["reason"] for s in out["skipped"]}
    assert reasons[NODES[1]["id"]] == "already drafted in an earlier run (ledger)"
    assert reasons[NODES[2]["id"]] == "already applied on Upwork"
    assert [c["id"] for c in out["candidates"]] == [NODES[0]["id"]]
    assert out["candidates"][0]["rank_score"] > 0


# ── flow ─────────────────────────────────────────────────────────────────────

class _Result:
    def __init__(self, raw):
        self.raw = raw


@pytest.fixture
def flow_env(mocker, monkeypatch, tmp_path):
    mocker.patch.object(udf, "append_log")
    monkeypatch.setenv("UPWORK_LEDGER", str(tmp_path / "drafted.json"))
    mocker.patch("src.automation.harness.provider.resolve", return_value=(object(), "p", "m"))
    jobs = [_j(id="1", title="Python scraper"), _j(id="2", title="Python iOS app"),
            _j(id="3", title="Python API", applicants=9)]
    mocker.patch.object(ut.UpworkAuth, "from_env", classmethod(lambda cls: cls(access_token="t")))
    mocker.patch.object(ut, "search", return_value=(jobs, []))
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
    mocker.patch.object(udf, "FreelancerProposalCrew", side_effect=kickoff_for("FreelancerProposalCrew"))
    mocker.patch.object(udf, "GreenhouseRelevanceCrew", side_effect=kickoff_for("GreenhouseRelevanceCrew"))
    return seen


def test_flow_drafts_declines_filters_and_never_submits(flow_env, tmp_path):
    out = json.loads(udf.UpworkDraftsFlow().kickoff(inputs={
        "keywords": "python", "candidate_background": "Python dev", "task_filter": "no APIs",
        "hourly_rate": 50}))
    assert [d["id"] for d in out["drafted"]] == ["1"]
    d = out["drafted"][0]
    assert d["proposal"] == "Proposal for Python scraper. Question?" and d["rank"] == 1
    assert d["url"].startswith("https://www.upwork.com/jobs/~0")
    assert d["suggested_rate"]["amount"] == 50 and d["status"] == "draft — not submitted"
    reasons = {s["id"]: s["reason"] for s in out["skipped"]}
    assert reasons["2"] == "LLM declined: no iOS experience"
    assert reasons["3"].startswith("filtered out")
    assert out["submitted"] == 0 and "no Connects" in out["note"] and "not submitted" in out["summary"]
    drafts = [i for name, i in flow_env if name == "FreelancerProposalCrew"]
    assert all(i["candidate_background"] == "Python dev" and i["platform"] == "Upwork" for i in drafts)
    assert set(json.loads((tmp_path / "drafted.json").read_text())) == {"1"}


def test_flow_caps_drafts(flow_env):
    out = json.loads(udf.UpworkDraftsFlow().kickoff(inputs={
        "keywords": "python", "candidate_background": "Python dev", "max_drafts": 1}))
    assert len(out["drafted"]) == 1


@pytest.mark.parametrize("inputs,match", [
    ({"keywords": "python"}, "candidate_background"),
    ({"candidate_background": "x"}, "keywords"),
])
def test_flow_requires_background_and_a_source(mocker, inputs, match):
    mocker.patch.object(udf, "append_log")
    with pytest.raises(ValueError, match=match):
        udf.UpworkDraftsFlow().kickoff(inputs=inputs)


def test_flow_without_llm_drafts_nothing_but_still_reports(mocker, monkeypatch, tmp_path):
    mocker.patch.object(udf, "append_log")
    monkeypatch.setenv("UPWORK_LEDGER", str(tmp_path / "d.json"))
    mocker.patch("src.automation.harness.provider.resolve", side_effect=RuntimeError("no key"))
    out = json.loads(udf.UpworkDraftsFlow().kickoff(inputs={"jobs_text": PASTED, "candidate_background": "x"}))
    assert out["drafted"] == [] and out["jobs_found"] == 2
    assert any("no LLM available" in w for w in out["warnings"])


def test_eval_adapter_never_calls_the_upwork_api(mocker):
    from src.automation.harness import evalset
    seen = {}

    def fake_kickoff(job_type, payload, provider, model):
        with pytest.raises(AssertionError, match="never call the Upwork API"):
            ut.graphql(None, None, ut.SEARCH_QUERY, {})
        seen["jobs"] = ut.search(None, None, ["python"])[0]
        return "{}"
    mocker.patch.object(evalset, "_kickoff", side_effect=fake_kickoff)
    case = type("C", (), {"job_type": "upwork_drafts", "fixtures": {"jobs": [NODES[0]]},
                          "payload": {"keywords": "python"}})()
    evalset._ADAPTERS["upwork_drafts"].run(case, "openai", "gpt")
    assert [j["id"] for j in seen["jobs"]] == [NODES[0]["id"]]
