#!/usr/bin/env python
"""End-to-end smoke test for a *running* Agent Auto System server.

Exercises every major surface over HTTP — auth, the automation manifest, the
system catalog, job create/list, a real scored run (Hacker News digest), custom
(no-code) automations, pipelines, schedules, admin CRUD, stats/overview — and
prints a pass/fail report. Exits non-zero if anything fails.

Usage
-----
    # 1. start the app in another terminal:
    uv run uvicorn src.main:app --reload --port 8000
    # 2. run the smoke test:
    uv run python scripts/smoke_test.py
    uv run python scripts/smoke_test.py --base-url http://localhost:8000 \
        --username admin --password admin
    uv run python scripts/smoke_test.py --no-run     # skip real LLM runs (fast/offline)
    uv run python scripts/smoke_test.py --keep       # don't delete what it creates

Notes
-----
* It writes to whatever database the server points at, but **cleans up after
  itself** (created jobs, runs, custom automations, and the test user are deleted
  unless --keep). Prefer running it against a dev/test database anyway.
* Real-run checks are skipped automatically when no LLM provider key is configured
  (detected via /health), or with --no-run.
"""
from __future__ import annotations

import argparse
import sys
import time

import httpx

# ── tiny test harness ──────────────────────────────────────────────────────────
GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


class Runner:
    def __init__(self, client: httpx.Client):
        self.c = client
        self.passed = self.failed = self.skipped = 0
        self.failures: list[str] = []

    def check(self, name: str, fn):
        try:
            detail = fn()
            self.passed += 1
            print(f"  {GREEN}✓{RESET} {name}{f'  {DIM}{detail}{RESET}' if detail else ''}")
        except _Skip as s:
            self.skipped += 1
            print(f"  {YELLOW}⊘{RESET} {name}  {DIM}skipped: {s}{RESET}")
        except Exception as e:  # noqa: BLE001
            self.failed += 1
            self.failures.append(f"{name}: {e}")
            print(f"  {RED}✗{RESET} {name}  {RED}{e}{RESET}")

    def section(self, title: str):
        print(f"\n{title}")


class _Skip(Exception):
    pass


def _eq(actual, expected, what=""):
    if actual != expected:
        raise AssertionError(f"{what} expected {expected!r}, got {actual!r}")


