"""Offline tests for scripts/freelancer_live_check.py (no network, no token)."""
import importlib.util
import sys
from pathlib import Path

import pytest

from src.automation.tools import freelancer_tool as ft

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "freelancer_live_check.py"
_spec = importlib.util.spec_from_file_location("freelancer_live_check", _PATH)
flc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(flc)

PROJECT = {"id": 7, "title": "Scrape a site", "type": "fixed", "status": "active", "seo_url": "python/x",
           "currency": {"code": "USD", "sign": "$", "exchange_rate": 1},
           "budget": {"minimum": 100, "maximum": 300}, "bid_stats": {"bid_count": 3, "bid_avg": 200},
           "jobs": [{"name": "Python"}], "description": "Scrape it", "time_submitted": 1}
SELF = {"status": "success", "result": {"id": 42, "username": "adalovelace", "status": {"email_verified": True},
                                         "membership_package": {"name": "Free", "bid_limit": 6}}}


class _Resp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Client:
    def __init__(self, self_resp=None, search_resp=None):
        self.self_resp = self_resp or _Resp(200, SELF)
        self.search_resp = search_resp or _Resp(200, {"status": "success", "result": {"projects": [PROJECT]}})
        self.posts = []

    def get(self, url, headers=None, params=None):
        if "/users/0.1/self/" in url:
            return self.self_resp
        if "/projects/0.1/projects/7/" in url:
            return _Resp(200, {"status": "success", "result": PROJECT})
        return self.search_resp

    def post(self, *a, **k):
        self.posts.append((a, k))
        return _Resp(200, {"status": "success", "result": {"id": 99}})

    def close(self):
        pass


@pytest.fixture
def run(monkeypatch, capsys):
    def _run(*argv, client=None, token="secret-token-value-123"):
        import httpx
        client = client or _Client()
        monkeypatch.setattr(httpx, "Client", lambda *a, **k: client)
        if token:
            monkeypatch.setenv("FREELANCER_OAUTH_TOKEN", token)
        else:
            monkeypatch.delenv("FREELANCER_OAUTH_TOKEN", raising=False)
        # setenv (not delenv) so monkeypatch restores it after --sandbox writes os.environ.
        monkeypatch.setenv("FREELANCER_API_BASE", "")
        monkeypatch.setattr(sys, "argv", ["freelancer_live_check.py", *argv])
        code = flc.main()
        return code, capsys.readouterr().out, client
    return _run


def test_missing_token_exits_2(run):
    code, out, _ = run(token="")
    assert code == 2 and "FREELANCER_OAUTH_TOKEN is not set" in out


def test_read_only_check_passes_and_never_posts_or_leaks(run):
    code, out, client = run()
    assert code == 0 and client.posts == []
    assert "secret-token-value-123" not in out and "adalovelace" not in out
    assert "ok       account" in out and "ok       payload" in out
    assert "'bidder_id': 42" in out and "'milestone_percentage': 100" in out


def test_bad_token_fails_with_a_scope_hint(run):
    bad = _Resp(401, {"status": "error", "message": "You must be logged in to perform this request",
                      "error_code": "RestExceptionCodes.NOT_AUTHENTICATED"})
    code, out, _ = run(client=_Client(self_resp=bad))
    assert code == 1 and "FAILED   account" in out and "fln:user_information" in out


def test_account_that_cannot_bid_fails(run):
    blocked = _Resp(200, {"status": "success", "result": {"id": 42, "username": "a", "status": {
        "email_verified": False}}})
    code, out, _ = run(client=_Client(self_resp=blocked))
    assert code == 1 and "email not verified" in out


def test_search_drift_fails(run):
    code, out, _ = run(client=_Client(search_resp=_Resp(200, {"status": "success", "result": {"projects": []}})))
    assert code == 1 and "FAILED   search" in out


def test_place_bid_needs_a_proposal_file(run):
    code, out, client = run("--place-bid", "7")
    assert code == 1 and client.posts == []


def test_place_bid_refuses_without_a_terminal(run, tmp_path, monkeypatch):
    prop = tmp_path / "p.txt"
    prop.write_text("I have built many scrapers in Python. " * 5)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    code, out, client = run("--place-bid", "7", "--proposal-file", str(prop))
    assert code == 1 and client.posts == [] and "interactive confirmation" in out


def test_place_bid_sends_only_after_typed_confirmation(run, tmp_path, monkeypatch):
    prop = tmp_path / "p.txt"
    prop.write_text("I have built many scrapers in Python. " * 5)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")
    code, out, client = run("--place-bid", "7", "--proposal-file", str(prop))
    assert client.posts == [] and "not confirmed" in out
    monkeypatch.setattr("builtins.input", lambda _prompt: "7")
    code, out, client = run("--place-bid", "7", "--proposal-file", str(prop))
    assert code == 0 and len(client.posts) == 1 and "bid 99 placed" in out
    assert client.posts[0][1]["json"]["project_id"] == 7


def test_sandbox_flag_switches_the_host(run):
    code, out, _ = run("--sandbox")
    assert f"API base: {ft.SANDBOX_BASE}" in out


@pytest.mark.parametrize("body,why", [
    ({"project_id": 1, "bidder_id": 2, "amount": 5.0, "period": 3, "milestone_percentage": 50}, ""),
    ({"project_id": 1, "bidder_id": 2, "amount": 5.0, "period": 3}, "missing required field milestone_percentage"),
    ({"project_id": "1", "bidder_id": 2, "amount": 5.0, "period": 3, "milestone_percentage": 50},
     "project_id is str, expected int"),
    ({"project_id": 1, "bidder_id": 2, "amount": 5.0, "period": 3, "milestone_percentage": 10},
     "milestone_percentage outside 20-100"),
])
def test_check_payload(body, why):
    assert flc.check_payload(body) == why


def test_masking():
    assert flc.mask("abcdefghij") == "abcd…(10 chars)" and flc.mask_name("adalovelace") == "ad***"
