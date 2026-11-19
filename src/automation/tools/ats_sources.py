"""
Discovery sources for ``ats_apply`` (Phase 2 of the job-platform plan).

A source finds jobs somewhere other than an employer's ATS board and returns
them in two buckets:

  * ``refs``   — links that ``ats_router.detect_ats`` resolves to a supported
    ATS (a whole board or one job). These flow into the normal adapter funnel,
    still subject to the run's keyword / location filters.
  * ``manual`` — everything else (a careers page, an e-mail address, a job-board
    page whose employer link is hidden): listed for the user to apply by hand,
    always with a link back to where it was found.

A new source is one function ``(client, keywords, locations, log) -> dict``
registered in ``SOURCES``.

What each source can actually give (verified live, Nov 2026):

  * ``hn`` — the latest "Ask HN: Who is hiring?" thread via the official
    Algolia API. ~15 % of posts link a supported ATS directly (Ashby, Lever,
    Greenhouse, Workable…); ~30 % give an e-mail. The richest source.
  * ``remoteok`` / ``remotive`` / ``weworkremotely`` — the boards' public JSON
    API / RSS. Their apply buttons do NOT expose the employer's link (RemoteOK
    redirects back to itself, Remotive needs its own flow, We Work Remotely
    requires an account behind a Cloudflare check), so only the rare listing
    whose text contains an ATS link is routed; the rest are manual, linked back
    to the board as its API terms require.

  * ``seek_au`` / ``seek_nz`` / ``jobstreet_sg|my|ph|id`` — Seek's platform
    search API (one per market). Applying is Seek's own logged-in Quick Apply
    and job pages sit behind a challenge, so every match is a manual lead.

Not implemented (see doc/ats_apply.md): Built In (apply link only for signed-in
users), Google Jobs (needs a paid SerpAPI key), JobsDB HK / TH (HTTP 403).
"""
import html
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable

from src.automation.tools.ats_common import match_title, noop
from src.automation.tools.ats_router import BoardRef, detect_ats

_HN_SEARCH = "https://hn.algolia.com/api/v1/search_by_date"
_HN_ITEM = "https://hn.algolia.com/api/v1/items/{id}"
_REMOTEOK = "https://remoteok.com/api"
_REMOTIVE = "https://remotive.com/api/remote-jobs"
_WWR_FEEDS = ("https://weworkremotely.com/categories/remote-programming-jobs.rss",
              "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss",
              "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss",
              "https://weworkremotely.com/categories/remote-front-end-programming-jobs.rss",
              "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss")

_URL_RE = re.compile(r"https?://[^\s\"'<>()\[\]]+")
# Plain and lightly obfuscated addresses: jobs@acme.com, jobs [at] acme [dot] com.
# A plain "." must touch both labels: "jobs@acme.com.<p>Apply" is not
# "acme.com.apply". Spaces are only allowed around a spelled-out [dot] / dot.
_EMAIL_RE = re.compile(
    r"([a-z0-9][\w.+-]*)\s*(?:@|\[at\]|\(at\)|\sat\s)\s*"
    r"([a-z0-9-]+(?:(?:\.|\s*\[dot\]\s*|\s*\(dot\)\s*|\sdot\s)[a-z0-9-]+)+)", re.I)
_CLOSED_RE = re.compile(r"no longer (hiring|taking applications|accepting applications)|"
                        r"position(s)? (has |have been )?filled|role (is )?filled|"
                        r"\bclosed\b.{0,20}\b(role|position|hiring)", re.I)
_CAREER_HINT = re.compile(r"career|jobs?\b|apply|hiring|join|work-with-us|positions|openings|"
                          r"recruit", re.I)


def _text(html_text: str) -> str:
    """HTML → plain text, keeping paragraph breaks."""
    t = re.sub(r"<p>|<br\s*/?>", "\n", html_text or "", flags=re.I)
    return html.unescape(re.sub(r"<[^>]+>", " ", t))


# Links that are boilerplate, not where to apply: an employer's EEO / AAP
# policy, privacy notice, benefits page… often a posting on the same ATS
# ("Penumbra's AAP Policy Statement" → a different Lever job).
_BOILERPLATE = re.compile(r"polic(y|ies)|statement|privacy|equal (employment )?opportunit|\beeo\b|\baap\b|"
                          r"affirmative action|accommodation|benefits|notice|terms|cookie|disclaimer", re.I)
