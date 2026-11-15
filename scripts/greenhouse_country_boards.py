#!/usr/bin/env python3
"""Find Greenhouse boards with software / AI engineering openings in a country.

Produces the "<Country>: N Greenhouse companies with software / AI engineering
openings" issues (#74-#78, #102-#104): a ranked company table plus a ready-to-paste
`greenhouse_apply` input block.

Two steps, so the slow network part runs once and any number of countries can be
reported from it:

  scan    harvest candidate board names from public job-list repos, check each one
          against the public Greenhouse jobs API, and cache every live board's
          jobs (title / location / url) in data/greenhouse_boards.json
  report  filter the cache by country + engineering title, write the issue
          markdown, and optionally open the GitHub issue

Usage:
    uv run python scripts/greenhouse_country_boards.py scan             # ~25 min
    uv run python scripts/greenhouse_country_boards.py scan --resume    # continue an interrupted scan
    uv run python scripts/greenhouse_country_boards.py report netherlands spain
    uv run python scripts/greenhouse_country_boards.py report hong_kong --create-issue
    uv run python scripts/greenhouse_country_boards.py report --list    # known countries
    # a country without a preset:
    uv run python scripts/greenhouse_country_boards.py report \
        --name France --match "france|paris|lyon" --location "Paris, France, Lyon"

The Greenhouse API blocks an IP (HTTP 403 on every board, for several minutes) when
it is hit too fast - 40 concurrent requests triggered it after ~9,000 calls. `scan`
therefore stays at a few requests per second and, if a block still happens, pauses
and retries instead of recording the board as dead.

Matching is by job title and location text only: seniority, language and visa
sponsorship are not checked, and a role listed in several countries counts for each.
"""
import argparse
import asyncio
import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

import httpx

DEFAULT_CACHE = "data/greenhouse_boards.json"
DEFAULT_OUT_DIR = "data/greenhouse_reports"
API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs"
USER_AGENT = "job-board-check/1.0"
TOP_N = 80

# ── candidate sources ────────────────────────────────────────────────────────
# (label used in the issue text, url, kind)
SOURCES = [
    ("Feashliaa/job-board-aggregator Greenhouse list",
     "https://raw.githubusercontent.com/Feashliaa/job-board-aggregator/main/data/greenhouse_companies.json",
     "json_list"),
    ("SimplifyJobs New-Grad-Positions",
     "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json",
     "simplify"),
    ("SimplifyJobs Summer2026-Internships",
     "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json",
     "simplify"),
    ("hiring-without-whiteboards",
     "https://raw.githubusercontent.com/poteto/hiring-without-whiteboards/main/README.md",
     "hww"),
]
SOURCES_BLURB = (
    "the Feashliaa/job-board-aggregator Greenhouse list, the SimplifyJobs and "
    "hiring-without-whiteboards job-list repos, plus boards already in the cache"
)

_BOARD_URL = re.compile(r"greenhouse\.io/([A-Za-z0-9_-]+)")
_BOARD_FOR = re.compile(r"[?&]for=([A-Za-z0-9_-]+)")
_NOT_BOARDS = {"embed", "v1", "boards", "jobs", "job_app", "job_board"}


