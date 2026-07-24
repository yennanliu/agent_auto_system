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

from src.automation.tools.contact_harvest import _EMAIL_RE
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


def extract_emails(website: str, log=None) -> dict:
    """Return {"website", "emails": [...], "pages_scanned", "guessed"}."""
    _log = log or (lambda _m: None)
    base = _normalize(website)
    if not base:
        return {"website": website, "emails": [], "pages_scanned": 0, "guessed": False}

    host = urllib.parse.urlparse(base).netloc
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

    guessed = False
    if not found and domain and not _is_shared_host(domain):
        found.add(f"info@{domain}")  # single best-guess role address
        guessed = True
        _log(f"No published email on {domain}; guessed info@{domain}")

    return {
        "website": base,
        "emails": _rank(found),
        "pages_scanned": pages_scanned,
        "guessed": guessed,
    }


def _is_shared_host(domain: str) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in _NO_GUESS_DOMAINS)


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


def _discover_contact_links(base: str, html: str | None) -> list[str]:
    """Pull same-site anchors whose href/text hints at a contact/about page."""
    if not html:
        return []
    links: list[str] = []
    for href in re.findall(r'<a[^>]+href=["\']([^"\']+)["\']', html, re.I):
        low = href.lower()
        if any(k in low for k in ("contact", "kontakt", "about", "impressum", "contacto")):
            full = urllib.parse.urljoin(base + "/", href)
            if urllib.parse.urlparse(full).netloc == urllib.parse.urlparse(base).netloc:
                links.append(full.split("#")[0])
    # de-dupe, keep order, cap
    out, seen = [], set()
    for link in links:
        if link not in seen:
            seen.add(link)
            out.append(link)
    return out[:4]


def _harvest(html: str) -> set[str]:
    emails: set[str] = set()
    # mailto: links are the most reliable signal.
    for m in re.findall(r'mailto:([^"\'?>\s]+)', html, re.I):
        emails.add(urllib.parse.unquote(m))
    # Then anything email-shaped in the raw HTML/text.
    emails.update(_EMAIL_RE.findall(html))
    return emails
