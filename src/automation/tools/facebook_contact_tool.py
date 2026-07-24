"""
Extract business contact info (email, website, phone) from a public Facebook
Page's About / 聯絡資訊 block.

A collection source for the email funnel: many TW SMEs use a Facebook Page *as*
their website, and the "About → Contact and basic info" tab openly lists an
email, phone, and the real website. Recovering that turns a lead the Maps funnel
drops (`facebook.com` is a `_NO_GUESS_DOMAINS` host) into a contactable one — and
the listed website is a chase-through back into the normal website extractor.

Facebook is JS-rendered and login-walled, so — unlike the nitter-first X source —
there is no cheap static path: `mbasic`/`m.facebook.com` return a login shell.
The one anonymously-reachable surface is the desktop
`/{page}/about_contact_and_basic_info` tab under a headless browser, whose
rendered body carries the contact block. Outbound links are wrapped in
`l.facebook.com/l.php?u=…`, so the real website is decoded from that `u=` param.

Partial results + a `warnings` list are returned instead of raising, matching
the other scraper tools in this package.
"""
import re
import urllib.parse

from crewai.tools import BaseTool
from pydantic import BaseModel

from src.automation.tools.contact_harvest import harvest_emails_from_text, social_platform

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# Bodies that mean "nothing to scrape here" rather than a real contact block.
_UNAVAILABLE = (
    "isn't available right now", "content isn't available",
    "page isn't available", "this page isn't available",
)
# A phone: an international +number, or a grouped run of >= 8 digits.
_PHONE_RE = re.compile(r"\+\d[\d\s\-().]{6,}\d|\(?\d{2,4}\)?[\s\-]\d{3,4}[\s\-]\d{3,4}")


class FacebookContactInput(BaseModel):
    page: str


class FacebookContactTool(BaseTool):
    name: str = "facebook_contact"
    description: str = (
        "Fetch a public Facebook Page's About/contact tab and extract business "
        "contact info: email addresses (including obfuscated ones), the linked "
        "website, and phone. Args: page (str — a Page slug/username or a full "
        "facebook.com URL)."
    )
    args_schema: type[BaseModel] = FacebookContactInput

    def _run(self, page: str) -> dict:
        return fetch_facebook_contact(page)


def fetch_facebook_contact(page: str, log=None) -> dict:
    """Return contact info harvested from a Facebook Page's About tab.

    Shape: {"page", "source", "emails": [...], "website", "phone", "category",
    "warnings": [...]}. Never raises — a failure yields empty fields and a
    populated `warnings` list.
    """
    _log = log or (lambda _m: None)
    slug = _extract_page_slug(page)
    if not slug:
        return _empty("", ["empty page"])

    try:
        data = _scrape_about(slug, _log)
    except Exception as exc:  # noqa: BLE001 — browser missing / timeout / wall
        return _empty(slug, [f"facebook playwright: {type(exc).__name__}"])

    _log(f"Facebook /{slug}: {len(data['emails'])} email(s), "
         f"website={data['website'] or '—'}")
    return {"page": slug, "source": "facebook (playwright)", **data}


# ── page/URL → slug ──────────────────────────────────────────────────────────

def _extract_page_slug(page: str) -> str:
    """Normalize a bare Page name or a full facebook.com URL to a slug.

    The funnel passes the business's Maps "website", which is usually a full URL
    (`https://www.facebook.com/acmebiz`). Numeric Pages use `profile.php?id=N`.
    """
    s = (page or "").strip()
    if not s:
        return ""
    if s.startswith(("http://", "https://")) or "facebook.com" in s or "/" in s:
        p = urllib.parse.urlparse(
            s if s.startswith(("http://", "https://")) else "https://" + s)
        if "profile.php" in p.path:
            pid = urllib.parse.parse_qs(p.query).get("id", [""])[0]
            return f"profile.php?id={pid}" if pid else ""
        seg = p.path.strip("/").split("/")[0]
        return urllib.parse.unquote(seg)
    return s.lstrip("@")


# ── scrape ───────────────────────────────────────────────────────────────────

def _about_url(slug: str) -> str:
    tab = "about_contact_and_basic_info"
    if slug.startswith("profile.php"):
        return f"https://www.facebook.com/{slug}&sk={tab}"
    return f"https://www.facebook.com/{urllib.parse.quote(slug)}/{tab}"


def _scrape_about(slug: str, log) -> dict:
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
            page.goto(_about_url(slug), timeout=30_000, wait_until="domcontentloaded")
            page.wait_for_timeout(3_500)
            body = page.locator("body").inner_text(timeout=5_000)
            hrefs = page.eval_on_selector_all(
                "a[href]", "els => els.map(e => e.getAttribute('href'))")
        finally:
            browser.close()

    warnings: list[str] = []
    low = body.lower()
    if any(m in low for m in _UNAVAILABLE):
        warnings.append("page not available anonymously")
        return {"emails": [], "website": "", "phone": "", "category": "",
                "warnings": warnings}

    emails = harvest_emails_from_text(body)
    website = _website_from_links(hrefs) or _website_from_body(body)
    phone = _phone_from_body(body)
    category = _labelled(body, "Categories") or _labelled(body, "Category")
    return {"emails": emails, "website": website, "phone": phone,
            "category": category, "warnings": warnings}


def _website_from_links(hrefs: list[str]) -> str:
    """Decode Facebook's `l.facebook.com/l.php?u=…` wrapper to the real site."""
    for h in hrefs:
        if not h or "l.facebook.com" not in h:
            continue
        u = urllib.parse.parse_qs(urllib.parse.urlparse(h).query).get("u", [""])[0]
        if u and not social_platform(u):
            return urllib.parse.unquote(u)
    return ""


def _website_from_body(body: str) -> str:
    """Fallback: a bare URL printed under the 'Websites' label."""
    for m in re.findall(r"https?://[^\s]+", body):
        url = m.rstrip(".,")
        if not social_platform(url) and "facebook.com" not in url:
            return url
    return ""


def _phone_from_body(body: str) -> str:
    m = _PHONE_RE.search(body)
    return re.sub(r"\s+", " ", m.group(0)).strip() if m else ""


def _labelled(body: str, label: str) -> str:
    """The line immediately after a label line (e.g. 'Categories\\nSoftware')."""
    lines = [ln.strip() for ln in body.splitlines()]
    for i, ln in enumerate(lines):
        if ln == label and i + 1 < len(lines) and lines[i + 1]:
            return lines[i + 1]
    return ""


def _empty(slug: str, warnings: list[str]) -> dict:
    return {"page": slug, "source": "", "emails": [], "website": "",
            "phone": "", "category": "", "warnings": warnings}
