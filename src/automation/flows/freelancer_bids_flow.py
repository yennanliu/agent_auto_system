"""Freelancer.com: find matching projects and draft a bid proposal for each.

Deterministic funnel (CLAUDE.md): the tool searches the official public API and
filters (budget in USD, type, competition, keywords, already-drafted); the LLM
is used only to judge relevance (optional ``task_filter``) and to draft each
proposal from the user's own ``candidate_background``. Nothing is submitted —
placing a bid needs the user's Freelancer account; the result lists projects
with a drafted proposal and a suggested bid to review and paste.
"""
import json
import os
import re

from crewai.flow.flow import Flow, listen, start
from pydantic import BaseModel

from src.automation.crews.freelancer_proposal_crew.crew import FreelancerProposalCrew
from src.automation.crews.greenhouse_relevance_crew.crew import GreenhouseRelevanceCrew
from src.automation.flows.base import FlowMixin
from src.automation.flows.greenhouse_apply_flow import _parse_verdict
from src.automation.flows.utils import extract_usage
from src.automation.progress import append_log
from src.automation.tools import freelancer_tool as ft

_SKIP_RE = re.compile(r"^\s*SKIP\s*:\s*(.*)", re.I | re.S)


def parse_proposal(text: str) -> tuple[str, str]:
    """(proposal, skip_reason): exactly one is non-empty for usable output."""
    text = (text or "").strip().strip('"').strip()
    m = _SKIP_RE.match(text)
    if m:
        return "", (m.group(1).strip().splitlines() or ["background doesn't fit"])[0][:200]
    return text, ""


def _money_range(lo, hi, sign: str) -> str:
    """"$250–$750", or a single end when the other is missing, or "?"."""
    if lo is not None and hi is not None:
        return f"{sign}{lo:g}–{sign}{hi:g}"
    one = lo if lo is not None else hi
    return f"{sign}{one:g}" if one is not None else "?"


def budget_text(p: dict) -> str:
    cur = p["currency"]
    usd = ""
    if cur != "USD" and (p["budget_min_usd"] is not None or p["budget_max_usd"] is not None):
        usd = f" (≈ {_money_range(p['budget_min_usd'], p['budget_max_usd'], '$')})"
    per_hour = " per hour" if p["type"] == "hourly" else ""
    return (f"{p['type'] or 'project'}, {_money_range(p['budget_min'], p['budget_max'], p['currency_sign'])}"
            f" {cur}{usd}{per_hour}")


class FreelancerBidsState(BaseModel):
    keywords: str = ""                 # comma-separated search terms (required)
    min_budget_usd: float = 0          # project's max budget must reach this (0 = any)
    max_budget_usd: float = 0          # project's min budget must not exceed this (0 = any)
    project_type: str = "any"          # any | fixed | hourly
    max_bids: int = 50                 # skip projects with more bids than this (0 = any)
    max_drafts: int = 5                # proposals to draft this run
    candidate_background: str = ""     # the only facts the drafts may use (required)
    proposal_template: str = ""        # optional reusable text (links / sign-off kept)
    task_filter: str = ""              # optional LLM relevance gate
    # Opt-in: place each drafted bid with the user's token (FREELANCER_OAUTH_TOKEN).
    submit_bids: bool = False
    bid_period_days: int = 7           # delivery time stated on the bid
    milestone_percentage: int = 100    # share of the bid requested as milestone payment
    run_id: int = 0
    usage: dict = {}
    llm_provider: str = ""
    llm_model: str = ""
    previous_error: str = ""


