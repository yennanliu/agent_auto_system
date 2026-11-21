"""Personio and BambooHR: discovery-only adapters, against real captured feeds
(ottonova on Personio, Scribd on BambooHR, 2026-11-21). No network.

Both list matching jobs under ``manual``: their forms carry a bot check
(Cloudflare Turnstile / a reCAPTCHA checkbox) that is never solved."""
import json
from pathlib import Path

import pytest

from src.automation.tools import ats_apply_tool as ats
from src.automation.tools.ats_bamboohr import BambooHRAdapter
from src.automation.tools.ats_personio import PersonioAdapter, parse_feed
from src.automation.tools.ats_router import BoardRef, parse_board_ref

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "ats"
FEED = (FIX / "personio_feed.xml").read_bytes()
BAMBOO = json.loads((FIX / "bamboohr_list.json").read_text(encoding="utf-8"))


class _Resp:
    def __init__(self, status=200, content=b"", data=None, ctype="application/json"):
        self.status_code, self.content, self._data = status, content, data
        self.headers = {"content-type": ctype}

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
        return self.routes.get(url, _Resp(404, ctype="text/html"))


# ── routing ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ref, want", [
    ("personio:clark", BoardRef("personio", "clark")),
    ("https://clark.jobs.personio.de/job/2894252/apply?display=en", BoardRef("personio", "clark", "2894252")),
    ("https://1komma5grad.jobs.personio.com/", BoardRef("personio", "1komma5grad")),
    ("bamboohr:scribd/144", BoardRef("bamboohr", "scribd", "144")),
    ("https://scribd.bamboohr.com/careers/144", BoardRef("bamboohr", "scribd", "144")),
    ("https://scribd.bamboohr.com/careers", BoardRef("bamboohr", "scribd")),
])
def test_routing(ref, want):
    assert parse_board_ref(ref) == want


@pytest.mark.parametrize("ref", ["personio:clark/abc", "bamboohr:scribd/x1", "https://www.bamboohr.com/careers",
                                 "https://scribd.bamboohr.com/pricing", "https://clark.jobs.personio.de/job/abc"])
def test_bad_refs_are_refused(ref):
    assert parse_board_ref(ref) is None


# ── Personio ─────────────────────────────────────────────────────────────────

def test_personio_feed_reads_positions_not_description_headings():
    jobs = parse_feed(FEED, "ottonova", "https://ottonova.jobs.personio.de")
    assert len(jobs) == 3
    titles = [j["title"] for j in jobs]
    assert not any(t.isupper() for t in titles)               # never "DEIN TEAM" (a nested <name>)
    j = jobs[0]
    assert j["job_id"] == "2888695" and j["title"].startswith("(Junior) Customer Support Agent")
    assert j["location"] == "München, Köln"                     # office + additionalOffices
    assert j["apply_url"] == "https://ottonova.jobs.personio.de/job/2888695/apply"


def test_personio_falls_back_to_the_com_domain():
    client = _Client({"https://acme.jobs.personio.com/xml": _Resp(content=FEED, ctype="text/xml")})
    jobs = PersonioAdapter().discover(client, BoardRef("personio", "acme"))
    assert client.urls == ["https://acme.jobs.personio.de/xml", "https://acme.jobs.personio.com/xml"]
    assert jobs[0]["url"].startswith("https://acme.jobs.personio.com/job/")


def test_personio_unknown_company():
    with pytest.raises(RuntimeError, match="no such Personio company"):
        PersonioAdapter().discover(_Client({}), BoardRef("personio", "nope"))


# ── BambooHR ─────────────────────────────────────────────────────────────────

def test_bamboohr_list():
    client = _Client({"https://scribd.bamboohr.com/careers/list": _Resp(data=BAMBOO)})
    jobs = BambooHRAdapter().discover(client, BoardRef("bamboohr", "scribd"))
    assert [j["job_id"] for j in jobs] == [str(r["id"]) for r in BAMBOO["result"]]
    j = jobs[0]
    assert j["title"] == "Senior Software Engineer" and j["location"] == "Amsterdam, North Holland"
    assert j["apply_url"] == "https://scribd.bamboohr.com/careers/144"


@pytest.mark.parametrize("resp", [_Resp(302, ctype="text/html"),                 # unknown → bamboohr.com
                                  _Resp(200, data=None, ctype="text/html")])     # the redirect, followed
def test_bamboohr_unknown_company(resp):
    client = _Client({"https://gitlab.bamboohr.com/careers/list": resp})
    with pytest.raises(RuntimeError, match="no such BambooHR company"):
        BambooHRAdapter().discover(client, BoardRef("bamboohr", "gitlab"))


# ── through the runner: listed for manual apply, with the reason ────────────

@pytest.mark.parametrize("adapter, board, url, resp, why", [
    (PersonioAdapter(), "personio:ottonova", "https://ottonova.jobs.personio.de/xml",
     _Resp(content=FEED, ctype="text/xml"), "Cloudflare Turnstile"),
    (BambooHRAdapter(), "bamboohr:scribd", "https://scribd.bamboohr.com/careers/list",
     _Resp(data=BAMBOO), "reCAPTCHA"),
])
def test_runner_lists_them_as_manual_without_a_browser(tmp_path, mocker, adapter, board, url, resp, why):
    mocker.patch("playwright.sync_api.sync_playwright")
    launch = mocker.patch.object(ats, "launch_browser")
    out = ats.run_ats_apply(boards=board, first_name="A", last_name="B", email="a@b.c", resume_path="",
                            client=_Client({url: resp}), adapters={adapter.platform: adapter},
                            ledger_path=str(tmp_path / "l.json"))
    assert "error" not in out and out["manual_count"] >= 1 and out["applied_count"] == 0
    assert all(why in m["reason"] and m["apply_url"] for m in out["manual"])
    launch.assert_not_called()



# ── PR #123 review ───────────────────────────────────────────────────────────

class _Boom:
    status_code = 503
    headers = {"content-type": "text/html"}

    def raise_for_status(self):
        raise RuntimeError("HTTP 503")


@pytest.mark.parametrize("resp", [_Boom(), _Resp(200, content=b"<html>not xml", ctype="text/html")])
def test_personio_any_failure_on_de_still_tries_com(resp):
    client = _Client({"https://acme.jobs.personio.de/xml": resp,
                      "https://acme.jobs.personio.com/xml": _Resp(content=FEED, ctype="text/xml")})
    jobs = PersonioAdapter().discover(client, BoardRef("personio", "acme"))
    assert len(jobs) == 3 and jobs[0]["url"].startswith("https://acme.jobs.personio.com/")


def test_personio_a_real_failure_is_not_reported_as_a_missing_board():
    client = _Client({"https://acme.jobs.personio.de/xml": _Boom()})          # .com: 404
    with pytest.raises(RuntimeError, match=r"Personio feed failed \(\.de: HTTP 503\)"):
        PersonioAdapter().discover(client, BoardRef("personio", "acme"))


def test_bamboohr_a_server_error_is_not_reported_as_a_missing_board():
    client = _Client({"https://scribd.bamboohr.com/careers/list": _Boom()})
    with pytest.raises(RuntimeError, match="HTTP 503"):
        BambooHRAdapter().discover(client, BoardRef("bamboohr", "scribd"))