def _slug(name: str) -> str:
    """Company name -> the board token it most often has ("Jane Street" -> janestreet)."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def boards_in_text(text: str) -> set[str]:
    """Board tokens in any greenhouse.io URL (hosted or `?for=` embed) in `text`."""
    found = {m.group(1).lower() for m in _BOARD_URL.finditer(text)}
    found |= {m.group(1).lower() for m in _BOARD_FOR.finditer(text)}
    return found - _NOT_BOARDS


def candidates_from(kind: str, text: str) -> set[str]:
    """Candidate board tokens from one downloaded source."""
    if kind == "json_list":
        return {str(x).lower() for x in json.loads(text)}
    found = boards_in_text(text)
    if kind == "simplify":
        found |= {_slug(row.get("company_name", "")) for row in json.loads(text)}
    elif kind == "hww":
        found |= {_slug(m.group(1)) for m in re.finditer(r"^- \[([^\]]+)\]", text, re.M)}
    return found


def clean_candidates(names) -> list[str]:
    return sorted({n for n in names if 1 < len(n) < 60} - _NOT_BOARDS)


# ── title / location filters ─────────────────────────────────────────────────
INCLUDE = re.compile(
    r"software|back[- ]?end|front[- ]?end|full[- ]?stack|\bmobile\b|\bios\b|android"
    r"|\bsre\b|site reliability|devops|platform engineer|infrastructure engineer"
    r"|cloud engineer|machine learning|\bml\b|\bmlops\b|\bai\b|data engineer"
    r"|data scientist|forward[- ]deployed|\bdeveloper\b|research engineer"
    r"|\bengineer(ing)?\b.*\b(python|java|golang|go|c\+\+|rust|scala|kotlin|typescript)\b"
    r"|\b(python|java|golang|c\+\+|rust|scala|kotlin|typescript|react|node)\b.*engineer",
    re.I,
)
EXCLUDE = re.compile(
    r"\bsales\b|support|solutions?\b|architect|security|\bqa\b|quality|\btest|\bsdet\b"
    r"|hardware|manufactur|mechanical|electrical|firmware|\bmanager\b|\bintern\b"
    r"|internship|new grad|graduate|working student|werkstudent|apprentice|recruit"
    r"|talent|account executive|customer success|marketing|business develop"
    r"|business lead|\bpartner|\bdesigner\b|\bfield\b|pre[- ]?sales|technical writer"
    r"|consultant|advocate|evangelist|\bhead of\b|\bdirector\b|\bvp\b|analyst"
    r"|program manag|product manag|trainer|content|legal|counsel|finance|strategy"
    r"|technician|\bRF\b|hr\b|people|material|leather|fabric|apparel|garment"
    r"|footwear|curriculum|property|real estate|land developer",
    re.I,
)


def is_engineering_title(title: str) -> bool:
    return bool(INCLUDE.search(title)) and not EXCLUDE.search(title)


# match: regex a job's location text must hit; reject: regex that vetoes it (same-named
# places elsewhere); location: the `location:` line for the greenhouse_apply input.
COUNTRIES = {
    "netherlands": {
        "name": "Netherlands",
        "match": r"netherlands|nederland|amsterdam|rotterdam|utrecht|eindhoven|the hague"
                 r"|den haag|delft|haarlem|groningen|leiden|nijmegen|arnhem|hoofddorp"
                 r"|schiphol|veldhoven|enschede|zwolle|breda|tilburg|maastricht"
                 r"|amersfoort|almere|den bosch|hertogenbosch|\bNL\b",
        "reject": r"canada|newfoundland|, NY\b",  # NL = Newfoundland; Amsterdam, NY
        "location": "Amsterdam, Netherlands, Rotterdam, Utrecht, Eindhoven, The Hague",
    },
    "hong_kong": {
        "name": "Hong Kong",
        "match": r"hong\s?kong|\bHKG?\b",
        "location": "Hong Kong",
    },
    "spain": {
        "name": "Spain",
        "match": r"spain|espa[ñn]a|madrid|barcelona|valencia|m[aá]laga|sevill[ae]|bilbao|zaragoza",
        "reject": r"california|venezuela|, CA\b",  # Valencia, CA / Valencia, Venezuela
        "location": "Spain, Madrid, Barcelona, Valencia, Málaga",
    },
    "japan": {
        "name": "Japan",
        "match": r"japan|tokyo|osaka|kyoto|yokohama|fukuoka|nagoya|日本|東京",
        "location": "Tokyo, Japan, Osaka",
    },
    "singapore": {
        "name": "Singapore",
        "match": r"singapore|\bSG\b",
        "location": "Singapore",
    },
    "taiwan": {
        "name": "Taiwan",
        "match": r"taiwan|taipei|hsinchu|taichung|kaohsiung|台灣|臺灣|台北",
        "location": "Taiwan, Taipei, Hsinchu",
    },
    "uk": {
        "name": "UK",
        "match": r"united kingdom|\bUK\b|england|scotland|wales|london|manchester"
                 r"|edinburgh|cambridge|bristol|glasgow|leeds|birmingham|belfast|oxford",
        # Cambridge MA, London ON, Birmingham AL, ...
        "reject": r"massachusetts|, MA\b|ontario|, ON\b|alabama|, AL\b|canada|united states",
        "location": "London, United Kingdom, UK, England, Manchester, Edinburgh, Cambridge",
    },
    "germany": {
        "name": "Germany",
        "match": r"germany|deutschland|berlin|munich|münchen|hamburg|frankfurt|cologne"
                 r"|köln|stuttgart|düsseldorf|dusseldorf|karlsruhe|leipzig|dresden",
        "reject": r", (CT|NH|NJ|MD|WI)\b",  # Berlin, CT / NH / ...
        "location": "Berlin, Germany, Munich, Hamburg, Frankfurt",
    },
}


def matching_roles(jobs: list[dict], match: re.Pattern, reject: re.Pattern | None) -> list[dict]:
    """A board's engineering roles located in the country, deduped by URL, by title."""
    seen, roles = set(), []
    for job in jobs:
        loc = job["location"]
        if not match.search(loc) or (reject and reject.search(loc)):
            continue
        if not is_engineering_title(job["title"]) or job["url"] in seen:
            continue
        seen.add(job["url"])
        roles.append(job)
    return sorted(roles, key=lambda j: j["title"].strip().lower())


