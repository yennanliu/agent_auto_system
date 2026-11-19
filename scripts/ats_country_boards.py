#!/usr/bin/env python3
"""Find Lever / Ashby / Workable boards with software / AI engineering openings in a country.

The multi-ATS sibling of scripts/greenhouse_country_boards.py: same two steps and
the same country presets and engineering-title filter, but for the platforms
`ats_apply` added in Phase 1. Its report's input block is ready to paste into an
`ats_apply` job (`platform:token` boards).

  scan    harvest candidate board names (public job-list repos), check each one
          with the platform adapter's own discover() — the exact code ats_apply
          runs — and cache every live board's jobs in data/ats_boards.json
  report  filter the cache by country + engineering title and write the markdown

Candidates come from the Feashliaa/job-board-aggregator Lever and Ashby lists
plus every Lever / Ashby / Workable link in the SimplifyJobs listings and the
hiring-without-whiteboards README (resolved with ats_router.detect_ats), plus
boards already in the cache.

Usage:
    uv run python scripts/ats_country_boards.py scan                     # all three platforms
    uv run python scripts/ats_country_boards.py scan --platform workable --limit 50
    uv run python scripts/ats_country_boards.py scan --resume
    uv run python scripts/ats_country_boards.py report taiwan japan
    uv run python scripts/ats_country_boards.py report --name France \\
        --match "france|paris|lyon" --location "Paris, France, Lyon"

An unknown board answers HTTP 404 on all three APIs; that is the only thing
recorded as dead. Throttling (403 / 429 / 5xx) pauses every worker and retries,
so a rate limit never marks a live board dead. Matching is by job title and
location text only.
"""
import argparse
import datetime
import importlib.util
import json
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.automation.tools.ats_ashby import AshbyAdapter  # noqa: E402
from src.automation.tools.ats_lever import LeverAdapter  # noqa: E402
from src.automation.tools.ats_router import BoardRef, detect_ats  # noqa: E402
from src.automation.tools.ats_workable import WorkableAdapter  # noqa: E402

# The country presets, title filter and ranking are shared with the Greenhouse scanner.
_spec = importlib.util.spec_from_file_location("greenhouse_country_boards",
                                               ROOT / "scripts" / "greenhouse_country_boards.py")
gcb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gcb)
COUNTRIES, matching_roles, resolve_countries, _cell, _slug = (
    gcb.COUNTRIES, gcb.matching_roles, gcb.resolve_countries, gcb._cell, gcb._slug)

DEFAULT_CACHE = "data/ats_boards.json"
DEFAULT_OUT_DIR = "data/ats_reports"
USER_AGENT = "job-board-check/1.0"
TOP_N = 80
PLATFORMS = ("lever", "ashby", "workable")
ADAPTERS = {"lever": LeverAdapter(), "ashby": AshbyAdapter(), "workable": WorkableAdapter()}
BOARD_URL = {"lever": "https://jobs.lever.co/{t}", "ashby": "https://jobs.ashbyhq.com/{t}",
             "workable": "https://apply.workable.com/{t}/"}

_AGG = "https://raw.githubusercontent.com/Feashliaa/job-board-aggregator/main/data/{p}_companies.json"
# (label, url, kind, platforms it can yield)
SOURCES = [
    ("Feashliaa/job-board-aggregator Lever list", _AGG.format(p="lever"), "json_list:lever"),
    ("Feashliaa/job-board-aggregator Ashby list", _AGG.format(p="ashby"), "json_list:ashby"),
    ("SimplifyJobs New-Grad-Positions",
     "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json",
     "urls"),
    ("SimplifyJobs Summer2026-Internships",
     "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json",
     "urls"),
    ("hiring-without-whiteboards",
     "https://raw.githubusercontent.com/poteto/hiring-without-whiteboards/main/README.md", "urls"),
]
SOURCES_BLURB = ("the Feashliaa/job-board-aggregator Lever and Ashby lists, every Lever / Ashby / "
                 "Workable link in the SimplifyJobs and hiring-without-whiteboards repos, plus "
                 "boards already in the cache")


# ── candidates ───────────────────────────────────────────────────────────────

def candidates_from(kind: str, text: str) -> dict[str, set[str]]:
    """{platform: board tokens} from one downloaded source."""
    out: dict[str, set[str]] = {p: set() for p in PLATFORMS}
    if kind.startswith("json_list:"):
        platform = kind.split(":", 1)[1]
        out[platform] = {str(x).strip() for x in json.loads(text) if str(x).strip()}
        return out
    for url in _urls(text):
        ref = detect_ats(url)
        if ref is not None and ref.platform in out:
            out[ref.platform].add(ref.company)
    return out


