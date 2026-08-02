"""
Discover businesses from Taiwan trade-association (公會 / 工會) member directories.

An alternative stage-1 source for the lead-collection funnel (see
doc/email_collect), sitting beside Google Maps:

    DISCOVER (Maps | 公會名錄 | 經濟部登記)  →  extract email  →  verify  →  dedupe

Why associations beat Maps for B2B outreach: a 公會 member directory *is* the
ICP list — every row is a real, dues-paying company in one industry, and the row
usually carries the company's **own website**, which is what the email-extraction
stage actually needs. Maps, by contrast, is geography-first and full of retail
storefronts.

Two ways in:

* **Built-in adapters** (`ASSOCIATIONS`) — hand-written for directories worth
  first-class support. Today: `tca` (台北市電腦商業同業公會), whose 會員e名錄 is a
  Big5 ASP app with a keyword search and a per-member detail page.
* **Generic scraper** — pass any member-list URL and we crawl it structurally:
  outbound links to member sites, Chinese-labelled fields (公司名稱 / 地址 / 電話 /
  網址 / E-mail), same-site detail pages, and 下一頁 pagination. Best-effort by
  design: TW association sites are hand-rolled and no single parser fits them
  all, so we return whatever we recognized plus a `warnings` list.

Like every scraper in this package it returns partial results instead of raising.
"""
import ipaddress
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

from crewai.tools import BaseTool
from pydantic import BaseModel

from src.automation.tools.contact_harvest import (
    harvest_emails_from_text,
    merge_by_keys,
    normalize_company_name,
    social_platform,
    unique_records,
)

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _UA}
_TIMEOUT = 25
_MAX_BYTES = 3 * 1024 * 1024

# Generic-crawler bounds. Association directories are small and slow; these keep
# one source from monopolising a run.
_MAX_LIST_PAGES = 5      # 下一頁 hops from the given URL
_MAX_DETAIL_PAGES = 40   # same-site member records opened for labelled fields

# Hosts that are never a member company's own site (the association's own
# domain is excluded separately, per-directory).
_NON_MEMBER_HOSTS = (
    "google.com", "gstatic.com", "googleapis.com", "youtube.com", "youtu.be",
    "adobe.com", "microsoft.com", "w3.org", "jquery.com", "bootstrapcdn.com",
    "line.me", "wa.me", "whatsapp.com", "maps.google.com",
)

# Chinese/English field labels seen across 公會 member pages. Order matters only
# for readability — matching is exact against a whole table cell.
_LABELS: dict[str, tuple[str, ...]] = {
    "name": ("公司名稱", "廠商名稱", "會員名稱", "會員廠商", "商號名稱",
             "企業名稱", "單位名稱", "公司", "Company Name"),
    "address": ("公司地址", "地址", "通訊地址", "營業地址", "會址", "Address"),
    "phone": ("公司電話", "電話", "聯絡電話", "連絡電話", "TEL", "Tel", "Phone"),
    "website": ("公司網址", "網址", "網站", "公司網站", "Website", "URL"),
    "email": ("電子郵件", "電子信箱", "E-mail", "Email", "E-Mail", "信箱", "郵件"),
    "category": ("業務類型", "營業項目", "產業類別", "主要產品", "行業別",
                 "經營項目", "業別"),
    "contact": ("聯絡人", "連絡人", "負責人", "代表人", "Contact"),
}

_NEXT_PAGE_TEXTS = ("下一頁", "下页", "下一頁›", "next", "Next", "»", "›")


class AssociationSearchInput(BaseModel):
    source: str
    keyword: str = ""
    limit: int = 30


class TWAssociationTool(BaseTool):
    name: str = "tw_association_directory"
    description: str = (
        "Search a Taiwan trade-association (公會/工會) member directory for "
        "companies and return each member's name, website, phone, address and "
        "category. Args: source (str — a built-in slug such as 'tca', or any "
        "member-directory URL), keyword (str, e.g. '科技'), limit (int). The "
        "website field feeds the email-extraction stage."
    )
    args_schema: type[BaseModel] = AssociationSearchInput

    def _run(self, source: str, keyword: str = "", limit: int = 30) -> dict:
        return search_association(source, keyword, limit)


# ── public API ───────────────────────────────────────────────────────────────

