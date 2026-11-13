#!/usr/bin/env python3
"""Run the eval regression set (golden cases under evals/). See doc/evals.md.

    uv run python scripts/run_evals.py                  # offline tier — no API keys
    uv run python scripts/run_evals.py --live           # live tier — real LLM + judge
    uv run python scripts/run_evals.py --live --only hacker_news_digest
    uv run python scripts/run_evals.py --live --update-baseline

Offline (PR CI): case files match the schema, every registered automation has a
case or a justified skip, and each reference output passes the validator and its
own expectations. Live (nightly): runs each case's LLM step from fixtures, scores
it with the existing judge, and fails on a drop beyond --tolerance vs
evals/baseline.json.

Exit codes: 0 green · 1 a check or case failed · 2 the live tier could not run
(no provider API key).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Order in which a run provider is auto-picked when --provider is not given.
_PROVIDER_ENV = (("openai", "OPENAI_API_KEY"), ("gemini", "GEMINI_API_KEY"),
                 ("anthropic", "ANTHROPIC_API_KEY"))


def _offline() -> int:
    from src.automation.harness import evalset

    cases, _ = evalset.load_cases()
    skips, _ = evalset.load_skips()
    problems = evalset.offline_problems()
    covered = sorted({c.job_type for c in cases})
    print(f"Eval set: {len(cases)} case(s) across {len(covered)} automation(s); "
          f"{len(skips)} skipped with a reason.")
    for jt in covered:
        print(f"  ✓ {jt}: {sum(c.job_type == jt for c in cases)} case(s)")
    for jt in skips:
        print(f"  – {jt}: skipped")
    if problems:
        print(f"\n✗ {len(problems)} problem(s):", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print("\n✓ Offline tier passed.")
    return 0


def _pick_provider(requested: str | None) -> str | None:
    env = dict(_PROVIDER_ENV)
    if requested:
        return requested if os.getenv(env.get(requested, "")) else None
    return next((p for p, var in _PROVIDER_ENV if os.getenv(var)), None)


def _live(args: argparse.Namespace) -> int:
    # Hermetic settings DB: keys and the judge choice come from the environment
    # (.env is loaded by src.database), never from a developer's local admin
    # settings — so a local run and the nightly run are configured the same way.
    workdir = tempfile.mkdtemp(prefix="evalset-")
    os.environ["DATABASE_URL"] = f"sqlite:///{workdir}/evals.db"
    os.environ.setdefault("OTEL_ENABLED", "false")
    from src.database import init_db

    init_db()

    from src.automation.harness import evalset
    from src.automation.harness.provider import normalize

    provider = _pick_provider(args.provider or os.getenv("EVAL_RUN_PROVIDER") or None)
    if not provider:
        print("✗ Live tier needs a provider API key in the environment "
              "(OPENAI_API_KEY / GEMINI_API_KEY / ANTHROPIC_API_KEY).", file=sys.stderr)
        return 2
    provider, model = normalize(provider, args.model or os.getenv("EVAL_RUN_MODEL") or None)
    print(f"Live tier: running with {provider} / {model} (tolerance {args.tolerance})")

    def progress(r: evalset.CaseResult) -> None:
        score = "–" if r.score is None else r.score
        print(f"  [{r.status:8}] {r.key}  score={score}  {r.comparison}  ({r.duration_secs}s)", flush=True)
        for f in r.failures:
            print(f"             - {f}")

    report = evalset.run_live(provider, model, only=args.only, tolerance=args.tolerance,
                              progress=progress)
    candidate = evalset.baseline_from(report, evalset.load_baseline())

    if args.report_dir:
        out = Path(args.report_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        (out / "report.md").write_text(evalset.render_markdown(report), encoding="utf-8")
        (out / "baseline.candidate.json").write_text(
            json.dumps(candidate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Report written to {out}/")

    if args.update_baseline:
        path = evalset.EVALS_ROOT / evalset.BASELINE_FILE
        path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Baseline updated: {path} ({len(candidate['scores'])} score(s)) — review and commit it.")

    c = report["counts"]
    print(f"\n{'✓' if report['ok'] else '✗'} Live tier: {c['pass']} pass, {c['fail']} fail, "
          f"{c['error']} error, {c['unscored']} unscored")
    if not report["results"]:
        print("✗ No cases matched.", file=sys.stderr)
    return 0 if report["ok"] else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true", help="run the live tier (real LLM + judge; costs money)")
    ap.add_argument("--provider", help="run provider (default: first of openai/gemini/anthropic with a key)")
    ap.add_argument("--model", help="run model (default: the provider's default)")
    ap.add_argument("--only", action="append", metavar="JOB_TYPE[/CASE_ID]",
                    help="live: limit to a job type or one case (repeatable)")
    ap.add_argument("--tolerance", type=float, default=10.0,
                    help="live: judge points a case may drop below its baseline (default 10)")
    ap.add_argument("--report-dir", help="live: write report.json / report.md / baseline.candidate.json here")
    ap.add_argument("--update-baseline", action="store_true",
                    help="live: write this run's scores to evals/baseline.json")
    args = ap.parse_args()
    return _live(args) if args.live else _offline()


if __name__ == "__main__":
    sys.exit(main())
