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
    # Template placeholders the site owner never edited.
    "website.com", "mysite.com", "yoursite.com", "site.com", "test.com",
    "company.com", "business.com", "mail.com",
    # Third-party widgets/aggregators embedded in pages — never the biz's inbox.
    "inline.app", "surveycake.com", "lin.ee", "line.me", "forms.gle",
    "bit.ly", "no8.io", "s.no8.io", "typeform.com", "calendly.com",
    "jotform.com", "hotjar.com", "intercom.io", "zendesk.com",
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


# ── company-name normalization ───────────────────────────────────────────────
#
# Taiwanese company names reach us in several spellings of the same thing: a 公會
# directory appends the Latin alias in brackets ("慧與科技股份有限公司（Hewlett
# Packard Enterprise Taiwan）"), 台 and 臺 are used interchangeably, and spacing
# varies. Three call sites compare or query on these names — the cross-source
# business merge, the 經濟部 registry lookup, and the Maps website resolver — and
# they must agree, or the same company merges in one place and not another.
_NAME_ALIAS_RE = re.compile(r"[（(\[【][^）)\]】]*[）)\]】]")


def strip_name_alias(name: str) -> str:
    """Drop a bracketed alias: `Acme（Acme Co., Ltd.）` → `Acme`."""
    return _NAME_ALIAS_RE.sub("", name or "")


def normalize_company_name(name: str) -> str:
    """Fold a company name to a comparable form (alias, spacing, 台/臺, case)."""
    return re.sub(r"\s+", "", strip_name_alias(name)).replace("台", "臺").lower()


# ── multi-key record merge ───────────────────────────────────────────────────

def merge_by_keys(index: dict, item: dict, keys) -> dict:
    """Fold `item` into `index` under every key that can identify it.

    A business is identified by more than one thing — its website domain and its
    company name — and the sources rarely supply both at once. A single-key
    index therefore splits one company in two: the 公會 list page knows the
    website (domain key), its detail page and the 經濟部 registry know only the
    name (name key), and neither insert can see the other.

    So every key an entry is known by points at the *same* dict, and a later
    arrival sharing any one of them merges instead of duplicating. If it bridges
    two entries that were previously separate, they collapse into one. Existing
    values always win; a later source only fills blanks. Because several keys
    map to one object, read the result back with :func:`unique_records`.
    """
    keys = [k for k in keys if k]
    if not keys:
        return item
    seen = [index[k] for k in keys if k in index]
    entry = seen[0] if seen else dict(item)
    for other in seen[1:]:                      # collapse entries this bridged
        if other is entry:
            continue
        _fill_blanks(entry, other)
        for key, value in list(index.items()):
            if value is other:
                index[key] = entry
    if seen:
        _fill_blanks(entry, item)
    for key in keys:
        index.setdefault(key, entry)
    return entry


def _fill_blanks(target: dict, source: dict) -> None:
    for field, value in source.items():
        if value and not target.get(field):
            target[field] = value


def unique_records(index: dict) -> list:
    """The distinct entries of a :func:`merge_by_keys` index, in insert order."""
    out, seen = [], set()
    for entry in index.values():
        if id(entry) not in seen:
            seen.add(id(entry))
            out.append(entry)
    return out


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
    if any(dom == d or dom.endswith("." + d) for d in _JUNK_DOMAINS):
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
# Reconstruct a whole obfuscated address in one shot (never rewriting stray "at"
# / "dot" words elsewhere). Two tiers, deliberately conservative because "at" is
# an ordinary English word — an over-eager rule turns prose like
# "advocate at Netlify. Evangelist" into a bogus "at@netlify.evangelist":
#
#   1. SYMBOL form — the at-marker is a fullwidth ＠ or bracketed [at]/(at)/{at};
#      here a literal "." is a safe dot separator (john[at]gmail.com). A plain
#      ASCII "@" is deliberately excluded — real "@" addresses are already found
#      by _EMAIL_RE, and admitting it here (with the loose whitespace this
#      pattern allows) turns an "@mention . Word" fragment into a bogus address.
#   2. WORD form — the at-marker is the bare word "at"; this is only trusted
#      when the dot separators are ALSO obfuscated ([dot]/(dot)/{dot}/ dot ),
#      never a literal "." (so "john at gmail dot com" matches, prose doesn't).
_AT_SYMBOL = r"(?:＠|[\[\(\{]\s*at\s*[\]\)\}])"
_DOT_SYMBOL = r"(?:\.|[\[\(\{]\s*dot\s*[\]\)\}])"
_DOT_WORD = r"(?:[\[\(\{]\s*dot\s*[\]\)\}]|\s+dot\s+)"
_LOCAL = r"([a-z0-9._%+\-]+)"
_TLD = r"([a-z]{2,})"


def _obf_re(at, dot):
    return re.compile(
        rf"{_LOCAL}\s*{at}\s*"
        rf"([a-z0-9\-]+(?:\s*{dot}\s*[a-z0-9\-]+)*)\s*{dot}\s*{_TLD}",
        re.IGNORECASE,
    )


_OBFUSCATED_RES = (_obf_re(_AT_SYMBOL, _DOT_SYMBOL),
                   _obf_re(r"\s+at\s+", _DOT_WORD))


def deobfuscate_emails(text: str) -> set[str]:
    """Reconstruct obfuscated addresses (`a [at] b [dot] com` → `a@b.com`)."""
    out: set[str] = set()
    for rx in _OBFUSCATED_RES:
        for local, domain, tld in rx.findall(text or ""):
            domain = re.sub(_DOT_WORD, ".", domain, flags=re.IGNORECASE)
            domain = re.sub(r"\s+", "", domain).strip(".")
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


# Cloudflare "Email Address Obfuscation" hides addresses behind a hex blob that
# JS decodes client-side — so a static fetch sees no email and we'd fall back to
# a guess. The scheme is trivial (XOR every byte by the first), so decode it here.
_CFEMAIL_RE = re.compile(
    r'(?:data-cfemail="|/cdn-cgi/l/email-protection#)([0-9a-fA-F]{4,})'
)


def decode_cfemail(html: str) -> set[str]:
    """Decode Cloudflare-obfuscated emails (`data-cfemail` / email-protection#)."""
    out: set[str] = set()
    for blob in _CFEMAIL_RE.findall(html or ""):
        try:
            raw = bytes.fromhex(blob)
            key = raw[0]
            email = "".join(chr(b ^ key) for b in raw[1:])
        except (ValueError, IndexError):
            continue
        if _EMAIL_RE.fullmatch(email):
            out.add(email.lower())
    return out


def _with_scheme(url: str) -> str:
    url = (url or "").strip()
    if url and not url.startswith(("http://", "https://")):
        return "https://" + url
    return url
