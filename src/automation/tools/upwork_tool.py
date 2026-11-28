"""
Upwork job discovery for ``upwork_drafts`` (Phase 4 of the job-platform plan).

**Draft-only, by design.** This module can *read* Upwork jobs; it has no code
path that submits a proposal or spends Connects. Every GraphQL document goes
through :func:`graphql`, which refuses anything that is not a ``query``
operation (``assert_read_only``), so even a future edit can't slip a
``createJobProposal`` mutation through it. The human opens each job's ``url``
and submits the drafted proposal themselves.

Two discovery sources, both read-only:

``api``
    Upwork's official GraphQL API (``POST https://api.upwork.com/graphql``),
    query ``marketplaceJobPostingsSearch`` (scope "Read marketplace Job
    Postings" + "Common Entities - Read-Only Access"). Needs the user's OAuth2
    access token — Upwork has no anonymous job search any more (the job RSS
    feeds were switched off on 2024-08-20, and every public job page answers a
    plain HTTP client with a Cloudflare 403). Tokens come from
    ``data/upwork_token.json`` (written by ``scripts/upwork_oauth.py``) or the
    ``UPWORK_ACCESS_TOKEN`` env var; with ``UPWORK_CLIENT_ID`` /
    ``UPWORK_CLIENT_SECRET`` and a refresh token an expired access token (24 h
    TTL) is refreshed once per run.

``pasted``
    Job postings the user pasted (copied from Upwork or from its job-alert
    e-mails), separated by ``---`` lines. No network at all — works without API
    access, and is the deliberate fallback instead of scraping a logged-in
    browser session (Upwork's ToS forbid scraping and its bot detection bans
    accounts; the point of this job type is to *not* risk the account).

``find_jobs`` merges both, filters deterministically (type, budget, rate,
competition, payment-verified client, keyword match, already applied, already
drafted), ranks the rest with a transparent score and returns partial results
plus ``warnings`` — it never raises.
"""
import hashlib
import json
import os
import re
import tempfile
import time
from collections.abc import Callable
from datetime import datetime

from src.automation.tools import freelancer_tool as _ledger  # shared drafted-ledger helpers
from src.automation.tools.ats_common import expand_keywords, phrase_in

GRAPHQL_URL = "https://api.upwork.com/graphql"
TOKEN_URL = "https://www.upwork.com/api/v3/oauth2/token"
AUTHORIZE_URL = "https://www.upwork.com/ab/account-security/oauth2/authorize"
JOB_URL = "https://www.upwork.com/jobs/{ciphertext}"
DEFAULT_LEDGER_PATH = "data/upwork_drafted.json"
DEFAULT_TOKEN_FILE = "data/upwork_token.json"
USER_AGENT = "agent-auto-system/upwork-drafts"
PAGE = 50
MAX_SEARCHES = 5
PAUSE_SECS = 2.0  # between API searches; Upwork allows 300 req/min per IP, we send ≤ 5 per run

# Node fields documented for MarketplaceJobPostingSearchResult. The minimal set
# (the docs' own "real-life example") is retried when the token's scopes don't
# cover one of the richer fields.
_FULL_FIELDS = """
        id
        title
        description
        ciphertext
        publishedDateTime
        createdDateTime
        totalApplicants
        experienceLevel
        durationLabel
        applied
        hourlyBudgetType
        amount { rawValue currency }
        hourlyBudgetMin { rawValue currency }
        hourlyBudgetMax { rawValue currency }
        skills { name prettyName }
        client {
          totalHires
          totalPostedJobs
          totalReviews
          totalFeedback
          verificationStatus
          totalSpent { rawValue currency }
          location { country }
        }
"""
_MIN_FIELDS = """
        id
        title
        description
        ciphertext
        amount { rawValue currency }
        skills { name }
        client { totalFeedback }
"""
_SEARCH_TEMPLATE = """query upworkDraftsJobSearch($filter: MarketplaceJobPostingsSearchFilter) {
  marketplaceJobPostingsSearch(
    marketPlaceJobFilter: $filter
    searchType: USER_JOBS_SEARCH
    sortAttributes: [{ field: RECENCY }]
  ) {
    totalCount
    edges {
      node {%s      }
    }
  }
}"""
SEARCH_QUERY = _SEARCH_TEMPLATE % _FULL_FIELDS
SEARCH_QUERY_MINIMAL = _SEARCH_TEMPLATE % _MIN_FIELDS

