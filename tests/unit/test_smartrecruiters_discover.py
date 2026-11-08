"""Unit tests for the SmartRecruiters discovery automation: company/posting refs,
discovery (fake Posting API client, pagination), seen-ledger, run orchestration,
validator and flow wiring. No network."""
import pytest

from src.automation.tools import smartrecruiters_discover_tool as sr

# ── company / posting refs ────────────────────────────────────────────────────

UUID = "75e56412-2f00-4a03-8766-691a73eeee75"


@pytest.mark.parametrize("ref, expected", [
    ("ServiceNow", ("ServiceNow", "")),
    ("BoschGroup", ("BoschGroup", "")),
    ("https://jobs.smartrecruiters.com/ServiceNow", ("ServiceNow", "")),
    ("jobs.smartrecruiters.com/ServiceNow/", ("ServiceNow", "")),
    ("https://careers.smartrecruiters.com/ServiceNow", ("ServiceNow", "")),
    ("https://jobs.smartrecruiters.com/ServiceNow/744000160326680-forward-deployed-engineer?oga=true",
     ("ServiceNow", "744000160326680")),
    (f"https://jobs.smartrecruiters.com/oneclick-ui/company/ServiceNow/publication/{UUID}?dcr_ci=ServiceNow",
     ("ServiceNow", UUID)),
])
def test_parse_company_ref_accepts_ids_and_urls(ref, expected):
    assert sr.parse_company_ref(ref) == expected


@pytest.mark.parametrize("ref", ["", "  ", "https://www.smartjobs.io/", "not a company!",
                                 "https://evil.com/ServiceNow", "https://smartrecruiters.com.evil.com/x"])
def test_parse_company_ref_rejects_other_sites(ref):
    assert sr.parse_company_ref(ref) is None


# ── discovery ─────────────────────────────────────────────────────────────────

def _p(pid, title, city="Taipei", country="tw", remote=False, full=None, company="Acme"):
    return {"id": str(pid), "uuid": f"{pid:08d}-0000-0000-0000-000000000000", "name": title,
            "company": {"identifier": company, "name": company},
            "location": {"city": city, "country": country, "remote": remote,
                         "fullLocation": full or f"{city}, {country.upper()}"}}


class _Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeAPI:
    """Stands in for httpx.Client over the Posting API, honouring limit/offset."""

    def __init__(self, companies, fail=()):
        self.companies, self.fail, self.calls = companies, set(fail), []

    def get(self, url, params=None):
        self.calls.append((url, dict(params or {})))
        token = url.split("/companies/")[1].split("/")[0]
        if token in self.fail:
            return _Resp({}, 500)
        rows = self.companies.get(token, [])
        off, lim = int(params.get("offset", 0)), min(int(params.get("limit", 100)), 100)
        return _Resp({"offset": off, "limit": lim, "totalFound": len(rows), "content": rows[off:off + lim]})

    def close(self):
        pass


def test_location_text_marks_remote_and_hybrid():
    assert sr._location_text({"fullLocation": "Atlanta, Georgia, United States", "remote": True}) \
        == "Atlanta, Georgia, United States (Remote)"
    assert sr._location_text({"city": "Taipei", "country": "tw", "hybrid": True}) == "Taipei, TW (Hybrid)"
    assert sr._location_text({}) == ""


def test_list_postings_paginates_and_respects_cap():
    api = FakeAPI({"Big": [_p(i, f"Job {i}") for i in range(250)]})
    rows, total = sr.list_postings(api, "Big", max_listed=1000)
    assert total == 250 and len(rows) == 250
    assert [c[1]["offset"] for c in api.calls] == [0, 100, 200]
    rows, total = sr.list_postings(FakeAPI({"Big": [_p(i, "J") for i in range(250)]}), "Big", max_listed=150)
    assert total == 250 and len(rows) == 150


def test_discover_filters_interleaves_and_warns():
    api = FakeAPI({
        "a": [_p(1, "Backend Engineer"), _p(2, "Senior Backend Engineer"), _p(3, "Recruiter")],
        "b": [_p(9, "Software Engineer, Backend", city="Remote", full="Anywhere", remote=True)],
    }, fail={"down"})
    warnings: list = []
    jobs, total = sr.discover_jobs(api, ["a", "b", "nosuch", "down", "https://smartjobs.io/x"],
                                   ["BE"], [], sr._noop, warnings)
    assert total == 4
    assert [(j["token"], j["job_id"]) for j in jobs] == [("a", "1"), ("b", "9"), ("a", "2")]
    assert jobs[0]["url"] == "https://jobs.smartrecruiters.com/a/1"
    assert jobs[1]["location"] == "Anywhere (Remote)"
    text = " | ".join(warnings)
    assert "nosuch: no open postings" in text
    assert "down: could not list postings" in text
    assert "not a SmartRecruiters company" in text


