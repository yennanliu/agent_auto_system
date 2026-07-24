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
    handle = (username or "").lstrip("@").strip()
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

    _log(f"X profile @{handle}: no nitter instance returned a profile")
    return _empty(handle, warnings[:6] or ["all nitter instances failed"])


# ── nitter profile-header parsing ────────────────────────────────────────────

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


def _empty(handle: str, warnings: list[str]) -> dict:
    return {"username": handle, "source": "", "emails": [], "website": "",
            "location": "", "bio": "", "warnings": warnings}
