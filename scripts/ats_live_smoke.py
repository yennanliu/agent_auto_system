#!/usr/bin/env python3
"""Live DRY-RUN smoke check of the apply adapters against real public boards.

An operational check, deliberately outside pytest (tests stay offline): for each
board it lists jobs through the platform's public API, reads the form, plans the
answers and fills the real form in a browser — but never clicks Submit, so
nothing is sent to any employer. It catches drift in a listing API, a form
definition (Greenhouse Job Board API / Lever HTML / Ashby GraphQL / Workable form
API) or a form's DOM that the hermetic fixtures can't.

Usage:
    uv run python scripts/ats_live_smoke.py                       # all default boards
    uv run python scripts/ats_live_smoke.py --boards lever:palantir,ashby:openai
    uv run python scripts/ats_live_smoke.py --headed              # watch it fill
    uv run python scripts/ats_live_smoke.py --boards "" --sources hn   # one discovery source

Each discovery source (hn, remoteok, remotive, weworkremotely) is checked the
same way with keywords="engineer": it must return jobs or manual leads, and any
ATS job it routes must fill cleanly.

Per board / source the outcome is:
  ok       a real form was opened, filled and its Submit button found
  skipped  every scanned job had a required question this test profile can't
           answer (not drift — the board's questions are just specific)
  FAILED   an API or form failure: listing error, form could not be loaded or
           filled, no Submit button, or anything was sent / ledgered

Exit code: 1 if any board FAILED, else 0. Free-text screening questions get a
fixed sentence (a stand-in for the LLM answerer); choices are never guessed.
"""
import argparse
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.automation.tools import ats_apply_tool as ats  # noqa: E402

DEFAULT_BOARDS = ("greenhouse:figma", "lever:palantir", "ashby:openai", "workable:huggingface")
DEFAULT_SOURCES = ("hn", "remoteok", "remotive", "weworkremotely")

PROFILE = dict(
    first_name="Test", last_name="DryRun", email="dry-run@example.com", phone="+1 415 555 0100",
    current_location="San Francisco, CA", authorized_countries="United States",
    years_experience=5, notice_period="2 weeks", languages="English, Chinese",
    school="Stanford University", linkedin_url="https://www.linkedin.com/in/example",
    github_url="https://github.com/example",
    extra_answers="office three days: Yes\npronouns: Prefer not to say",
)


def _answer_fn(label, kind, options, job):
    return "" if options else "I admire the product and want to help build it."


def check_board(board: str, workdir: Path, scans: int, source: str = "") -> tuple[str, str]:
    """(outcome, detail) for one board, or for one discovery source when
    ``source`` is set — see the module docstring."""
    resume = workdir / "cv.pdf"
    resume.write_bytes(b"%PDF-1.4\n% live smoke dry-run resume\n")
    name = source or board
    ledger = workdir / f"ledger-{name.replace(':', '_').replace('/', '_')}.json"
    out = ats.run_ats_apply(
        boards=board, sources=source, keywords="engineer" if source else "",
        **PROFILE, resume_path=str(resume), ledger_path=str(ledger),
        max_applications=1, max_scans_per_board=scans, dry_run=True, answer_fn=_answer_fn,
        log=lambda m: print(f"    {m}"),
    )
    if source:
        if any(w.startswith(f"{source}: source unavailable") or "unknown source" in w
               for w in out.get("warnings", [])):
            return "FAILED", "; ".join(out["warnings"][:2])
        if not (out.get("total_listed") or out.get("manual")):
            return "FAILED", "the source returned no jobs and no leads"
    if out.get("error"):
        return "FAILED", out["error"]
    if any("could not list jobs" in w or "not supported" in w for w in out.get("warnings", [])):
        return "FAILED", "; ".join(out["warnings"][:3])
    if not source and not out.get("total_listed"):
        return "FAILED", "listing API returned no jobs"
    if out.get("submitted_count") or ledger.exists():
        return "FAILED", "something was submitted or ledgered during a dry run"
    # Any job that failed — including a form definition that could not be
    # loaded — is drift; only unanswerable required questions are a skip.
    failed = [e for e in out.get("skipped", []) if e.get("status") == "failed"]
    if failed:
        return "FAILED", "; ".join(f"{e['job_id']}: {e.get('reason', '')[:120]}" for e in failed[:3])
    if out.get("applied"):
        e = out["applied"][0]
        return "ok", f"{e['job_id']} prepared ({len(e.get('answers', []))} answers), not sent"
    if out.get("manual"):
        return "ok", f"{len(out['manual'])} lead(s) listed for manual apply"
    return "skipped", "; ".join(e.get("reason", "")[:80] for e in out.get("skipped", [])[:2])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--boards", default=",".join(DEFAULT_BOARDS),
                    help="comma-separated boards (URLs or platform:token)")
    ap.add_argument("--sources", default=",".join(DEFAULT_SOURCES),
                    help="comma-separated discovery sources to check ('' to skip)")
    ap.add_argument("--scans", type=int, default=25, help="max jobs examined per board")
    ap.add_argument("--headed", action="store_true", help="show the browser")
    args = ap.parse_args()
    os.environ["ATS_HEADLESS"] = "0" if args.headed else "1"

    results = []
    with tempfile.TemporaryDirectory(prefix="ats-smoke-") as tmp:
        for board in [b.strip() for b in args.boards.split(",") if b.strip()]:
            print(f"== {board}")
            try:
                outcome, detail = check_board(board, Path(tmp), args.scans)
            except Exception as exc:  # noqa: BLE001 — report, keep checking the rest
                outcome, detail = "FAILED", f"{type(exc).__name__}: {exc}"
            results.append((board, outcome, detail))
        for source in [x.strip() for x in args.sources.split(",") if x.strip()]:
            print(f"== source:{source}")
            try:
                outcome, detail = check_board("", Path(tmp), args.scans, source=source)
            except Exception as exc:  # noqa: BLE001
                outcome, detail = "FAILED", f"{type(exc).__name__}: {exc}"
            results.append((f"source:{source}", outcome, detail))

    print("\nSummary")
    for board, outcome, detail in results:
        print(f"  {outcome:8} {board:28} {detail}")
    return 1 if any(o == "FAILED" for _, o, _ in results) else 0


if __name__ == "__main__":
    sys.exit(main())
