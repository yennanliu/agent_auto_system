"""
Shared contact-harvesting primitives for the lead-collection funnel.

Extracted from `email_extract_tool.py` so every source — website HTML, a
Facebook About block, an Instagram bio, an X profile description — filters and
ranks emails the same way. Social bios in particular routinely *obfuscate*
addresses (`name [at] domain [dot] com`, `name(a)domain`) to dodge naive
scrapers, so this module also de-obfuscates before harvesting.

The strict email regex is the real gate: de-obfuscation is deliberately liberal
because a bad substitution only matters if it happens to form a syntactically
valid address, and the downstream MX/SMTP verify stage drops the rest.
"""
import re
import urllib.parse

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# Emails that are never a real contact: analytics/CDN/CMS placeholders and the
# fake addresses templates ship with.
_JUNK_DOMAINS = {
    "sentry.io", "wixpress.com", "example.com", "example.org", "example.net",
    "domain.com", "email.com", "yourdomain.com", "sentry-next.wixpress.com",
    "godaddy.com", "schema.org", "w3.org", "googleapis.com", "gstatic.com",
    "cloudflare.com", "wordpress.com", "wix.com", "squarespace.com",
}
_JUNK_LOCALPARTS = {"you", "your", "name", "email", "user", "username", "example"}
_IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico")
_ROLE_LOCALPARTS = (
    "info", "contact", "hello", "sales", "office", "enquiries", "enquiry",
    "inquiries", "admin", "support", "hi", "team", "mail", "business",
)

# Shared platforms where a business's Maps "website" often points. A guessed
# `info@<here>` is nonsense, and (for the social sources) these are the hosts we
# route to a dedicated profile extractor instead of the plain website scraper.
_SOCIAL_HOSTS = {
    "facebook.com": "facebook", "fb.com": "facebook", "fb.me": "facebook",
    "m.facebook.com": "facebook", "mbasic.facebook.com": "facebook",
    "instagram.com": "instagram", "instagr.am": "instagram",
    "x.com": "x", "twitter.com": "x", "mobile.twitter.com": "x",
    "threads.net": "threads",
    "linktr.ee": "linktree", "linktree.com": "linktree",
}


def social_platform(url: str) -> str | None:
    """Return the canonical platform name for a social URL, else None.

    'https://www.facebook.com/mybiz' → 'facebook'; a normal domain → None.
    """
    host = urllib.parse.urlparse(_with_scheme(url)).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return None
    for h, name in _SOCIAL_HOSTS.items():
        if host == h or host.endswith("." + h):
            return name
    return None


def is_valid_email(email: str, site_domain: str = "") -> bool:
    """True if `email` looks like a real, contactable address (not junk/asset)."""
    email = email.strip().lower()
    if email.endswith(_IMG_EXT):
        return False
    if "@" not in email or email.count("@") != 1:
        return False
    local, _, dom = email.partition("@")
    if not local or local in _JUNK_LOCALPARTS:
        return False
    if dom in _JUNK_DOMAINS:
        return False
    if any(dom.endswith("." + d) or dom == d for d in _JUNK_DOMAINS):
        return False
    # Reject obvious asset hashes: very long hex local parts.
    if len(local) > 40:
        return False
    return True


def rank_emails(emails) -> list[str]:
    """Role addresses (info@, contact@…) first, then alphabetical for stability."""
    def key(e: str):
        local = e.split("@", 1)[0]
        is_role = any(local == r or local.startswith(r) for r in _ROLE_LOCALPARTS)
        return (0 if is_role else 1, e)
    return sorted(emails, key=key)


# ── obfuscation ─────────────────────────────────────────────────────────────
#
# Matches a whole obfuscated address in one shot so we never rewrite stray "at"
# / "dot" words elsewhere in the text. Handles: local (at) domain (dot) tld with
# the separators written as @/[at]/(at)/{at}/" at " and ./[dot]/(dot)/" dot ",
# where the domain itself may contain further (possibly obfuscated) dots.
_AT = r"(?:@|＠|[\[\(\{]\s*at\s*[\]\)\}]|\s+at\s+)"
_DOT = r"(?:\.|[\[\(\{]\s*dot\s*[\]\)\}]|\s+dot\s+)"
_OBFUSCATED_RE = re.compile(
    rf"([a-z0-9._%+\-]+)\s*{_AT}\s*"
    rf"([a-z0-9\-]+(?:\s*{_DOT}\s*[a-z0-9\-]+)*)\s*{_DOT}\s*([a-z]{{2,}})",
    re.IGNORECASE,
)


def deobfuscate_emails(text: str) -> set[str]:
    """Reconstruct obfuscated addresses (`a [at] b [dot] com` → `a@b.com`)."""
    out: set[str] = set()
    for local, domain, tld in _OBFUSCATED_RE.findall(text or ""):
        domain = re.sub(_DOT, ".", domain, flags=re.IGNORECASE)
        domain = re.sub(r"\s+", "", domain)
        candidate = f"{local}@{domain}.{tld}".lower()
        # Round-trip through the strict regex so only well-formed results survive.
        if _EMAIL_RE.fullmatch(candidate):
            out.add(candidate)
    return out


def harvest_emails_from_text(text: str, site_domain: str = "") -> list[str]:
    """Pull, validate, and rank all emails (plain + obfuscated) from free text."""
    found = {e.lower() for e in _EMAIL_RE.findall(text or "")}
    found |= deobfuscate_emails(text or "")
    valid = {e for e in found if is_valid_email(e, site_domain)}
    return rank_emails(valid)


def _with_scheme(url: str) -> str:
    url = (url or "").strip()
    if url and not url.startswith(("http://", "https://")):
        return "https://" + url
    return url
