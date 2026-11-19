"""
Freelancer.com project discovery (Phase 4 of the job-platform plan, draft-only).

The official API's active-project search answers without a login:

    GET https://www.freelancer.com/api/projects/0.1/projects/active/
        ?query=<terms>&limit=50&full_description=true&job_details=true&compact=true

Each project carries its budget (in its own currency, with an ``exchange_rate``
to USD), type (fixed / hourly), bid count and average bid, skills ("jobs") and
description. ``find_projects`` searches once per keyword, merges, and filters
deterministically (budget in USD, type, competition, keyword match, skips
projects already drafted in an earlier run); the flow then has the LLM draft a
proposal for each — the only judgement step.

Placing the bid needs the user's Freelancer OAuth token and is NOT done here:
the output is projects + drafted proposals for the user to review and submit.

Scraper policy (CLAUDE.md): partial results + ``warnings``, never raise.
"""
import json
import os
import re
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime

from src.automation.tools.ats_common import expand_keywords, phrase_in

API = "https://www.freelancer.com/api/projects/0.1/projects/active/"
PROJECT_URL = "https://www.freelancer.com/projects/{seo_url}"
DEFAULT_LEDGER_PATH = "data/freelancer_drafted.json"
USER_AGENT = "agent-auto-system/freelancer-bids"
PAGE = 50
MAX_SEARCHES = 5


def _noop(_m: str) -> None:
    pass


def to_usd(amount, exchange_rate) -> float | None:
    try:
        return round(float(amount) * float(exchange_rate), 2)
    except (TypeError, ValueError):
        return None


def normalize(p: dict) -> dict:
    """One API project → the flat shape the flow and the report use."""
    cur = p.get("currency") or {}
    budget = p.get("budget") or {}
    stats = p.get("bid_stats") or {}
    rate = cur.get("exchange_rate")
    seo = p.get("seo_url") or str(p.get("id"))
    return {
        "id": str(p.get("id")),
        "title": (p.get("title") or "").strip(),
        "type": p.get("type") or "",
        "currency": cur.get("code") or "",
        "currency_sign": cur.get("sign") or "",
        "budget_min": budget.get("minimum"),
        "budget_max": budget.get("maximum"),
        "budget_min_usd": to_usd(budget.get("minimum"), rate),
        "budget_max_usd": to_usd(budget.get("maximum"), rate),
        "bid_count": int(stats.get("bid_count") or 0),
        "bid_avg": stats.get("bid_avg"),
        "skills": [j.get("name") for j in p.get("jobs") or [] if j.get("name")],
        "description": (p.get("description") or p.get("preview_description") or "").strip(),
        "url": PROJECT_URL.format(seo_url=seo),
        "submitted_at": p.get("time_submitted"),
        "nonpublic": bool(p.get("nonpublic")),
        "status": p.get("frontend_project_status") or p.get("status") or "",
    }


def matches_keywords(project: dict, keywords: list[str]) -> bool:
    """Any keyword phrase (role shorthand expanded) in the title or a skill."""
    if not keywords:
        return True
    text = " ".join([project["title"], *project["skills"]]).lower()
    return any(phrase_in(k, text) for k in expand_keywords(keywords))


def skip_reason(project: dict, *, min_budget_usd: float, max_budget_usd: float,
                project_type: str, max_bids: int, keywords: list[str]) -> str:
    """Why a project is filtered out, or "" to keep it."""
    if project["status"] not in ("", "open", "active"):
        return f"not open ({project['status']})"
    if project["nonpublic"]:
        return "not public"
    if project_type in ("fixed", "hourly") and project["type"] != project_type:
        return f"{project['type'] or 'unknown'} project (wanted {project_type})"
    hi, lo = project["budget_max_usd"], project["budget_min_usd"]
    if min_budget_usd and (hi or lo or 0) < min_budget_usd:
        return f"budget up to ${hi or lo or 0:g} < ${min_budget_usd:g}"
    if max_budget_usd and (lo or 0) > max_budget_usd:
        return f"budget from ${lo:g} > ${max_budget_usd:g}"
    if max_bids and project["bid_count"] > max_bids:
        return f"{project['bid_count']} bids already (max {max_bids})"
    if not matches_keywords(project, keywords):
        return "title / skills don't match the keywords"
    return ""