_SCOPE_ERR = re.compile(r"scope|permission|not authori[sz]ed to access|cannot query field", re.I)
_AUTH_ERR = re.compile(r"authenticat|expired|invalid.*token|unauthori[sz]ed", re.I)


def _noop(_m: str) -> None:
    pass


# ── the read-only guard ──────────────────────────────────────────────────────

class ReadOnlyViolation(RuntimeError):
    """Raised when anything tries to send a non-query GraphQL operation."""


def assert_read_only(document: str) -> None:
    """Refuse every GraphQL document that isn't a plain ``query`` — proposals
    (``createJobProposal``) and every other write are mutations, so this is the
    single choke point that keeps the job type draft-only."""
    body = re.sub(r"#[^\n]*", "", document or "").strip()
    if not body.startswith("query") or re.search(r"\b(mutation|subscription)\b", body, re.I) \
            or re.search(r"proposal", body, re.I):
        raise ReadOnlyViolation("upwork_drafts only sends read-only GraphQL queries "
                                "(no proposals, no Connects)")


# ── credentials ──────────────────────────────────────────────────────────────

class UpworkAuth:
    """The user's OAuth2 tokens. Never logs or returns the token values."""

    def __init__(self, access_token: str = "", refresh_token: str = "", client_id: str = "",
                 client_secret: str = "", tenant_id: str = "", token_file: str = ""):
        self.access_token, self.refresh_token = access_token, refresh_token
        self.client_id, self.client_secret = client_id, client_secret
        self.tenant_id, self.token_file = tenant_id, token_file
        self.refreshed = False

    @classmethod
    def from_env(cls) -> "UpworkAuth":
        path = os.getenv("UPWORK_TOKEN_FILE", DEFAULT_TOKEN_FILE)
        saved: dict = {}
        try:
            with open(path, encoding="utf-8") as f:
                saved = json.load(f) or {}
        except (OSError, ValueError):
            saved = {}
        if not isinstance(saved, dict):
            saved = {}
        return cls(access_token=saved.get("access_token") or os.getenv("UPWORK_ACCESS_TOKEN", "").strip(),
                   refresh_token=saved.get("refresh_token") or os.getenv("UPWORK_REFRESH_TOKEN", "").strip(),
                   client_id=os.getenv("UPWORK_CLIENT_ID", "").strip(),
                   client_secret=os.getenv("UPWORK_CLIENT_SECRET", "").strip(),
                   tenant_id=os.getenv("UPWORK_TENANT_ID", "").strip(), token_file=path)

    @property
    def configured(self) -> bool:
        return bool(self.access_token or self.can_refresh)

    @property
    def can_refresh(self) -> bool:
        return bool(self.refresh_token and self.client_id and self.client_secret)

    def headers(self) -> dict:
        h = {"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json",
             "User-Agent": USER_AGENT}
        if self.tenant_id:
            h["X-Upwork-API-TenantId"] = self.tenant_id
        return h

    def refresh(self, client) -> None:
        """Refresh Token Grant (RFC 6749 §6). Raises on failure."""
        resp = client.post(TOKEN_URL, data={"grant_type": "refresh_token", "refresh_token": self.refresh_token,
                                            "client_id": self.client_id, "client_secret": self.client_secret})
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code >= 400 or not data.get("access_token"):
            raise RuntimeError(f"token refresh failed (HTTP {resp.status_code})")
        self.access_token = data["access_token"]
        self.refresh_token = data.get("refresh_token") or self.refresh_token
        self.refreshed = True
        save_tokens(self.token_file, data, fallback_refresh=self.refresh_token)