def search_association(source: str, keyword: str = "", limit: int = 30,
                       log=None) -> dict:
    """Discover up to `limit` member companies from one association directory.

    `source` is either a built-in slug (see :data:`ASSOCIATIONS`) or a URL to a
    member-list page. Returns
    ``{"source", "association", "businesses": [...], "warnings": [...]}``; each
    business matches the Maps shape (name/website/phone/address/category/maps_url)
    so the funnel can consume both interchangeably.
    """
    _log = log or (lambda _m: None)
    limit = max(1, min(int(limit or 1), 500))
    source = (source or "").strip()
    warnings: list[str] = []

    spec = ASSOCIATIONS.get(source.lower())
    if spec:
        _log(f"Searching {spec['name']} member directory for '{keyword or 'all'}'...")
        businesses = spec["search"](keyword, limit, warnings, _log)
        label = spec["name"]
    elif source.lower().startswith(("http://", "https://")):
        label = urllib.parse.urlparse(source).netloc
        # This URL comes straight from a run payload, so it is untrusted input.
        reason = _reject_reason(source)
        if reason:
            return {"source": source, "association": label, "businesses": [],
                    "warnings": [f"refusing to crawl {source}: {reason}"]}
        _log(f"Crawling member directory {source}...")
        businesses = _scrape_generic(source, keyword, limit, warnings, _log)
    else:
        return {"source": source, "association": "", "businesses": [],
                "warnings": [f"unknown association source '{source}' — use a slug "
                             f"({', '.join(sorted(ASSOCIATIONS))}) or a directory URL"]}

    for biz in businesses:
        biz.setdefault("discovery", f"association:{source.lower()}")
    _log(f"{label}: {len(businesses)} member(s)")
    return {"source": source, "association": label,
            "businesses": businesses[:limit], "warnings": warnings[:8]}


def list_associations() -> list[dict]:
    """Built-in directories, for the UI picker."""
    return [{"slug": slug, "name": spec["name"], "url": spec["url"]}
            for slug, spec in sorted(ASSOCIATIONS.items())]


# ── built-in adapter: 台北市電腦商業同業公會 (TCA) ─────────────────────────────
#
# 會員e名錄 is a Big5 classic-ASP app:
#   list   GET  /tcaprdqc.asp?bytype=company&BNA_C=<kw>&page=<n>   (10 rows/page)
#   detail POST /members_list.asp  no=<會員編號>
# The list page carries only 編號/名稱/電話; the website — the field the funnel
# actually needs — lives on the detail page, so we open one per member.

_TCA_BASE = "https://www.tca.org.tw"
# The whole app is Big5; cp950 is the superset Python ships, so it also decodes
# the Big5-HKSCS characters that show up in a few company names.
_TCA_CHARSET = "cp950"
_TCA_ROWS_PER_PAGE = 10
_TCA_ROW_RE = re.compile(
    r"<td[^>]*>\s*(\d+)\s*</td>\s*"
    r"<td[^>]*>\s*<a[^>]*GoList\('(\d+)'\)[^>]*>(.*?)</a>\s*</td>\s*"
    r"<td[^>]*>(.*?)</td>",
    re.I | re.S,
)


def _tca_search(keyword: str, limit: int, warnings: list[str], log) -> list[dict]:
    keyword = (keyword or "").strip()
    if len(keyword) < 2:
        warnings.append("TCA 會員名錄 requires a keyword of 2+ characters "
                        "(it matches Chinese company names, e.g. '科技')")
        return []

    members: list[tuple[str, str, str]] = []  # (member_no, name, phone)
    seen: set[str] = set()
    pages = (limit + _TCA_ROWS_PER_PAGE - 1) // _TCA_ROWS_PER_PAGE
    for page in range(1, pages + 1):
        url = f"{_TCA_BASE}/tcaprdqc.asp?" + urllib.parse.urlencode(
            {"bytype": "company", "BNA_C": keyword, "page": page},
            encoding=_TCA_CHARSET, errors="replace")
        html = _fetch(url, encoding=_TCA_CHARSET)
        if html is None:
            warnings.append(f"TCA list page {page} unavailable")
            break
        rows = _TCA_ROW_RE.findall(html)
        if not rows:
            break
        for _no, sno, name, phone in rows:
            if sno in seen:
                continue
            seen.add(sno)
            members.append((sno, _plain(name), _plain(phone)))
        if len(members) >= limit:
            break

    # One sequential POST per member at a 25 s timeout, so an unbounded `limit`
    # (clamped only at 500) could hold the run for hours. Same ceiling the
    # generic crawler applies to its detail pages, and the truncation is
    # reported rather than silent.
    take = min(len(members), limit, _MAX_DETAIL_PAGES)
    if take < min(len(members), limit):
        # Only when the safety cap is what bit — stopping at the caller's own
        # `limit` is the limit working, not a truncation worth warning about.
        warnings.append(f"TCA: read {take} of {len(members)} matched member "
                        f"record(s) — narrow the keyword for the rest")
    businesses: list[dict] = []
    for i, (sno, name, phone) in enumerate(members[:take], 1):
        log(f"[{i}/{take}] Reading TCA member {name}")
        detail = _tca_member(sno)
        businesses.append({
            "name": detail.get("name") or name,
            "website": detail.get("website", ""),
            "phone": detail.get("phone") or phone,
            "address": detail.get("address", ""),
            "category": detail.get("category", ""),
            "maps_url": "",
            "member_no": sno,
            "emails": detail.get("emails", []),
        })
    return businesses