def rank_boards(boards: dict, country: dict) -> list[tuple[str, list[dict]]]:
    """[(board, roles)] for boards with at least one match, most roles first."""
    match = re.compile(country["match"], re.I)
    reject = re.compile(country["reject"], re.I) if country.get("reject") else None
    rows = [(b, matching_roles(jobs, match, reject)) for b, jobs in boards.items()]
    return sorted((r for r in rows if r[1]), key=lambda r: (-len(r[1]), r[0]))


def _cell(text: str) -> str:
    """Keep a job title from breaking the markdown table / link syntax."""
    return text.replace("|", "\\|").replace("[", "(").replace("]", ")").strip()


def render_issue(country: dict, rows, cache: dict) -> tuple[str, str]:
    """(title, body) of the country issue."""
    name, date = country["name"], cache["date"]
    total, top = len(rows), rows[:TOP_N]
    head = f"{total} companies" if total <= TOP_N else f"Top {TOP_N} of {total} companies"
    lines = [
        f"**Greenhouse only**: every company below hosts its jobs on Greenhouse, so the "
        f"`greenhouse_apply` automation can run against all of them. {head} with a live "
        f"software / AI / ML engineering opening in **{name}**, checked on **{date}**, "
        f"sorted by number of matching roles.",
        "",
        f"Found by checking about {cache['candidates']:,} candidate board names (harvested "
        f"from {SOURCES_BLURB}) against the public Greenhouse API; "
        f"{len(cache['boards']):,} were live boards. \"Roles\" counts {name}-located titles "
        f"for software / backend / frontend / mobile / SRE / DevOps / platform / ML / AI / "
        f"data / forward-deployed engineering, and leaves out sales, support, solutions, "
        f"architect, security, QA/test, hardware/manufacturing, manager, intern and "
        f"new-grad titles. Matching is by job title and location text only; seniority, "
        f"language and visa sponsorship were not checked.",
        "",
        "### Automation input",
        "Paste into a `greenhouse_apply` job (dry run first):",
        "```text",
        f"boards:   {', '.join(b for b, _ in top)}",
        f"location: {country['location']}",
        "keywords: software engineer, backend, SRE, platform, machine learning, "
        "AI engineer, data engineer, forward deployed",
        "```",
        "",
        "| # | Company | Roles | Example roles | Notes |",
        "|---|---|---|---|---|",
    ]
    for i, (board, roles) in enumerate(top, 1):
        examples = "; ".join(f"[{_cell(j['title'])[:60]}]({j['url']})" for j in roles[:3])
        lines.append(
            f"| {i} | [{board}](https://job-boards.greenhouse.io/{board}) "
            f"| {len(roles)} | {examples} |  |"
        )
    if total <= TOP_N:
        lines += ["", f"**Only {total} companies:** every Greenhouse board found with a "
                      f"matching {name} role."]
    title = (f"{name}: {len(top)} Greenhouse companies with software / AI engineering "
             f"openings ({date})")
    return title, "\n".join(lines) + "\n"


