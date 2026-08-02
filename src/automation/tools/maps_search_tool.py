"""
Discover SMBs / businesses from Google Maps for a search query + region.

This is stage 1 of the lead-collection funnel (see doc/email_collect):
    DISCOVER  →  extract email  →  verify  →  dedupe

Google Maps has no free structured export, so we drive a headless Chromium
session with Playwright: run the search, scroll the results feed to load N
listings, then open each place panel to read its website, phone, address, and
category. The business's own **website** is what the next stage scrapes for an
email — Maps itself rarely exposes one.

Selectors track Google Maps' current consumer DOM (class names like `hfpxzc` /
`DUwDvf` are Google's own and do change over time); every field read is guarded
so a markup shift degrades a single field rather than failing the whole run.
Partial results + a `warnings` list are returned instead of raising, matching
the other scraper tools in this package.
"""
import re
import urllib.parse

from crewai.tools import BaseTool
from pydantic import BaseModel

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_BASE = "https://www.google.com/maps/search/"
_PHONE_PREFIX = "phone:tel:"


class MapsSearchInput(BaseModel):
    query: str
    region: str = ""
    limit: int = 15


class MapsSearchTool(BaseTool):
    name: str = "maps_search"
    description: str = (
        "Search Google Maps for businesses matching a query in a region and "
        "return each listing's name, website, phone, address, and category. "
        "Args: query (str, e.g. 'AI agency'), region (str, e.g. 'Taipei' or "
        "'Berlin'), limit (int, number of listings). The website field feeds "
        "the email-extraction stage."
    )
    args_schema: type[BaseModel] = MapsSearchInput

    def _run(self, query: str, region: str = "", limit: int = 15) -> dict:
        return search_maps(query, region, limit)


def search_maps(query: str, region: str = "", limit: int = 15, log=None) -> dict:
    """Discover up to `limit` businesses for `query` in `region`.

    Returns {"query", "region", "businesses": [...], "warnings": [...]}.
    Each business: {name, website, phone, address, category, maps_url}.
    """
    limit = max(1, min(int(limit), 500))
    _log = log or (lambda _m: None)
    term = f"{query} {region}".strip()
    warnings: list[str] = []

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001
        return {"query": query, "region": region, "businesses": [],
                "warnings": [f"playwright unavailable: {exc}"]}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--lang=en-US"],
        )
        ctx = browser.new_context(
            user_agent=_UA, locale="en-US",
            viewport={"width": 1366, "height": 900},
        )
        page = ctx.new_page()
        try:
            url = f"{_BASE}{urllib.parse.quote(term)}?hl=en"
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            _dismiss_consent(page)

            place_urls = _collect_place_urls(page, limit, warnings, _log)
            if not place_urls:
                warnings.append("no listings found in results feed")

            businesses: list[dict] = []
            for i, purl in enumerate(place_urls[:limit], 1):
                _log(f"Opening listing {i}/{min(len(place_urls), limit)}...")
                biz = _read_place(page, purl, warnings)
                if biz and biz.get("name"):
                    businesses.append(biz)

            return {"query": query, "region": region,
                    "businesses": businesses, "warnings": warnings[:8]}
        finally:
            browser.close()


def resolve_websites(names, region: str = "", log=None) -> dict:
    """Look up a website for each company name. Returns {name: website or ""}.

    The bridge that makes website-less sources usable by the funnel: 經濟部's
    company registry publishes an authoritative name + address but never a URL,
    and without a URL there is nothing for the email extractor to scrape. Maps
    already knows the mapping, so we ask it — searching the *exact registered
    name* rather than a category, which is a far more precise query than the
    discovery search and usually lands straight on the place panel.

    One browser is reused across every lookup: launching Chromium per name would
    dominate the cost of the whole stage. Each name is independently guarded, so
    one dead lookup never sinks the batch.

    Deliberately conservative — a hit is only returned when the place's own name
    corroborates it (see :func:`_name_matches`). Some real companies are missed
    that way, notably ones Maps lists under an English trade name while the
    registry holds the Chinese legal one, but a missed website costs one lead
    whereas a wrong one puts a stranger's email under a real company's name.
    """
    _log = log or (lambda _m: None)
    names = [n for n in dict.fromkeys(n.strip() for n in names) if n]
    out: dict[str, str] = {}
    if not names:
        return out

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001
        _log(f"Cannot resolve websites (playwright unavailable: {exc})")
        return out

    # Unlike the discovery search, this one runs in Chinese: the names we're
    # resolving are Taiwanese legal names, and an English-locale Maps answers
    # with English trade names ("Trend Micro" for 趨勢科技股份有限公司) that the
    # corroboration check below can never match.
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--lang=zh-TW"],
        )
        ctx = browser.new_context(
            user_agent=_UA, locale="zh-TW",
            viewport={"width": 1366, "height": 900},
        )
        page = ctx.new_page()
        try:
            for i, name in enumerate(names, 1):
                site = _resolve_one(page, name, region)
                out[name] = site
                _log(f"[{i}/{len(names)}] {name} → {site or 'no website found'}")
        finally:
            browser.close()
    return out


