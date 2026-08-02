"""Unit tests for the Maps website resolver used by website-less lead sources.

The safety-critical part is :func:`_name_matches`: Google Maps never answers
"not found", so without a name check a registry company Maps has never heard of
picks up whichever unrelated business was nearest — a wrong email filed under a
real company. These tests pin that behaviour, plus the browser-free paths.
"""
import pytest

from src.automation.tools import maps_search_tool as M


@pytest.mark.parametrize("query,found", [
    # Registry legal name vs the trade name Maps lists.
    ("鼎高網路行銷有限公司", "鼎高網路行銷"),
    ("奧美廣告股份有限公司", "奧美廣告"),
    ("賽博整合行銷有限公司", "賽博整合行銷（CyberMaster Co., Ltd.）"),
    # 台/臺 is the same character as far as TW addresses and names go.
    ("台灣大哥大股份有限公司", "臺灣大哥大"),
    # Punctuation and spacing differences.
    ("SHOPLINE", "Shopline"),
    ("Acme Co., Ltd.", "ACME"),
    ("光寶科技股份有限公司", "光寶科技 LITE-ON"),
])
def test_name_matches_accepts_the_same_company(query, found):
    assert M._name_matches(query, found) is True


@pytest.mark.parametrize("query,found", [
    # The real regression: Maps returned a different 鼎高 for this query.
    ("鼎高網路行銷有限公司", "鼎高科技 監視器停車場設備影像智慧應用廠商"),
    # No listing at all → Maps falls back to something nearby.
    ("zzz不存在的公司名稱xyz", "安利美特線上商店"),
    ("奧美廣告股份有限公司", "潮網科技股份有限公司"),
    # A one-character stem must not match by containment.
    ("大有限公司", "大同股份有限公司"),
    ("", "奧美廣告"),
    ("奧美廣告股份有限公司", ""),
])
def test_name_matches_rejects_a_different_company(query, found):
    assert M._name_matches(query, found) is False


@pytest.mark.parametrize("name,core", [
    ("鼎高網路行銷有限公司", "鼎高網路行銷"),
    ("台灣積體電路製造股份有限公司", "臺灣積體電路製造"),
    ("Acme Co., Ltd.", "acme"),          # stacked suffixes: "acme"+"co"+"ltd"
    ("有限公司", "有限"),                 # never stripped below two characters
])
def test_name_core_strips_suffixes_and_noise(name, core):
    assert M._name_core(name) == core


def test_resolve_websites_dedupes_and_skips_blanks(monkeypatch):
    seen = []

    def _fake_resolve(_page, name, region):
        seen.append((name, region))
        return f"https://{len(seen)}.example"

    monkeypatch.setattr(M, "_resolve_one", _fake_resolve)
    monkeypatch.setattr(M, "sync_playwright", _stub_playwright(), raising=False)

    out = M.resolve_websites([" A ", "A", "", "  ", "B"], "台北")
    assert [n for n, _ in seen] == ["A", "B"]
    assert set(out) == {"A", "B"}


def test_resolve_websites_returns_empty_without_playwright(monkeypatch):
    """Importing playwright is done lazily; a missing browser must not raise."""
    import builtins

    real_import = builtins.__import__

    def _no_playwright(name, *args, **kwargs):
        if name.startswith("playwright"):
            raise ImportError("playwright not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_playwright)
    logs = []
    assert M.resolve_websites(["A"], "台北", log=logs.append) == {}
    assert any("playwright unavailable" in m for m in logs)


def test_resolve_websites_short_circuits_on_no_names():
    assert M.resolve_websites([], "台北") == {}
    assert M.resolve_websites(["", "   "], "台北") == {}


def _stub_playwright():
    """Minimal sync_playwright() stand-in: a context manager yielding a browser."""
    class _Page:
        pass

    class _Ctx:
        def new_page(self): return _Page()

    class _Browser:
        def new_context(self, **_kw): return _Ctx()
        def close(self): pass

    class _Chromium:
        def launch(self, **_kw): return _Browser()

    class _PW:
        chromium = _Chromium()
        def __enter__(self): return self
        def __exit__(self, *_a): return False

    return lambda: _PW()
