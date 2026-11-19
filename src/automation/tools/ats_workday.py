"""Workday adapter — discovery only.

Workday is the largest pool of employer job boards (about 1,300 distinct
boards in the SimplifyJobs listings alone), and its job list is public:

    POST https://<tenant>.<wdN>.myworkdayjobs.com/wday/cxs/<tenant>/<site>/jobs
    {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "..."}

→ ``{"total": N, "jobPostings": [{title, externalPath, locationsText, …}]}``.
An unknown site answers 404, an unknown tenant 422, a closed board 410 and a
disabled one 403; only 429 means throttling. Pages hold at most 20,
so instead of listing a big board (Northrop Grumman has 3,000+ jobs) the run's
keywords are sent as ``searchText``.

Workday throttles hard and across tenants: quick requests to *different*
companies get HTTP 429, with no Retry-After header. Requests are paced and a
429 backs off (5 s, 10 s, 20 s, 40 s) before giving up with the HTTP error, so
the board scanner can pause too.

Applying is NOT automated: every employer needs its own Workday candidate
account (sign-up, e-mail verification, a 5–7 step wizard). Matching jobs are
returned under ``manual`` with their link.
"""
import time

from src.automation.tools.ats_apply_tool import Adapter
from src.automation.tools.ats_router import BoardRef

PAGE = 20                  # Workday's maximum page size
MAX_PAGES_LISTING = 10     # no search terms: the first 200 jobs
MAX_PAGES_PER_TERM = 5     # per search term: up to 100 jobs
MAX_TERMS = 3
PACE_S = 1.0               # between page requests
BACKOFF_S = (5, 10, 20, 40)


def split_token(token: str) -> tuple[str, str, str]:
    """``"ngc.wd1/Site"`` → ``("ngc", "wd1", "Site")``."""
    host, _, site = token.partition("/")
    tenant, _, wd = host.rpartition(".")
    if not (tenant and wd and site):
        raise ValueError(f"not a Workday board token: {token!r} (want <tenant>.<wdN>/<site>)")
    return tenant, wd, site


class WorkdayAdapter(Adapter):
    platform = "workday"
    label = "Workday"
    can_apply = False
    manual_reason = ("every employer needs its own Workday candidate account, so applying "
                     "isn't automated — open apply_url to apply by hand")

    def __init__(self, sleep=time.sleep):
        self._sleep = sleep

    def _post(self, client, url: str, body: dict) -> dict:
        for wait in (*BACKOFF_S, None):
            resp = client.post(url, json=body)
            # Workday throttles with 429 only. 404 (unknown site), 422 (unknown
            # tenant), 410 (closed board) and 403 (a disabled / locked site)
            # are this board's answer, not a rate limit — never wait on them.
            if resp.status_code in (403, 404, 410, 422):
                raise RuntimeError(f"no such Workday site (HTTP {resp.status_code})")
            if resp.status_code == 429 and wait is not None:
                self._sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        raise AssertionError("unreachable")

    def discover(self, client, ref: BoardRef, search: tuple[str, ...] = ()) -> list[dict]:
        tenant, wd, site = split_token(ref.company)
        api = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
        base = f"https://{tenant}.{wd}.myworkdayjobs.com/{site}"
        if ref.job_id:
            # One job: search its requisition id ("…_R10225937-1" → "R10225937-1").
            terms, max_pages = [ref.job_id.rsplit("_", 1)[-1]], 1
        elif search:
            terms, max_pages = [t for t in search if t.strip()][:MAX_TERMS], MAX_PAGES_PER_TERM
        else:
            terms, max_pages = [""], MAX_PAGES_LISTING
        jobs: dict[str, dict] = {}
        first = True
        for term in terms:
            for page in range(max_pages):
                if not first:
                    self._sleep(PACE_S)
                first = False
                data = self._post(client, api, {"appliedFacets": {}, "limit": PAGE,
                                                "offset": page * PAGE, "searchText": term})
                postings = data.get("jobPostings") or []
                for p in postings:
                    path = (p.get("externalPath") or "").rstrip("/")
                    jid = path.rsplit("/", 1)[-1]
                    if not jid or jid in jobs:
                        continue
                    url = base + path
                    jobs[jid] = {
                        "platform": self.platform, "token": ref.company, "job_id": jid,
                        "title": (p.get("title") or "").strip()[:200] or jid,
                        "company": tenant, "location": (p.get("locationsText") or "").strip()[:160],
                        "url": url, "apply_url": url,
                    }
                if len(postings) < PAGE or (page + 1) * PAGE >= int(data.get("total") or 0):
                    break
        return list(jobs.values())

    # Never reached (can_apply is False); listed for the Adapter contract.
    def load_form(self, client, job):  # pragma: no cover
        raise NotImplementedError("Workday applications are manual")

    def plan(self, form, job, profile, answer_fn):  # pragma: no cover
        raise NotImplementedError("Workday applications are manual")

    def apply(self, page, job, answers, dry_run, log):  # pragma: no cover
        raise NotImplementedError("Workday applications are manual")