def _urls(text: str) -> list[str]:
    return re.findall(r"https?://[^\s\"'<>()\]]+", text)


def clean(names) -> list[str]:
    return sorted({n for n in names if 1 < len(n) < 80 and "/" not in n})


def harvest(client: httpx.Client, platforms) -> dict[str, set[str]]:
    names: dict[str, set[str]] = {p: set() for p in platforms}
    for label, url, kind in SOURCES:
        try:
            resp = client.get(url, follow_redirects=True, timeout=120)
            resp.raise_for_status()
            found = candidates_from(kind, resp.text)
        except Exception as exc:  # noqa: BLE001 — a missing source shrinks the scan
            print(f"  ! {label}: {exc}", file=sys.stderr)
            continue
        counts = {p: len(found[p]) for p in platforms if found.get(p)}
        print(f"  {label}: {counts or 'nothing for these platforms'}")
        for p in platforms:
            names[p] |= found.get(p, set())
    return names


# ── checking boards ──────────────────────────────────────────────────────────

class Gate:
    """Pauses every worker while the API is throttling us."""
    def __init__(self, pause: int):
        self.pause, self.open, self.lock = pause, threading.Event(), threading.Lock()
        self.open.set()

    def block(self, why: str) -> None:
        with self.lock:
            if not self.open.is_set():
                return
            self.open.clear()
        print(f"  {why} - pausing {self.pause}s", file=sys.stderr)
        time.sleep(self.pause)
        self.open.set()


def check_board(client, platform: str, token: str, gate: Gate, tries: int = 6):
    """("live", jobs) | ("dead", None) | ("unknown", None) for one board."""
    for _ in range(tries):
        gate.open.wait()
        try:
            jobs = ADAPTERS[platform].discover(client, BoardRef(platform, token))
            return "live", [{"title": j["title"], "location": j["location"], "url": j["url"]}
                            for j in jobs]
        except RuntimeError as exc:
            if str(exc).startswith("no such"):        # the adapters' 404 → not on this ATS
                return "dead", None
            time.sleep(2)                              # malformed answer: retry
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code == 404:
                return "dead", None
            if code in (403, 429) or code >= 500:
                gate.block(f"{platform} HTTP {code}")
            else:
                time.sleep(2)
        except httpx.HTTPError:
            time.sleep(3)
    return "unknown", None                             # left out: --resume retries it


def scan(args) -> None:
    path = Path(args.cache)
    old = gcb.load_cache(path)
    platforms = [args.platform] if args.platform else list(PLATFORMS)
    cache = old if (args.resume and old) else {"platforms": dict(old.get("platforms", {}))}
    cache["date"] = datetime.date.today().isoformat()
    gate = Gate(args.block_pause)
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=60, follow_redirects=True) as client:
        print("Harvesting candidate board names...")
        names = harvest(client, platforms)
        for platform in platforms:
            prev = (old.get("platforms") or {}).get(platform, {})
            state = cache["platforms"].get(platform) if args.resume else None
            state = state or {"boards": {}, "dead": []}
            # boards that were live last time stay candidates even if a source drops them
            cands = clean(names[platform] | set(prev.get("boards", {})) |
                          {b.split(":", 1)[1] for b in args.board or [] if b.startswith(platform + ":")})
            if args.limit:
                cands = cands[: args.limit]
            state["candidates"] = len(cands)
            cache["platforms"][platform] = state
            checked = set(state["boards"]) | set(state["dead"])
            todo = [c for c in cands if c not in checked]
            print(f"{platform}: {len(cands):,} candidates, {len(todo):,} to check "
                  f"(~{len(todo) * args.delay / args.workers / 60:.0f}+ min)")
            done = 0

            def work(token, platform=platform, state=state):
                nonlocal done
                status, jobs = check_board(client, platform, token, gate)
                if status == "live":
                    state["boards"][token] = jobs
                elif status == "dead":
                    state["dead"].append(token)
                done += 1
                if done % 250 == 0:
                    print(f"  {platform}: {done:,}/{len(todo):,} checked, {len(state['boards']):,} live")
                    gcb.save_cache(path, cache)
                time.sleep(args.delay)

            try:
                with ThreadPoolExecutor(max_workers=args.workers) as pool:
                    list(pool.map(work, todo))
            finally:
                gcb.save_cache(path, cache)
            missed = len(cands) - len(set(state["boards"]) | set(state["dead"]))
            print(f"{platform}: {len(state['boards']):,} live, {len(state['dead']):,} not on {platform}"
                  + (f", {missed:,} unanswered - re-run with --resume" if missed else ""))
    print(f"→ {path}")


# ── report ───────────────────────────────────────────────────────────────────

