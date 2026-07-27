"""
Extract business contact info (email, website, bio) from a public Instagram
profile.

A collection source for the email funnel: many SMEs — especially shops,
studios, and restaurants — use Instagram *as* their storefront and put a
contact email in the bio (often lightly obfuscated) plus a real website in the
profile's external-link field. Recovering that turns a lead the Maps funnel
drops (`instagram.com` is a `_NO_GUESS_DOMAINS` host) into a contactable one —
and the external link is a chase-through back into the normal website extractor.

Strategy (mirrors the X source): try a cheap static fetch first — the logged-out
profile HTML still embeds the bio and `external_url` in inline JSON — then fall
back to a headless-browser render for the cases Instagram serves behind its
login wall. On total failure we return partial results + a `warnings` list
rather than raising, matching the other scraper tools in this package.
"""
import http.cookiejar
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from crewai.tools import BaseTool
from pydantic import BaseModel

from src.automation.tools.contact_harvest import harvest_emails_from_text, social_platform

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"}
# Markers of the logged-out login shell Instagram serves instead of a profile.
_LOGIN_MARKERS = ("login • instagram", "log into instagram", "loginform")


class InstagramContactInput(BaseModel):
    profile: str


class InstagramContactTool(BaseTool):
    name: str = "instagram_contact"
    description: str = (
        "Fetch a public Instagram profile and extract business contact info "
        "from its bio/header: email addresses (including obfuscated ones), the "
        "linked website, and bio text. Args: profile (str — a handle, with or "
        "without a leading @, or a full instagram.com URL)."
    )
    args_schema: type[BaseModel] = InstagramContactInput

    def _run(self, profile: str) -> dict:
        return fetch_instagram_contact(profile)


def fetch_instagram_contact(profile: str, log=None) -> dict:
    """Return contact info harvested from an Instagram profile.

    Shape: {"username", "source", "emails": [...], "website", "bio",
    "warnings": [...]}. Never raises — a failure yields empty fields and a
    populated `warnings` list.
    """
    _log = log or (lambda _m: None)
    handle = _extract_handle(profile)
    if not handle:
        return _empty("", ["empty username"])

    warnings: list[str] = []

    # Static-first: the logged-out profile HTML often still embeds the bio and
    # external_url in inline JSON — no browser needed.
    try:
        html = _fetch(f"https://www.instagram.com/{urllib.parse.quote(handle)}/")
    except urllib.error.HTTPError as exc:
        warnings.append(f"instagram: HTTP {exc.code}")
        html = ""
    except Exception as exc:  # noqa: BLE001 — dead network → try the browser
        warnings.append(f"instagram: {type(exc).__name__}")
        html = ""

    data = _parse_profile_html(html) if html else None
    if data and (data["emails"] or data["bio"] or data["website"]):
        _log(f"Instagram @{handle}: {len(data['emails'])} email(s), "
             f"website={data['website'] or '—'}")
        return {"username": handle, "source": "instagram.com", **data,
                "warnings": warnings[:6]}
    if html and _looks_login_walled(html):
        warnings.append("instagram: login wall on static fetch")

    # Fallback: render the profile so inline JSON / the header DOM populates.
    _log(f"Instagram @{handle}: static fetch thin, trying render via Playwright")
    try:
        prof = _scrape_profile_with_playwright(handle)
    except Exception as exc:  # noqa: BLE001 — browser missing / timeout / wall
        warnings.append(f"instagram playwright: {type(exc).__name__}")
        prof = None
    if prof and (prof["emails"] or prof["bio"] or prof["website"]):
        _log(f"Instagram @{handle}: {len(prof['emails'])} email(s) via render, "
             f"website={prof['website'] or '—'}")
        return {"username": handle, "source": "instagram.com (playwright)", **prof,
                "warnings": warnings[:6]}
    if prof is not None:
        warnings.append("instagram playwright: no bio/email on profile")

    _log(f"Instagram @{handle}: no source returned a profile")
    return _empty(handle, warnings[:6] or ["all sources failed"])


# ── handle / URL parsing ──────────────────────────────────────────────────────

def _extract_handle(profile: str) -> str:
    """Normalize a bare handle or a full instagram.com URL down to the handle.

    The funnel passes the business's Maps "website", usually a full profile URL
    (`https://www.instagram.com/acmebiz/`). Instagram handles may contain dots
    and underscores, so — unlike X — a bare dotted handle (`acme.studio`) is not
    treated as a URL.
    """
    s = (profile or "").strip()
    if not s:
        return ""
    if (s.startswith(("http://", "https://"))
            or "instagram.com" in s or "instagr.am" in s or "/" in s):
        p = urllib.parse.urlparse(
            s if s.startswith(("http://", "https://")) else "https://" + s)
        seg = p.path.strip("/").split("/")[0]
        return urllib.parse.unquote(seg).lstrip("@")
    return s.lstrip("@")