_ANCHOR_RE = re.compile(r"<a\b[^>]*?href\s*=\s*([\"'])(.*?)\1[^>]*>(.*?)</a>", re.I | re.S)


# On a job-board listing, a link only counts as *this* listing's application
# when its anchor or the words before it say so — not "take a look at our
# Senior DevOps position" (a different role) or a policy footer.
_APPLY_SIGNAL = re.compile(r"\bapply|\bapplication|submit (your )?(cv|resume|r[ée]sum[ée])", re.I)


def _links(html_text: str, need: re.Pattern | None = None) -> list[str]:
    """Every distinct URL in an HTML fragment (anchors in either quote style,
    and bare URLs), minus boilerplate links — judged by the anchor text and
    the words just before the link. With ``need``, only links whose context
    matches it are kept."""
    raw_html = html_text or ""
    found: list[tuple[str, str]] = []  # (url, anchor text + preceding words)
    for m in _ANCHOR_RE.finditer(raw_html):
        before = _text(raw_html[max(0, m.start() - 120):m.start()])[-80:]
        found.append((m.group(2), f"{before} {_text(m.group(3))}"))
    plain = _text(_ANCHOR_RE.sub(" ", raw_html))
    for m in _URL_RE.finditer(plain):
        found.append((m.group(0), plain[max(0, m.start() - 80):m.start()]))
    out: list[str] = []
    for url, context in found:
        u = html.unescape(url).rstrip(".,;:!?")
        if _BOILERPLATE.search(context) or u in out or (need is not None and not need.search(context)):
            continue
        out.append(u)
    return out


def _emails(text: str) -> list[str]:
    out: list[str] = []
    for user, domain in _EMAIL_RE.findall(text or ""):
        domain = re.sub(r"\s*(?:\[dot\]|\(dot\)|\sdot\s)\s*", ".", domain, flags=re.I)
        # "hr@acme.io.Apply now": a Title-case word after a lower-case domain is
        # the next sentence. An all-caps (ACME.CO.UK) or consistently
        # capitalised domain is kept whole.
        labels = domain.split(".")
        while (len(labels) > 2 and labels[-1][:1].isupper() and labels[-1][1:].islower()
               and labels[-2].islower()):
            labels.pop()
        domain = ".".join(labels)
        domain = re.sub(r"\s+", "", domain).strip(".").lower()
        if "." not in domain or domain.endswith((".png", ".jpg", ".gif")):
            continue
        addr = f"{user.lower()}@{domain}"
        if addr not in out:
            out.append(addr)
    return out


def _route(links: list[str]) -> tuple[list[BoardRef], list[str]]:
    """Split links into supported-ATS refs and the rest. When a company's
    specific job links are present, its bare board link is dropped: the post
    named those roles, not every opening at the company."""
    refs: list[BoardRef] = []
    other: list[str] = []
    for u in links:
        ref = detect_ats(u)
        if ref is not None and ref.supported:
            if ref not in refs:
                refs.append(ref)
        else:  # another site, or an ATS we can't apply on (Workday): a manual link
            other.append(u)
    with_jobs = {(r.platform, r.company.lower()) for r in refs if r.job_id}
    refs = [r for r in refs if r.job_id or (r.platform, r.company.lower()) not in with_jobs]
    return refs, other


def _careers_link(links: list[str]) -> str:
    """The link most likely to be where you apply: a careers / jobs page first."""
    links = [u for u in links if "ycombinator.com" not in u]
    return next((u for u in links if _CAREER_HINT.search(u)), links[0] if links else "")


# ── Hacker News: "Ask HN: Who is hiring?" ────────────────────────────────────

def latest_hn_thread(client) -> tuple[str, str]:
    """(item id, title) of the newest monthly "Who is hiring?" story."""
    resp = client.get(_HN_SEARCH, params={"tags": "story,author_whoishiring",
                                          "query": "who is hiring", "hitsPerPage": 5})
    resp.raise_for_status()
    for hit in resp.json().get("hits") or []:
        title = hit.get("title") or ""
        if title.lower().startswith("ask hn: who is hiring"):
            return str(hit["objectID"]), title
    raise RuntimeError("no 'Ask HN: Who is hiring?' thread found")


