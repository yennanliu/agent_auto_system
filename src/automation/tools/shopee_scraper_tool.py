"""
Scrape sellers behind the top products for a Shopee (shopee.tw) search keyword.

Shopee requires a logged-in session and is heavily bot-protected, so this tool
reuses a PERSISTED browser session instead of logging in each run:

  1. Run the one-time helper once to log in manually (handles captcha / OTP):
         uv run python scripts/shopee_login.py
     It saves the storage state (cookies + localStorage) to the path in
     SHOPEE_STORAGE_STATE (default: data/shopee_state.json).
  2. This tool loads that state into a headless Chromium context, so every
     request is authenticated.

Strategy: prefer Shopee's internal JSON API (search_items + get_shop_detail)
issued through the authenticated browser context — it carries the session
cookies and is far more reliable than DOM scraping. Falls back to DOM scraping
of the search/product pages if the API is blocked.
"""
import os
import re
import urllib.parse
from datetime import UTC, datetime

from crewai.tools import BaseTool
from pydantic import BaseModel

DEFAULT_STATE_PATH = "data/shopee_state.json"
_BASE = "https://shopee.tw"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# Matches Shopee product URLs ending in "-i.{shopid}.{itemid}".
_IID_RE = re.compile(r"-i\.(\d+)\.(\d+)")

# Shopee serves an anti-bot captcha wall under /verify/captcha, and its JSON
# APIs return HTTP 403 with error code 90309999 once a session looks scripted.
_CAPTCHA_URL_MARK = "/verify/captcha"
_ANTIBOT_HINT = (
    "Shopee served an anti-bot captcha and blocked the search. Re-seed a fresh "
    "session by running `uv run python scripts/shopee_login.py` (clear the "
    "captcha in the browser window it opens), then retry. Spacing runs further "
    "apart, or running with SHOPEE_HEADED=1 to clear a captcha manually, also helps."
)


def _headless() -> bool:
    """Headless unless SHOPEE_HEADED is set — a headed run lets a human clear a
    captcha manually when the anti-bot wall trips."""
    return os.getenv("SHOPEE_HEADED", "").strip().lower() not in ("1", "true", "yes")


def _is_captcha(page) -> bool:
    """True if the page got redirected to Shopee's captcha verification wall."""
    try:
        return _CAPTCHA_URL_MARK in (page.url or "")
    except Exception:  # noqa: BLE001
        return False


def _is_blocked(errors: list[str]) -> bool:
    """True if the recorded errors indicate an anti-bot block rather than an
    ordinary empty/sparse result — drives the actionable hint we surface."""
    return any(("captcha" in e or "anti-bot" in e or "HTTP 403" in e) for e in errors)


class ShopeeScrapeInput(BaseModel):
    keyword: str
    limit: int = 5


class ShopeeSellerScraperTool(BaseTool):
    name: str = "shopee_seller_scraper"
    description: str = (
        "Search shopee.tw for a keyword, open the top N products, and collect "
        "the seller (shop) behind each one: shop name, shop URL, location, "
        "join date, rating, rating count, follower count, item count, and "
        "response rate. Args: keyword (str), limit (int, number of products)."
    )
    args_schema: type[BaseModel] = ShopeeScrapeInput

    def _run(self, keyword: str, limit: int = 5) -> dict:
        limit = max(1, min(int(limit), 100))
        state_path = os.getenv("SHOPEE_STORAGE_STATE", DEFAULT_STATE_PATH)
        username = os.getenv("SHOPEE_USERNAME", "")
        password = os.getenv("SHOPEE_PASSWORD", "")

        # No saved session yet — try an automated login with the .env credentials.
        # Shopee usually blocks headless logins with captcha/OTP, so this is a
        # best-effort fallback; the reliable path is `scripts/shopee_login.py`.
        if not os.path.exists(state_path):
            if username and password:
                try:
                    _login_with_credentials(state_path, username, password)
                except Exception as exc:  # noqa: BLE001
                    return {
                        "keyword": keyword, "sellers": [],
                        "error": (
                            f"Auto-login failed ({type(exc).__name__}: {exc}). Shopee likely "
                            "required a captcha/OTP. Run `uv run python scripts/shopee_login.py` "
                            "once to log in manually and save the session."
                        ),
                    }
            if not os.path.exists(state_path):
                return {
                    "keyword": keyword, "sellers": [],
                    "error": (
                        f"No Shopee session at '{state_path}' and no usable SHOPEE_USERNAME/"
                        "SHOPEE_PASSWORD in .env. Log in once with "
                        "`uv run python scripts/shopee_login.py`."
                    ),
                }

        try:
            return _scrape(keyword, limit, state_path)
        except Exception as exc:  # noqa: BLE001 — surface a clean error to the agent
            return {
                "keyword": keyword,
                "sellers": [],
                "error": f"{type(exc).__name__}: {exc}",
            }


