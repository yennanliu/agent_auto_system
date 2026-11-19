"""Workday discovery adapter (ats_workday) against a real captured response. No network."""
import json
from pathlib import Path

import httpx
import pytest

from src.automation.tools import ats_apply_tool as ats
from src.automation.tools.ats_router import BoardRef
from src.automation.tools.ats_workday import WorkdayAdapter, split_token

FIX = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "ats" / "workday_jobs.json")
                 .read_text(encoding="utf-8"))
API = "https://3m.wd1.myworkdayjobs.com/wday/cxs/3m/Search/jobs"


class _Resp:
    def __init__(self, status, data=None):
        self.status_code, self._data = status, data

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            req = httpx.Request("POST", API)
            raise httpx.HTTPStatusError("x", request=req, response=httpx.Response(self.status_code, request=req))


class _Client:
    def __init__(self, *responses):
        self.responses, self.bodies = list(responses), []

    def post(self, url, json=None):
        assert url == API
        self.bodies.append(json)
        return self.responses.pop(0)


def _adapter():
    sleeps = []
    return WorkdayAdapter(sleep=sleeps.append), sleeps


def test_split_token():
    assert split_token("ngc.wd1/Northrop_Grumman_External_Site") == ("ngc", "wd1", "Northrop_Grumman_External_Site")
    with pytest.raises(ValueError):
        split_token("ngc")


def test_discover_uses_the_first_keywords_as_search_text():
    ad, _ = _adapter()
    client = _Client(_Resp(200, FIX), _Resp(200, FIX))
    jobs = ad.discover(client, BoardRef("workday", "3m.wd1/Search"), search=("engineer", "", "data"))
    assert [b["searchText"] for b in client.bodies] == ["engineer", "data"]
    assert all(b["limit"] == 20 and b["offset"] == 0 for b in client.bodies)  # short page → stop
    j = jobs[0]
    assert j["platform"] == "workday" and j["token"] == "3m.wd1/Search" and j["company"] == "3m"
    assert j["url"].startswith("https://3m.wd1.myworkdayjobs.com/Search/job/")
    assert j["location"] == FIX["jobPostings"][0]["locationsText"]
    assert len(jobs) == len(FIX["jobPostings"])                           # deduped across terms


def test_discover_pages_until_total():
    ad, sleeps = _adapter()
    full = {"total": 45, "jobPostings": [{"title": f"Engineer {i}", "externalPath": f"/job/US/E_{i}",
                                          "locationsText": "US"} for i in range(20)]}
    page2 = {"total": 45, "jobPostings": [{"title": f"Engineer {i}", "externalPath": f"/job/US/E_{i}",
                                           "locationsText": "US"} for i in range(20, 40)]}
    last = {"total": 45, "jobPostings": [{"title": "Engineer 40", "externalPath": "/job/US/E_40"}]}
    client = _Client(_Resp(200, full), _Resp(200, page2), _Resp(200, last))
    jobs = ad.discover(client, BoardRef("workday", "3m.wd1/Search"), search=("engineer",))
    assert [b["offset"] for b in client.bodies] == [0, 20, 40] and len(jobs) == 41
    assert sleeps == [1.0, 1.0]                                           # paced between pages


def test_one_job_ref_searches_its_requisition_id():
    ad, _ = _adapter()
    client = _Client(_Resp(200, FIX))
    ad.discover(client, BoardRef("workday", "3m.wd1/Search", "Sr-EHS-Engineer_R01164059"))
    assert client.bodies[0]["searchText"] == "R01164059"


def test_429_backs_off_then_succeeds():
    ad, sleeps = _adapter()
    client = _Client(_Resp(429), _Resp(429), _Resp(200, FIX))
    assert ad.discover(client, BoardRef("workday", "3m.wd1/Search"), search=("engineer",))
    assert sleeps == [5, 10]


def test_persistent_429_raises_for_the_scanner_to_pause():
    ad, _ = _adapter()
    client = _Client(*[_Resp(429)] * 5)
    with pytest.raises(httpx.HTTPStatusError):
        ad.discover(client, BoardRef("workday", "3m.wd1/Search"))


@pytest.mark.parametrize("status", [403, 404, 410, 422])
def test_board_errors_are_dead_never_waited_on(status):
    ad, sleeps = _adapter()
    with pytest.raises(RuntimeError, match="no such Workday site"):
        ad.discover(_Client(_Resp(status)), BoardRef("workday", "3m.wd1/Search"))
    assert sleeps == []


def test_runner_lists_workday_jobs_as_manual_without_a_resume(tmp_path, mocker):
    mocker.patch("playwright.sync_api.sync_playwright")
    launch = mocker.patch.object(ats, "launch_browser")
    ad, _ = _adapter()
    client = _Client(_Resp(200, FIX))
    out = ats.run_ats_apply(boards="https://3m.wd1.myworkdayjobs.com/en-US/Search", keywords="engineer",
                            first_name="A", last_name="B", email="a@b.c", resume_path="",
                            client=client, adapters={"workday": ad}, ledger_path=str(tmp_path / "l.json"))
    assert "error" not in out and out["manual_count"] == len(FIX["jobPostings"])
    assert all("its own Workday candidate account" in m["reason"] for m in out["manual"])
    launch.assert_not_called()
