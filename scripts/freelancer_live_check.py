#!/usr/bin/env python3
"""Live check of Freelancer.com bid placement with your own token (read-only by default).

An operational check, deliberately outside pytest (tests stay offline). With
``FREELANCER_OAUTH_TOKEN`` set it exercises exactly the code the
``freelancer_bids`` job uses (``src/automation/tools/freelancer_tool.py``):

  1. account   GET /api/users/0.1/self/ — the token works, who it bids as, and
               what the API says about the account's ability to bid (email
               verified, suspended, membership bid limit, bids remaining when
               the token may see it).
  2. search    one real public project search (the job's search step).
  3. payload   the exact JSON body ``place_bid`` would POST for a real project,
               checked with ``bid_problem`` and against the documented
               required fields. NOT posted.

Nothing is sent unless you pass ``--place-bid <project_id>`` together with
``--proposal-file``; you are then shown the bid and must type the project id
to confirm. That places one REAL bid (it may use one of your bid credits).

Usage:
    FREELANCER_OAUTH_TOKEN=... uv run python scripts/freelancer_live_check.py
    FREELANCER_OAUTH_TOKEN=... uv run python scripts/freelancer_live_check.py --keywords "django, scraping"
    FREELANCER_OAUTH_TOKEN=<sandbox token> uv run python scripts/freelancer_live_check.py --sandbox
    FREELANCER_OAUTH_TOKEN=... uv run python scripts/freelancer_live_check.py \\
        --place-bid 40839735 --proposal-file my_proposal.txt [--amount 250] [--sandbox]

``--sandbox`` points every call at https://www.freelancer-sandbox.com (same as
``FREELANCER_API_BASE``); tokens only work on the environment that issued them.

Exit code: 0 all checks ok, 1 a check FAILED (API drift, a bad token, an account
that cannot bid, or a bid that was refused), 2 FREELANCER_OAUTH_TOKEN is not set.
The token and your username are never printed in full.
"""
import argparse
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.automation.tools import freelancer_tool as ft  # noqa: E402

REQUIRED_FIELDS = {"project_id": int, "bidder_id": int, "amount": float, "period": int,
                   "milestone_percentage": int}
# A stand-in proposal used only to build the dry-run payload (never posted).
DRY_RUN_PROPOSAL = ("Dry-run proposal used only to validate the request payload. It is never sent: "
                    "this text exists to make the description field realistic in length.")


def mask(value: str, keep: int = 4) -> str:
    value = str(value or "")
    if len(value) <= keep:
        return "*" * len(value)
    return f"{value[:keep]}…({len(value)} chars)"


def mask_name(value: str) -> str:
    value = str(value or "")
    return (value[:2] + "***") if value else "(none)"


def fmt(v) -> str:
    return "unknown (not exposed to this token)" if v is None else str(v)


def check_payload(body: dict) -> str:
    """Why this body doesn't match the documented POST /bids/ schema, or ""."""
    for name, typ in REQUIRED_FIELDS.items():
        if name not in body:
            return f"missing required field {name}"
        if not isinstance(body[name], typ) or isinstance(body[name], bool):
            return f"{name} is {type(body[name]).__name__}, expected {typ.__name__}"
    if not 20 <= body["milestone_percentage"] <= 100:
        return "milestone_percentage outside 20-100"
    if body["period"] < 1 or body["amount"] <= 0:
        return "period / amount must be positive"
    if not isinstance(body.get("description", ""), str):
        return "description must be a string"
    return ""