def _resolve_one(page, name: str, region: str) -> str:
    """Website for a single business name, or "" if Maps has no confident match.

    Maps never says "not found": search a company it doesn't list and it happily
    returns whatever else is nearby. Taking that website would staple a
    stranger's email onto the company we were asked about — a lead that looks
    verified and is wrong — so the place's *name* has to corroborate the hit
    before its website is accepted.
    """
    term = f"{name} {region}".strip()
    try:
        page.goto(f"{_BASE}{urllib.parse.quote(term)}?hl=zh-TW",
                  wait_until="domcontentloaded", timeout=30_000)
        _dismiss_consent(page)
        # An unambiguous name skips the results feed — Maps opens the place
        # panel directly, so look for the website link before anything else.
        try:
            page.wait_for_selector(
                'a[data-item-id="authority"], a[href*="/maps/place/"]',
                timeout=12_000)
        except Exception:  # noqa: BLE001 — no panel and no feed → give up on this name
            return ""
        site = _attr(page, 'a[data-item-id="authority"]', "href")
        if site:
            found = _text(page, "h1.DUwDvf") or _text(page, "h1")
            return site if _name_matches(name, found) else ""
        # A results feed instead of a panel: the exact company is often not the
        # top hit, so check the first few before concluding Maps doesn't have it.
        hrefs = page.evaluate(
            """(n) => Array.from(document.querySelectorAll('a[href*="/maps/place/"]'))
                          .slice(0, n).map(a => a.href)""",
            _RESOLVE_CANDIDATES,
        )
        for href in hrefs:
            place = _read_place(page, href, []) or {}
            if place.get("website") and _name_matches(name, place.get("name", "")):
                return place["website"]
        return ""
    except Exception:  # noqa: BLE001 — nav crash / timeout → treat as unresolved
        return ""


# How many search results to check before giving up on a name. Each one is a
# page load, so this trades wall-clock for recall; 3 covers the common case
# where the exact company sits just under a sponsored/bigger neighbour.
_RESOLVE_CANDIDATES = 3


# Corporate suffixes carried by a registered name but almost never by the trade
# name Maps shows ("鼎高網路行銷有限公司" is listed as "鼎高網路行銷"). Longest
# first, and matched *after* punctuation is stripped — so "Co., Ltd." arrives
# here as the single token "coltd".
_CORP_SUFFIXES = (
    "股份有限公司", "有限公司", "企業有限公司", "企業社", "工作室", "事務所",
    "分公司", "行號", "商行", "公司",
    "companylimited", "coltd", "limited", "corp", "llc", "ltd", "inc",
)


def _name_matches(query: str, found: str) -> bool:
    """True if the place Maps opened is plausibly the company we searched for.

    Compares the *cores* — names with punctuation, the bracketed Latin alias many
    TW registries append, the 台/臺 spelling split, and the corporate suffix all
    removed — and accepts a containment either way, since Maps lists trade names
    and the registry lists legal ones. Short cores (a 1-character stem) must
    match exactly; containment on those is coincidence, not evidence.
    """
    a, b = _name_core(query), _name_core(found)
    if not a or not b:
        return False
    if a == b:
        return True
    if min(len(a), len(b)) < 2:
        return False
    return a in b or b in a