def _tca_member(member_no: str) -> dict:
    """Read one 會員 detail record (POST members_list.asp)."""
    body = urllib.parse.urlencode(
        {"no": member_no}, encoding=_TCA_CHARSET).encode("ascii")
    html = _fetch(f"{_TCA_BASE}/members_list.asp",
                  encoding=_TCA_CHARSET, data=body)
    if not html:
        return {}
    fields = _labelled_fields(html)
    fields["emails"] = _member_emails(html, "www.tca.org.tw")
    return fields


ASSOCIATIONS: dict[str, dict] = {
    "tca": {
        "name": "台北市電腦商業同業公會 (TCA)",
        "url": f"{_TCA_BASE}/findprod2.asp",
        "search": _tca_search,
    },
}


# ── generic member-directory crawler ─────────────────────────────────────────

def _scrape_generic(url: str, keyword: str, limit: int,
                    warnings: list[str], log) -> list[dict]:
    """Crawl an arbitrary 公會 member-list page.

    Three passes per list page, cheapest first:
      1. **Outbound links** — an anchor pointing off the association's own domain
         is, on a member page, almost always that member's website; the anchor
         text is the company name.
      2. **Labelled cells** — tables/definition lists using 公司名稱 / 網址 / 電話…
      3. **Detail pages** — same-site member records, opened for the labelled
         fields the list page omits (bounded by ``_MAX_DETAIL_PAGES``).
    Then follows 下一頁 up to ``_MAX_LIST_PAGES``.
    """
    host = urllib.parse.urlparse(url).netloc.lower()
    found: dict[str, dict] = {}   # dedupe key → business
    detail_urls: list[str] = []
    page_url: str | None = url

    for page_no in range(1, _MAX_LIST_PAGES + 1):
        if not page_url:
            break
        html = _fetch_public(page_url, warnings)
        if html is None:
            warnings.append(f"directory page unreachable: {page_url}")
            break

        for biz in _members_from_list(html, page_url, host):
            _add_business(found, biz)
        for link in _detail_links(html, page_url, host):
            if link not in detail_urls:
                detail_urls.append(link)

        if len(_members(found)) >= limit and not _needs_detail(found):
            break
        page_url = _next_page(html, page_url, host)
        if page_url:
            log(f"Following directory page {page_no + 1}...")

    # Open member records for rows still missing a website (the field that
    # decides whether the lead can produce an email at all).
    if detail_urls and (len(_members(found)) < limit or _needs_detail(found)):
        for i, durl in enumerate(detail_urls[:_MAX_DETAIL_PAGES], 1):
            if len(_members(found)) >= limit and not _needs_detail(found):
                break
            html = _fetch_public(durl, warnings)
            if html is None:
                continue
            fields = _labelled_fields(html)
            if not fields.get("name"):
                continue
            fields.setdefault("emails", _member_emails(html, host))
            fields["source_url"] = durl
            if not fields.get("website"):
                out = _outbound_links(html, durl, host)
                if len(out) == 1:
                    fields["website"] = out[0][1]
            _add_business(found, fields)
            if i % 10 == 0:
                log(f"Read {i} member record(s) from {host}")

    businesses = [_normalize_business(b) for b in _members(found)]
    if keyword:
        kw = keyword.strip().lower()
        matched = [b for b in businesses
                   if kw in b["name"].lower() or kw in b["category"].lower()]
        # A keyword that filters everything out is more likely a mismatch with
        # this directory's labelling than a genuine "no members" — keep the rows
        # and say so, rather than silently returning nothing.
        if matched:
            businesses = matched
        elif businesses:
            warnings.append(
                f"keyword '{keyword}' matched no member names on {host}; "
                "returning the full page instead")
    if not businesses:
        warnings.append(f"no member companies recognized on {host}")
    return businesses[:limit]