def save_tokens(path: str, data: dict, fallback_refresh: str = "") -> None:
    """Persist a token answer to the git-ignored token file (atomic, 0600)."""
    if not path:
        return
    out = {"access_token": data.get("access_token", ""),
           "refresh_token": data.get("refresh_token") or fallback_refresh,
           "expires_at": int(time.time()) + int(data.get("expires_in") or 86400)}
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".upwork_token_", suffix=".json", dir=folder)
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(out, f)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# ── GraphQL ──────────────────────────────────────────────────────────────────

class UpworkAPIError(RuntimeError):
    def __init__(self, message: str, *, auth: bool = False, scope: bool = False):
        super().__init__(message)
        self.auth, self.scope = auth, scope


def graphql(client, auth: UpworkAuth, document: str, variables: dict) -> dict:
    """POST one read-only query; returns ``data``. Raises UpworkAPIError."""
    assert_read_only(document)
    resp = client.post(GRAPHQL_URL, headers=auth.headers(), json={"query": document, "variables": variables})
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code in (401, 403):
        msg = (body.get("message") if isinstance(body, dict) else "") or f"HTTP {resp.status_code}"
        raise UpworkAPIError(f"HTTP {resp.status_code}: {msg}", auth=True)
    if resp.status_code == 429:
        raise UpworkAPIError("rate limited (HTTP 429)")
    if resp.status_code >= 400:
        raise UpworkAPIError(f"HTTP {resp.status_code}")
    errors = body.get("errors") if isinstance(body, dict) else None
    if errors:
        msg = "; ".join(str(e.get("message") if isinstance(e, dict) else e) for e in errors)[:300]
        if not (body.get("data") or {}).get("marketplaceJobPostingsSearch"):
            raise UpworkAPIError(msg, auth=bool(_AUTH_ERR.search(msg)) and not _SCOPE_ERR.search(msg),
                                 scope=bool(_SCOPE_ERR.search(msg)))
    return (body.get("data") or {}) if isinstance(body, dict) else {}


# ── normalising ──────────────────────────────────────────────────────────────

def _money(m) -> float | None:
    if not isinstance(m, dict):
        return None
    try:
        v = float(m.get("rawValue"))
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _ts(value) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def normalize(node: dict) -> dict:
    """One ``marketplaceJobPostingsSearch`` node → the flat shape the flow uses."""
    client = node.get("client") or {}
    hmin, hmax = _money(node.get("hourlyBudgetMin")), _money(node.get("hourlyBudgetMax"))
    fixed = _money(node.get("amount"))
    if hmin or hmax or node.get("hourlyBudgetType"):
        jtype = "hourly"
    elif fixed:
        jtype = "fixed"
    else:
        jtype = ""
    status = client.get("verificationStatus")
    cipher = node.get("ciphertext") or ""
    return {
        "id": str(node.get("id") or cipher),
        "ciphertext": cipher,
        "title": (node.get("title") or "").strip(),
        "description": (node.get("description") or "").strip(),
        "url": JOB_URL.format(ciphertext=cipher) if cipher else "",
        "type": jtype,
        "fixed_budget": fixed if jtype == "fixed" else None,
        "hourly_min": hmin,
        "hourly_max": hmax,
        "currency": ((node.get("amount") or node.get("hourlyBudgetMax") or {}).get("currency") or "USD"),
        "applicants": node.get("totalApplicants"),
        "experience_level": node.get("experienceLevel") or "",
        "duration": node.get("durationLabel") or "",
        "skills": [s.get("prettyName") or s.get("name") for s in node.get("skills") or []
                   if isinstance(s, dict) and (s.get("prettyName") or s.get("name"))],
        "client": {
            "payment_verified": None if status in (None, "UNKNOWN") else status == "VERIFIED",
            "hires": client.get("totalHires"),
            "posted_jobs": client.get("totalPostedJobs"),
            "reviews": client.get("totalReviews"),
            "feedback": client.get("totalFeedback"),
            "spent": _money(client.get("totalSpent")),
            "country": (client.get("location") or {}).get("country") or "",
        },
        "published_at": node.get("publishedDateTime") or node.get("createdDateTime") or "",
        "applied": bool(node.get("applied")),
        "source": "api",
    }


# ── pasted postings (no network) ─────────────────────────────────────────────

