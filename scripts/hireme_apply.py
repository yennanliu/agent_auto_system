"""
Bulk auto-apply to 104.com.tw jobs sourced from the "Hire Me - 104 Job Finder"
site (https://web-production-ea48ce.up.railway.app/).

The finder is just a ranked search front-end over 104: you type keywords, it
LLM-expands them, scrapes 104, scores each hit against your preference /
penalty chips, and returns a table where every row is a `104.com.tw/job/<id>`
link. Its browser form POSTs to a streaming NDJSON endpoint (`/api/search`), so
this script talks to that endpoint directly instead of driving the page — same
results, no browser needed for the search half.

The apply half is the existing 104 automation: `_apply_to_job()` from
`src/automation/tools/tw104_apply_tool.py`, driven with the session persisted by
`scripts/104_login.py`. Nothing about the 104 funnel is re-implemented here.

    uv run python scripts/hireme_apply.py --limit 5                # dry-run preview
    uv run python scripts/hireme_apply.py --limit 100 --submit     # for real

Defaults mirror the site's own defaults (台北/新北, AI preference, the usual
非軟體 penalties). Results stream to --out after every job, so an interrupted
run keeps what it already did.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.automation.tools.tw104_apply_tool import (  # noqa: E402
    DEFAULT_STATE_PATH,
    _UA,
    _APPLY_BTN_SELECTORS,
    _apply_to_job,
    _first_visible,
    _looks_logged_out,
)

FINDER = "https://web-production-ea48ce.up.railway.app"
DEFAULT_AREAS = ["台北", "新北"]
DEFAULT_PREFS = ["AI"]
DEFAULT_PENALTIES = ["嵌入式", "硬體", "韌體", "實習", "替代役"]


def log(msg: str) -> None:
    print(msg, flush=True)


# ── search (Hire Me finder) ──────────────────────────────────────────────────

def search(keywords, areas, min_salary, preferences, penalties) -> list[dict]:
    """POST /api/search and read the NDJSON stream. Each line is one event;
    the single `result` event carries the scored, ranked job list."""
    body = json.dumps({
        "keywords": keywords, "areas": areas, "min_salary": min_salary,
        "preferences": preferences, "penalties": penalties,
    }).encode()
    req = Request(f"{FINDER}/api/search", data=body,
                  headers={"Content-Type": "application/json", "User-Agent": _UA})
    jobs: list[dict] = []
    with urlopen(req, timeout=600) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("type") == "expand":
                log(f"🔍 expanded keywords: {', '.join(msg.get('expanded', []))}")
            elif msg.get("type") == "progress":
                log(f"   … {msg.get('current')}/{msg.get('total')} {msg.get('keyword', '')}")
            elif msg.get("type") == "result":
                jobs = msg.get("jobs", [])
                log(f"✓ finder returned {msg.get('total', len(jobs))} job(s)")
    return jobs


def to_104_job(j: dict) -> dict | None:
    """Finder row → the job dict `_apply_to_job` expects."""
    url = (j.get("url") or "").split("?")[0].rstrip("/")
    job_id = url.rsplit("/job/", 1)[-1].lower() if "/job/" in url else ""
    if not job_id:
        return None
    return {"job_id": job_id, "url": url, "title": j.get("title", ""),
            "company": j.get("company", ""), "applied": False}


# ── apply ────────────────────────────────────────────────────────────────────

def _shows_already_applied(page) -> bool:
    """104 replaces the 應徵 button with a "MM/DD已應徵" stamp on jobs you have
    already applied to. Read off the rendered page rather than the button —
    the button element is gone entirely in that state. Checked after the fact
    (the funnel already navigated there) so we never pay a second page load."""
    try:
        if "已應徵" in (page.inner_text("body", timeout=5000) or ""):
            return True
    except Exception:  # noqa: BLE001
        pass
    btn = _first_visible(page, _APPLY_BTN_SELECTORS)
    try:
        return btn is not None and "已應徵" in (btn.inner_text(timeout=2000) or "")
    except Exception:  # noqa: BLE001
        return False


def apply_all(jobs, *, target, cover_letter, dry_run, delay, state_path, out_path) -> dict:
    """Walk the ranked list until `target` new applications land. Jobs already
    applied to (a large slice of the top of the list, from earlier runs) are
    skipped without counting against the target."""
    from playwright.sync_api import sync_playwright

    results: list[dict] = []
    warnings: list[str] = []
    summary = {"target": target, "candidates": len(jobs), "dry_run": dry_run,
               "results": results, "warnings": warnings}

    def flush() -> None:
        summary["counts"] = {
            k: sum(1 for r in results if r.get("status") == k)
            for k in {r.get("status") for r in results}
        }
        Path(out_path).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    with sync_playwright() as pw:
        # headless=False: 104 serves headless browsers empty pages (see the tool).
        browser = pw.chromium.launch(
            headless=False, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(storage_state=state_path, user_agent=_UA,
                                  locale="zh-TW", viewport={"width": 1366, "height": 900})
        page = ctx.new_page()
        try:
            page.goto("https://www.104.com.tw/jobs/main/", wait_until="domcontentloaded",
                      timeout=30000)
            page.wait_for_timeout(1500)
            if _looks_logged_out(page):
                summary["error"] = ("Not logged in to 104.com.tw — re-run "
                                    "`uv run python scripts/104_login.py`.")
                flush()
                return summary
            log("✓ authenticated with 104.com.tw\n")

            consecutive_fail = 0
            done = 0
            for i, job in enumerate(jobs, 1):
                if done >= target:
                    break
                log(f"[{done}/{target} applied | scan {i}/{len(jobs)}] {job['job_id']} — "
                    f"{job['title'][:50]} @ {job['company'][:30]}")
                try:
                    entry = _apply_to_job(ctx, page, job, cover_letter, dry_run,
                                          log, warnings)
                    if entry.get("status") != "submitted" and _shows_already_applied(page):
                        entry.update(status="skipped", submitted=False,
                                     reason="already applied (已應徵)")
                        log("↷ already applied — next")
                except Exception as exc:  # noqa: BLE001 — one bad job never stops the run
                    entry = {**job, "status": "failed",
                             "reason": f"{type(exc).__name__}: {exc}"}
                    log(f"✗ {type(exc).__name__}: {exc}")

                results.append(entry)
                if entry.get("status") in ("submitted", "prepared"):
                    done += 1
                flush()

                # A long unbroken failure streak means the session died or 104
                # started throttling — stop rather than burn through the list.
                consecutive_fail = 0 if entry.get("status") in (
                    "submitted", "prepared", "skipped") else consecutive_fail + 1
                if consecutive_fail >= 10:
                    warnings.append("aborted: 10 consecutive failures")
                    log("\n✗ 10 consecutive failures — aborting (session expired?)")
                    break

                if delay:
                    page.wait_for_timeout(int(delay * 1000))
        finally:
            browser.close()

    flush()
    return summary


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--keywords", default="AI engineer", help="comma-separated")
    p.add_argument("--areas", default=",".join(DEFAULT_AREAS))
    p.add_argument("--prefs", default=",".join(DEFAULT_PREFS))
    p.add_argument("--penalties", default=",".join(DEFAULT_PENALTIES))
    p.add_argument("--min-salary", type=int, default=0)
    p.add_argument("--limit", type=int, default=100,
                   help="target number of NEW applications (already-applied jobs "
                        "don't count against it)")
    p.add_argument("--submit", action="store_true",
                   help="actually send applications (default: dry-run preview)")
    p.add_argument("--letter-file", default="",
                   help="path to a custom 自我推薦信; omit to keep 104's 系統預設")
    p.add_argument("--delay", type=float, default=2.0, help="seconds between jobs")
    p.add_argument("--out", default="data/hireme_apply_results.json")
    p.add_argument("--state", default=os.getenv("TW104_STORAGE_STATE", DEFAULT_STATE_PATH))
    a = p.parse_args()

    if not os.path.exists(a.state):
        log(f"✗ No saved 104 session at '{a.state}'. Run "
            "`uv run python scripts/104_login.py` once.")
        return 1

    csv = lambda s: [x.strip() for x in s.split(",") if x.strip()]  # noqa: E731
    cover_letter = Path(a.letter_file).read_text(encoding="utf-8") if a.letter_file else ""

    log(f"Searching Hire Me for: {a.keywords}")
    rows = search(csv(a.keywords), csv(a.areas), a.min_salary,
                  csv(a.prefs), csv(a.penalties))
    jobs, seen = [], set()
    for r in rows:
        j = to_104_job(r)
        if j and j["job_id"] not in seen:
            seen.add(j["job_id"])
            jobs.append(j)
    if not jobs:
        log("✗ no jobs returned")
        return 1

    mode = "SUBMIT (real applications)" if a.submit else "DRY-RUN (nothing sent)"
    log(f"\nTarget {a.limit} new application(s) from {len(jobs)} ranked job(s) — {mode}\n")

    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    s = apply_all(jobs, target=a.limit, cover_letter=cover_letter,
                  dry_run=not a.submit, delay=a.delay, state_path=a.state,
                  out_path=a.out)

    if s.get("error"):
        log(f"\n✗ {s['error']}")
        return 1
    counts = s.get("counts", {})
    log("\n" + "=" * 60)
    log(f"{'Submitted' if a.submit else 'Prepared'}: "
        f"{counts.get('submitted', 0) + counts.get('prepared', 0)}  |  "
        f"already applied: {counts.get('skipped', 0)}  |  "
        f"failed: {counts.get('failed', 0) + counts.get('unconfirmed', 0)}")
    log(f"Full results → {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