# Row delimiters for splitting a list page into per-member blocks.
_ROW_SPLIT_RE = re.compile(r"(?=<(?:tr|li|article)\b)", re.I)


def _members_from_list(html: str, base: str, host: str) -> list[dict]:
    """Members recognizable from a list page alone (outbound link + labels)."""
    out: list[dict] = []
    for text, href in _outbound_links(html, base, host):
        if text:
            out.append({"name": text, "website": href})
    for block in _label_blocks(html):
        fields = _labelled_fields(block)
        if fields.get("name"):
            fields["source_url"] = base
            out.append(fields)
    return out


def _label_blocks(html: str) -> list[str]:
    """Split a page into the units `_labelled_fields` may safely run over.

    Two layouts both occur on these sites, and the split has to tell them apart:

    * **One member, fields down the page** (`<tr>公司名稱…</tr><tr>電話…</tr>`) —
      the fields belong together, so the whole page is one block. Splitting per
      row here would strand the name from its phone and website.
    * **Many members, one per row** — running `_labelled_fields` across the
      whole page would return the *first* value for each label independently, so
      a row missing a field silently borrows the next member's, fabricating a
      company whose website then gets scraped for "its" email.

    A page carrying more than one name label is the second kind.
    """
    names = sum(1 for c in _cells(html) if c.rstrip("：: 　") in _LABELS["name"])
    return [html] if names <= 1 else _ROW_SPLIT_RE.split(html)


def _member_emails(html: str, directory_host: str) -> list[str]:
    """Emails published on a member page, minus the association's own.

    Every 公會 page carries the guild's own contact address in its header or
    footer. Harvested naively that address would be attached to whichever member
    record happened to be parsed first — one bogus, confidently-wrong lead per
    run — so anything on the directory's own domain is dropped here.
    """
    own = directory_host.removeprefix("www.").lower()
    out = []
    for email in harvest_emails_from_text(_plain(html)):
        domain = email.rsplit("@", 1)[-1].lower()
        if own and (domain == own or domain.endswith("." + own)
                    or own.endswith("." + domain)):
            continue
        out.append(email)
    return out


def _members(found: dict) -> list[dict]:
    """The distinct member rows in the index (several keys share one row)."""
    return unique_records(found)


def _needs_detail(found: dict) -> bool:
    """True while some discovered member still has no website to scrape."""
    return any(not b.get("website") for b in _members(found))


def _add_business(found: dict, biz: dict) -> None:
    """Insert or merge a member row under both its website and name keys.

    One member routinely arrives twice — once from an outbound link on the list
    page (website known) and once from its detail page (name known, website not
    yet parsed) — so indexing on either key alone yields two rows for one
    company. See :func:`merge_by_keys`.
    """
    website = (biz.get("website") or "").strip()
    name = (biz.get("name") or "").strip()
    if not name and not website:
        return
    entry = merge_by_keys(found, biz, (_dedupe_key(website), _name_key(name)))
    # A merge can supply a website the row didn't arrive with; register it so
    # the next sighting of that domain finds this entry.
    site_key = _dedupe_key(entry.get("website") or "")
    if site_key:
        found.setdefault(site_key, entry)


def _name_key(name: str) -> str:
    """Fold a member name for dedupe — shared with the flow's cross-source merge."""
    return normalize_company_name(name) or name.strip().lower()


def _dedupe_key(website: str) -> str:
    host = urllib.parse.urlparse(_with_scheme(website)).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _normalize_business(biz: dict) -> dict:
    return {
        "name": (biz.get("name") or "").strip(),
        "website": _with_scheme((biz.get("website") or "").strip()),
        "phone": (biz.get("phone") or "").strip(),
        "address": (biz.get("address") or "").strip(),
        "category": (biz.get("category") or "").strip(),
        "maps_url": "",
        **({"emails": biz["emails"]} if biz.get("emails") else {}),
        **({"contact": biz["contact"]} if biz.get("contact") else {}),
        **({"member_no": biz["member_no"]} if biz.get("member_no") else {}),
        **({"source_url": biz["source_url"]} if biz.get("source_url") else {}),
    }


_ANCHOR_RE = re.compile(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                        re.I | re.S)