def parse_hn_post(post: dict) -> dict:
    """One top-level HN comment → ``{id, company, header, text, links, emails}``.
    The convention is a first line of ``Company | Role | Location | …``."""
    raw = post.get("text") or ""
    text = _text(raw).strip()
    header = next((ln.strip() for ln in text.split("\n") if ln.strip()), "")
    company = header.split("|")[0].strip()[:120] if "|" in header else ""
    return {"id": str(post.get("id") or ""), "company": company, "header": header[:240],
            "text": text, "links": _links(raw), "emails": _emails(text)}


def hn_source(client, keywords: list[str], locations: list[str], log: Callable = noop) -> dict:
    thread_id, title = latest_hn_thread(client)
    resp = client.get(_HN_ITEM.format(id=thread_id))
    resp.raise_for_status()
    posts = [p for p in resp.json().get("children") or [] if p.get("text")]
    refs: list[BoardRef] = []
    manual: list[dict] = []
    matched = 0
    for p in map(parse_hn_post, posts):
        # A post usually lists several roles: match keywords against the whole
        # post and locations against it too (the header is often abbreviated).
        if not match_title(p["text"], p["text"], keywords, locations):
            continue
        if _CLOSED_RE.search(p["header"]) or _CLOSED_RE.search(p["text"][:300]):
            continue  # "UPDATE: no longer hiring"
        matched += 1
        ats_refs, other = _route(p["links"])
        refs.extend(r for r in ats_refs if r not in refs)
        if ats_refs:
            continue  # the ATS adapters take it from here
        career = _careers_link(other)
        if not (career or p["emails"]):
            continue
        manual.append({
            "source": "hn", "company": p["company"] or p["header"][:60], "title": p["header"],
            "url": career, "email": ", ".join(p["emails"][:2]),
            "source_url": f"https://news.ycombinator.com/item?id={p['id']}",
            "reason": "posted on HN Who is hiring without a supported ATS link — "
                      + ("e-mail or " if p["emails"] else "") + "apply via the link",
        })
    log(f"hn: '{title}' — {len(posts)} posts, {matched} matching, "
        f"{len(refs)} ATS link(s), {len(manual)} manual lead(s)")
    return {"refs": refs, "manual": manual, "warnings": []}


# ── remote job boards (discovery only; employer links are hidden) ────────────

def _board_jobs(name: str, label: str, jobs: list[dict], keywords, locations, log,
                reason: str = "") -> dict:
    """Shared tail for the remote boards: filter, route any ATS link found in
    the listing text, list the rest for manual apply with a link back."""
    refs: list[BoardRef] = []
    manual: list[dict] = []
    for j in jobs:
        if not match_title(j["title"], j["location"] or "remote", keywords, locations):
            continue
        # Only a link presented as how to apply for THIS listing is routed;
        # cross-promoted roles and footers leave the listing as a manual lead.
        ats_refs, _ = _route(_links(j.get("html", ""), need=_APPLY_SIGNAL)
                             + ([j["apply_url"]] if j.get("apply_url") else []))
        job_refs = [r for r in ats_refs if r.job_id] or ats_refs
        if job_refs:
            refs.extend(r for r in job_refs if r not in refs)
            continue
        manual.append({
            "source": name, "company": j["company"], "title": j["title"],
            "location": j["location"], "url": j["url"], "email": "",
            "source_url": j["url"],
            "reason": reason or (f"via {label} — the board doesn't expose the employer's "
                                 "application link; open url to apply"),
        })
    log(f"{name}: {len(jobs)} listing(s), {len(refs)} routed to an ATS, {len(manual)} manual")
    return {"refs": refs, "manual": manual, "warnings": []}


def remoteok_source(client, keywords, locations, log: Callable = noop) -> dict:
    resp = client.get(_REMOTEOK)
    resp.raise_for_status()
    data = resp.json()
    jobs = [{"title": (j.get("position") or "").strip(), "company": (j.get("company") or "").strip(),
             "location": (j.get("location") or "").strip(), "url": j.get("url") or "",
             "apply_url": j.get("apply_url") or "", "html": j.get("description") or ""}
            for j in (data[1:] if isinstance(data, list) else []) if isinstance(j, dict) and j.get("id")]
    return _board_jobs("remoteok", "Remote OK", jobs, keywords, locations, log)