def test_discover_location_filter_matches_remote_marker():
    api = FakeAPI({"a": [_p(1, "Engineer", city="Taipei"), _p(2, "Engineer", city="NYC", remote=True)]})
    jobs, _ = sr.discover_jobs(api, ["a"], [], ["remote"], sr._noop, [])
    assert [j["job_id"] for j in jobs] == ["2"]


def test_discover_posting_url_bypasses_filters():
    api = FakeAPI({"a": [_p(1, "Recruiter"), _p(2, "Office Manager")]})
    jobs, _ = sr.discover_jobs(api, ["https://jobs.smartrecruiters.com/a/2-office-manager"],
                               ["backend"], ["berlin"], sr._noop, [])
    assert [j["job_id"] for j in jobs] == ["2"]
    uuid = jobs[0]["uuid"]
    jobs, _ = sr.discover_jobs(api, [f"https://jobs.smartrecruiters.com/oneclick-ui/company/a/publication/{uuid}"],
                               ["backend"], [], sr._noop, [])
    assert [j["job_id"] for j in jobs] == ["2"]


def test_discover_warns_when_picked_posting_is_closed():
    warnings: list = []
    jobs, _ = sr.discover_jobs(FakeAPI({"a": [_p(1, "X")]}), ["jobs.smartrecruiters.com/a/999-gone"],
                               [], [], sr._noop, warnings)
    assert jobs == [] and any("posting 999 is not open" in w for w in warnings)


def test_discover_warns_when_company_exceeds_scan_cap():
    warnings: list = []
    sr.discover_jobs(FakeAPI({"a": [_p(i, "X") for i in range(30)]}), ["a"], [], [], sr._noop,
                     warnings, max_listed=10)
    assert any("first 10 of 30" in w for w in warnings)


def test_job_has_posting_and_apply_links_and_newest_first():
    rows = [_p(1, "Engineer"), _p(2, "Engineer")]
    rows[0]["releasedDate"], rows[1]["releasedDate"] = "2026-10-01T00:00:00Z", "2026-11-05T00:00:00Z"
    rows[1]["department"], rows[1]["typeOfEmployment"] = {"label": "R&D"}, {"label": "Full-time"}
    jobs, _ = sr.discover_jobs(FakeAPI({"Acme": rows}), ["Acme"], [], [], sr._noop, [])
    assert [j["job_id"] for j in jobs] == ["2", "1"]
    j = jobs[0]
    assert j["posted"] == "2026-11-05" and j["department"] == "R&D" and j["employment_type"] == "Full-time"
    assert j["url"] == "https://jobs.smartrecruiters.com/Acme/2"
    assert j["apply_url"] == ("https://jobs.smartrecruiters.com/oneclick-ui/company/Acme/publication/"
                              "00000002-0000-0000-0000-000000000000?dcr_ci=Acme")


# ── run orchestration ─────────────────────────────────────────────────────────

def _run(tmp_path, **kw):
    kw.setdefault("client", FakeAPI({"a": [_p(1, "Backend Engineer"), _p(2, "Backend Lead"), _p(3, "Recruiter")],
                                     "b": [_p(9, "Backend Developer")]}))
    return sr.run_smartrecruiters_discover(ledger_path=str(tmp_path / "seen.json"), **kw)


def test_run_returns_matches_and_only_new_skips_seen(tmp_path):
    r = _run(tmp_path, companies="a, b", keywords="backend")
    assert r["jobs_found"] == 3 and r["matched_count"] == 3 and r["total_listed"] == 4
    assert r["summary"].startswith("3 new matching posting(s) from 2 companies")
    again = _run(tmp_path, companies="a, b", keywords="backend")
    assert again["jobs_found"] == 0 and again["already_seen_count"] == 3
    everything = _run(tmp_path, companies="a, b", keywords="backend", only_new=False)
    assert everything["jobs_found"] == 3


def test_run_caps_results_and_only_records_returned_postings(tmp_path):
    r = _run(tmp_path, companies="a, b", keywords="backend", max_results=2)
    assert [j["job_id"] for j in r["jobs"]] == ["1", "9"]
    r = _run(tmp_path, companies="a, b", keywords="backend")
    assert [j["job_id"] for j in r["jobs"]] == ["2"]          # the capped-out one is still new