def _outbound_links(html: str, base: str, host: str) -> list[tuple[str, str]]:
    """(anchor text, absolute URL) for links leaving the association's domain."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for href, inner in _ANCHOR_RE.findall(html):
        full = urllib.parse.urljoin(base, href.strip())
        if not full.lower().startswith(("http://", "https://")):
            continue
        link_host = urllib.parse.urlparse(full).netloc.lower()
        if not link_host or _same_site(link_host, host):
            continue
        if social_platform(full) or any(
                link_host == h or link_host.endswith("." + h)
                for h in _NON_MEMBER_HOSTS):
            continue
        if full in seen:
            continue
        seen.add(full)
        out.append((_plain(inner), full))
    return out


def _detail_links(html: str, base: str, host: str) -> list[str]:
    """Same-site links that look like a member record rather than navigation."""
    hints = ("member", "company", "comp", "vendor", "factory", "detail",
             "list.asp", "info", "shop", "firm", "會員", "廠商", "公司")
    out: list[str] = []
    for href, inner in _ANCHOR_RE.findall(html):
        full = urllib.parse.urljoin(base, href.strip().split("#")[0])
        if not full.lower().startswith(("http://", "https://")):
            continue
        if not _same_site(urllib.parse.urlparse(full).netloc.lower(), host):
            continue
        if full.rstrip("/") == base.rstrip("/"):
            continue
        blob = f"{full} {_plain(inner)}"
        # A record link carries an id-ish query/path segment; a nav link doesn't.
        if any(h in blob for h in hints) and re.search(r"\d{2,}", full):
            out.append(full)
    return list(dict.fromkeys(out))


def _next_page(html: str, base: str, host: str) -> str | None:
    for href, inner in _ANCHOR_RE.findall(html):
        if _plain(inner).strip() not in _NEXT_PAGE_TEXTS:
            continue
        full = urllib.parse.urljoin(base, href.strip())
        if (full.lower().startswith(("http://", "https://"))
                and _same_site(urllib.parse.urlparse(full).netloc.lower(), host)
                and full.rstrip("/") != base.rstrip("/")):
            return full
    return None


def _same_site(a: str, b: str) -> bool:
    a, b = a.removeprefix("www."), b.removeprefix("www.")
    return a == b or a.endswith("." + b) or b.endswith("." + a)


# ── labelled-field extraction ────────────────────────────────────────────────

_CELL_BREAK_RE = re.compile(
    r"</?(?:td|th|tr|br|li|dt|dd|p|div|h[1-6]|span|table|tbody)[^>]*>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.I | re.S)


def _cells(html: str) -> list[str]:
    """Split HTML into visible text cells, preserving intra-cell spacing.

    Label→value extraction needs cell boundaries, so we break on the structural
    tags and drop everything else — good enough for the table/definition-list
    markup every one of these directories is built from.
    """
    text = _SCRIPT_RE.sub(" ", html or "")
    text = _CELL_BREAK_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = _unescape(text)
    return [c.strip() for c in text.split("\n") if c.strip()]


def _labelled_fields(html: str) -> dict:
    """Pull ``公司名稱``/``網址``/``電話``… values out of a member record.

    A label may sit in its own cell (``<td>網址</td><td>http://…</td>``) or lead
    its value inside one (``網址：http://…``); both forms appear, often on the
    same page.
    """
    cells = _cells(html)
    out: dict[str, str] = {}
    for i, cell in enumerate(cells):
        stripped = cell.rstrip("：: 　")
        for field, labels in _LABELS.items():
            if out.get(field):
                continue
            if stripped in labels:
                value = cells[i + 1].strip() if i + 1 < len(cells) else ""
                # The next cell being another label means this one had no value.
                if value and not _is_label(value):
                    out[field] = value
            else:
                for label in labels:
                    m = re.match(rf"{re.escape(label)}\s*[：:]\s*(\S.*)", cell)
                    if m:
                        out[field] = m.group(1).strip()
                        break
    if out.get("website"):
        out["website"] = _clean_url(out["website"])
        if not out["website"]:
            out.pop("website")
    return out


def _is_label(value: str) -> bool:
    stripped = value.rstrip("：: 　")
    return any(stripped in labels for labels in _LABELS.values())


def _clean_url(value: str) -> str:
    m = re.search(r"(?:https?://)?(?:www\.)?[\w\-]+(?:\.[\w\-]+)+(?:/\S*)?", value)
    if not m:
        return ""
    url = _with_scheme(m.group(0).rstrip(".,；;、"))
    host = urllib.parse.urlparse(url).netloc
    return url if host and "." in host else ""


def _with_scheme(url: str) -> str:
    url = (url or "").strip()
    if url and not url.lower().startswith(("http://", "https://")):
        return "https://" + url
    return url


def _plain(html_fragment: str) -> str:
    return re.sub(r"\s+", " ", _unescape(_TAG_RE.sub(" ", html_fragment or ""))).strip()


def _unescape(text: str) -> str:
    import html as _html
    return _html.unescape(text)


# ── HTTP ─────────────────────────────────────────────────────────────────────

_CHARSET_RE = re.compile(rb'charset=["\']?\s*([\w\-]+)', re.I)


# ── SSRF guard ───────────────────────────────────────────────────────────────
#
# The generic crawler fetches a URL supplied in the run payload and hands the
# page's text and any harvested emails back to the caller. Unguarded, that is a
# read primitive against anything the server can reach — cloud metadata
# (169.254.169.254), localhost admin panels, RFC1918 hosts. So every address a
# URL resolves to must be public, and because urllib follows redirects, the
# check has to run again on each hop rather than only on the URL we were given.

def _reject_reason(url: str) -> str:
    """Why `url` must not be fetched, or "" if it's a fine public target."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"unsupported scheme '{parsed.scheme}'"
    host = parsed.hostname
    if not host:
        return "no host in URL"
    try:
        infos = socket.getaddrinfo(host, parsed.port or
                                   (443 if parsed.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except OSError as exc:
        return f"host does not resolve ({type(exc).__name__})"
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return "host resolved to an unparseable address"
        if not ip.is_global or ip.is_multicast:
            return f"host resolves to the non-public address {ip}"
    return ""


class _PublicOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-run the host check on every redirect hop.

    Without this a public host can 302 to http://169.254.169.254/ and the guard
    on the original URL buys nothing.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        reason = _reject_reason(newurl)
        if reason:
            raise urllib.error.HTTPError(
                newurl, code, f"blocked redirect: {reason}", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_SAFE_OPENER = urllib.request.build_opener(_PublicOnlyRedirectHandler)


def _fetch_public(url: str, warnings: list[str]):
    """`_fetch`, but only for a host that passes the guard. None if it doesn't.

    Checking the seed URL alone is not enough: the crawler follows 下一頁 and
    member-record links, and `_same_site` accepts sub-domains — so a payload URL
    at a public `evil.com` can hand us `internal.evil.com`, resolve that to
    127.0.0.1, and get the page's text and emails harvested into leads. Every
    hop the crawler chooses for itself goes through the same check as the one
    the user supplied.
    """
    reason = _reject_reason(url)
    if reason:
        warnings.append(f"skipped {url}: {reason}")
        return None
    return _fetch(url)


def _fetch(url: str, encoding: str | None = None, data: bytes | None = None):
    """GET/POST a directory page, decoded with the right charset.

    Association sites are old: Big5 is still common and often only declared in a
    `<meta>` tag, so sniff the declared charset when the caller doesn't know it.
    Returns None on any transport error — the crawl continues without the page.
    The decode is inside the `try` too, so a page declaring a charset Python
    can't honour costs that one page rather than raising out of the tool.
    """
    try:
        req = urllib.request.Request(url, data=data, headers=_HEADERS)
        with _SAFE_OPENER.open(req, timeout=_TIMEOUT) as resp:
            raw = resp.read(_MAX_BYTES)
            enc = encoding or _sniff_charset(raw, resp.headers.get("Content-Type", ""))
        return raw.decode(enc, errors="replace")
    except Exception:  # noqa: BLE001 — dead link / timeout / TLS / bad charset
        return None


def _sniff_charset(raw: bytes, content_type: str) -> str:
    m = re.search(r"charset=([\w\-]+)", content_type or "", re.I)
    if m:
        return _canonical_charset(m.group(1))
    m2 = _CHARSET_RE.search(raw[:4096])
    if m2:
        return _canonical_charset(m2.group(1).decode("ascii", errors="ignore"))
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        # Undeclared and not UTF-8 → Big5, and route it through the canonical
        # mapping like every declared charset so HKSCS characters in company
        # names survive instead of decoding to replacement characters.
        return _canonical_charset("big5")


def _canonical_charset(name: str) -> str:
    name = (name or "").strip().lower()
    # big5 pages routinely declare the (narrower) big5 while using big5-hkscs /
    # cp950 extensions; cp950 is the superset Python ships, so prefer it.
    if name in ("big5", "big-5", "cp950", "ms950", "big5-hkscs"):
        return "cp950"
    try:
        "".encode(name)
        return name
    except LookupError:
        return "utf-8"
