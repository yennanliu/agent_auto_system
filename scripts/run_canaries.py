#!/usr/bin/env python3
"""Run the read-only scraper drift canaries once (for cron / CI / a quick check).

Each browser automation declares a cheap probe on its AutomationSpec
(src/automation/spec.py). This loads the listing page (or GETs an endpoint) and
checks the selectors the automation depends on are still there. It never submits,
applies or sends: probes are pure data, non-GET requests are blocked, and no
apply/send code is imported. See src/automation/canary.py.

Results are stored in the app database (DATABASE_URL), so the admin API
(GET /api/admin/canaries) shows them, and an alert is raised when an automation
flips to failing (log line + CANARY_ALERT_WEBHOOK if set).

Usage:
    uv run python scripts/run_canaries.py                      # every enabled automation
    uv run python scripts/run_canaries.py --only tw104_apply,greenhouse_apply
    uv run python scripts/run_canaries.py --list               # what would run, no network
    uv run python scripts/run_canaries.py --json --no-alert

Exit code: 1 if any probe came back `failed` (site drift), else 0. Expired or
missing login sessions and probe errors are reported but do not fail the command.
"""
import argparse
import json
import logging
import sys

from src.automation import canary, spec
from src.database import init_db


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--only", default="", help="comma-separated job types to probe")
    ap.add_argument("--all", action="store_true", help="include automations disabled in the admin allowlist")
    ap.add_argument("--list", action="store_true", help="list declared canaries and exit")
    ap.add_argument("--json", action="store_true", help="print results as JSON")
    ap.add_argument("--no-alert", action="store_true", help="record results but skip the webhook")
    args = ap.parse_args()

    if args.list:
        for jt, s in spec.REGISTRY.items():
            if s.canary:
                where = f"session={s.canary.session}" if s.canary.session else "login-free"
                print(f"{jt:24} {s.canary.kind:8} {where:18} {s.canary.url}")
            elif s.browser:
                print(f"{jt:24} (none)   {s.canary_skip}")
        return 0

    only = [t.strip() for t in args.only.split(",") if t.strip()]
    unknown = [t for t in only if t not in spec.canaries()]
    if unknown:
        print(f"no canary declared for: {', '.join(unknown)}", file=sys.stderr)
        return 2

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    init_db()
    results = canary.run_all(only or None, notify=not args.no_alert, include_disabled=args.all)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for r in results:
            flag = f"  [ALERT: {r['alert']}]" if r["alert"] else ""
            print(f"{r['job_type']:24} {r['status']:20} {r['duration_ms']:>6} ms  {r['detail']}{flag}")
    return 1 if any(r["status"] == canary.FAILED for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