# ── scan ─────────────────────────────────────────────────────────────────────
def load_cache(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def save_cache(path: Path, cache: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False))
    tmp.replace(path)


async def harvest(client: httpx.AsyncClient) -> set[str]:
    names: set[str] = set()
    for label, url, kind in SOURCES:
        try:
            resp = await client.get(url, follow_redirects=True, timeout=120)
            resp.raise_for_status()
            found = candidates_from(kind, resp.text)
        except Exception as exc:  # a missing source shrinks the scan, it doesn't stop it
            print(f"  ! {label}: {exc}", file=sys.stderr)
            continue
        print(f"  {label}: {len(found):,} names")
        names |= found
    return names


async def check_boards(client, todo: list[str], cache: dict, path: Path,
                       workers: int, delay: float, block_pause: int) -> None:
    """Fill cache["boards"] / cache["dead"] for every name in `todo`."""
    queue: asyncio.Queue[str] = asyncio.Queue()
    for name in todo:
        queue.put_nowait(name)
    unblocked = asyncio.Event()
    unblocked.set()
    done = 0

    async def worker():
        nonlocal done
        while not queue.empty():
            board = queue.get_nowait()
            for _ in range(6):
                await unblocked.wait()
                try:
                    resp = await client.get(API.format(board=board), timeout=60)
                except httpx.HTTPError:
                    await asyncio.sleep(3)
                    continue
                if resp.status_code == 404:
                    cache["dead"].append(board)
                    break
                if resp.status_code == 200:
                    cache["boards"][board] = [
                        {"title": j.get("title") or "",
                         "location": (j.get("location") or {}).get("name") or "",
                         "url": j.get("absolute_url") or ""}
                        for j in resp.json().get("jobs", [])
                    ]
                    break
                # 403 / 429 / 5xx: rate-limited or blocked - everyone waits, then retry
                if unblocked.is_set():
                    unblocked.clear()
                    print(f"  HTTP {resp.status_code} - pausing {block_pause}s "
                          f"({queue.qsize():,} left)", file=sys.stderr)
                    await asyncio.sleep(block_pause)
                    unblocked.set()
            # a board that never answered is left out of both lists: --resume retries it
            done += 1
            if done % 500 == 0:
                print(f"  {done:,}/{len(todo):,} checked, {len(cache['boards']):,} live")
                save_cache(path, cache)
            await asyncio.sleep(delay)

    await asyncio.gather(*(worker() for _ in range(workers)))


