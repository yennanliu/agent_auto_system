"""
Extract contact emails from a business website.

Stage 2 of the lead-collection funnel (see doc/email_collect):
    discover  →  EXTRACT EMAIL  →  verify  →  dedupe

Given a website, fetch the homepage plus a handful of common contact pages
(/contact, /about, /impressum for EU sites, localized variants), then pull
emails from both `mailto:` links and the rendered text. Junk (tracking/CDN/
placeholder addresses, image filenames mistaken for emails) is filtered out and
role addresses (info@, contact@, hello@…) are ranked first — they're the safest
to cold-email and the most likely to be monitored.

Pure `urllib` (no browser): SMB sites are mostly static and this keeps the stage
cheap. If nothing is found, a single role address (`info@<domain>`) is *guessed*
and flagged `guessed=True` so the verify stage can confirm it before use — never
on shared hosts (facebook.com, etc.), where a guess would be meaningless.
"""
import re
import ssl
import urllib.parse
import urllib.request

from crewai.tools import BaseTool
from pydantic import BaseModel

from src.automation.tools.contact_harvest import _EMAIL_RE, _ROLE_LOCALPARTS
from src.automation.tools.contact_harvest import decode_cfemail as _decode_cfemail
from src.automation.tools.contact_harvest import deobfuscate_emails as _deobfuscate
from src.automation.tools.contact_harvest import is_valid_email as _is_valid
from src.automation.tools.contact_harvest import rank_emails as _rank

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
_MAX_BYTES = 3 * 1024 * 1024
# SMB sites routinely have expired/self-signed/misconfigured certs. We only read
# public HTML (no credentials sent), so skip verification to avoid losing leads.
_SSL_CTX = ssl._create_unverified_context()

# Common contact-page paths across locales (EU 'impressum' is often where a
# German site legally must list its email).
_CANDIDATE_PATHS = [
    "", "contact", "contact-us", "contactus", "contact.html",
    "about", "about-us", "kontakt", "contacto", "impressum", "team", "support",
]

# Shared platforms where a business's Maps "website" often points: guessing
# info@<here> is nonsense (info@facebook.com is not the shop's inbox), so we skip
# the role-address fallback when the site itself lives on one of these hosts.
_NO_GUESS_DOMAINS = {
    "facebook.com", "instagram.com", "twitter.com", "x.com", "linktr.ee",
    "linktree.com", "youtube.com", "tiktok.com", "line.me", "wa.me",
    "whatsapp.com", "google.com", "business.site", "shopee.tw", "yelp.com",
    "wixsite.com", "blogspot.com", "pinterest.com", "threads.net",
}


# At most this many addresses per site. One business's site routinely lists a
# long tail of regional/branch role addresses (info.taiwan@, info.brazil@, …) —
# for cold outreach we only need the best few, and keeping all of them lets one
# big company swamp a send. Applied after the off-domain filter + ranking.
_MAX_EMAILS_PER_SITE = 3

# Multi-label public suffixes we must treat as one unit so `info@acme.com.tw`
# and `sales@shop.acme.com.tw` resolve to the same registrable domain
# (`acme.com.tw`). Not exhaustive — just the ones common in this funnel's markets.
_TWO_LEVEL_TLDS = {
    "com.tw", "org.tw", "net.tw", "gov.tw", "edu.tw", "idv.tw", "game.tw",
    "ebiz.tw", "club.tw", "com.cn", "net.cn", "org.cn", "com.hk", "org.hk",
    "com.au", "net.au", "org.au", "co.uk", "org.uk", "co.jp", "or.jp", "ne.jp",
    "com.sg", "com.my", "co.kr", "com.br", "co.nz", "com.mx", "co.th", "com.vn",
}


class EmailExtractInput(BaseModel):
    website: str


class WebEmailExtractTool(BaseTool):
    name: str = "web_email_extract"
    description: str = (
        "Fetch a business website (homepage + common contact/about/impressum "
        "pages) and extract contact email addresses, ranked with role addresses "
        "(info@, contact@…) first. Falls back to guessing role addresses from "
        "the domain if none are published. Args: website (str URL)."
    )
    args_schema: type[BaseModel] = EmailExtractInput

    def _run(self, website: str) -> dict:
        return extract_emails(website)