def suggested_bid(project: dict) -> float | None:
    """A starting bid in the project's currency: the competition's average
    when it sits inside the budget, else the budget's midpoint."""
    lo, hi, avg = project["budget_min"], project["budget_max"], project["bid_avg"]
    if lo is None and hi is None:
        return round(avg) if avg else None
    lo = lo if lo is not None else hi
    hi = hi if hi is not None else lo
    if avg and lo <= avg <= hi:
        return round(avg)
    return round((lo + hi) / 2)


# ── drafted-projects ledger (don't redraft the same project every run) ──────

def load_ledger(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record_drafted(path: str, project: dict) -> None:
    ledger = load_ledger(path)
    ledger[project["id"]] = {"title": project["title"],
                             "drafted_at": datetime.now(UTC).isoformat(timespec="seconds")}
    # Write a sibling temp file and atomically swap it in: a failed or
    # interrupted write leaves the previous ledger intact, never an empty one.
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".freelancer_ledger_", suffix=".json", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(ledger, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# ── search ───────────────────────────────────────────────────────────────────

def search(client, keywords: list[str], log: Callable = _noop) -> tuple[list[dict], list[str]]:
    """Active projects for each keyword (one API search each, merged by id)."""
    warnings: list[str] = []
    queries = [k for k in keywords if k.strip()][:MAX_SEARCHES] or [""]
    if len([k for k in keywords if k.strip()]) > MAX_SEARCHES:
        warnings.append(f"searched only the first {MAX_SEARCHES} keywords")
    seen: dict[str, dict] = {}
    for q in queries:
        try:
            resp = client.get(API, params={"query": q, "limit": PAGE, "full_description": "true",
                                           "job_details": "true", "compact": "true",
                                           "sort_field": "time_updated"})
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") != "success":
                raise RuntimeError(data.get("message") or data.get("status") or "API error")
        except Exception as exc:  # noqa: BLE001 — one failed search never sinks the run
            warnings.append(f"search '{q}' failed ({exc})")
            log(f"⚠ search '{q}' failed ({exc})")
            continue
        found = []
        for raw in (data.get("result") or {}).get("projects") or []:
            try:
                found.append(normalize(raw))
            except Exception as exc:  # noqa: BLE001 — one odd record never sinks the search
                warnings.append(f"skipped malformed project {raw.get('id', '?') if isinstance(raw, dict) else '?'} "
                                f"({type(exc).__name__})")
        for p in found:
            seen.setdefault(p["id"], p)
        log(f"search '{q or '(all)'}': {len(found)} active project(s)")
    return list(seen.values()), warnings


def find_projects(*, keywords: str = "", min_budget_usd: float = 0, max_budget_usd: float = 0,
                  project_type: str = "any", max_bids: int = 0, ledger_path: str | None = None,
                  client=None, log: Callable = _noop) -> dict:
    """Search + deterministic filters. Returns ``{candidates, skipped, warnings,
    projects_found}``; candidates are newest-first."""
    import httpx
    kw = [k.strip() for k in re.split(r"[,\n;]", keywords or "") if k.strip()]
    ledger_path = ledger_path or os.getenv("FREELANCER_LEDGER", DEFAULT_LEDGER_PATH)
    owns = client is None
    client = client or httpx.Client(timeout=25, headers={"User-Agent": USER_AGENT})
    try:
        projects, warnings = search(client, kw, log)
    finally:
        if owns:
            client.close()
    drafted = load_ledger(ledger_path)
    candidates, skipped = [], []
    for p in sorted(projects, key=lambda x: x.get("submitted_at") or 0, reverse=True):
        why = "already drafted in an earlier run (ledger)" if p["id"] in drafted else skip_reason(
            p, min_budget_usd=float(min_budget_usd or 0), max_budget_usd=float(max_budget_usd or 0),
            project_type=project_type or "any", max_bids=int(max_bids or 0), keywords=kw)
        if why:
            skipped.append({**_brief(p), "reason": why})
        else:
            candidates.append(p)
    log(f"{len(projects)} project(s) found, {len(candidates)} pass the filters")
    return {"candidates": candidates, "skipped": skipped, "warnings": warnings,
            "projects_found": len(projects)}


def _brief(p: dict) -> dict:
    return {k: p[k] for k in ("id", "title", "url", "type", "currency", "budget_min", "budget_max",
                              "budget_min_usd", "budget_max_usd", "bid_count")}


def drafted_entry(p: dict, proposal: str) -> dict:
    return {**_brief(p), "skills": p["skills"][:8], "bid_avg": p["bid_avg"],
            "suggested_bid": suggested_bid(p), "proposal": proposal}