async def scan(args) -> None:
    path = Path(args.cache)
    old = load_cache(path)
    known = set(old.get("boards", {}))
    if args.resume and old:
        cache = old
        cache.setdefault("dead", [])
    else:
        cache = {"boards": {}, "dead": []}
    cache["date"] = datetime.date.today().isoformat()

    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}) as client:
        print("Harvesting candidate board names...")
        names = await harvest(client)
        # boards that were live last time stay candidates even if a source drops them
        candidates = clean_candidates(names | known | set(args.board or []))
        if args.limit:
            candidates = candidates[: args.limit]
        cache["candidates"] = len(candidates)
        checked = set(cache["boards"]) | set(cache["dead"])
        todo = [c for c in candidates if c not in checked]
        print(f"{len(candidates):,} candidates, {len(todo):,} to check "
              f"(~{len(todo) * args.delay / args.workers / 60:.0f}+ min)")
        try:
            await check_boards(client, todo, cache, path, args.workers, args.delay,
                               args.block_pause)
        finally:
            save_cache(path, cache)

    missed = len(candidates) - len(set(cache["boards"]) | set(cache["dead"]))
    print(f"{len(cache['boards']):,} live boards, {len(cache['dead']):,} not on Greenhouse"
          + (f", {missed:,} unanswered - re-run with --resume" if missed else "")
          + f" → {path}")


# ── report ───────────────────────────────────────────────────────────────────
def resolve_countries(args) -> list[dict]:
    if args.name:
        if not (args.match and args.location):
            sys.exit("--name needs --match and --location")
        return [{"name": args.name, "match": args.match, "reject": args.reject,
                 "location": args.location}]
    unknown = [c for c in args.countries if c not in COUNTRIES]
    if unknown:
        sys.exit(f"unknown country {', '.join(unknown)} - known: {', '.join(COUNTRIES)}; "
                 f"or use --name/--match/--location")
    if not args.countries:
        sys.exit("name at least one country (see --list) or use --name/--match/--location")
    return [COUNTRIES[c] for c in args.countries]


def report(args) -> None:
    if args.list:
        for key, c in COUNTRIES.items():
            print(f"{key:12} {c['location']}")
        return
    countries = resolve_countries(args)
    cache = load_cache(Path(args.cache))
    if not cache.get("boards"):
        sys.exit(f"no scan cache at {args.cache} - run `scan` first")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for country in countries:
        rows = rank_boards(cache["boards"], country)
        title, body = render_issue(country, rows, cache)
        out = out_dir / f"{_slug(country['name'])}_{cache['date']}.md"
        out.write_text(body)
        print(f"{title}\n  {len(rows)} companies, {sum(len(r) for _, r in rows)} roles → {out}")
        if args.create_issue:
            cmd = ["gh", "issue", "create", "--title", title, "--body-file", str(out)]
            if args.repo:
                cmd += ["--repo", args.repo]
            subprocess.run(cmd, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scan", help="check candidate boards against the Greenhouse API")
    p.add_argument("--cache", default=DEFAULT_CACHE)
    p.add_argument("--resume", action="store_true",
                   help="keep the cached results and only check names not answered yet")
    p.add_argument("--board", action="append", help="extra board name to check (repeatable)")
    p.add_argument("--limit", type=int, help="only check the first N candidates (for testing)")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--delay", type=float, default=0.5,
                   help="seconds each worker waits between requests (default 0.5)")
    p.add_argument("--block-pause", type=int, default=120,
                   help="seconds to wait when the API answers 403/429 (default 120)")
    p.set_defaults(func=lambda a: asyncio.run(scan(a)))

    p = sub.add_parser("report", help="write the per-country issue markdown")
    p.add_argument("countries", nargs="*", help=f"any of: {', '.join(COUNTRIES)}")
    p.add_argument("--list", action="store_true", help="list the known countries")
    p.add_argument("--cache", default=DEFAULT_CACHE)
    p.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    p.add_argument("--name", help="display name of a country without a preset")
    p.add_argument("--match", help="regex its job locations must match (with --name)")
    p.add_argument("--reject", help="regex that vetoes a location (with --name)")
    p.add_argument("--location", help="`location:` line for the automation input (with --name)")
    p.add_argument("--create-issue", action="store_true", help="open the issue with `gh`")
    p.add_argument("--repo", help="owner/name for --create-issue (default: current repo)")
    p.set_defaults(func=report)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
