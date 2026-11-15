"""
Discover job postings that companies publish on SmartRecruiters
(``jobs.smartrecruiters.com/<Company>``) and return them with apply links.

Discover-only by design. The Posting API is public, but the application form sits
behind DataDome bot protection, which challenges automated browsers and then
restricts the network outright. Getting through would take bot-detection evasion,
which we don't do. So this automation stops at discovery and the user applies in
their own browser. See doc/smartrecruiters_discover.md.

Funnel:
  1. Discover: ``api.smartrecruiters.com/v1/companies/<id>/postings`` (100 per
     page), newest first, filtered by title keywords (role shorthand expanded)
     and location, using the same rules as ``greenhouse_apply`` (imported from
     there, not copied). Interleaved across companies so one big employer can't
     take the whole limit.
  2. Only new: a local seen-ledger skips postings an earlier run already returned.
  3. Optional second gate: an LLM ``relevance_fn(title, meta)``. Fails open.

Scraper policy (per CLAUDE.md): partial results + ``warnings``; one bad company
never aborts the run.
"""
import json
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

from crewai.tools import BaseTool
from pydantic import BaseModel

from src.automation.tools.greenhouse_apply_tool import (
    _interleave,
    _noop,
    _off_loop,
    _phrase_in,
    _split_list,
    expand_keywords,
)

_API = "https://api.smartrecruiters.com/v1/companies"
_PAGE = 100                      # the API silently caps `limit` at 100
_POSTING_URL = "https://jobs.smartrecruiters.com/{token}/{job_id}"
_APPLY_URL = ("https://jobs.smartrecruiters.com/oneclick-ui/company/{token}"
              "/publication/{uuid}?dcr_ci={token}")
DEFAULT_LEDGER_PATH = "data/smartrecruiters_seen.json"
DEFAULT_MAX_LISTED = 1000        # postings fetched per company (BoschGroup lists 5k+)

_COMPANY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SR_HOST = "smartrecruiters.com"
_NON_COMPANY_HOSTS = ("api.", "www.")   # their first path segment is not a company
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


# ── company / posting refs ────────────────────────────────────────────────────

def parse_company_ref(ref: str) -> tuple[str, str] | None:
    """``(company, posting)`` from whatever the user pasted, or None.

    ``posting`` is ``""`` for a whole company, a numeric posting id
    (``jobs.smartrecruiters.com/ServiceNow/744000160326680-some-title``) or a
    publication uuid (``…/oneclick-ui/company/ServiceNow/publication/<uuid>``).
    The API treats company identifiers case-insensitively; the case given is kept.
    """
    ref = (ref or "").strip().strip("/")
    if not ref:
        return None
    if "://" not in ref and "/" not in ref and "." not in ref:
        return (ref, "") if _COMPANY_RE.match(ref) else None
    if "://" not in ref:
        ref = "https://" + ref
    u = urlparse(ref)
    host = u.netloc.lower()
    if not (host == _SR_HOST or host.endswith("." + _SR_HOST)) or host.startswith(_NON_COMPANY_HOSTS):
        return None
    parts = [p for p in u.path.split("/") if p]
    if parts[:2] == ["oneclick-ui", "company"] and len(parts) >= 3:
        company = parts[2]
        posting = parts[4] if len(parts) >= 5 and parts[3] == "publication" else ""
        posting = posting if _UUID_RE.match(posting) else ""
    elif parts:
        company = parts[0]
        m = re.match(r"^(\d+)", parts[1]) if len(parts) >= 2 else None
        posting = m.group(1) if m else ""
    else:
        company = (parse_qs(u.query).get("dcr_ci") or [""])[0]
        posting = ""
    if not company or not _COMPANY_RE.match(company):
        return None
    return company, posting


# ── discovery (JSON API) ──────────────────────────────────────────────────────

def _location_text(loc: dict) -> str:
    loc = loc or {}
    text = loc.get("fullLocation") or ", ".join(
        p for p in (loc.get("city"), loc.get("region"), (loc.get("country") or "").upper()) if p)
    if loc.get("remote"):
        text = f"{text} (Remote)" if text else "Remote"
    elif loc.get("hybrid"):
        text = f"{text} (Hybrid)" if text else "Hybrid"
    return text.strip()


def match_posting(job: dict, keywords: list[str], locations: list[str]) -> bool:
    """Same semantics as Greenhouse's ``match_job``: the title matches ANY keyword
    phrase (role shorthand expanded) and the location ANY substring
    ("remote" matches postings the API flags as remote)."""
    title = (job.get("title") or "").lower()
    loc = (job.get("location") or "").lower()
    if keywords and not any(_phrase_in(kw, title) for kw in expand_keywords(keywords)):
        return False
    if locations and not any(lc.lower() in loc for lc in locations):
        return False
    return True