_URL_RE = re.compile(r"https?://(?:www\.)?upwork\.com/(?:jobs|freelance-jobs/apply)/[^\s)>\]\"']+", re.I)
_CIPHER_RE = re.compile(r"~0[0-9a-z]{6,}", re.I)
_SEP_RE = re.compile(r"^\s*(?:-{3,}|={3,})\s*$", re.M)
_NUM = r"\$\s?([\d,]+(?:\.\d+)?)"
_HOURLY_RE = re.compile(r"(?:hourly[^$\n]{0,20})?" + _NUM + r"\s*(?:-|–|to)\s*" + _NUM + r"\s*(?:/\s?h(?:ou)?r|per hour)?",
                        re.I)
_FIXED_RE = re.compile(r"(?:fixed[- ]price|budget|est\. budget)[^$\n]{0,20}" + _NUM, re.I)
_PROPOSALS_RE = re.compile(r"proposals?\s*:?\s*(less than\s*)?(\d+)\s*(?:to\s*(\d+)|\+)?", re.I)


def _f(s: str) -> float:
    return float(s.replace(",", ""))


def _split_pasted(text: str) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if _SEP_RE.search(text):
        return [b.strip() for b in _SEP_RE.split(text) if b.strip()]
    # No separators: one block per Upwork job URL line (when there are several).
    lines = text.splitlines()
    starts = [i for i, ln in enumerate(lines) if _URL_RE.search(ln)]
    if len(starts) <= 1:
        return [text]
    starts[0] = 0
    return ["\n".join(lines[a:b]).strip() for a, b in zip(starts, [*starts[1:], len(lines)], strict=True)]


def parse_pasted_block(block: str) -> dict:
    """Best-effort parse of one pasted posting; unknown values stay None."""
    m = _URL_RE.search(block)
    url = m.group(0).rstrip(".,;") if m else ""
    cm = _CIPHER_RE.search(url) or _CIPHER_RE.search(block)
    cipher = cm.group(0) if cm else ""
    lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
    title = next((re.sub(r"^(title|job)\s*:\s*", "", ln, flags=re.I) for ln in lines
                  if not _URL_RE.search(ln)), "")[:200]
    body = "\n".join(ln for ln in lines if not _URL_RE.fullmatch(ln) and ln != title).strip()
    hourly = _HOURLY_RE.search(block)
    hourly_hit = bool(hourly) and bool(re.search(r"hourly|/\s?h(?:ou)?r|per hour", block, re.I))
    fixed = _FIXED_RE.search(block)
    if hourly_hit:
        jtype, hmin, hmax, fixed_budget = "hourly", _f(hourly.group(1)), _f(hourly.group(2)), None
    elif fixed or re.search(r"fixed[- ]price", block, re.I):
        jtype, hmin, hmax = "fixed", None, None
        fixed_budget = _f(fixed.group(1)) if fixed else None
    else:
        jtype, hmin, hmax, fixed_budget = "", None, None, None
    applicants = None
    p = _PROPOSALS_RE.search(block)
    if p:
        lo, hi = int(p.group(2)), p.group(3)
        applicants = (lo - 1 if p.group(1) else int(hi) if hi else lo)
    verified = True if re.search(r"payment (method )?verified", block, re.I) else (
        False if re.search(r"payment (method )?(un|not )verified", block, re.I) else None)
    jid = cipher or ("pasted-" + hashlib.sha1((url or title or block).encode()).hexdigest()[:12])
    return {
        "id": jid, "ciphertext": cipher, "title": title, "description": body[:6000],
        "url": url or (JOB_URL.format(ciphertext=cipher) if cipher else ""),
        "type": jtype, "fixed_budget": fixed_budget, "hourly_min": hmin, "hourly_max": hmax,
        "currency": "USD", "applicants": applicants, "experience_level": "", "duration": "",
        "skills": [], "client": {"payment_verified": verified, "hires": None, "posted_jobs": None,
                                 "reviews": None, "feedback": None, "spent": None, "country": ""},
        "published_at": "", "applied": False, "source": "pasted",
    }