def fetch_project(client, project_id: str) -> dict:
    """One project by id via the public API, normalized like a search hit."""
    resp = client.get(ft.api_base() + f"/api/projects/0.1/projects/{int(project_id)}/",
                      params={"full_description": "true", "job_details": "true", "compact": "true"})
    data = ft._answer(resp)
    if not data.get("id"):
        raise ft.FreelancerAPIError(f"project {project_id} not found")
    return ft.normalize(data)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--keywords", default="python", help="search keywords for the read-only search")
    ap.add_argument("--sandbox", action="store_true", help=f"use {ft.SANDBOX_BASE}")
    ap.add_argument("--period", type=int, default=7, help="delivery days on the (dry-run or real) bid")
    ap.add_argument("--milestone", type=int, default=100, help="milestone %% (20-100)")
    ap.add_argument("--place-bid", metavar="PROJECT_ID", help="place ONE real bid on this project")
    ap.add_argument("--proposal-file", help="proposal text for --place-bid (>= 100 chars, no email)")
    ap.add_argument("--amount", type=float, help="bid amount in the project's currency "
                                                 "(default: the job's suggested bid)")
    args = ap.parse_args()

    if args.sandbox:
        os.environ["FREELANCER_API_BASE"] = ft.SANDBOX_BASE
    token = os.getenv("FREELANCER_OAUTH_TOKEN", "").strip()
    if not token:
        print("FREELANCER_OAUTH_TOKEN is not set. Create a personal access token at "
              "https://accounts.freelancer.com/settings/develop (sandbox: "
              "https://accounts.freelancer-sandbox.com/settings/develop) — see doc/freelancer_bids.md.")
        return 2
    if args.place_bid and not args.proposal_file:
        print("--place-bid needs --proposal-file: a real bid is never sent with placeholder text.")
        return 1

    import httpx
    print(f"API base: {ft.api_base()}   token: {mask(token)}")
    results: list[tuple[str, str, str]] = []
    client = httpx.Client(timeout=25, headers={"User-Agent": ft.USER_AGENT})
    try:
        # 1. account ----------------------------------------------------------
        print("== account (GET /api/users/0.1/self/)")
        me = None
        try:
            me = ft.account_status(client, token)
            print(f"    user id {me['id']}, username {mask_name(me['username'])}")
            print(f"    email verified: {fmt(me['email_verified'])}, payment verified: "
                  f"{fmt(me['payment_verified'])}, suspended: {fmt(me['suspended'])}, "
                  f"limited account: {fmt(me['limited_account'])}")
            print(f"    membership: {fmt(me['membership'])}, bid limit: {fmt(me['bid_limit'])}, "
                  f"bids remaining: {fmt(me['bids_remaining'])}")
            if me["can_bid"]:
                results.append(("account", "ok", f"can bid as user {me['id']}"))
            else:
                results.append(("account", "FAILED", "account cannot bid: " + "; ".join(me["blockers"])))
        except ft.FreelancerAPIError as exc:
            hint = " — the token needs the 'basic' + 'fln:user_information' scopes" if exc.is_auth else ""
            results.append(("account", "FAILED", f"{exc}{hint}"))
        except httpx.HTTPError as exc:
            results.append(("account", "FAILED", f"{type(exc).__name__}: {exc}"))

        # 2. search -----------------------------------------------------------
        print(f"== search ('{args.keywords}')")
        candidates = []
        with tempfile.TemporaryDirectory(prefix="freelancer-check-") as tmp:
            try:
                found = ft.find_projects(keywords=args.keywords, ledger_path=str(Path(tmp) / "ledger.json"),
                                         client=client, log=lambda m: print(f"    {m}"))
                candidates = found["candidates"]
                if found["warnings"]:
                    results.append(("search", "FAILED", "; ".join(found["warnings"][:2])))
                elif not found["projects_found"] and ft.api_base() != ft.DEFAULT_BASE:
                    # The sandbox is mostly empty: post a project there from a second account.
                    results.append(("search", "skipped", "no active projects on this host for these keywords"))
                elif not found["projects_found"]:
                    results.append(("search", "FAILED", "the search returned no projects"))
                else:
                    results.append(("search", "ok", f"{found['projects_found']} project(s), "
                                                    f"{len(candidates)} pass the default filters"))
            except Exception as exc:  # noqa: BLE001 — report, keep checking
                results.append(("search", "FAILED", f"{type(exc).__name__}: {exc}"))

        # 3. payload (dry run) --------------------------------------------------
        print("== payload (what place_bid would POST; not sent)")
        bidder_id = me["id"] if me else 0
        target = next((p for p in candidates
                       if not ft.bid_problem(p, DRY_RUN_PROPOSAL, ft.suggested_bid(p))), None)
        if target is None:
            results.append(("payload", "skipped", "no searched project has a budget to base a bid on"))
        else:
            body = ft.bid_payload(bidder_id=bidder_id, project=target, amount=ft.suggested_bid(target),
                                  period_days=args.period, milestone_percentage=args.milestone,
                                  description=DRY_RUN_PROPOSAL)
            shown = {**body, "description": body["description"][:40] + "…"}
            print(f"    POST {ft.api_base()}{ft.BIDS_PATH}")
            print(f"    project: {target['title'][:70]} ({target['url']})")
            print(f"    body: {shown}")
            why = check_payload(body)
            if not bidder_id:
                why = why or "no bidder id (the account check failed)"
            results.append(("payload", "FAILED" if why else "ok",
                             why or f"{target['currency_sign']}{body['amount']:g} {target['currency']} on "
                                    f"project {body['project_id']}, all required fields valid"))

        # 4. optional real bid ---------------------------------------------------
        if args.place_bid:
            results.append(("place-bid", *place_real_bid(client, token, me, args)))
    finally:
        client.close()

    print("\nSummary")
    for name, outcome, detail in results:
        print(f"  {outcome:8} {name:10} {detail}")
    return 1 if any(o == "FAILED" for _, o, _ in results) else 0