def _label(obj) -> str:
    return ((obj or {}).get("label") or "").strip() if isinstance(obj, dict) else ""


def _to_job(token: str, p: dict) -> dict:
    jid = str(p.get("id") or "")
    uuid = str(p.get("uuid") or "")
    return {
        "token": token,
        "job_id": jid,
        "uuid": uuid,
        "title": (p.get("name") or "").strip()[:200] or jid,
        "company": ((p.get("company") or {}).get("name") or token).strip()[:120],
        "location": _location_text(p.get("location"))[:160],
        "posted": (p.get("releasedDate") or "")[:10],
        "department": _label(p.get("department"))[:80],
        "employment_type": _label(p.get("typeOfEmployment"))[:40],
        "url": _POSTING_URL.format(token=token, job_id=jid),
        "apply_url": _APPLY_URL.format(token=token, uuid=uuid) if uuid else "",
    }


def list_postings(client, token: str, max_listed: int) -> tuple[list[dict], int]:
    """A company's postings (up to ``max_listed``) and its total count."""
    out: list[dict] = []
    total = 0
    offset = 0
    while offset < max_listed:
        resp = client.get(f"{_API}/{token}/postings", params={"limit": _PAGE, "offset": offset})
        resp.raise_for_status()
        data = resp.json() or {}
        total = int(data.get("totalFound") or 0)
        page = data.get("content") or []
        out.extend(page)
        offset += len(page)
        if not page or offset >= total:
            break
    return out[:max_listed], total


def discover_jobs(client, companies: list[str], keywords: list[str], locations: list[str],
                  log: Callable, warnings: list, max_listed: int = DEFAULT_MAX_LISTED,
                  searched: list | None = None) -> tuple[list[dict], int]:
    """Matching postings across companies (newest first within a company,
    interleaved across companies), plus the total number listed. Companies that
    were actually listed are appended to ``searched`` when given."""
    groups: list[list[dict]] = []
    total = 0
    done: set[tuple[str, str]] = set()
    for ref in companies:
        parsed = parse_company_ref(ref)
        if parsed is None:
            warnings.append(f"'{ref}': not a SmartRecruiters company or URL")
            log(f"⚠ '{ref}': not a SmartRecruiters company or URL — skipped")
            continue
        token, only = parsed
        if (token.lower(), only) in done:
            continue  # company ids are case-insensitive: "ServiceNow, servicenow" is one company
        done.add((token.lower(), only))
        try:
            postings, found = list_postings(client, token, max_listed)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{token}: could not list postings ({exc})")
            log(f"⚠ {token}: could not list postings ({exc})")
            continue
        if not found:
            warnings.append(f"{token}: no open postings (or no such SmartRecruiters company)")
            log(f"⚠ {token}: no open postings (or no such company)")
            continue
        if found > len(postings) and not only:
            warnings.append(f"{token}: scanned the first {len(postings)} of {found} postings")
        total += found
        if searched is not None:
            searched.append(token)
        picked = []
        for p in postings:
            job = _to_job(token, p)
            if not job["job_id"]:
                continue
            if only:
                # A pasted posting URL is an explicit pick — filters don't apply.
                if only not in (job["job_id"], job["uuid"]):
                    continue
            elif not match_posting(job, keywords, locations):
                continue
            picked.append(job)
        if only and not picked:
            warnings.append(f"{token}: posting {only} is not open (or beyond the first {len(postings)})")
        picked.sort(key=lambda j: j["posted"], reverse=True)
        log(f"{token}: {len(picked)} matching of {found} open posting(s)")
        groups.append(picked)
    return _interleave(groups), total


# ── seen-ledger (only show new postings) ──────────────────────────────────────

def _ledger_key(job: dict) -> str:
    return f"{job['token'].lower()}/{job['job_id']}"