def parse_pasted(text: str) -> tuple[list[dict], list[str]]:
    jobs, warnings = [], []
    for i, block in enumerate(_split_pasted(text), 1):
        try:
            job = parse_pasted_block(block)
        except Exception as exc:  # noqa: BLE001 — one odd block never sinks the run
            warnings.append(f"pasted job #{i} could not be read ({type(exc).__name__})")
            continue
        if not job["title"]:
            warnings.append(f"pasted job #{i} has no title; skipped")
            continue
        if not job["url"]:
            warnings.append(f"pasted job #{i} ('{job['title'][:40]}') has no Upwork link; "
                            "you'll need to find it to submit")
        jobs.append(job)
    return jobs, warnings


# ── filters, ranking, suggested rate ─────────────────────────────────────────

def matches_keywords(job: dict, keywords: list[str]) -> bool:
    if not keywords:
        return True
    text = " ".join([job["title"], *job["skills"]]).lower()
    return any(phrase_in(k, text) for k in expand_keywords(keywords))


def skip_reason(job: dict, *, job_type: str = "any", min_fixed_budget: float = 0,
                min_hourly_rate: float = 0, max_applicants: int = 0,
                payment_verified_only: bool = False, keywords: list[str] | None = None) -> str:
    """Why a job is filtered out, or "" to keep it. Unknown values pass."""
    if job.get("applied"):
        return "already applied on Upwork"
    if job_type in ("fixed", "hourly") and job["type"] and job["type"] != job_type:
        return f"{job['type']} job (wanted {job_type})"
    if min_fixed_budget and job["type"] == "fixed" and job["fixed_budget"] is not None \
            and job["fixed_budget"] < min_fixed_budget:
        return f"fixed budget ${job['fixed_budget']:g} < ${min_fixed_budget:g}"
    if min_hourly_rate and job["type"] == "hourly":
        top = job["hourly_max"] or job["hourly_min"]
        if top is not None and top < min_hourly_rate:
            return f"hourly rate up to ${top:g} < ${min_hourly_rate:g}"
    if max_applicants and job["applicants"] is not None and job["applicants"] > max_applicants:
        return f"{job['applicants']} proposals already (max {max_applicants})"
    if payment_verified_only and job["client"]["payment_verified"] is False:
        return "client's payment method is not verified"
    # Pasted jobs were picked by the user, so the keyword match applies to API hits only.
    if job.get("source") == "api" and not matches_keywords(job, keywords or []):
        return "title / skills don't match the keywords"
    return ""


def rank(job: dict, keywords: list[str], now: float | None = None) -> tuple[int, list[str]]:
    """A transparent 0–100 fit score plus the reasons that moved it."""
    score, why = 40, []
    kws = expand_keywords(keywords or [])
    title = job["title"].lower()
    if kws and any(phrase_in(k, title) for k in kws):
        score += 20
        why.append("keyword in title")
    elif kws and any(phrase_in(k, " ".join(job["skills"]).lower()) for k in kws):
        score += 10
        why.append("keyword in skills")
    c = job["client"]
    if c["payment_verified"] is True:
        score += 10
        why.append("payment verified")
    elif c["payment_verified"] is False:
        score -= 15
        why.append("payment not verified")
    if (c["hires"] or 0) > 0:
        score += 5
        why.append(f"client has {c['hires']} hire(s)")
    if c["feedback"] and (c["reviews"] or 0) > 0 and c["feedback"] >= 4.5:
        score += 5
        why.append(f"client rated {c['feedback']:.1f}")
    n = job["applicants"]
    if n is not None:
        bump = 15 if n < 5 else 10 if n < 10 else 5 if n < 20 else -10 if n >= 50 else 0
        score += bump
        why.append(f"{n} proposal(s) so far")
    published = _ts(job.get("published_at"))
    if published:
        age_h = ((now or time.time()) - published) / 3600
        if age_h <= 24:
            score += 10
            why.append("posted in the last 24 h")
        elif age_h <= 72:
            score += 5
            why.append("posted in the last 3 days")
    return max(0, min(100, score)), why


