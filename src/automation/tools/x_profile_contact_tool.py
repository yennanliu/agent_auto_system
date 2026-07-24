"""
Extract business contact info (email, website, location) from a public X
(Twitter) profile's bio/header.

This is a *collection source* for the email funnel — distinct from
`x_scraper_tool.py`, which pulls a profile's recent posts. SMEs that use X as
their storefront routinely put a contact email in the bio (often lightly
obfuscated) and a real website in the profile-website field. Recovering that
turns a lead the Maps funnel drops (`x.com` is a `_NO_GUESS_DOMAINS` host) into
a contactable one — and the profile-website is a chase-through back into the
normal website extractor.

Strategy: read the profile via nitter (plain HTTP, no login wall, and its markup
exposes the whole header block in one static page). We reuse the shared nitter
instance list and headers from `x_scraper_tool`. On total failure we return
partial results + a `warnings` list rather than raising, matching the other
scraper tools in this package.
"""
import html as _html_mod
import http.cookiejar
import re
import urllib.error
import urllib.parse
import urllib.request

from crewai.tools import BaseTool
from pydantic import BaseModel

from src.automation.tools.contact_harvest import harvest_emails_from_text
from src.automation.tools.x_scraper_tool import (
    _HEADERS,
    _clean,
    _get_nitter_instances,
)


class XProfileContactInput(BaseModel):
    username: str


class XProfileContactTool(BaseTool):
    name: str = "x_profile_contact"
    description: str = (
        "Fetch a public X (Twitter) profile and extract business contact info "
        "from its bio/header: email addresses (including obfuscated ones), the "
        "linked website, and location. Args: username (str handle, with or "
        "without a leading @)."
    )
    args_schema: type[BaseModel] = XProfileContactInput

    def _run(self, username: str) -> dict:
        return fetch_x_profile_contact(username)


def fetch_x_profile_contact(username: str, log=None) -> dict:
    """Return contact info harvested from @username's X profile header.

    Shape: {"username", "source", "emails": [...], "website", "location",
    "bio", "warnings": [...]}. Never raises — a failure yields empty fields and
    a populated `warnings` list.
    """
    _log = log or (lambda _m: None)
    handle = _extract_handle(username)
    if not handle:
        return _empty("", ["empty username"])

    warnings: list[str] = []
    for base in _get_nitter_instances():
        try:
            html = _fetch(f"{base.rstrip('/')}/{urllib.parse.quote(handle)}")
        except urllib.error.HTTPError as exc:
            warnings.append(f"{base}: HTTP {exc.code}")
            continue
        except Exception as exc:  # noqa: BLE001 — dead instance → try the next
            warnings.append(f"{base}: {type(exc).__name__}")
            continue

        head = html[:500].lower()
        if "bot" in head or "captcha" in head:
            warnings.append(f"{base}: bot-detection page")
            continue

        card = _profile_card(html)
        if card is None:
            warnings.append(f"{base}: no profile card (suspended / not found?)")
            continue

        bio = _profile_bio(card)
        website = _profile_website(card)
        location = _profile_field(card, "profile-location")
        # Emails hide in the bio; the website field occasionally is a mailto too.
        emails = harvest_emails_from_text(f"{bio} {website}")
        _log(f"X profile @{handle}: {len(emails)} email(s), "
             f"website={website or '—'}")
        return {
            "username": handle,
            "source": base,
            "emails": emails,
            "website": website,
            "location": location,
            "bio": bio,
            "warnings": warnings[:6],
        }

    # Nitter is largely dead/blocked in practice — fall back to reading the
    # logged-out x.com profile directly with Playwright. x.com serves a static
    # "lite" header (bio + destination links) before the login wall.
    _log(f"X profile @{handle}: nitter unavailable, trying x.com via Playwright")
    try:
        prof = _scrape_profile_with_playwright(handle)
    except Exception as exc:  # noqa: BLE001 — browser missing / timeout / wall
        warnings.append(f"x.com playwright: {type(exc).__name__}")
        prof = None
    if prof and (prof["emails"] or prof["bio"]):
        _log(f"X profile @{handle}: {len(prof['emails'])} email(s) via x.com, "
             f"website={prof['website'] or '—'}")
        return {"username": handle, "source": "x.com (playwright)", **prof,
                "warnings": warnings[:6]}
    if prof is not None:
        warnings.append("x.com playwright: no bio/email on profile")

    _log(f"X profile @{handle}: no source returned a profile")
    return _empty(handle, warnings[:6] or ["all sources failed"])


# ── nitter profile-header parsing ────────────────────────────────────────────