# ── credential auto-login (best-effort fallback) ─────────────────────────────────

def _login_with_credentials(state_path: str, username: str, password: str) -> None:
    """Attempt a headless login with .env credentials and persist the session.

    Best-effort only: Shopee commonly interrupts headless logins with a captcha
    or SMS/email OTP, in which case no session is saved and the caller falls back
    to the manual `scripts/shopee_login.py` helper. Raises on hard failures.
    """
    from playwright.sync_api import sync_playwright

    os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(user_agent=_UA, locale="zh-TW",
                                  viewport={"width": 1366, "height": 900})
        page = ctx.new_page()
        try:
            page.goto(f"{_BASE}/buyer/login", wait_until="domcontentloaded", timeout=30_000)
            page.locator('input[name="loginKey"]').first.fill(username, timeout=10_000)
            pw_field = page.locator('input[name="password"]').first
            pw_field.fill(password, timeout=10_000)
            # Submit with Enter: a login-page modal/overlay (e.g. app-download or
            # consent popup) often sits on top of the submit button and intercepts
            # the click, so pressing Enter in the field is more reliable.
            pw_field.press("Enter")
            page.wait_for_timeout(5000)
            # Fallback: if still on the login page, force-click the button past any overlay.
            if "/buyer/login" in page.url:
                try:
                    page.locator('button:has-text("登入"), button:has-text("Log In")') \
                        .first.click(timeout=8_000, force=True)
                    page.wait_for_timeout(5000)
                except Exception:  # noqa: BLE001 — overlay/captcha; handled by caller
                    pass

            # Only persist if we actually left the login page (no captcha/OTP wall).
            if "/buyer/login" not in page.url:
                ctx.storage_state(path=state_path)
        finally:
            browser.close()


# ── orchestration ───────────────────────────────────────────────────────────────

def _scrape(keyword: str, limit: int, state_path: str) -> dict:
    from playwright.sync_api import sync_playwright

    errors: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=_headless(),
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            storage_state=state_path,
            user_agent=_UA,
            viewport={"width": 1366, "height": 900},
            locale="zh-TW",
        )
        page = ctx.new_page()
        try:
            # Warm up the session so cookies / anti-bot tokens are in place.
            page.goto(_BASE, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(1500)

            products = _search_api(page, keyword, limit, errors) \
                or _search_dom(page, keyword, limit, errors)

            if not products:
                return {
                    "keyword": keyword,
                    "source": "shopee.tw",
                    "requested": limit,
                    "seller_count": 0,
                    "sellers": [],
                    "warnings": errors[:6],
                    "error": (_ANTIBOT_HINT if _is_blocked(errors)
                              else "No products found — "
                                   + (" | ".join(errors[:4]) or "empty result")),
                }

            # One seller per unique shop, preserving search order, up to limit.
            sellers: list[dict] = []
            seen: set[int] = set()
            for prod in products:
                shopid = prod["shopid"]
                if shopid in seen:
                    continue
                seen.add(shopid)
                seller = _shop_detail_api(page, shopid, errors) or _blank_seller(shopid)
                seller["product_title"] = prod.get("name", "")
                seller["product_url"] = prod.get("url", "")
                sellers.append(seller)
                if len(sellers) >= limit:
                    break

            result = {
                "keyword": keyword,
                "source": "shopee.tw",
                "requested": limit,
                "seller_count": len(sellers),
                "sellers": sellers,
            }
            if errors:
                # Always surface the errors, even on a partial success, so a run
                # that lost most of its quota to throttling isn't reported as a
                # clean win.
                result["warnings"] = errors[:6]
            if not sellers:
                result["error"] = (_ANTIBOT_HINT if _is_blocked(errors)
                                   else (" | ".join(errors[:4]) or "no sellers found"))
            elif len(sellers) < limit:
                note = (f"Collected {len(sellers)} of {limit} requested sellers — "
                        "Shopee throttled or blocked further result pages.")
                if _is_blocked(errors):
                    note += " " + _ANTIBOT_HINT
                result["note"] = note
            return result
        finally:
            browser.close()


# ── Shopee internal API (preferred) ───────────────────────────────────────────────

def _api_get(page, path: str, errors: list[str]) -> dict | None:
    """GET a Shopee API path through the authenticated context; return parsed JSON."""
    try:
        resp = page.request.get(
            f"{_BASE}{path}",
            headers={
                "Referer": _BASE + "/",
                "X-Requested-With": "XMLHttpRequest",
                "X-API-SOURCE": "pc",
                "Accept": "application/json",
            },
            timeout=20_000,
        )
        if resp.status != 200:
            tag = path.split('?')[0]
            # 403 here is Shopee's anti-bot wall (error 90309999), not a normal
            # miss — flag it so the caller can emit the captcha hint.
            errors.append(f"api {tag}: anti-bot block (HTTP 403)"
                          if resp.status == 403 else f"api {tag}: HTTP {resp.status}")
            return None
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"api {path.split('?')[0]}: {type(exc).__name__}")
        return None