def extract_emails(website: str, log=None, render: bool = False) -> dict:
    """Return {"website", "emails": [...], "pages_scanned", "guessed"}.

    When `render` is set and the cheap static (urllib) pass finds nothing, retry
    with a headless browser before falling back to a guess: most JS-heavy SMB
    sites (Wix/Squarespace/SPAs) never put their email in the raw HTML, so the
    static scraper would otherwise guess `info@<domain>` on a site that actually
    publishes a real address. The browser is the expensive path, so it fires
    only on an otherwise-empty result.
    """
    _log = log or (lambda _m: None)
    base = _normalize(website)
    if not base:
        return {"website": website, "emails": [], "pages_scanned": 0, "guessed": False}

    host = urllib.parse.urlparse(base).netloc.lower()
    domain = host[4:] if host.startswith("www.") else host

    found: set[str] = set()
    pages_scanned = 0
    # Homepage first, then its discovered contact links, then the static guesses.
    homepage_html = _fetch(base)
    urls = [base] + _discover_contact_links(base, homepage_html) + \
           [urllib.parse.urljoin(base + "/", p) for p in _CANDIDATE_PATHS if p]
    seen_urls: set[str] = set()

    for url in urls:
        if url in seen_urls or pages_scanned >= 8:
            continue
        seen_urls.add(url)
        html = homepage_html if url == base else _fetch(url)
        if html is None:
            continue
        pages_scanned += 1
        for em in _harvest(html):
            if _is_valid(em, domain):
                found.add(em.lower())

    # JS fallback — only now that the static pass came up empty.
    if not found and render and domain and not _is_shared_host(domain):
        rendered, r_pages = _render_and_harvest(base, domain, _log)
        found |= rendered
        pages_scanned += r_pages

    guessed = False
    if not found and domain and not _is_shared_host(domain):
        found.add(f"info@{domain}")  # single best-guess role address
        guessed = True
        _log(f"No published email on {domain}; guessed info@{domain}")

    return {
        "website": base,
        "emails": _finalize_emails(found, domain),
        "pages_scanned": pages_scanned,
        "guessed": guessed,
    }


# The browser is the slow path — two loads (homepage + one contact page) catch
# the overwhelming majority, so cap rendered pages low.
_MAX_RENDERED = 3


def _render_and_harvest(base: str, domain: str, log) -> tuple[set[str], int]:
    """Load the site in a headless browser (JS executed) and harvest emails.

    Reuses one browser to render the homepage, then — if still nothing — the
    contact links it discovers (including Chinese-labelled ones). Best-effort:
    a missing Playwright, nav timeout, or crash just returns what we have.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # noqa: BLE001 — playwright not installed → skip render
        return set(), 0

    found: set[str] = set()
    scanned = 0
    log(f"Static scrape found nothing on {domain}; rendering with browser...")
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                page = browser.new_context(
                    user_agent=_HEADERS["User-Agent"]
                ).new_page()

                def render(url: str) -> str | None:
                    try:
                        page.goto(url, wait_until="networkidle", timeout=20000)
                        return page.content()
                    except Exception:  # noqa: BLE001 — nav timeout / crash
                        return None

                def harvest_into(html: str) -> None:
                    for em in _harvest(html):
                        if _is_valid(em, domain):
                            found.add(em.lower())

                home = render(base)
                if home:
                    scanned += 1
                    harvest_into(home)
                    for url in ([] if found else _discover_contact_links(base, home)):
                        if scanned >= _MAX_RENDERED:
                            break
                        html = render(url)
                        if not html:
                            continue
                        scanned += 1
                        harvest_into(html)
                        if found:
                            break
            finally:
                browser.close()
    except Exception:  # noqa: BLE001 — launch failure → fall through to guess
        return found, scanned

    if found:
        log(f"Browser render recovered {len(found)} email(s) on {domain}")
    return found, scanned


def _is_shared_host(domain: str) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in _NO_GUESS_DOMAINS)


def _registrable_domain(host: str) -> str:
    """Collapse a host to its registrable domain (eTLD+1), TW/CN/etc. aware.

    `www.shop.acme.com.tw` → `acme.com.tw`; `mail.acme.com` → `acme.com`.
    """
    host = (host or "").lower().strip().strip(".")
    if host.startswith("www."):
        host = host[4:]
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    last2 = ".".join(labels[-2:])
    take = 3 if last2 in _TWO_LEVEL_TLDS else 2
    return ".".join(labels[-take:])


def _same_site_domain(email_domain: str, site_domain: str) -> bool:
    """True if an email's domain belongs to the site (same registrable domain
    or a sub/parent-domain relation). Used to drop third-party addresses —
    a vendor's, a font CDN's, a distributor's — scraped off the page."""
    a, b = (email_domain or "").lower(), (site_domain or "").lower()
    if not a or not b:
        return False
    if a == b or a.endswith("." + b) or b.endswith("." + a):
        return True
    return _registrable_domain(a) == _registrable_domain(b)


