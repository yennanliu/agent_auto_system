"""Daily new-job report: today's SWE / backend / full-stack / AI / FDE postings.

Reads each company's official careers board through the public JSON API that
backs it (Greenhouse, Lever, Ashby, Workday, or the company's own search API),
keeps the postings first published *today* (UTC) whose title matches a target
role, and writes one line per job to ``report/YYYYMMDD/new_job.txt``:

    Company | Role | URL

Stdlib only — runs on a bare GitHub Actions runner. A board that fails is
reported as a warning and skipped; it never fails the whole run.

    python scripts/daily_new_jobs.py                # today (UTC)
    python scripts/daily_new_jobs.py --date 2026-11-19 --out report
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path

# Big tech, FAANG and top AI companies (ETFs and traditional corporates left out).
# (name, source, board) — board is the token/slug the source's API expects.
COMPANIES = [
    # Big tech / FAANG
    ("Amazon", "amazon", ""),
    ("Microsoft", "microsoft", ""),
    ("Netflix", "netflix", ""),
    ("NVIDIA", "workday", "nvidia.wd5/NVIDIAExternalCareerSite"),
    ("Salesforce", "workday", "salesforce.wd12/External_Career_Site"),
    ("Adobe", "workday", "adobe.wd5/external_experienced"),
    ("Micron", "workday", "micron.wd1/External"),
    ("Marvell", "workday", "marvell.wd1/MarvellCareers"),
    ("Palantir", "lever", "palantir"),
    ("Spotify", "lever", "spotify"),
    ("Airbnb", "greenhouse", "airbnb"),
    ("Stripe", "greenhouse", "stripe"),
    ("Pinterest", "greenhouse", "pinterest"),
    ("Coinbase", "greenhouse", "coinbase"),
    ("Reddit", "greenhouse", "reddit"),
    ("Dropbox", "greenhouse", "dropbox"),
    ("Lyft", "greenhouse", "lyft"),
    ("DoorDash", "greenhouse", "doordashusa"),
    ("Robinhood", "greenhouse", "robinhood"),
    ("Instacart", "greenhouse", "instacart"),
    ("Figma", "greenhouse", "figma"),
    ("Datadog", "greenhouse", "datadog"),
    ("Cloudflare", "greenhouse", "cloudflare"),
    ("MongoDB", "greenhouse", "mongodb"),
    ("SoFi", "greenhouse", "sofi"),
    ("Waymo", "greenhouse", "waymo"),
    ("SpaceX", "greenhouse", "spacex"),
    ("Rocket Lab", "greenhouse", "rocketlab"),
    ("Planet Labs", "greenhouse", "planetlabs"),
    ("Notion", "ashby", "notion"),
    ("Snowflake", "ashby", "snowflake"),
    ("Ramp", "ashby", "ramp"),
    # Top AI companies
    ("OpenAI", "ashby", "openai"),
    ("Anthropic", "greenhouse", "anthropic"),
    ("xAI", "greenhouse", "xai"),
    ("Databricks", "greenhouse", "databricks"),
    ("Scale AI", "greenhouse", "scaleai"),
    ("CoreWeave", "greenhouse", "coreweave"),
    ("Together AI", "greenhouse", "togetherai"),
    ("Glean", "greenhouse", "gleanwork"),
    ("Figure AI", "greenhouse", "figureai"),
    ("Perplexity", "ashby", "perplexity"),
    ("Cohere", "ashby", "cohere"),
    ("Cursor", "ashby", "cursor"),
    ("Cognition", "ashby", "cognition"),
    ("ElevenLabs", "ashby", "elevenlabs"),
    ("Harvey", "ashby", "harvey"),
    ("Sierra", "ashby", "sierra"),
    ("Decagon", "ashby", "decagon"),
    ("Replit", "ashby", "replit"),
    ("Runway", "ashby", "runway"),
    ("Character.AI", "ashby", "character"),
]

ROLE = re.compile(
    r"\bsoftware (development )?(engineer|developer)\b|\bs[wd]e\b"   # SWE
    r"|\bback[- ]?end (\w+ )?(engineer|developer)\b"  # backend
    r"|\bfull[- ]?stack\b"                            # full stack
    r"|\bai(/ml)? (\w+ )?engineer\b"                  # AI engineer
    r"|\bforward[- ]deployed (\w+ )?engineer\b|\bfde\b",  # FDE
    re.I,
)
# Management titles, and chip-design jobs that reuse "backend" / "AI engineer" wording.
NOT_ROLE = re.compile(r"\b(manager|director|head of|vp|vice president|recruit\w*"
                      r"|asic|vlsi|rtl|physical design|analog)\b", re.I)

# Search terms for the sources that need a query rather than a full board dump.
QUERIES = ["software engineer", "backend engineer", "full stack engineer",
           "ai engineer", "forward deployed engineer"]
UA = {"User-Agent": "Mozilla/5.0 (daily-new-jobs)", "Accept": "application/json"}


def is_target_role(title: str) -> bool:
    return bool(ROLE.search(title)) and not NOT_ROLE.search(title)


def on_day(stamp, day: date) -> bool:
    """True if an ISO-8601 string or epoch (s or ms) timestamp falls on ``day`` in UTC."""
    if stamp in (None, ""):
        return False
    if isinstance(stamp, (int, float)):
        when = datetime.fromtimestamp(stamp / 1000 if stamp > 1e11 else stamp, UTC)
    else:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        when = when.astimezone(UTC) if when.tzinfo else when.replace(tzinfo=UTC)  # naive = UTC
    return when.date() == day


def _get(url: str, body: dict | None = None, tries: int = 4):
    """GET (or POST ``body`` as JSON) and decode; back off on throttling and timeouts."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {**UA, **({"Content-Type": "application/json"} if data else {})}
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except (urllib.error.HTTPError, TimeoutError) as exc:
            retryable = not isinstance(exc, urllib.error.HTTPError) or exc.code in (429, 502, 503)
            if not retryable or attempt == tries - 1:
                raise
            time.sleep(5 * 2 ** attempt)


