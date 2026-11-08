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


def test_resolve_geo_builtin_markets_skip_network(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("built-in markets must not hit the typeahead")
    monkeypatch.setattr(t.urllib.request, "urlopen", boom)
    # "UK" must be aliased (the typeahead maps it to Ukraine) to the built-in id.
    assert t._resolve_geo_id("UK") == ("101165590", "United Kingdom")
    assert t._resolve_geo_id("taiwan")[0] == "104187078"
    # Every alias target resolves offline.
    for alias in t._GEO_ALIASES.values():
        assert t._GEO_FALLBACK.get(alias.lower()), alias


def test_wants_geo():
    assert t._wants_geo("Taiwan")
    assert not t._wants_geo("")
    assert not t._wants_geo(" Remote ")
    assert not t._wants_geo("anywhere")


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
        def read(self): return b'[{"id":"999","displayName":"Taichung, Taiwan"}]'
    monkeypatch.setattr(t.urllib.request, "urlopen", lambda *a, **k: Resp())
    assert t._resolve_geo_id("Taichung") == ("999", "Taichung, Taiwan")


class _FakeLoc:
    def __init__(self, vis): self._vis = vis
    def count(self): return len(self._vis)
    def nth(self, i): return type("E", (), {"is_visible": lambda _s: self._vis[i]})()


class _FakePage:
    """texts: [(text, visible)]"""
    def __init__(self, texts): self._texts = texts

    def get_by_text(self, text, exact=False):
        return _FakeLoc([v for t_, v in self._texts if text in t_])


def test_hit_daily_limit_detects_visible_dialog():
    limit = "You reached today’s Easy Apply limit"
    assert t._hit_daily_limit(_FakePage([(limit, True)]))
    # Visible copy after a hidden template copy still counts.
    assert t._hit_daily_limit(_FakePage([(limit, False), (limit, True)]))
    assert not t._hit_daily_limit(_FakePage([("Contact info", True)]))


def test_hit_daily_limit_ignores_hidden_text():
    assert not t._hit_daily_limit(_FakePage([("Easy Apply limit", False)]))


def test_easy_apply_selectors_cover_link_variant():
    # New job-view UI renders Easy Apply as <a aria-label="Easy Apply to this job">.
    assert any(s.startswith("a[aria-label*='Easy Apply']") for s in t._EASY_APPLY_SELECTORS)