def load_ledger(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record_seen(path: str, jobs: list[dict]) -> None:
    if not jobs:
        return
    ledger = load_ledger(path)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    for job in jobs:
        ledger.setdefault(_ledger_key(job), {"title": job.get("title", ""),
                                             "company": job.get("company", ""), "seen_at": now})
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(ledger, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)  # atomic: a crash mid-write never corrupts the ledger


# ── orchestration ─────────────────────────────────────────────────────────────

def run_smartrecruiters_discover(
    *,
    companies: str,
    keywords: str = "",
    location: str = "",
    max_results: int = 50,
    only_new: bool = True,
    relevance_fn: Callable[[str, str], tuple[bool, str]] | None = None,
    log: Callable[[str], None] | None = None,
    ledger_path: str | None = None,
    max_listed: int | None = None,
    client=None,
) -> dict:
    """Discover matching SmartRecruiters postings. See the module docstring."""
    import httpx

    log = log or _noop
    ledger_path = ledger_path or os.getenv("SMARTRECRUITERS_LEDGER", DEFAULT_LEDGER_PATH)
    max_results = max(1, min(int(max_results), 500))
    max_listed = max_listed or int(os.getenv("SMARTRECRUITERS_MAX_LISTED", DEFAULT_MAX_LISTED))
    company_list = _split_list(companies)
    base = {"companies": company_list, "keywords": keywords, "location": location,
            "only_new": only_new, "jobs": [], "skipped": [], "warnings": []}
    if not company_list:
        return {**base, "error": "No SmartRecruiters companies given."}

    warnings: list[str] = []
    owns_client = client is None
    client = client or httpx.Client(timeout=20, follow_redirects=True,
                                    headers={"User-Agent": "agent-auto-system/smartrecruiters-discover"})
    try:
        log("Discovering SmartRecruiters jobs via the public Posting API...")
        searched: list[str] = []
        candidates, total_listed = discover_jobs(client, company_list, _split_list(keywords),
                                                 _split_list(location), log, warnings, max_listed,
                                                 searched)
        ledger = load_ledger(ledger_path) if only_new else {}
        jobs: list[dict] = []
        skipped: list[dict] = []
        seen = filtered = 0
        for job in candidates:
            if len(jobs) >= max_results:
                break
            if _ledger_key(job) in ledger:
                seen += 1
                continue
            if relevance_fn is not None:
                meta = (f"Company: {job['company']}\nLocation: {job['location']}\n"
                        f"Department: {job['department']}\nType: {job['employment_type']}")
                try:
                    keep, why = _off_loop(relevance_fn, job["title"], meta)
                except Exception as exc:  # noqa: BLE001 — the gate never drops a job by failing
                    keep, why = True, ""
                    log(f"⚠ {job['token']}/{job['job_id']}: relevance filter errored ({exc}); keeping")
                if not keep:
                    filtered += 1
                    skipped.append({**job, "reason": f"filtered out: {why}" if why else "filtered out"})
                    continue
            jobs.append(job)
        try:
            record_seen(ledger_path, jobs)
        except OSError as exc:  # results still go back; next run may repeat them
            warnings.append(f"could not update the seen-ledger {ledger_path} ({exc})")
            log(f"⚠ could not update the seen-ledger ({exc}); results are still returned")

        summary = (f"{len(jobs)} {'new ' if only_new else ''}matching posting(s) from "
                   f"{len(searched)} compan{'y' if len(searched) == 1 else 'ies'} "
                   f"({total_listed} listed, {len(candidates)} matched")
        summary += f", {seen} already seen" if seen else ""
        summary += f", {filtered} filtered by task_filter" if filtered else ""
        summary += "). Open apply_url to apply in your own browser."
        log(summary)
        # Summary and counts lead: the LLM judge reads only the first few KB.
        return {
            "summary": summary,
            "jobs_found": len(jobs),
            "total_listed": total_listed,
            "matched_count": len(candidates),
            "already_seen_count": seen,
            "filtered_count": filtered,
            **base,
            "jobs": jobs,
            "skipped": skipped[:100],
            "warnings": warnings[:50],
        }
    finally:
        if owns_client:
            client.close()


# ── BaseTool wrapper (no LLM gate; catalog/agent parity) ──────────────────────

class SmartRecruitersDiscoverInput(BaseModel):
    companies: str
    keywords: str = ""
    location: str = ""
    max_results: int = 50
    only_new: bool = True


class SmartRecruitersDiscoverTool(BaseTool):
    name: str = "smartrecruiters_discover"
    description: str = (
        "Find job postings on SmartRecruiters. Args: companies (comma-separated "
        "company identifiers or jobs.smartrecruiters.com URLs), keywords (title "
        "filter), location, max_results, only_new. Uses the public Posting API and "
        "returns each posting with its posting URL and apply URL; it never applies."
    )
    args_schema: type[BaseModel] = SmartRecruitersDiscoverInput

    def _run(self, companies: str, keywords: str = "", location: str = "",
             max_results: int = 50, only_new: bool = True) -> dict:
        try:
            return run_smartrecruiters_discover(companies=companies, keywords=keywords,
                                                location=location, max_results=max_results,
                                                only_new=only_new)
        except Exception as exc:  # noqa: BLE001
            return {"companies": companies, "jobs": [], "skipped": [], "warnings": [],
                    "error": f"{type(exc).__name__}: {exc}"}