# Each fetcher yields (title, url, posted) for one board; ``posted`` is judged by on_day,
# except Workday/Amazon, whose fetchers pre-filter on their own "today" signal and yield True.

def greenhouse(board, day):
    for j in _get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs")["jobs"]:
        yield j["title"], j["absolute_url"], j.get("first_published")


def lever(board, day):
    for j in _get(f"https://api.lever.co/v0/postings/{board}?mode=json"):
        yield j["text"], j["hostedUrl"], j["createdAt"]


def ashby(board, day):
    for j in _get(f"https://api.ashbyhq.com/posting-api/job-board/{board}")["jobs"]:
        yield j["title"], j["jobUrl"], j.get("publishedAt")


def workday(board, day):
    host, site = board.split("/")
    base = f"https://{host}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{host.split('.')[0]}/{site}/jobs"
    for q in QUERIES:
        for offset in range(0, 100, 20):  # Workday caps a page at 20 and can't sort by date
            page = _get(api, {"limit": 20, "offset": offset, "searchText": q, "appliedFacets": {}})
            posts = page.get("jobPostings", [])
            for j in posts:
                if j.get("postedOn") == "Posted Today":
                    yield j["title"], f"{base}/{site}{j['externalPath']}", True
            if len(posts) < 20:
                break


def amazon(board, day):
    want = f"{day:%B} {day.day}, {day.year}"  # e.g. "November 19, 2026"
    for q in QUERIES:
        for offset in range(0, 500, 100):  # sorted newest first: stop at the first older post
            jobs = _get("https://www.amazon.jobs/en/search.json?sort=recent&result_limit=100"
                        f"&offset={offset}&base_query={urllib.request.quote(q)}")["jobs"]
            fresh = [j for j in jobs if j["posted_date"] == want]
            for j in fresh:
                yield j["title"], "https://www.amazon.jobs" + j["job_path"], True
            if not jobs or len(fresh) < len(jobs):
                break


def microsoft(board, day):
    for q in QUERIES:
        for start in range(0, 200, 10):  # sorted newest first
            pos = _get("https://apply.careers.microsoft.com/api/pcsx/search?domain=microsoft.com"
                       f"&sort_by=timestamp&start={start}&query={urllib.request.quote(q)}"
                       )["data"]["positions"]
            for j in pos:
                yield j["name"], "https://apply.careers.microsoft.com" + j["positionUrl"], j["postedTs"]
            if not pos or not on_day(pos[-1]["postedTs"], day):
                break


def netflix(board, day):
    for q in QUERIES:
        pos = _get("https://explore.jobs.netflix.net/api/apply/v2/jobs?domain=netflix.com"
                   f"&sort_by=new&num=100&query={urllib.request.quote(q)}")["positions"]
        for j in pos:
            yield j["name"], j["canonicalPositionUrl"], j["t_create"]


SOURCES = {f.__name__: f for f in (greenhouse, lever, ashby, workday, amazon, microsoft, netflix)}


def scan(company: str, source: str, board: str, day: date) -> list[tuple[str, str, str]]:
    seen, rows = set(), []
    for title, url, posted in SOURCES[source](board, day):
        if url in seen or not is_target_role(title):
            continue
        if posted is True or on_day(posted, day):
            seen.add(url)
            rows.append((company, title.strip(), url))
    return rows


def collect(day: date) -> tuple[list[tuple[str, str, str]], list[str]]:
    rows, warnings = [], []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(scan, n, s, b, day): n for n, s, b in COMPANIES}
        for fut, name in futures.items():
            try:
                rows += fut.result()
            except Exception as exc:  # one broken board must not sink the report
                warnings.append(f"{name}: {type(exc).__name__}: {exc}")
    return sorted(rows, key=lambda r: (r[0].lower(), r[1].lower())), warnings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", type=date.fromisoformat,
                    default=datetime.now(UTC).date(), help="YYYY-MM-DD (default: today UTC)")
    ap.add_argument("--out", default="report", help="report root directory")
    args = ap.parse_args()

    rows, warnings = collect(args.date)
    path = Path(args.out) / f"{args.date:%Y%m%d}" / "new_job.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{c} | {t} | {u}\n" for c, t, u in rows))

    print(f"{len(rows)} new jobs on {args.date} -> {path}")
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