def suggested_rate(job: dict, hourly_rate: float = 0) -> dict | None:
    """What to enter on the proposal: ``{amount, unit, basis}`` or None."""
    if job["type"] == "fixed":
        if job["fixed_budget"]:
            return {"amount": round(job["fixed_budget"]), "unit": "fixed", "basis": "the client's fixed budget"}
        return None
    if job["type"] != "hourly" and not hourly_rate:
        return None
    lo, hi = job["hourly_min"], job["hourly_max"]
    if hourly_rate:
        basis = "your hourly rate"
        if hi and hourly_rate > hi:
            basis += f" (above the client's ${hi:g}/hr max)"
        elif lo and hourly_rate < lo:
            basis += f" (below the client's ${lo:g}/hr minimum — consider raising it)"
        return {"amount": round(hourly_rate, 2), "unit": "hour", "basis": basis}
    if lo or hi:
        mid = ((lo or hi) + (hi or lo)) / 2
        return {"amount": round(mid, 2), "unit": "hour", "basis": "middle of the client's hourly range"}
    return None


def budget_text(job: dict) -> str:
    if job["type"] == "fixed":
        return f"fixed price, ${job['fixed_budget']:g}" if job["fixed_budget"] else "fixed price, budget not stated"
    if job["type"] == "hourly":
        lo, hi = job["hourly_min"], job["hourly_max"]
        if lo and hi:
            return f"hourly, ${lo:g}–${hi:g}/hr"
        if lo or hi:
            return f"hourly, ${lo or hi:g}/hr"
        return "hourly, rate not stated"
    return "budget not stated"


# ── search + funnel ──────────────────────────────────────────────────────────

def search(client, auth: UpworkAuth, keywords: list[str], *, log: Callable = _noop,
           pause: float = PAUSE_SECS) -> tuple[list[dict], list[str]]:
    """One API search per keyword (max 5), merged by id. Never raises."""
    warnings: list[str] = []
    queries = [k for k in keywords if k.strip()]
    if len(queries) > MAX_SEARCHES:
        warnings.append(f"searched only the first {MAX_SEARCHES} keywords")
    seen: dict[str, dict] = {}
    document = SEARCH_QUERY
    for i, q in enumerate(queries[:MAX_SEARCHES]):
        if i and pause:
            time.sleep(pause)
        variables = {"filter": {"searchExpression_eq": q, "pagination_eq": {"after": "0", "first": PAGE}}}
        data = None
        for _attempt in range(3):
            try:
                data = graphql(client, auth, document, variables)
                break
            except UpworkAPIError as exc:
                if exc.scope and document is SEARCH_QUERY:
                    warnings.append(f"the token's scopes don't cover every job field ({exc}); "
                                    "using the minimal field set")
                    document = SEARCH_QUERY_MINIMAL
                    continue
                if exc.auth and auth.can_refresh and not auth.refreshed:
                    try:
                        auth.refresh(client)
                        log("Upwork access token refreshed.")
                        continue
                    except Exception as rexc:  # noqa: BLE001
                        warnings.append(f"Upwork token refresh failed ({rexc})")
                if exc.auth:
                    warnings.append(f"Upwork API rejected the token ({exc}) — run "
                                    "scripts/upwork_oauth.py to authorize again")
                    return list(seen.values()), warnings
                warnings.append(f"search '{q}' failed ({exc})")
                break
            except ReadOnlyViolation:
                raise  # a programming error, never a warning
            except Exception as exc:  # noqa: BLE001 — network etc.; one search never sinks the run
                warnings.append(f"search '{q}' failed ({type(exc).__name__}: {str(exc)[:120]})")
                break
        if data is None:
            continue
        edges = ((data.get("marketplaceJobPostingsSearch") or {}).get("edges")) or []
        found = 0
        for e in edges:
            node = (e or {}).get("node") if isinstance(e, dict) else None
            if not isinstance(node, dict):
                continue
            try:
                job = normalize(node)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"skipped malformed job {node.get('id', '?')} ({type(exc).__name__})")
                continue
            seen.setdefault(job["id"], job)
            found += 1
        log(f"search '{q}': {found} job(s)")
    return list(seen.values()), warnings


