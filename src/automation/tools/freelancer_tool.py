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

Placing bids is opt-in (``place_bid``): it needs the user's Freelancer OAuth
token (``FREELANCER_OAUTH_TOKEN``) and the flow only calls it when the run sets
``submit_bids``. Without both, the output is projects + drafted proposals for
the user to review and submit.

Scraper policy (CLAUDE.md): partial results + ``warnings``, never raise.
"""
import json
import os
import re
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime

from src.automation.tools.ats_common import expand_keywords, phrase_in

DEFAULT_BASE = "https://www.freelancer.com"
SANDBOX_BASE = "https://www.freelancer-sandbox.com"
API = DEFAULT_BASE + "/api/projects/0.1/projects/active/"
PROJECT_URL = DEFAULT_BASE + "/projects/{seo_url}"
DEFAULT_LEDGER_PATH = "data/freelancer_drafted.json"
USER_AGENT = "agent-auto-system/freelancer-bids"
PAGE = 50
SEARCH_PATH = "/api/projects/0.1/projects/active/"
MAX_SEARCHES = 5


def _noop(_m: str) -> None:
    pass


def api_base() -> str:
    """``FREELANCER_API_BASE`` (e.g. the sandbox, ``https://www.freelancer-sandbox.com``)
    or production. Tokens are locked to their environment: a sandbox token only
    works against the sandbox host. Read per call so a script can switch it."""
    return (os.getenv("FREELANCER_API_BASE", "").strip() or DEFAULT_BASE).rstrip("/")


def _url(path: str) -> str:
    return api_base() + path


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
    project_url = api_base() + "/projects/{seo_url}"
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
        "url": project_url.format(seo_url=seo),
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


def record_drafted(path: str, project: dict, status: str = "drafted") -> None:
    """``status`` "drafted" (proposal written) or "bid_placed"."""
    ledger = load_ledger(path)
    ledger[project["id"]] = {"title": project["title"], "status": status,
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
            resp = client.get(_url(SEARCH_PATH), params={"query": q, "limit": PAGE, "full_description": "true",
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
        seen = drafted.get(p["id"])
        why = (("already bid on" if isinstance(seen, dict) and seen.get("status") == "bid_placed"
                else "already drafted") + " in an earlier run (ledger)") if seen is not None else skip_reason(
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


# ── placing bids (opt-in; needs the user's OAuth token) ─────────────────────
#
# Checked against the official reference (developers.freelancer.com):
#   GET  /api/users/0.1/self/       scopes: basic, fln:user_information
#   POST /api/projects/0.1/bids/    scopes: basic, fln:project_manage
#        JSON body, required: project_id, bidder_id, amount, period, milestone_percentage
#        (optional: description, profile_id); header ``freelancer-oauth-v1: <token>``.
# Errors are ``{"status": "error", "message", "error_code": "<Service>ExceptionCodes.<X>",
# "request_id"}`` (some answers nest it as ``error: {code, detail}``); 401 = not
# authenticated, 429 = ``AuthorisationExceptionCodes.RATE_LIMITED``.

SELF_PATH = "/api/users/0.1/self/"
BIDS_PATH = "/api/projects/0.1/bids/"
SELF_API = DEFAULT_BASE + SELF_PATH
BIDS_API = DEFAULT_BASE + BIDS_PATH
MIN_PROPOSAL_CHARS = 100   # Freelancer rejects very short bid descriptions
MAX_PROPOSAL_CHARS = 1500  # and caps them
# BID_MILESTONE_INVALID: "Please fill in the milestone percentage (20 - 100 required …)".
MIN_MILESTONE_PERCENTAGE = 20
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

# Documented error codes, by what they mean for the rest of the run.
#   auth     — the token itself is bad / lacks a scope: stop bidding.
#   account  — this account can't bid right now (quota, verification,
#              restriction, rate limit): every later bid would fail too, stop.
#   duplicate— already bid on this project: not a failure, record it.
#   project  — anything else is specific to this one project: try the next.
_AUTH_CODES = ("NOT_AUTHENTICATED", "INVALID_ACCESS_TOKEN", "INVALID_TOKEN", "TOKEN_EXPIRED",
               "INSUFFICIENT_SCOPE", "INVALID_SCOPE", "PERMISSION_DENIED")
_ACCOUNT_CODES = ("BID_LIMIT_EXCEEDED", "EMAIL_VERIFICATION_REQUIRED", "RESTRICTED_FROM_BIDDING",
                  "MUST_RESPOND_TO_EXISTING_PROJECT_AWARD", "RATE_LIMITED", "ACCOUNT_LIMITED",
                  "NEGATIVE_BALANCE")
_HINTS = {
    "BID_LIMIT_EXCEEDED": "no bids left on your membership for this period",
    "EMAIL_VERIFICATION_REQUIRED": "verify your email on Freelancer.com before bidding",
    "RESTRICTED_FROM_BIDDING": "your account is restricted from bidding (take an exam or verify payment)",
    "MUST_RESPOND_TO_EXISTING_PROJECT_AWARD": "reply to your awarded projects first",
    "RATE_LIMITED": "rate limited by the API, try later",
    "PERMISSION_DENIED": "the token's user is not the bidder, or the token lacks the fln:project_manage scope",
    "NOT_AUTHENTICATED": "token missing, expired (personal tokens last 30 days) or for the other environment",
    "DUPLICATE_BID": "you have already bid on this project",
    "PROJECT_NOT_ACTIVE": "project is frozen or closed",
    "PROJECT_DELETED": "project was deleted",
    "INVALID_PROJECT": "project not found",
    "SKILL_REQUIREMENTS_NOT_MET": "add the project's skills to your profile to bid on it",
    "BID_AMOUNT_INVALID": "bid amount below the project's minimum",
    "BID_DESCRIPTION_CONTAINS_EMAIL": "proposal contains an email address",
    "BID_DESCRIPTION_INVALID_LENGTH": "proposal length rejected",
    "BID_MILESTONE_INVALID": "milestone percentage must be 20-100",
    "RESTRICTED_FROM_BIDDING_PREMIUM": "project needs more reviews, verification or a paid membership",
    "RESTRICTED_FROM_BIDDING_PREMIUM_VERIFIED": "project needs a Verified by Freelancer account",
    "UNLISTED_NOT_PREFERRED": "project is for Preferred Freelancers only",
    "NDA_NOT_SIGNED": "project needs a signed NDA",
    "BIDDER_NOT_QUALIFIED": "project needs a passed exam",
    "BIDDER_NOT_INVITED": "project is invite-only",
    "BIDDER_OUT_OF_RANGE": "local project outside your range",
}


class FreelancerAPIError(RuntimeError):
    """An error answer from the authenticated API, with its error_code."""
    def __init__(self, message: str, code: str = "", status: int = 0):
        super().__init__(message)
        self.code, self.status = code, status

    @property
    def short_code(self) -> str:
        """``ProjectExceptionCodes.BID_LIMIT_EXCEEDED`` → ``BID_LIMIT_EXCEEDED``."""
        return (self.code or "").rsplit(".", 1)[-1].upper()

    @property
    def kind(self) -> str:
        """``auth`` | ``account`` | ``duplicate`` | ``project`` (see ``_AUTH_CODES``)."""
        c = self.short_code
        if c == "DUPLICATE_BID":
            return "duplicate"
        if c in _ACCOUNT_CODES or self.status == 429:
            return "account"
        if self.status == 401 or c in _AUTH_CODES or (not c and self.status == 403):
            return "auth"
        return "project"

    @property
    def is_auth(self) -> bool:
        return self.kind == "auth"

    @property
    def stops_bidding(self) -> bool:
        """True when every later bid in the run would fail the same way."""
        return self.kind in ("auth", "account")

    @property
    def hint(self) -> str:
        return _HINTS.get(self.short_code, "")

    def __str__(self) -> str:
        msg = super().__str__()
        parts = [msg]
        if self.short_code and self.short_code not in msg:
            parts.append(f"[{self.short_code}]")
        if self.hint and self.hint.lower() not in msg.lower():
            parts.append(f"({self.hint})")
        return " ".join(parts)


def _auth(token: str) -> dict:
    return {"freelancer-oauth-v1": token, "Content-Type": "application/json"}


def _answer(resp) -> dict:
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if resp.status_code >= 400 or data.get("status") != "success":
        nested = data.get("error") if isinstance(data.get("error"), dict) else {}
        raise FreelancerAPIError(
            str(data.get("message") or nested.get("detail") or f"HTTP {resp.status_code}"),
            str(data.get("error_code") or nested.get("code") or ""), resp.status_code)
    return data.get("result") or {}


# Projections on /users/0.1/self/ that say whether the account can bid.
ACCOUNT_PARAMS = {"status": "true", "membership_details": "true", "balance_details": "true",
                  "limited_account": "true"}


def whoami(client, token: str) -> dict:
    """``{id, username}`` of the token's account (the bidder)."""
    res = _answer(client.get(_url(SELF_PATH), headers=_auth(token)))
    if not res.get("id"):
        raise FreelancerAPIError("token accepted but no user id returned")
    return {"id": int(res["id"]), "username": res.get("username") or ""}


def account_status(client, token: str) -> dict:
    """``whoami`` plus what the API exposes about the account's ability to bid:
    ``{id, username, email_verified, payment_verified, suspended, closed,
    limited_account, membership, bid_limit, bids_remaining, can_bid, blockers}``.
    Fields the API leaves out are ``None`` (``account_balances`` needs the
    ``financial:get`` permission, so ``bids_remaining`` is often unknown)."""
    res = _answer(client.get(_url(SELF_PATH), headers=_auth(token), params=ACCOUNT_PARAMS))
    if not res.get("id"):
        raise FreelancerAPIError("token accepted but no user id returned")
    st = res.get("status") if isinstance(res.get("status"), dict) else {}
    pkg = res.get("membership_package") if isinstance(res.get("membership_package"), dict) else {}
    bal = res.get("account_balances") if isinstance(res.get("account_balances"), dict) else {}
    out = {
        "id": int(res["id"]), "username": res.get("username") or "",
        "email_verified": st.get("email_verified"), "payment_verified": st.get("payment_verified"),
        "suspended": res.get("suspended"), "closed": res.get("closed"),
        "limited_account": res.get("limited_account"),
        "membership": pkg.get("name"), "bid_limit": pkg.get("bid_limit"),
        "bids_remaining": bal.get("bids_remaining"),
    }
    blockers = []
    if out["suspended"]:
        blockers.append("account suspended")
    if out["closed"]:
        blockers.append("account closed")
    if out["email_verified"] is False:
        blockers.append("email not verified (EMAIL_VERIFICATION_REQUIRED)")
    if out["bids_remaining"] is not None and out["bids_remaining"] <= 0:
        blockers.append("no bids remaining (BID_LIMIT_EXCEEDED)")
    out["blockers"] = blockers
    out["can_bid"] = not blockers
    return out


def bid_problem(project: dict, proposal: str, amount) -> str:
    """Why this bid must not be sent, or "" when it may."""
    if amount is None or amount <= 0:
        return "no budget to base a bid amount on"
    if len(proposal.strip()) < MIN_PROPOSAL_CHARS:
        return f"proposal shorter than {MIN_PROPOSAL_CHARS} characters"
    if _EMAIL_RE.search(proposal):
        # The API rejects it (BID_DESCRIPTION_CONTAINS_EMAIL); contacts aren't allowed in bids.
        return "proposal contains an email address (not allowed in bids)"
    lo, hi = project.get("budget_min"), project.get("budget_max")
    if (lo is not None and amount < lo) or (hi is not None and amount > hi):
        return f"bid {amount:g} outside the budget"
    return ""


def bid_payload(*, bidder_id: int, project: dict, amount: float, period_days: int,
                milestone_percentage: int, description: str) -> dict:
    """The exact JSON body ``place_bid`` POSTs (all documented-required fields)."""
    return {"project_id": int(project["id"]), "bidder_id": int(bidder_id), "amount": float(amount),
            "period": max(1, int(period_days)),
            "milestone_percentage": max(MIN_MILESTONE_PERCENTAGE, min(100, int(milestone_percentage))),
            "description": description.strip()[:MAX_PROPOSAL_CHARS]}


def place_bid(client, token: str, *, bidder_id: int, project: dict, amount: float,
              period_days: int, milestone_percentage: int, description: str) -> dict:
    """POST one bid. Returns ``{bid_id}``; raises FreelancerAPIError."""
    body = bid_payload(bidder_id=bidder_id, project=project, amount=amount, period_days=period_days,
                       milestone_percentage=milestone_percentage, description=description)
    res = _answer(client.post(_url(BIDS_PATH), headers=_auth(token), json=body))
    return {"bid_id": res.get("id")}