_SEARCH_PAGE_SIZE = 60  # Shopee search_items caps page size at 60 items.


def _max_search_pages(limit: int) -> int:
    """Page budget for the search pagination loop.

    `limit` counts UNIQUE shops, but many top products share a shop, so one page
    of 60 products yields fewer than 60 unique shops. Allow enough pages to gather
    `limit` unique shops even with heavy repetition, bounded so a sparse/blocked
    search can't loop forever.
    """
    return max(3, limit // 20 + 2)


def _search_api(page, keyword: str, limit: int, errors: list[str]) -> list[dict]:
    """Fetch search results, paginating until we have at least `limit` UNIQUE
    shops (since sellers repeat across products) or results run out.

    The previous version requested exactly `limit` products and let the caller
    dedupe by shop, so a keyword whose top results came from one shop collapsed
    to a single seller. Fetching a larger pool and stopping on unique-shop count
    keeps a request for N sellers actually returning N sellers.
    """
    kw = urllib.parse.quote(keyword)
    products: list[dict] = []
    unique_shops: set[int] = set()
    offset = 0
    for page_idx in range(_max_search_pages(limit)):
        path = (
            f"/api/v4/search/search_items?by=relevancy&keyword={kw}"
            f"&limit={_SEARCH_PAGE_SIZE}&newest={offset}&order=desc&page_type=search"
            f"&scenario=PAGE_GLOBAL_SEARCH&version=2"
        )
        data = _api_get(page, path, errors)
        if not isinstance(data, dict):
            break
        items = data.get("items")
        if not isinstance(items, list) or not items:
            break
        for entry in items:
            basic = entry.get("item_basic") or entry.get("basic") or entry
            shopid, itemid = basic.get("shopid"), basic.get("itemid")
            if not shopid or not itemid:
                continue
            products.append({
                "shopid": int(shopid),
                "itemid": int(itemid),
                "name": basic.get("name", ""),
                "url": f"{_BASE}/product/{shopid}/{itemid}",
            })
            unique_shops.add(int(shopid))
        if len(unique_shops) >= limit:
            break
        if len(items) < _SEARCH_PAGE_SIZE:
            break  # last page — no more results to fetch
        offset += _SEARCH_PAGE_SIZE
        if page_idx + 1 < _max_search_pages(limit):
            page.wait_for_timeout(800)  # be gentle with the anti-bot layer
    if not products:
        errors.append("api search: no items in payload")
    return products


def _shop_detail_api(page, shopid: int, errors: list[str]) -> dict | None:
    data = _api_get(page, f"/api/v4/shop/get_shop_detail?shopid={shopid}", errors)
    if not isinstance(data, dict):
        return None
    d = data.get("data")
    if not isinstance(d, dict):
        return None
    account = d.get("account")
    username = account.get("username", "") if isinstance(account, dict) else ""
    return {
        "shop_name": d.get("name", "") or username,
        "shop_url": f"{_BASE}/{username}" if username else f"{_BASE}/shop/{shopid}",
        "location": d.get("shop_location", ""),
        "joined": _epoch_to_date(d.get("ctime")),
        "rating_star": round(float(d.get("rating_star") or 0), 2),
        "rating_count": _rating_total(d),
        "follower_count": d.get("follower_count", 0),
        "item_count": d.get("item_count", 0),
        "response_rate": d.get("response_rate", 0),
    }


def _blank_seller(shopid: int) -> dict:
    """Full-schema placeholder used when shop detail can't be fetched, so every
    seller object has a consistent set of keys for the agent and downstream code."""
    return {
        "shop_name": "",
        "shop_url": f"{_BASE}/shop/{shopid}",
        "location": "",
        "joined": "",
        "rating_star": 0.0,
        "rating_count": 0,
        "follower_count": 0,
        "item_count": 0,
        "response_rate": 0,
    }


def _rating_total(d: dict) -> int:
    return sum(int(d.get(k) or 0) for k in ("rating_bad", "rating_normal", "rating_good"))


def _epoch_to_date(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=UTC).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return ""


# ── DOM fallback ────────────────────────────────────────────────────────────────

def _search_dom(page, keyword: str, limit: int, errors: list[str]) -> list[dict]:
    """Scrape product links off the rendered search pages when the API is blocked.

    Shopee search is PAGE-paginated (``?page=N``), not infinite-scroll, so one
    page yields at most ~40 cards no matter how far we scroll — we must walk the
    page numbers to gather `limit` unique shops. Within a page the grid lazy-loads
    per scroll, so we step down in small increments and re-harvest rather than
    jumping to the bottom once (a single big jump loads only the first ~20 cards).

    The anti-bot layer intermittently throttles page loads; a blocked page is
    skipped (with one backoff retry inside `_open_search_page`) instead of ending
    the whole scrape, and we stop only after several consecutive failures.
    """
    kw = urllib.parse.quote(keyword)
    products: list[dict] = []
    seen: set[tuple[int, int]] = set()
    unique_shops: set[int] = set()

    # Allow a few extra page numbers beyond the nominal budget so throttled early
    # pages don't consume the whole allowance before we reach loadable ones.
    max_page = _max_search_pages(limit) + 4
    consecutive_fail = 0
    pg = 0
    while pg <= max_page and len(unique_shops) < limit:
        if not _open_search_page(page, kw, pg, errors):
            consecutive_fail += 1
            if consecutive_fail >= 3:
                break  # anti-bot is consistently blocking — stop hammering it
            pg += 1
            continue
        consecutive_fail = 0
        # Gradual scroll: re-harvest after each small wheel step until a pass adds
        # no new cards (the grid is fully loaded or stopped lazy-loading).
        prev = -1
        for _ in range(12):
            count = _harvest_dom_links(page, products, seen, unique_shops)
            if count == prev:
                break
            prev = count
            page.mouse.wheel(0, 3_000)
            page.wait_for_timeout(700)
        pg += 1
        page.wait_for_timeout(1_200)  # pace page loads to dodge the throttle
    if not products:
        errors.append("dom search: no product links matched")
    return products


def _open_search_page(page, kw: str, pg: int, errors: list[str]) -> bool:
    """Navigate to search result page `pg` and wait for product cards.

    Retries once after a backoff because the anti-bot layer often throttles the
    first hit on a page. Returns False (recording the error) if it stays blocked.
    """
    url = f"{_BASE}/search?keyword={kw}&page={pg}"
    for attempt in range(2):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            # A captcha redirect will never render the grid — bail immediately
            # with a clear marker instead of burning the 8s selector timeout and
            # a retry on a page that can't load.
            if _is_captcha(page):
                errors.append(f"dom search p{pg}: captcha wall")
                return False
            # A loadable grid renders within ~1-2s of domcontentloaded; a blocked
            # page never renders, so a short wait trims the doomed case while the
            # one-shot retry below still covers a slow grid under throttling.
            page.wait_for_selector('a[href*="-i."]', timeout=8_000)
            return True
        except Exception as exc:  # noqa: BLE001
            if _is_captcha(page):
                errors.append(f"dom search p{pg}: captcha wall")
                return False
            if attempt == 0:
                page.wait_for_timeout(2_500)  # back off past the throttle, retry
                continue
            errors.append(f"dom search p{pg}: {type(exc).__name__}")
    return False


def _harvest_dom_links(page, products: list[dict], seen: set[tuple[int, int]],
                       unique_shops: set[int]) -> int:
    """Scan the current DOM for product links, appending newly seen ones.

    Returns the running total product count so the caller can detect when a
    scroll pass stopped adding cards.

    Pulls every link's data in a single `page.evaluate` round-trip rather than
    per-element locator reads: far fewer Playwright↔browser hops and immune to
    elements detaching mid-scroll (the DOM mutates while the grid lazy-loads).
    """
    try:
        links = page.evaluate(
            """() => Array.from(document.querySelectorAll('a[href*="-i."]')).map(a => ({
                href: a.getAttribute('href') || '',
                aria: a.getAttribute('aria-label') || '',
                text: a.innerText || '',
            }))"""
        )
    except Exception:  # noqa: BLE001 — navigation/teardown mid-eval; harvest later
        return len(products)

    for link in links:
        m = _IID_RE.search(link["href"])
        if not m:
            continue
        shopid, itemid = int(m.group(1)), int(m.group(2))
        if (shopid, itemid) in seen:
            continue
        seen.add((shopid, itemid))
        name = (link["aria"] or link["text"] or "").strip()[:200]
        products.append({
            "shopid": shopid,
            "itemid": itemid,
            "name": name,
            "url": urllib.parse.urljoin(_BASE, link["href"]),
        })
        unique_shops.add(shopid)
    return len(products)