def test_run_relevance_gate_filters_and_fails_open(tmp_path):
    def gate(title, meta):
        if "Lead" in title:
            return False, "management"
        if "Developer" in title:
            raise RuntimeError("llm down")
        return True, ""
    r = _run(tmp_path, companies="a, b", keywords="backend", relevance_fn=gate)
    assert [j["job_id"] for j in r["jobs"]] == ["1", "9"]     # errors keep the posting
    assert r["filtered_count"] == 1 and r["skipped"][0]["reason"] == "filtered out: management"
    again = _run(tmp_path, companies="a, b", keywords="backend")
    assert [j["job_id"] for j in again["jobs"]] == ["2"]      # filtered ≠ seen


def test_run_no_companies_is_an_error_not_a_raise(tmp_path):
    r = _run(tmp_path, companies=" , ")
    assert r["error"] and r["jobs"] == []


def test_run_api_failure_is_a_warning(tmp_path):
    r = _run(tmp_path, companies="a, down", client=FakeAPI({"a": [_p(1, "Engineer")]}, fail={"down"}))
    assert r["jobs_found"] == 1 and any("down: could not list" in w for w in r["warnings"])


def test_ledger_roundtrip_and_corrupt_file(tmp_path):
    path = tmp_path / "seen.json"
    path.write_text("not json")
    assert sr.load_ledger(str(path)) == {}
    sr.record_seen(str(path), [{"token": "Acme", "job_id": "1", "title": "T", "company": "Acme"}])
    assert "acme/1" in sr.load_ledger(str(path))


def test_basetool_wraps_exceptions(mocker):
    mocker.patch.object(sr, "run_smartrecruiters_discover", side_effect=RuntimeError("boom"))
    out = sr.SmartRecruitersDiscoverTool()._run(companies="a")
    assert out["error"] == "RuntimeError: boom" and out["jobs"] == []


# ── validator / flow ──────────────────────────────────────────────────────────

def test_validator_accepts_results_and_zero_new_rejects_error():
    from src.automation.harness.validator import validate

    assert validate("smartrecruiters_discover", {"jobs": [{"job_id": "1"}], "jobs_found": 1,
                                                 "summary": "1 new matching posting(s)."}).valid
    assert validate("smartrecruiters_discover", {"jobs": [], "jobs_found": 0,
                                                 "summary": "0 new matching posting(s)."}).valid
    assert not validate("smartrecruiters_discover", {"jobs": [], "error": "No companies"}).valid


def test_flow_raises_on_missing_required():
    from src.automation.flows.smartrecruiters_discover_flow import SmartRecruitersDiscoverFlow

    with pytest.raises(Exception):
        SmartRecruitersDiscoverFlow().kickoff(inputs={"keywords": "backend"})


def test_flow_passes_args_and_no_llm_by_default(mocker):
    captured = {}
    mocker.patch("src.automation.flows.smartrecruiters_discover_flow.run_smartrecruiters_discover",
                 side_effect=lambda **kw: captured.update(kw) or {"jobs": [], "jobs_found": 0})
    resolve = mocker.patch("src.automation.harness.provider.resolve",
                           return_value=(object(), "gemini", "gemini/gemini-2.5-flash"))
    from src.automation.flows.smartrecruiters_discover_flow import SmartRecruitersDiscoverFlow

    SmartRecruitersDiscoverFlow().kickoff(inputs={"companies": "ServiceNow", "keywords": "SRE",
                                                  "max_results": 7, "only_new": False})
    assert captured["companies"] == "ServiceNow" and captured["keywords"] == "SRE"
    assert captured["max_results"] == 7 and captured["only_new"] is False
    assert captured["relevance_fn"] is None
    resolve.assert_not_called()


def test_flow_wires_relevance_gate_when_filter_set(mocker):
    captured = {}
    mocker.patch("src.automation.flows.smartrecruiters_discover_flow.run_smartrecruiters_discover",
                 side_effect=lambda **kw: captured.update(kw) or {"jobs": [], "jobs_found": 0})
    mocker.patch("src.automation.harness.provider.resolve",
                 return_value=(object(), "gemini", "gemini/gemini-2.5-flash"))
    from src.automation.flows.smartrecruiters_discover_flow import SmartRecruitersDiscoverFlow

    SmartRecruitersDiscoverFlow().kickoff(inputs={"companies": "ServiceNow", "task_filter": "backend only"})
    assert callable(captured["relevance_fn"])