def _fetch(url: str) -> str:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    req = urllib.request.Request(url, headers=_HEADERS)
    resp = opener.open(req, timeout=15)
    return resp.read().decode("utf-8", errors="replace")


# ── profile HTML parsing (shared by static + rendered paths) ───────────────────

def _parse_profile_html(html: str) -> dict:
    """Extract bio / emails / website from a profile's inline JSON + meta tags.

    Instagram embeds the profile as inline JSON (`"biography":"…"`,
    `"external_url":"…"`, and — for business accounts — `"business_email"` /
    `"public_email"`). We harvest emails from the bio plus any explicit email
    field, and take the external link as the chase-through website.
    """
    bio = _json_field(html, "biography")
    if not bio:
        bio = _meta_description(html)
    explicit = [_json_field(html, k) for k in ("business_email", "public_email")]
    emails = harvest_emails_from_text(" ".join(x for x in [bio, *explicit] if x))

    website = _unwrap_ig_link(_json_field(html, "external_url"))
    if website and social_platform(website):
        website = ""  # a social link is not a chase-through target
    return {"emails": emails, "website": website, "bio": bio}


def _json_field(html: str, key: str) -> str:
    """Read a JSON string value for `key` from inline HTML, decoding escapes."""
    m = re.search(rf'"{re.escape(key)}":\s*"((?:[^"\\]|\\.)*)"', html or "")
    if not m:
        return ""
    raw = m.group(1)
    try:
        return json.loads(f'"{raw}"')
    except (ValueError, json.JSONDecodeError):
        return raw


def _meta_description(html: str) -> str:
    """Fallback bio source: the og:description / description meta tag content."""
    for tag in re.findall(r"<meta\b[^>]*>", html or "", re.IGNORECASE):
        low = tag.lower()
        if 'property="og:description"' in low or 'name="description"' in low:
            m = re.search(r'content=["\']([^"\']*)["\']', tag, re.IGNORECASE)
            if m:
                return m.group(1)
    return ""


def _unwrap_ig_link(url: str) -> str:
    """Decode Instagram's `l.instagram.com/?u=…` link wrapper to the real URL."""
    url = (url or "").strip()
    if not url:
        return ""
    p = urllib.parse.urlparse(url if url.startswith(("http://", "https://"))
                              else "https://" + url)
    if "l.instagram.com" in p.netloc:
        u = urllib.parse.parse_qs(p.query).get("u", [""])[0]
        return urllib.parse.unquote(u) if u else ""
    return url


def _looks_login_walled(html: str) -> bool:
    low = (html or "")[:4000].lower()
    return any(m in low for m in _LOGIN_MARKERS)


# ── Playwright fallback ────────────────────────────────────────────────────────

def _scrape_profile_with_playwright(handle: str) -> dict:
    """Render the logged-out profile with headless Chromium, then re-parse."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=_UA, locale="en-US",
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.new_page()
        page.route("**/*.{png,jpg,jpeg,gif,mp4,webm,svg,woff,woff2}",
                   lambda r: r.abort())
        try:
            page.goto(f"https://www.instagram.com/{urllib.parse.quote(handle)}/",
                      timeout=30_000, wait_until="domcontentloaded")
            page.wait_for_timeout(3_000)
            html = page.content()
            hrefs = page.eval_on_selector_all(
                "a[href]", "els => els.map(e => e.getAttribute('href'))")
        finally:
            browser.close()

    data = _parse_profile_html(html)
    # The rendered header links the external site via an `l.instagram.com`
    # wrapper even when the inline JSON is absent.
    if not data["website"]:
        data["website"] = _website_from_hrefs(hrefs)
    return data


def _website_from_hrefs(hrefs) -> str:
    for h in hrefs or []:
        if not h or "l.instagram.com" not in h:
            continue
        u = urllib.parse.parse_qs(urllib.parse.urlparse(h).query).get("u", [""])[0]
        if u:
            url = urllib.parse.unquote(u)
            if not social_platform(url):
                return url
    return ""


def _empty(handle: str, warnings: list[str]) -> dict:
    return {"username": handle, "source": "", "emails": [], "website": "",
            "bio": "", "warnings": warnings}