# ── main ─────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--username", default="admin")
    ap.add_argument("--password", default="admin")
    ap.add_argument("--no-run", action="store_true", help="skip real LLM runs")
    ap.add_argument("--keep", action="store_true", help="don't clean up created entities")
    ap.add_argument("--run-timeout", type=int, default=90, help="seconds to await a run")
    args = ap.parse_args()

    base = args.base_url.rstrip("/")
    created = {"jobs": [], "runs": [], "custom": [], "users": []}

    print(f"Smoke-testing {base} as {args.username!r}")
    with httpx.Client(base_url=base, timeout=30.0, follow_redirects=True) as c:
        r = Runner(c)

        # ── Infrastructure ──────────────────────────────────────────────────────
        r.section("Infrastructure")
        providers = {}

        def _health():
            nonlocal providers
            d = c.get("/health").raise_for_status().json()
            _eq(d["status"], "ok", "status")
            providers = d.get("providers", {})
            live = [p for p, ok in providers.items() if ok]
            return f"providers: {', '.join(live) or 'none'}; db={d.get('db')}"
        r.check("GET /health", _health)
        r.check("GET / (UI served)", lambda: _eq(c.get("/").status_code, 200, "status"))
        r.check("GET /docs (OpenAPI)", lambda: _eq(c.get("/docs").status_code, 200, "status"))

        # ── Auth ────────────────────────────────────────────────────────────────
        r.section("Authentication")

        def _needs_auth():
            # a fresh (cookieless) client must be rejected by a gated route
            with httpx.Client(base_url=base, timeout=10.0) as anon:
                code = anon.get("/api/jobs").status_code
            if code not in (401, 403):
                raise AssertionError(f"gated route returned {code} without a session")
            return f"unauthenticated → {code}"
        r.check("gated route blocks anonymous", _needs_auth)

        def _login():
            resp = c.post("/api/auth/login", json={"username": args.username, "password": args.password})
            if resp.status_code != 200:
                raise AssertionError(f"login failed ({resp.status_code}); pass --username/--password")
            d = resp.json()
            return f"user #{d['id']}, admin={d['is_admin']}"
        r.check("POST /api/auth/login", _login)
        r.check("GET /api/auth/me", lambda: _eq(c.get("/api/auth/me").json()["username"], args.username, "username"))

        is_admin = c.get("/api/auth/me").json().get("is_admin", False)

        # ── Manifest & system catalog ─────────────────────────────────────────────
        r.section("Manifest & catalog")
        manifest = {}

        def _manifest():
            nonlocal manifest
            data = c.get("/api/automations/manifest").raise_for_status().json()["automations"]
            manifest = {a["job_type"]: a for a in data}
            assert len(manifest) >= 12, f"only {len(manifest)} automations"
            hn = manifest["hacker_news_digest"]
            assert hn["custom_ui"] is False and [f["name"] for f in hn["fields"]] == ["limit"]
            assert manifest["pipeline"]["custom_ui"] is True
            assert hn["steps"][0] == ["Start", "Starting"]
            return f"{len(manifest)} automations"
        r.check("GET /api/automations/manifest", _manifest)

        def _catalog():
            d = c.get("/api/system").raise_for_status().json()
            for k, n in (("agents", 6), ("tools", 7), ("crews", 6), ("workflows", 7)):
                assert len(d[k]) >= n, f"{k}={len(d[k])} < {n}"
            assert all(a.get("role") and a.get("source_code") for a in d["agents"]), "agent role/source missing"
            return f"{len(d['agents'])} agents, {len(d['tools'])} tools, {len(d['crews'])} crews"
        r.check("GET /api/system (catalog)", _catalog)

        # ── Jobs ──────────────────────────────────────────────────────────────────
        r.section("Jobs")
        hn_job = {}

        def _create_job():
            nonlocal hn_job
            hn_job = c.post("/api/jobs", json={
                "name": "smoke: HN digest", "job_type": "hacker_news_digest",
                "payload": {"limit": 3, "llm_provider": "openai", "llm_model": "gpt-4o-mini"},
            }).raise_for_status().json()
            created["jobs"].append(hn_job["id"])
            return f"job #{hn_job['id']}"
        r.check("POST /api/jobs (hacker_news_digest)", _create_job)
        r.check("GET /api/jobs (list)", lambda: (
            None if any(j["id"] == hn_job.get("id") for j in c.get("/api/jobs").json())
            else (_ for _ in ()).throw(AssertionError("created job not in list"))))
        r.check("GET /api/jobs/{id}", lambda: _eq(
            c.get(f"/api/jobs/{hn_job['id']}").json()["job_type"], "hacker_news_digest", "job_type"))

        # ── Real scored run (needs an LLM key) ─────────────────────────────────────
        r.section("Run (end-to-end, scored)")
        have_key = any(providers.values())

        def _run_job():
            if args.no_run:
                raise _Skip("--no-run")
            if not have_key:
                raise _Skip("no LLM provider key configured")
            rid = c.post(f"/api/jobs/{hn_job['id']}/run").raise_for_status().json()["run_id"]
            created["runs"].append(rid)
            deadline = time.time() + args.run_timeout
            status, run = "pending", {}
            while time.time() < deadline:
                run = c.get(f"/api/runs/{rid}").json()
                status = run["status"]
                if status in ("success", "failed"):
                    break
                time.sleep(2)
            if status != "success":
                raise AssertionError(f"run ended {status}: {str(run.get('result'))[:120]}")
            assert run.get("eval_score") is not None, "no eval score"
            return f"run #{rid} success, score ★{run['eval_score']}, {run.get('duration_secs')}s"
        r.check("POST /run → poll to success + score", _run_job)

        def _run_stream():
            if args.no_run or not have_key:
                raise _Skip("no run to stream")
            # SSE endpoint should stream and terminate; just confirm it responds.
            with c.stream("GET", f"/api/runs/{created['runs'][-1]}/stream", timeout=10.0) as s:
                _eq(s.status_code, 200, "status")
            return "SSE ok"
        r.check("GET /api/runs/{id}/stream", _run_stream)

        # ── Custom (no-code) automations — Phase 3G ────────────────────────────────
        r.section("Custom automations (no-code)")
        cust = {}

        def _create_custom():
            nonlocal cust
            if not is_admin:
                raise _Skip("requires admin")
            resp = c.post("/api/admin/custom-automations", json={
                "name": "Smoke Tagline", "icon": "🏷️",
                "description": "smoke test", "instructions": "Write a one-line tagline for the product. Return JSON {\"tagline\": \"...\"}.",
                "output_hint": "JSON with a tagline string",
                "fields": [{"name": "product", "label": "Product", "type": "text", "required": True}],
                "temperature": 0.4,
            })
            resp.raise_for_status()
            cust = resp.json()
            created["custom"].append(cust["id"])
            return cust["job_type"]
        r.check("POST /api/admin/custom-automations", _create_custom)

        def _custom_in_manifest():
            if not cust:
                raise _Skip("not created")
            m = {a["job_type"]: a for a in c.get("/api/automations/manifest").json()["automations"]}
            assert cust["job_type"] in m, "custom automation missing from manifest"
            assert m[cust["job_type"]]["custom_ui"] is False
            return "renders via generic form"
        r.check("custom automation appears in manifest", _custom_in_manifest)

        def _run_custom():
            if not cust:
                raise _Skip("not created")
            if args.no_run or not have_key:
                raise _Skip("no LLM key / --no-run")
            job = c.post("/api/jobs", json={
                "name": "smoke: custom run", "job_type": cust["job_type"],
                "payload": {"product": "a smart kettle", "llm_provider": "openai", "llm_model": "gpt-4o-mini"},
            }).raise_for_status().json()
            created["jobs"].append(job["id"])
            rid = c.post(f"/api/jobs/{job['id']}/run").raise_for_status().json()["run_id"]
            created["runs"].append(rid)
            deadline = time.time() + args.run_timeout
            while time.time() < deadline:
                run = c.get(f"/api/runs/{rid}").json()
                if run["status"] in ("success", "failed"):
                    if run["status"] != "success":
                        raise AssertionError(f"custom run {run['status']}")
                    return f"run #{rid} success, ★{run.get('eval_score')}"
                time.sleep(2)
            raise AssertionError("custom run timed out")
        r.check("run a custom automation end-to-end", _run_custom)

        # ── Pipeline ────────────────────────────────────────────────────────────────
        r.section("Pipeline")

        def _create_pipeline():
            job = c.post("/api/jobs", json={
                "name": "smoke: pipeline", "job_type": "pipeline",
                "payload": {"steps": [
                    {"job_type": "hacker_news_digest", "payload": {"limit": 2}},
                    {"job_type": "web_scraper", "payload": {"url": "https://example.com"}},
                ], "llm_provider": "openai", "llm_model": "gpt-4o-mini"},
            }).raise_for_status().json()
            created["jobs"].append(job["id"])
            return f"pipeline job #{job['id']} (2 steps)"
        r.check("POST /api/jobs (pipeline, 2 steps)", _create_pipeline)

        # ── Schedules ───────────────────────────────────────────────────────────────
        r.section("Schedules")
        sched = {}

        def _create_schedule():
            nonlocal sched
            sched = c.post("/api/jobs", json={
                "name": "smoke: daily digest", "job_type": "hacker_news_digest",
                "payload": {"limit": 5}, "schedule": "0 8 * * *",
            }).raise_for_status().json()
            created["jobs"].append(sched["id"])
            _eq(sched["schedule"], "0 8 * * *", "schedule")
            return "cron 0 8 * * *"
        r.check("POST /api/jobs (scheduled)", _create_schedule)

        def _list_schedules():
            d = c.get("/api/schedules").raise_for_status().json()
            items = d if isinstance(d, list) else d.get("schedules", d.get("items", []))
            assert any(s.get("job", {}).get("id", s.get("id")) == sched.get("id") or
                       s.get("name") == "smoke: daily digest" for s in items), "schedule not listed"
            return f"{len(items)} scheduled"
        r.check("GET /api/schedules", _list_schedules)

        def _bad_cron():
            code = c.post("/api/jobs", json={
                "name": "smoke: bad cron", "job_type": "hacker_news_digest",
                "payload": {"limit": 1}, "schedule": "not a cron",
            }).status_code
            if code not in (400, 422):
                raise AssertionError(f"bad cron accepted ({code})")
            return f"rejected → {code}"
        r.check("invalid cron rejected", _bad_cron)

        # ── Admin ─────────────────────────────────────────────────────────────────
        r.section("Admin")

        def _admin(fn, need_admin=True):
            if need_admin and not is_admin:
                raise _Skip("requires admin")
            return fn()
        r.check("GET /api/admin/users", lambda: _admin(
            lambda: f"{len(c.get('/api/admin/users').raise_for_status().json())} user(s)"))
        r.check("GET /api/admin/llm-keys", lambda: _admin(
            lambda: f"{len(c.get('/api/admin/llm-keys').raise_for_status().json())} providers"))
        r.check("GET /api/admin/automations", lambda: _admin(
            lambda: f"{len(c.get('/api/admin/automations').raise_for_status().json().get('all', []))} in allowlist"))
        r.check("GET /api/admin/eval-judge", lambda: _admin(
            lambda: (c.get("/api/admin/eval-judge").raise_for_status(), "ok")[1]))

        def _user_crud():
            if not is_admin:
                raise _Skip("requires admin")
            u = c.post("/api/admin/users", json={
                "username": "smoke_user", "password": "smoke-pass-123",
                "is_admin": False, "allowed_automations": ["hacker_news_digest"],
            })
            if u.status_code == 409:  # left over from a previous run
                u = next(x for x in c.get("/api/admin/users").json() if x["username"] == "smoke_user")
            else:
                u = u.raise_for_status().json()
            created["users"].append(u["id"])
            return f"user #{u['id']} created"
        r.check("POST /api/admin/users (create)", _user_crud)

        # ── Stats & overview ─────────────────────────────────────────────────────
        r.section("Stats & overview")

        def _stats():
            d = c.get("/api/stats").raise_for_status().json()
            for k in ("total_runs", "success", "failed", "by_type", "trend"):
                assert k in d, f"missing {k}"
            _eq(len(d["trend"]), 7, "trend days")
            return f"total_runs={d['total_runs']}"
        r.check("GET /api/stats", _stats)
        r.check("GET /api/jobs/{id}/overview", lambda: (
            c.get(f"/api/jobs/{hn_job['id']}/overview").raise_for_status(), "ok")[1])

        # ── Cleanup ──────────────────────────────────────────────────────────────
        if not args.keep:
            r.section("Cleanup")

            def _cleanup():
                for rid in created["runs"]:
                    c.delete(f"/api/runs/{rid}")
                for jid in created["jobs"]:
                    c.delete(f"/api/jobs/{jid}")
                for cid in created["custom"]:
                    c.delete(f"/api/admin/custom-automations/{cid}")
                for uid in created["users"]:
                    c.delete(f"/api/admin/users/{uid}")
                return (f"removed {len(created['runs'])} runs, {len(created['jobs'])} jobs, "
                        f"{len(created['custom'])} custom, {len(created['users'])} users")
            r.check("delete created entities", _cleanup)
        else:
            print(f"\n{DIM}--keep: leaving created entities in place{RESET}")

        # ── Summary ────────────────────────────────────────────────────────────────
        total = r.passed + r.failed + r.skipped
        print(f"\n{'─' * 60}")
        print(f"{GREEN}{r.passed} passed{RESET}, {RED}{r.failed} failed{RESET}, "
              f"{YELLOW}{r.skipped} skipped{RESET}  ({total} checks)")
        if r.failures:
            print(f"\n{RED}Failures:{RESET}")
            for f in r.failures:
                print(f"  • {f}")
        return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(main())