def _name_core(name: str) -> str:
    """A company name reduced to its distinctive stem, for comparison only."""
    name = re.sub(r"[（(][^）)]*[）)]", "", name or "")     # drop "（Acme Co., Ltd.）"
    name = re.sub(r"[\s\-_·・,，.。&＆'\"]+", "", name).lower()
    name = name.replace("台", "臺")
    # Repeat: one name can stack suffixes ("acme" + "co" + "ltd" → "acmecoltd").
    # Never strip down past two characters — what's left would be noise.
    changed = True
    while changed:
        changed = False
        for suffix in _CORP_SUFFIXES:
            if name.endswith(suffix) and len(name) - len(suffix) >= 2:
                name = name[: -len(suffix)]
                changed = True
                break
    return name


def _dismiss_consent(page) -> None:
    """Best-effort click through Google's EU consent wall if it appears."""
    if "consent." not in page.url and "consent" not in (page.title() or "").lower():
        return
    for label in ("Reject all", "Accept all", "I agree", "拒絕全部", "全部接受"):
        try:
            btn = page.locator(f'button:has-text("{label}")').first
            if btn.count():
                btn.click(timeout=4_000)
                page.wait_for_timeout(1_500)
                return
        except Exception:  # noqa: BLE001 — try the next label / proceed anyway
            continue


def _collect_place_urls(page, limit: int, warnings: list[str], log) -> list[str]:
    """Scroll the results feed until it holds >= limit place links (or stalls)."""
    try:
        page.wait_for_selector('a[href*="/maps/place/"]', timeout=15_000)
    except Exception:  # noqa: BLE001
        warnings.append("results feed did not render")
        return []

    seen: list[str] = []
    seen_set: set[str] = set()
    stale = 0
    for _ in range(20):
        prev = len(seen_set)  # count before this pass, to detect a stalled feed
        hrefs = page.evaluate(
            """() => Array.from(document.querySelectorAll('a[href*="/maps/place/"]'))
                       .map(a => a.href)"""
        )
        for h in hrefs:
            if h not in seen_set:
                seen_set.add(h)
                seen.append(h)
        if len(seen) >= limit:
            break
        # Stop early once the feed stops yielding new listings for a few passes.
        if len(seen_set) == prev:
            stale += 1
            if stale >= 3:
                break
        else:
            stale = 0
        # Scroll the feed container (not the window) to trigger lazy-loading.
        try:
            page.evaluate(
                """() => { const f = document.querySelector('div[role="feed"]');
                           if (f) f.scrollBy(0, f.scrollHeight); }"""
            )
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(1_600)
    log(f"Discovered {len(seen)} listing link(s)")
    return seen


def _read_place(page, purl: str, warnings: list[str]) -> dict | None:
    try:
        page.goto(purl, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_selector("h1", timeout=10_000)
        page.wait_for_timeout(600)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"place load failed: {type(exc).__name__}")
        return None

    return {
        "name":     _text(page, "h1.DUwDvf") or _text(page, "h1"),
        "website":  _attr(page, 'a[data-item-id="authority"]', "href"),
        "phone":    _phone(page),
        "address":  _aria_after(page, 'button[data-item-id="address"]'),
        "category": _text(page, 'button[jsaction*="category"]'),
        "maps_url": purl,
    }


def _text(page, selector: str) -> str:
    try:
        loc = page.locator(selector).first
        if loc.count():
            return (loc.inner_text(timeout=2_000) or "").strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def _attr(page, selector: str, attr: str) -> str:
    try:
        loc = page.locator(selector).first
        if loc.count():
            return (loc.get_attribute(attr, timeout=2_000) or "").strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def _aria_after(page, selector: str) -> str:
    """Read a button's aria-label and drop the leading 'Label: ' prefix."""
    raw = _attr(page, selector, "aria-label")
    return re.sub(r"^[^:]{1,20}:\s*", "", raw).strip() if raw else ""


def _phone(page) -> str:
    item = _attr(page, f'button[data-item-id^="{_PHONE_PREFIX}"]', "data-item-id")
    if item and _PHONE_PREFIX in item:
        return item.split(_PHONE_PREFIX, 1)[1].strip()
    return _aria_after(page, 'button[data-item-id^="phone"]')