_REMOTIVE_MAX_SEARCHES = 5  # be gentle with a free API


def remotive_source(client, keywords, locations, log: Callable = noop) -> dict:
    # The API takes one search term, so search each keyword and merge — a
    # single "python" search would drop the "designer" alternative before the
    # shared matcher ever sees it. No keywords: the software-dev category.
    queries = [{"search": k} for k in keywords[:_REMOTIVE_MAX_SEARCHES]] or [{"category": "software-dev"}]
    warnings = [f"remotive: searched only the first {_REMOTIVE_MAX_SEARCHES} keywords"] \
        if len(keywords) > _REMOTIVE_MAX_SEARCHES else []
    jobs: list[dict] = []
    seen: set[str] = set()
    for params in queries:
        resp = client.get(_REMOTIVE, params=params)
        resp.raise_for_status()
        for j in resp.json().get("jobs") or []:
            url = j.get("url") or ""
            if url in seen:
                continue
            seen.add(url)
            jobs.append({"title": (j.get("title") or "").strip(),
                         "company": (j.get("company_name") or "").strip(),
                         "location": (j.get("candidate_required_location") or "").strip(),
                         "url": url, "html": j.get("description") or ""})
    out = _board_jobs("remotive", "Remotive", jobs, keywords, locations, log)
    out["warnings"] = warnings
    return out


def parse_wwr_feed(xml_text: str) -> list[dict]:
    jobs = []
    for item in ET.fromstring(xml_text).iter("item"):
        title = (item.findtext("title") or "").strip()
        company, _, role = title.partition(":")
        jobs.append({"title": (role or company).strip(), "company": company.strip() if role else "",
                     "location": (item.findtext("region") or "").strip(),
                     "url": (item.findtext("link") or "").strip(),
                     "html": item.findtext("description") or ""})
    return jobs


def weworkremotely_source(client, keywords, locations, log: Callable = noop) -> dict:
    jobs: list[dict] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for feed in _WWR_FEEDS:
        try:
            resp = client.get(feed)
            resp.raise_for_status()
            for j in parse_wwr_feed(resp.text):
                if j["url"] not in seen:
                    seen.add(j["url"])
                    jobs.append(j)
        except Exception as exc:  # noqa: BLE001 — one feed down never sinks the rest
            warnings.append(f"weworkremotely: {feed.rsplit('/', 1)[-1]} unavailable ({exc})")
    out = _board_jobs("weworkremotely", "We Work Remotely", jobs, keywords, locations, log)
    out["warnings"] = warnings
    return out


# ── Seek / JobStreet (one platform, one search API) ──────────────────────────

# name → (site base URL, siteKey, label, country appended to job locations).
# JobStreet labels SG jobs "Central Region", so the country is added for the
# location filter. JobsDB HK / TH answer the API with HTTP 403 (bot wall) and
# job pages on every site are behind a challenge, so these are search-only.
SEEK_SITES = {
    "seek_au": ("https://au.seek.com", "AU-Main", "Seek AU", "Australia"),
    "seek_nz": ("https://nz.seek.com", "NZ-Main", "Seek NZ", "New Zealand"),
    "jobstreet_sg": ("https://sg.jobstreet.com", "SG-Main", "JobStreet SG", "Singapore"),
    "jobstreet_my": ("https://my.jobstreet.com", "MY-Main", "JobStreet MY", "Malaysia"),
    "jobstreet_ph": ("https://ph.jobstreet.com", "PH-Main", "JobStreet PH", "Philippines"),
    "jobstreet_id": ("https://id.jobstreet.com", "ID-Main", "JobStreet ID", "Indonesia"),
}
_SEEK_PAGE_SIZE = 50
_SEEK_MAX_SEARCHES = 5  # an undocumented API: keep the footprint small