def find_jobs(*, keywords: str = "", jobs_text: str = "", job_type: str = "any",
              min_fixed_budget: float = 0, min_hourly_rate: float = 0, max_applicants: int = 0,
              payment_verified_only: bool = False, ledger_path: str | None = None,
              auth: UpworkAuth | None = None, client=None, log: Callable = _noop,
              pause: float = PAUSE_SECS) -> dict:
    """Discover + filter + rank. Returns ``{candidates, skipped, warnings,
    jobs_found, sources}``; candidates are best-ranked first. Never raises."""
    kw = [k.strip() for k in re.split(r"[,\n;]", keywords or "") if k.strip()]
    ledger_path = ledger_path or os.getenv("UPWORK_LEDGER", DEFAULT_LEDGER_PATH)
    warnings: list[str] = []
    jobs: list[dict] = []
    sources: list[str] = []

    if jobs_text.strip():
        pasted, w = parse_pasted(jobs_text)
        jobs += pasted
        warnings += w
        sources.append("pasted")
        log(f"{len(pasted)} pasted job(s) read")

    if kw:
        auth = auth or UpworkAuth.from_env()
        if not auth.configured:
            warnings.append("Upwork API not configured (no UPWORK_ACCESS_TOKEN / data/upwork_token.json) — "
                            "keyword search skipped. Run scripts/upwork_oauth.py, or paste jobs instead")
            log("Upwork API not configured; keyword search skipped.")
        else:
            import httpx
            owns = client is None
            client = client or httpx.Client(timeout=30)
            try:
                if not auth.access_token and auth.can_refresh:
                    try:
                        auth.refresh(client)
                    except Exception as exc:  # noqa: BLE001
                        warnings.append(f"Upwork token refresh failed ({exc})")
                if auth.access_token:
                    found, w = search(client, auth, kw, log=log, pause=pause)
                    jobs += found
                    warnings += w
                    sources.append("api")
            finally:
                if owns:
                    client.close()

    # De-dup across sources (a pasted job the API also returned keeps the API copy).
    by_id: dict[str, dict] = {}
    for j in jobs:
        if j["id"] not in by_id or j["source"] == "api":
            by_id[j["id"]] = j
    jobs = list(by_id.values())

    drafted = _ledger.load_ledger(ledger_path)
    candidates, skipped = [], []
    now = time.time()
    for j in jobs:
        why = "already drafted in an earlier run (ledger)" if j["id"] in drafted else skip_reason(
            j, job_type=job_type or "any", min_fixed_budget=float(min_fixed_budget or 0),
            min_hourly_rate=float(min_hourly_rate or 0), max_applicants=int(max_applicants or 0),
            payment_verified_only=bool(payment_verified_only), keywords=kw)
        if why:
            skipped.append({**brief(j), "reason": why})
            continue
        score, reasons = rank(j, kw, now)
        candidates.append({**j, "rank_score": score, "rank_reasons": reasons})
    candidates.sort(key=lambda c: (c["rank_score"], _ts(c.get("published_at"))), reverse=True)
    log(f"{len(jobs)} job(s) found, {len(candidates)} pass the filters")
    return {"candidates": candidates, "skipped": skipped, "warnings": warnings,
            "jobs_found": len(jobs), "sources": sources}


def brief(j: dict) -> dict:
    return {"id": j["id"], "title": j["title"], "url": j["url"], "type": j["type"],
            "budget": budget_text(j), "applicants": j["applicants"], "source": j["source"]}


def drafted_entry(j: dict, proposal: str, rank_no: int, hourly_rate: float = 0) -> dict:
    return {"rank": rank_no, "rank_score": j.get("rank_score"), "rank_reasons": j.get("rank_reasons", []),
            **brief(j), "skills": j["skills"][:8], "client": j["client"],
            "suggested_rate": suggested_rate(j, hourly_rate), "proposal": proposal,
            "status": "draft — not submitted"}


def record_drafted(path: str, job: dict) -> None:
    """Remember the job id only (Upwork's API terms forbid keeping its data > 24 h)."""
    _ledger.record_drafted(path, {"id": job["id"], "title": ""}, "drafted")