def place_real_bid(client, token: str, me: dict | None, args) -> tuple[str, str]:
    print(f"== place-bid (REAL) on project {args.place_bid}")
    if not me:
        return "FAILED", "the account check failed; not bidding"
    try:
        proposal = Path(args.proposal_file).read_text(encoding="utf-8").strip()
    except OSError as exc:
        return "FAILED", f"cannot read the proposal file ({exc})"
    try:
        project = fetch_project(client, args.place_bid)
    except Exception as exc:  # noqa: BLE001
        return "FAILED", f"cannot load project {args.place_bid}: {exc}"
    amount = args.amount if args.amount is not None else ft.suggested_bid(project)
    why = ft.bid_problem(project, proposal, amount)
    if why:
        return "FAILED", f"not placed: {why}"
    body = ft.bid_payload(bidder_id=me["id"], project=project, amount=amount, period_days=args.period,
                          milestone_percentage=args.milestone, description=proposal)
    print(f"    project : {project['title'][:80]}")
    print(f"    url     : {project['url']}  (status {project['status'] or '?'}, {project['bid_count']} bids)")
    print(f"    amount  : {project['currency_sign']}{amount:g} {project['currency']} "
          f"(budget {project['budget_min']}–{project['budget_max']})")
    print(f"    period  : {body['period']} day(s), milestone {body['milestone_percentage']}%")
    print(f"    proposal: {len(body['description'])} chars")
    if not sys.stdin.isatty():
        return "FAILED", "refusing to place a real bid without an interactive confirmation"
    answer = input(f"This places a REAL bid on {ft.api_base()}. Type the project id to confirm: ").strip()
    if answer != str(project["id"]):
        return "skipped", "not confirmed; nothing sent"
    try:
        res = ft.place_bid(client, token, bidder_id=me["id"], project=project, amount=amount,
                           period_days=args.period, milestone_percentage=args.milestone,
                           description=proposal)
    except ft.FreelancerAPIError as exc:
        if exc.kind == "duplicate":
            return "ok", f"already bid on project {project['id']} ({exc})"
        return "FAILED", f"{exc} [kind: {exc.kind}]"
    except Exception as exc:  # noqa: BLE001
        return "FAILED", f"{type(exc).__name__}: {exc}"
    return "ok", f"bid {res.get('bid_id')} placed on project {project['id']}"


if __name__ == "__main__":
    sys.exit(main())