def _extract_handle(username: str) -> str:
    """Normalize a bare handle or a full X URL down to the handle.

    The funnel passes the business's Maps "website" here, which is usually a
    full profile URL (`https://x.com/acmebiz`), not a bare handle.
    """
    s = (username or "").strip()
    if not s:
        return ""
    # Anything URL-shaped (scheme, slash, or a dot — handles never contain dots).
    if s.startswith(("http://", "https://")) or "/" in s or "." in s:
        p = urllib.parse.urlparse(s if s.startswith(("http://", "https://"))
                                  else "https://" + s)
        first = p.path.strip("/").split("/")[0]
        return first.lstrip("@")
    return s.lstrip("@")


def _fetch(url: str) -> str:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    req = urllib.request.Request(url, headers=_HEADERS)
    resp = opener.open(req, timeout=15)
    return resp.read().decode("utf-8", errors="replace")


def _profile_card(html: str) -> str | None:
    """Return the raw HTML of the profile-card block, or None if absent.

    Sliced from the opening `class="profile-card"` to the timeline that follows
    it — bounded so a huge post feed never bloats the downstream regexes.
    """
    m = re.search(r'class="profile-card\b', html)
    if not m:
        return None
    start = m.start()
    end = html.find('class="timeline', start)
    return html[start : end if end != -1 else start + 8000]


def _profile_bio(card: str) -> str:
    m = re.search(r'class="profile-bio"[^>]*>(.*?)</div>', card, re.DOTALL)
    return _clean(m.group(1)) if m else ""


def _profile_field(card: str, cls: str) -> str:
    m = re.search(rf'class="{re.escape(cls)}"[^>]*>(.*?)</div>', card, re.DOTALL)
    return _clean(m.group(1)) if m else ""


def _profile_website(card: str) -> str:
    """Prefer the human-readable link text nitter renders (the real URL)."""
    block = re.search(r'class="profile-website"[^>]*>(.*?)</div>', card, re.DOTALL)
    if not block:
        return ""
    inner = block.group(1)
    text = _clean(inner)
    if text:
        # nitter shows the destination as the anchor text (e.g. "acme.com.tw").
        return text
    href = re.search(r'href=["\']([^"\']+)["\']', inner)
    if href:
        return _html_mod.unescape(href.group(1))
    return ""


# ── x.com Playwright fallback ────────────────────────────────────────────────
#
# Logged out, x.com serves a static "lite" header whose markup carries no stable
# data-testids, but the bio (with its destination links) is the first
# `dir="auto" … text-body` block and always precedes the follow-stats links and
# the post feed. We bound parsing to that header so a tweet's text/email can't
# leak in.
_CDN_HOSTS = ("x.com", "twitter.com", "t.co", "twimg.com", "google.", "apple.")


def _scrape_profile_with_playwright(handle: str) -> dict:
    """Read the logged-out x.com profile header via headless Chromium."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=_HEADERS["User-Agent"], locale="en-US",
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.new_page()
        page.route("**/*.{png,jpg,jpeg,gif,mp4,webm,svg,woff,woff2}",
                   lambda r: r.abort())
        try:
            page.goto(f"https://x.com/{urllib.parse.quote(handle)}",
                      timeout=30_000, wait_until="domcontentloaded")
            page.wait_for_timeout(3_000)
            html = page.content()
        finally:
            browser.close()

    return _parse_xcom_header(html, handle)


def _parse_xcom_header(html: str, handle: str) -> dict:
    """Extract bio / emails / website from the x.com lite-HTML profile header."""
    # Bound to the header: everything before the follow-count links (or the
    # post feed). Keeps the bio-scoped parse clear of tweet content.
    bounds = [html.find(f"/{handle}/{seg}")
              for seg in ("following", "verified_followers", "followers")]
    bounds = [b for b in bounds if b != -1]
    header = html[: min(bounds)] if bounds else html

    # The bio is the first `dir="auto" … text-body` block in the header.
    bios = re.findall(r'<div dir="auto"[^>]*\btext-body\b[^>]*>(.*?)</div>',
                      header, re.DOTALL)
    bio_html = bios[0] if bios else ""
    bio = _clean(bio_html)

    emails = harvest_emails_from_text(bio)
    website = ""
    for raw in re.findall(r'href=["\'](https?://[^"\']+)["\']', header):
        url = _html_mod.unescape(raw)
        host = urllib.parse.urlparse(url).netloc.lower()
        if host and not any(c in host for c in _CDN_HOSTS):
            website = url
            break
    return {"emails": emails, "website": website, "location": "", "bio": bio}


def _empty(handle: str, warnings: list[str]) -> dict:
    return {"username": handle, "source": "", "emails": [], "website": "",
            "location": "", "bio": "", "warnings": warnings}