def _finalize_emails(found: set[str], site_domain: str) -> list[str]:
    """Off-domain filter + per-site cap over the harvested set.

    1. Keep only addresses on the site's own domain (a business's real inbox);
       fall back to the off-domain ones only when nothing on-domain was found,
       so a site that publishes solely a `@gmail.com` still yields a lead.
    2. Rank role-first, then keep at most `_MAX_EMAILS_PER_SITE`, preferring a
       generic role address (`info@`, `contact@`) over a regional variant
       (`info.taiwan@`) so one company's branch list can't flood the results.
    """
    on = {e for e in found if _same_site_domain(e.split("@", 1)[-1], site_domain)}
    kept = on or found

    def cap_key(e: str):
        local = e.split("@", 1)[0].lower()
        exact_role = local in _ROLE_LOCALPARTS
        role_prefix = any(local.startswith(r) for r in _ROLE_LOCALPARTS)
        tier = 0 if exact_role else (1 if role_prefix else 2)
        return (tier, len(local), e)

    return sorted(_rank(kept), key=cap_key)[:_MAX_EMAILS_PER_SITE]


def _normalize(website: str) -> str:
    website = (website or "").strip()
    if not website:
        return ""
    if not website.startswith(("http://", "https://")):
        website = "https://" + website
    p = urllib.parse.urlparse(website)
    if not p.netloc:
        return ""
    return f"{p.scheme}://{p.netloc}"


def _fetch(url: str) -> str | None:
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        resp = urllib.request.urlopen(req, timeout=15, context=_SSL_CTX)
        cl = resp.headers.get("Content-Length")
        if cl and int(cl) > _MAX_BYTES:
            return None
        return resp.read(_MAX_BYTES).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — dead link / timeout / TLS error → skip page
        return None


# href hints (latin) and visible-text hints (incl. Chinese) for a contact/about
# page. TW SMB sites routinely label the link 聯絡我們 / 關於我們 while the href is
# an opaque slug, so we must look at the anchor's inner text too.
_LINK_HREF_HINTS = ("contact", "kontakt", "about", "impressum", "contacto",
                    "connect", "reach-us", "get-in-touch")
_LINK_TEXT_HINTS = ("contact", "about", "聯絡", "聯繫", "連絡", "關於",
                    "關於我們", "聯絡我們", "聯繫我們", "客服", "諮詢")


def _discover_contact_links(base: str, html: str | None) -> list[str]:
    """Pull same-site anchors whose href OR link text hints at a contact page."""
    if not html:
        return []
    base_host = urllib.parse.urlparse(base).netloc
    links: list[str] = []
    # Capture href and the anchor's inner text together so a Chinese label on an
    # opaque href still gets picked up.
    for href, text in re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                                 html, re.I | re.S):
        low = href.lower()
        text_plain = re.sub(r"<[^>]+>", "", text)
        hit = (any(k in low for k in _LINK_HREF_HINTS)
               or any(k in text_plain for k in _LINK_TEXT_HINTS))
        if not hit:
            continue
        full = urllib.parse.urljoin(base + "/", href)
        if urllib.parse.urlparse(full).netloc == base_host:
            links.append(full.split("#")[0])
    # de-dupe, keep order, cap
    out, seen = [], set()
    for link in links:
        if link not in seen:
            seen.add(link)
            out.append(link)
    return out[:5]


def _harvest(html: str) -> set[str]:
    emails: set[str] = set()
    # mailto: links are the most reliable signal.
    for m in re.findall(r'mailto:([^"\'?>\s]+)', html, re.I):
        emails.add(urllib.parse.unquote(m))
    # Then anything email-shaped in the raw HTML/text.
    emails.update(_EMAIL_RE.findall(html))
    # Obfuscated forms static regex misses: `info [at] domain [dot] com` and
    # Cloudflare's hex-encoded addresses — both very common on SMB sites and the
    # difference between a real address and a fallback guess.
    emails.update(_deobfuscate(html))
    emails.update(_decode_cfemail(html))
    return emails