def rank(cache: dict, country: dict) -> list[tuple[str, str, list[dict]]]:
    """[(platform, board, roles)] across platforms, most matching roles first."""
    match = re.compile(country["match"], re.I)
    reject = re.compile(country["reject"], re.I) if country.get("reject") else None
    rows = []
    for platform, state in (cache.get("platforms") or {}).items():
        for board, jobs in state.get("boards", {}).items():
            roles = matching_roles(jobs, match, reject)
            if roles:
                rows.append((platform, board, roles))
    return sorted(rows, key=lambda r: (-len(r[2]), r[0], r[1].lower()))


def render_issue(country: dict, rows, cache: dict) -> tuple[str, str]:
    name, date = country["name"], cache["date"]
    total, top = len(rows), rows[:TOP_N]
    live = sum(len(s.get("boards", {})) for s in cache.get("platforms", {}).values())
    cands = sum(s.get("candidates", 0) for s in cache.get("platforms", {}).values())
    by_platform = {p: sum(1 for r in top if r[0] == p) for p in PLATFORMS}
    head = f"{total} companies" if total <= TOP_N else f"Top {TOP_N} of {total} companies"
    lines = [
        f"**Lever / Ashby / Workable**: every company below hosts its jobs on one of these, so "
        f"the `ats_apply` automation can run against all of them. {head} with a live software / "
        f"AI / ML engineering opening in **{name}**, checked on **{date}**, sorted by number of "
        f"matching roles ({', '.join(f'{n} {p.title()}' for p, n in by_platform.items() if n)}).",
        "",
        f"Found by checking about {cands:,} candidate board names (harvested from {SOURCES_BLURB}) "
        f"with each platform's public API; {live:,} were live boards. \"Roles\" uses the same "
        f"engineering-title filter as the Greenhouse report. Matching is by job title and location "
        f"text only; seniority, language and visa sponsorship were not checked.",
        "",
        "### Automation input",
        "Paste into an `ats_apply` job (dry run first):",
        "```text",
        f"boards:   {', '.join(f'{p}:{b}' for p, b, _ in top)}",
        f"location: {country['location']}",
        "keywords: software engineer, backend, SRE, platform, machine learning, "
        "AI engineer, data engineer, forward deployed",
        "```",
        "",
        "| # | Company | ATS | Roles | Example roles |",
        "|---|---|---|---|---|",
    ]
    for i, (platform, board, roles) in enumerate(top, 1):
        examples = "; ".join(f"[{_cell(j['title'])[:60]}]({j['url']})" for j in roles[:3])
        lines.append(f"| {i} | [{board}]({BOARD_URL[platform].format(t=board)}) | {platform} "
                     f"| {len(roles)} | {examples} |")
    title = (f"{name}: {len(top)} Lever / Ashby / Workable companies with software / AI "
             f"engineering openings ({date})")
    return title, "\n".join(lines) + "\n"


def report(args) -> None:
    if args.list:
        for key, c in COUNTRIES.items():
            print(f"{key:12} {c['location']}")
        return
    countries = resolve_countries(args)
    cache = gcb.load_cache(Path(args.cache))
    if not any(s.get("boards") for s in (cache.get("platforms") or {}).values()):
        sys.exit(f"no scan cache at {args.cache} - run `scan` first")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for country in countries:
        rows = rank(cache, country)
        title, body = render_issue(country, rows, cache)
        out = out_dir / f"{_slug(country['name'])}_{cache['date']}.md"
        out.write_text(body)
        print(f"{title}\n  {len(rows)} companies, {sum(len(r) for *_, r in rows)} roles → {out}")
        if args.create_issue:
            cmd = ["gh", "issue", "create", "--title", title, "--body-file", str(out)]
            if args.repo:
                cmd += ["--repo", args.repo]
            subprocess.run(cmd, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scan", help="check candidate boards against each platform's API")
    p.add_argument("--platform", choices=PLATFORMS, help="only this platform (default: all)")
    p.add_argument("--cache", default=DEFAULT_CACHE)
    p.add_argument("--resume", action="store_true", help="continue an interrupted scan")
    p.add_argument("--board", action="append", help="extra platform:token to check (repeatable)")
    p.add_argument("--limit", type=int, help="only check the first N candidates per platform")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--delay", type=float, default=0.5, help="seconds each worker waits per board")
    p.add_argument("--block-pause", type=int, default=120,
                   help="seconds to pause everyone after a 403 / 429 / 5xx")
    p = sub.add_parser("report", help="write the per-country markdown")
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
    args = parser.parse_args()
    scan(args) if args.cmd == "scan" else report(args)


if __name__ == "__main__":
    main()
