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
    browser = _stub_playwright(monkeypatch)

    out = M.resolve_websites([" A ", "A", "", "  ", "B"], "台北")
    # Assert the whole call, not just the names: a regression that drops the
    # region would silently resolve the right name in the wrong place.
    assert seen == [("A", "台北"), ("B", "台北")]
    assert set(out) == {"A", "B"}
    # One browser for the batch, closed afterwards — not one per name.
    assert browser.launches == 1
    assert browser.closed is True


def test_resolve_websites_paces_between_lookups(monkeypatch):
    """Back-to-back Maps navigations invite a rate-limit wall."""
    pauses = []
    monkeypatch.setattr(M, "_resolve_one", lambda page, *_a: "")
    monkeypatch.setattr(M, "_pause", lambda _page, ms: pauses.append(ms))
    _stub_playwright(monkeypatch)
    M.resolve_websites(["A", "B", "C"], "台北")
    assert pauses == [M._RESOLVE_PAUSE_MS, M._RESOLVE_PAUSE_MS]  # not after the last


def test_resolve_websites_closes_the_browser_it_opened(monkeypatch):
    """One browser for the whole batch, and it must be closed even on failure."""
    browser = _stub_playwright(monkeypatch)
    monkeypatch.setattr(M, "_resolve_one",
                        lambda *_a: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        M.resolve_websites(["A"], "台北")
    assert browser.closed is True


def test_resolve_websites_closes_the_browser_when_the_page_cannot_open(monkeypatch):
    """Context/page creation must be inside the try, or the process leaks."""
    browser = _stub_playwright(monkeypatch, context_error=RuntimeError("no context"))
    with pytest.raises(RuntimeError):
        M.resolve_websites(["A"], "台北")
    assert browser.closed is True


def test_resolve_websites_returns_empty_without_playwright(monkeypatch):
    """The playwright import is lazy; no browser package must not raise."""
    import sys

    # A None entry in sys.modules makes `import x` raise ImportError — which is
    # what a machine without the browser package would do.
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    logs = []
    assert M.resolve_websites(["A"], "台北", log=logs.append) == {}
    assert any("playwright unavailable" in m for m in logs)


def test_resolve_websites_short_circuits_on_no_names():
    assert M.resolve_websites([], "台北") == {}
    assert M.resolve_websites(["", "   "], "台北") == {}


def _stub_playwright(monkeypatch, context_error=None):
    """Install a browser-free `sync_playwright()` and return the fake browser.

    Patches `playwright.sync_api.sync_playwright` — the module attribute — not
    the tool's namespace: `resolve_websites` imports it *inside* the function,
    so a name bound on the tool module is never read. Getting this wrong makes
    the test launch a real Chromium, which passes on a dev box with browsers
    installed and fails in CI, which has none.

    The returned browser records `launches` and `closed` so tests can pin the
    lifecycle. `context_error` makes `new_context()` raise, to check that a
    failure before the loop still closes the browser.
    """
    class _Page:
        def wait_for_timeout(self, _ms): pass

    class _Ctx:
        def new_page(self): return _Page()

    class _Browser:
        def __init__(self):
            self.closed = False
            self.launches = 0
        def new_context(self, **_kw):
            if context_error:
                raise context_error
            return _Ctx()
        def close(self): self.closed = True

    browser = _Browser()

    class _Chromium:
        def launch(self, **_kw):
            browser.launches += 1
            return browser

    class _PW:
        chromium = _Chromium()
        def __enter__(self): return self
        def __exit__(self, *_a): return False

    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: _PW())
    return browser
