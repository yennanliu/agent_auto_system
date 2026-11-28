"""Upwork: find and rank matching jobs and draft a proposal for each — DRAFT ONLY.

Deterministic funnel (CLAUDE.md): ``upwork_tool.find_jobs`` discovers jobs
(official GraphQL API with the user's token, and/or postings the user pasted),
filters and ranks them; the LLM only judges relevance (optional
``task_filter``) and drafts each proposal from the user's own
``candidate_background`` (the drafting crew is shared with ``freelancer_bids``).

**Nothing is ever submitted and no Connects are spent.** There is no submit
code path in this flow or its tool — the tool's GraphQL helper refuses any
non-query document. The result lists each job's URL, a suggested rate / bid and
the drafted proposal; a human opens the URL, reviews and clicks Submit.
"""
import json
import os

from crewai.flow.flow import Flow, listen, start
from pydantic import BaseModel

from src.automation.crews.freelancer_proposal_crew.crew import FreelancerProposalCrew
from src.automation.crews.greenhouse_relevance_crew.crew import GreenhouseRelevanceCrew
from src.automation.flows.base import FlowMixin
from src.automation.flows.freelancer_bids_flow import parse_proposal
from src.automation.flows.greenhouse_apply_flow import _parse_verdict
from src.automation.flows.utils import extract_usage
from src.automation.progress import append_log
from src.automation.tools import upwork_tool as ut

DRAFT_ONLY_NOTE = ("Draft-only: nothing was submitted and no Connects were spent. Open each job's url, "
                   "review the proposal (and answer any screening questions) and submit it on Upwork yourself.")


class UpworkDraftsState(BaseModel):
    keywords: str = ""                 # API search terms (needs the user's Upwork API token)
    jobs_text: str = ""                # pasted postings, separated by '---' lines (no API needed)
    candidate_background: str = ""     # the only facts the drafts may use (required)
    proposal_template: str = ""        # optional reusable text (links / sign-off kept)
    task_filter: str = ""              # optional LLM relevance gate
    job_type: str = "any"              # any | fixed | hourly
    min_fixed_budget: float = 0        # skip fixed-price jobs below this (USD)
    min_hourly_rate: float = 0         # skip hourly jobs whose range tops out below this
    hourly_rate: float = 0             # your rate — used for the suggested rate on hourly jobs
    max_applicants: int = 20           # skip jobs with more proposals than this (0 = any)
    payment_verified_only: bool = True
    max_drafts: int = 5
    run_id: int = 0
    usage: dict = {}
    llm_provider: str = ""
    llm_model: str = ""
    previous_error: str = ""


class UpworkDraftsFlow(FlowMixin, Flow[UpworkDraftsState]):

    @start()
    def validate_payload(self):
        self._check_required("candidate_background")
        if not (self.state.keywords.strip() or self.state.jobs_text.strip()):
            raise ValueError("Missing required fields: give keywords (API search) or paste jobs_text")
        append_log(self.state.run_id,
                   f"Payload validated — keywords '{self.state.keywords}', "
                   f"{'pasted jobs, ' if self.state.jobs_text.strip() else ''}"
                   f"type {self.state.job_type}, max {self.state.max_drafts} draft(s), draft-only")
        return self.state.model_dump()

    @listen(validate_payload)
    def execute_drafts(self, _):
        st = self.state
        log = lambda m: append_log(st.run_id, m)  # noqa: E731
        log("Searching Upwork jobs...")
        found = ut.find_jobs(keywords=st.keywords, jobs_text=st.jobs_text, job_type=st.job_type,
                             min_fixed_budget=st.min_fixed_budget, min_hourly_rate=st.min_hourly_rate,
                             max_applicants=st.max_applicants, payment_verified_only=st.payment_verified_only,
                             log=log)
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
        ledger = os.getenv("UPWORK_LEDGER", ut.DEFAULT_LEDGER_PATH)
        for j in found["candidates"] if llm is not None else []:
            if len(drafted) >= max_drafts:
                break
            if st.task_filter.strip():
                try:
                    verdict = _parse_verdict(_kick(GreenhouseRelevanceCrew, {
                        "task_filter": st.task_filter, "job_title": j["title"],
                        "job_meta": (f"Upwork job ({ut.budget_text(j)})\n"
                                     f"Skills: {', '.join(j['skills']) or '(none tagged)'}\n"
                                     f"Description: {j['description'][:1500] or '(none)'}")}))
                except Exception as exc:  # noqa: BLE001 — fail open
                    verdict = None
                    log(f"Relevance judge failed ({exc}); keeping job.")
                if verdict is not None and not verdict.get("relevant"):
                    skipped.append({**ut.brief(j), "reason": f"filtered out: {verdict.get('reason') or 'task_filter'}",
                                    "filtered": True})
                    continue
            try:
                text = _kick(FreelancerProposalCrew, {
                    "platform": "Upwork", "project_title": j["title"], "project_budget": ut.budget_text(j),
                    "project_skills": ", ".join(j["skills"]) or "(none tagged)",
                    "project_description": j["description"][:3000] or "(no description)",
                    "candidate_background": st.candidate_background,
                    "proposal_template": st.proposal_template.strip() or "(none)",
                    "previous_error": st.previous_error or "(none)"})
            except Exception as exc:  # noqa: BLE001 — one job never sinks the run
                skipped.append({**ut.brief(j), "reason": f"drafting failed ({type(exc).__name__})"})
                continue
            proposal, why = parse_proposal(text)
            if not proposal:
                skipped.append({**ut.brief(j), "reason": f"LLM declined: {why or 'empty draft'}"})
                log(f"↷ {j['title'][:60]}: declined — {why}")
                continue
            drafted.append(ut.drafted_entry(j, proposal, len(drafted) + 1, st.hourly_rate))
            try:
                ut.record_drafted(ledger, j)
            except OSError as exc:  # a ledger problem never costs the run its drafts
                warnings.append(f"could not record job {j['id']} as drafted ({exc}); "
                                "it may be drafted again next run")
            log(f"✓ drafted: {j['title'][:60]}")

        st.usage = usage
        summary = (f"{found['jobs_found']} job(s) found, {len(found['candidates'])} passed the filters, "
                   f"{len(drafted)} proposal(s) drafted (not submitted), {len(skipped)} skipped.")
        log("Upwork drafting complete, formatting result...")
        return json.dumps({
            "summary": summary, "sources": found["sources"], "jobs_found": found["jobs_found"],
            "drafted_count": len(drafted), "skipped_count": len(skipped), "submitted": 0,
            "drafted": drafted, "skipped": skipped[:100], "warnings": warnings[:50],
            "note": DRAFT_ONLY_NOTE,
        }, ensure_ascii=False)