def parse_seek_jobs(data: dict, base: str, country: str) -> list[dict]:
    jobs = []
    for j in data.get("data") or []:
        jid = str(j.get("id") or "")
        if not jid:
            continue
        labels = [loc.get("label") for loc in j.get("locations") or [] if loc.get("label")]
        where = "; ".join(labels)
        jobs.append({
            "title": (j.get("title") or "").strip(),
            "company": (j.get("companyName") or (j.get("advertiser") or {}).get("description") or "").strip(),
            "location": f"{where}, {country}" if where and country.lower() not in where.lower() else (where or country),
            "url": f"{base}/job/{jid}", "html": "",
        })
    return jobs


def _seek_source(name: str) -> Callable:
    base, site_key, label, country = SEEK_SITES[name]

    def source(client, keywords, locations, log: Callable = noop) -> dict:
        # One search per keyword (the API takes one), the first location as
        # Seek's own "where"; the shared matcher still filters what comes back.
        queries = keywords[:_SEEK_MAX_SEARCHES] or [""]
        jobs: list[dict] = []
        seen: set[str] = set()
        for kw in queries:
            params = {"siteKey": site_key, "keywords": kw, "page": 1, "pageSize": _SEEK_PAGE_SIZE}
            if locations:
                params["where"] = locations[0]
            resp = client.get(f"{base}/api/jobsearch/v5/search", params=params)
            resp.raise_for_status()
            for j in parse_seek_jobs(resp.json(), base, country):
                if j["url"] not in seen:
                    seen.add(j["url"])
                    jobs.append(j)
        out = _board_jobs(name, label, jobs, keywords, locations, log,
                          reason=f"via {label} — apply on {label.split()[0]} with your own account "
                                 "(its Quick Apply isn't automated); open url")
        if len(keywords) > _SEEK_MAX_SEARCHES:
            out["warnings"] = [f"{name}: searched only the first {_SEEK_MAX_SEARCHES} keywords"]
        return out

    source.__name__ = f"{name}_source"
    return source


SOURCES: dict[str, Callable] = {
    "hn": hn_source,
    "remoteok": remoteok_source,
    "remotive": remotive_source,
    "weworkremotely": weworkremotely_source,
    **{name: _seek_source(name) for name in SEEK_SITES},
}
_ALIASES = {"hackernews": "hn", "hacker_news": "hn", "hn_hiring": "hn", "whoishiring": "hn", "remote_ok": "remoteok",
            "wwr": "weworkremotely", "we_work_remotely": "weworkremotely",
            "seek": "seek_au", "seek_australia": "seek_au", "seek_new_zealand": "seek_nz",
            "jobstreet_singapore": "jobstreet_sg", "jobstreet_malaysia": "jobstreet_my",
            "jobstreet_philippines": "jobstreet_ph", "jobstreet_indonesia": "jobstreet_id"}


def resolve_sources(names: list[str]) -> tuple[list[str], list[str]]:
    """(known source names, unknown names) from user input."""
    known, unknown = [], []
    for n in names:
        key = re.sub(r"[\s-]+", "_", n.strip().lower())
        key = _ALIASES.get(key, key)
        if key in SOURCES:
            if key not in known:
                known.append(key)
        elif key:
            unknown.append(n)
    return known, unknown


def collect(client, names: list[str], keywords: list[str], locations: list[str],
            log: Callable = noop) -> dict:
    """Run every named source; merge refs (deduped) and manual leads (deduped
    by link / e-mail). A failing source becomes a warning, never an error."""
    known, unknown = resolve_sources(names)
    refs: list[BoardRef] = []
    manual: list[dict] = []
    warnings = [f"unknown source '{n}' (use: {', '.join(SOURCES)})" for n in unknown]
    seen_manual: set[str] = set()
    for name in known:
        try:
            out = SOURCES[name](client, keywords, locations, log)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{name}: source unavailable ({exc})")
            log(f"⚠ {name}: source unavailable ({exc})")
            continue
        refs.extend(r for r in out["refs"] if r not in refs)
        for m in out["manual"]:
            key = m.get("url") or m.get("email") or m.get("source_url")
            if key and key not in seen_manual:
                seen_manual.add(key)
                manual.append({**m, "status": "manual"})
        warnings.extend(out.get("warnings") or [])
    return {"refs": refs, "manual": manual, "warnings": warnings}
