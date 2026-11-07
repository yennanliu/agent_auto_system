"""Unit tests for the LinkedIn Easy Apply search helpers (no browser)."""
from urllib.parse import parse_qs, urlparse

from src.automation.tools import linkedin_apply_tool as t


def test_search_url_uses_new_results_path_and_geo_id():
    url = t._search_url("Software Engineer", "Taiwan", 2, geo_id="104187078")
    u = urlparse(url)
    assert u.path == "/jobs/search-results/"
    q = parse_qs(u.query)
    assert q["geoId"] == ["104187078"]
    assert q["f_AL"] == ["true"]
    assert q["start"] == ["25"]


def test_search_url_remote_and_no_geo():
    q = parse_qs(urlparse(t._search_url("x", "", 1, remote=True)).query)
    assert "geoId" not in q
    assert q["f_WT"] == ["2"]


def test_clean_title_strips_verified_suffix():
    assert t._clean_title("Automation Engineer (Verified job)") == "Automation Engineer"
    assert t._clean_title("  Backend Engineer ") == "Backend Engineer"


def test_resolve_geo_numeric_and_remote_need_no_network(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("network must not be called")
    monkeypatch.setattr(t.urllib.request, "urlopen", boom)
    assert t._resolve_geo_id("104187078") == ("104187078", "104187078")
    assert t._resolve_geo_id("Remote") == (None, "")
    assert t._resolve_geo_id("") == (None, "")


def test_resolve_geo_alias_and_fallback_when_offline(monkeypatch):
    seen = []

    def offline(req, timeout=0):
        seen.append(req.full_url)
        raise OSError("offline")
    monkeypatch.setattr(t.urllib.request, "urlopen", offline)
    # "UK" must be aliased (the typeahead maps it to Ukraine) and then fall back.
    assert t._resolve_geo_id("UK") == ("101165590", "United Kingdom")
    assert "United%20Kingdom" in seen[0]
    assert t._resolve_geo_id("taiwan")[0] == "104187078"


def test_resolve_geo_unknown_offline_warns(monkeypatch):
    monkeypatch.setattr(t.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    warnings: list = []
    assert t._resolve_geo_id("Atlantis", warnings=warnings) == (None, "")
    assert warnings and "Atlantis" in warnings[0]


def test_resolve_geo_uses_typeahead_first_hit(monkeypatch):
    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'[{"id":"106907071","displayName":"Taipei, Taiwan"}]'
    monkeypatch.setattr(t.urllib.request, "urlopen", lambda *a, **k: Resp())
    assert t._resolve_geo_id("Taipei") == ("106907071", "Taipei, Taiwan")