class FreelancerBidsFlow(FlowMixin, Flow[FreelancerBidsState]):

    @start()
    def validate_payload(self):
        self._check_required("keywords", "candidate_background")
        append_log(self.state.run_id,
                   f"Payload validated — keywords '{self.state.keywords}', "
                   f"budget ${self.state.min_budget_usd:g}–${self.state.max_budget_usd or '∞'}, "
                   f"type {self.state.project_type}, max {self.state.max_drafts} draft(s)")
        return self.state.model_dump()

    @listen(validate_payload)
    def execute_drafts(self, _):
        st = self.state
        log = lambda m: append_log(st.run_id, m)  # noqa: E731
        log("Searching Freelancer.com projects...")
        found = ft.find_projects(keywords=st.keywords, min_budget_usd=st.min_budget_usd,
                                 max_budget_usd=st.max_budget_usd, project_type=st.project_type,
                                 max_bids=st.max_bids, log=log)
        skipped, warnings = list(found["skipped"]), list(found["warnings"])
        drafted: list[dict] = []
        usage = {"prompt_tokens": 0, "completion_tokens": 0}

        from src.automation.harness.provider import resolve as resolve_llm
        try:
            llm, _p, _m = resolve_llm(st.llm_provider or None, st.llm_model or None, temperature=0.5)
        except Exception as exc:  # noqa: BLE001
            llm = None
            warnings.append(f"no LLM available ({exc}); nothing drafted")
            log(f"No LLM available ({exc}).")

        def _kick(crew_cls, inputs):
            result = crew_cls(llm=llm).crew().kickoff(inputs=inputs)
            u = extract_usage(result)
            usage["prompt_tokens"] += u.get("prompt_tokens", 0)
            usage["completion_tokens"] += u.get("completion_tokens", 0)
            return (result.raw if hasattr(result, "raw") else str(result)) or ""

        max_drafts = max(1, min(int(st.max_drafts or 5), 25))

        # Bidding is opt-in and needs the token; resolve the bidder once.
        bidder, bid_client = None, None
        no_bid_reason = ""  # with submit_bids on, why a draft wasn't bid on
        token = os.getenv("FREELANCER_OAUTH_TOKEN", "").strip()
        if st.submit_bids and not token:
            no_bid_reason = "FREELANCER_OAUTH_TOKEN is not set"
            warnings.append("submit_bids is on but FREELANCER_OAUTH_TOKEN is not set — drafts only")
        elif st.submit_bids:
            import httpx
            bid_client = httpx.Client(timeout=25, headers={"User-Agent": ft.USER_AGENT})
            try:
                bidder = ft.whoami(bid_client, token)
                log(f"Bidding as {bidder['username'] or bidder['id']}")
            except Exception as exc:  # noqa: BLE001
                no_bid_reason = f"the Freelancer token could not be verified ({exc})"
                warnings.append(f"could not verify the Freelancer token ({exc}) — drafts only")
        for p in found["candidates"] if llm is not None else []:
            if len(drafted) >= max_drafts:
                break
            if st.task_filter.strip():
                try:
                    verdict = _parse_verdict(_kick(GreenhouseRelevanceCrew, {
                        "task_filter": st.task_filter, "job_title": p["title"],
                        # The description often decides it (greenfield vs. maintenance, …).
                        "job_meta": (f"Freelance project ({budget_text(p)})\n"
                                     f"Skills: {', '.join(p['skills'])}\n"
                                     f"Description: {p['description'][:1500] or '(none)'}")}))
                except Exception as exc:  # noqa: BLE001 — fail open
                    verdict = None
                    log(f"Relevance judge failed ({exc}); keeping project.")
                if verdict is not None and not verdict.get("relevant"):
                    skipped.append({**ft._brief(p), "reason": f"filtered out: {verdict.get('reason') or 'task_filter'}",
                                    "filtered": True})
                    continue
            try:
                text = _kick(FreelancerProposalCrew, {
                    "project_title": p["title"], "project_budget": budget_text(p),
                    "project_skills": ", ".join(p["skills"]) or "(none tagged)",
                    "project_description": p["description"][:3000] or "(no description)",
                    "candidate_background": st.candidate_background,
                    "proposal_template": st.proposal_template.strip() or "(none)",
                    "previous_error": st.previous_error or "(none)"})
            except Exception as exc:  # noqa: BLE001 — one project never sinks the run
                skipped.append({**ft._brief(p), "reason": f"drafting failed ({type(exc).__name__})"})
                continue
            proposal, why = parse_proposal(text)
            if not proposal:
                skipped.append({**ft._brief(p), "reason": f"LLM declined: {why or 'empty draft'}"})
                log(f"↷ {p['title'][:60]}: declined — {why}")
                continue
            entry = ft.drafted_entry(p, proposal)
            status = "drafted"
            if bidder is not None:
                entry["bid"] = self._place(bid_client, token, bidder, p, entry, log)
                if entry["bid"]["status"] == "placed":
                    status = "bid_placed"
                elif entry["bid"].get("auth_error"):
                    warnings.append(f"bidding stopped: {entry['bid']['reason']}")
                    no_bid_reason = f"bidding stopped after an authentication error ({entry['bid']['reason']})"
                    bidder = None  # a token problem won't fix itself mid-run
            elif st.submit_bids:
                # Every draft reports an outcome when bidding was asked for.
                entry["bid"] = {"status": "not_placed", "reason": no_bid_reason or "bidding unavailable"}
            drafted.append(entry)
            try:
                ft.record_drafted(os.getenv("FREELANCER_LEDGER", ft.DEFAULT_LEDGER_PATH), p, status)
            except OSError as exc:  # a ledger problem never costs the run its drafts
                warnings.append(f"could not record project {p['id']} as drafted ({exc}); "
                                "it may be drafted again next run")
            log(f"✓ drafted: {p['title'][:60]}")

        if bid_client is not None:
            bid_client.close()
        st.usage = usage
        placed = sum(1 for d in drafted if (d.get("bid") or {}).get("status") == "placed")
        sent = f"{placed} bid(s) placed" if st.submit_bids else "not submitted"
        summary = (f"{found['projects_found']} project(s) found, {len(found['candidates'])} passed "
                   f"the filters, {len(drafted)} proposal(s) drafted ({sent}), "
                   f"{len(skipped)} skipped.")
        log("Freelancer drafting complete, formatting result...")
        return json.dumps({
            "summary": summary, "projects_found": found["projects_found"],
            "drafted_count": len(drafted), "skipped_count": len(skipped), "bids_placed": placed,
            "drafted": drafted, "skipped": skipped[:100], "warnings": warnings[:50],
            "note": ("Bids were placed only where drafted[].bid.status is 'placed'." if st.submit_bids else
                     "Draft-only: review each proposal and place the bid on Freelancer.com yourself."),
        }, ensure_ascii=False)

    def _place(self, client, token, bidder, project, entry, log) -> dict:
        """Place one bid; never raises. ``{status: placed|not_placed|failed, …}``."""
        amount = entry.get("suggested_bid")
        why = ft.bid_problem(project, entry["proposal"], amount)
        if why:
            return {"status": "not_placed", "reason": why}
        try:
            res = ft.place_bid(client, token, bidder_id=bidder["id"], project=project, amount=amount,
                               period_days=self.state.bid_period_days,
                               milestone_percentage=self.state.milestone_percentage,
                               description=entry["proposal"])
        except ft.FreelancerAPIError as exc:
            log(f"✗ bid on {project['title'][:50]} failed: {exc}")
            return {"status": "failed", "reason": str(exc)[:200], "auth_error": exc.is_auth}
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "reason": f"{type(exc).__name__}: {str(exc)[:160]}"}
        log(f"✓ bid placed on {project['title'][:50]}: {project['currency_sign']}{amount:g}")
        return {"status": "placed", "bid_id": res.get("bid_id"), "amount": amount,
                "currency": project["currency"]}
