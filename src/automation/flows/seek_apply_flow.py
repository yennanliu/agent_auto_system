"""Seek / JobStreet Quick Apply flow (deterministic funnel).

The tool drives search → details → wizard itself; the LLM is only the optional
relevance gate and the answerer for required employer questions the rules miss
(both shared with the ATS apply flows via ``build_llm_callbacks``). Link-out
jobs on a supported ATS are handed to ``run_ats_apply`` when the run has the
contact details an ATS form needs."""
import json
from functools import partial

from crewai.flow.flow import Flow, listen, start

from src.automation.flows.base import FlowMixin
from src.automation.flows.greenhouse_apply_flow import GreenhouseApplyState, build_llm_callbacks
from src.automation.progress import append_log
from src.automation.tools.ats_common import build_profile
from src.automation.tools.seek_apply_tool import resolve_site, run_seek_apply

# Profile inputs shared by the Seek planner and (for link-out jobs) ATS Apply.
_PROFILE_FIELDS = (
    "phone", "current_location", "linkedin_url", "github_url", "website_url", "years_experience",
    "salary_expectation", "work_authorized", "needs_sponsorship", "authorized_countries",
    "extra_answers", "current_salary", "notice_period", "languages", "programming_languages",
    "ai_experience", "reason_for_leaving", "has_criminal_record", "school", "degree", "discipline",
    "current_company", "current_title", "current_start",
)


class SeekApplyState(GreenhouseApplyState):
    """``keywords`` is required (comma-separated search phrases); ``site`` is
    one market: au · nz · sg · my · ph · id. Name / e-mail / résumé are only
    needed to route link-out jobs to ATS Apply — Seek's own Quick Apply uses
    the account's profile and saved résumé."""
    site: str = "au"
    max_pages: int = 3
    resume_name: str = ""           # pick this saved Seek résumé (substring); blank = Seek's default
    route_offsite: bool = True      # link-out jobs on Greenhouse / Lever / … → ATS Apply
    salary_expectation: str = ""    # e.g. "120000" — picks the nearest salary band


class SeekApplyFlow(FlowMixin, Flow[SeekApplyState]):

    @start()
    def validate_payload(self):
        self._check_required("keywords")
        if resolve_site(self.state.site) is None:
            raise ValueError(f"Unknown site '{self.state.site}' — use au, nz, sg, my, ph or id")
        append_log(
            self.state.run_id,
            f"Payload validated — keywords '{self.state.keywords}', site '{self.state.site}', "
            f"location '{self.state.location or 'any'}', "
            f"max {self.state.max_applications} application(s), dry_run={self.state.dry_run}",
        )
        return self.state.model_dump()

    @listen(validate_payload)
    def execute_apply(self, _):
        st = self.state
        relevance_fn, answer_fn, usage_acc = build_llm_callbacks(st)
        log = partial(append_log, st.run_id)
        profile = build_profile(
            first_name=st.first_name, last_name=st.last_name, email=st.email,
            cover_letter_text=st.cover_letter, resume_path=st.resume_path,
            **{f: getattr(st, f) for f in _PROFILE_FIELDS},
        )

        ats_apply_fn = None
        if st.route_offsite:
            if st.first_name.strip() and st.last_name.strip() and st.email.strip():
                from src.automation.tools.ats_apply_tool import run_ats_apply

                def ats_apply_fn(*, boards: str, max_applications: int) -> dict:  # noqa: F811
                    return run_ats_apply(
                        boards=boards, first_name=st.first_name, last_name=st.last_name,
                        email=st.email, resume_path=st.resume_path, cover_letter=st.cover_letter,
                        max_applications=max_applications, dry_run=st.dry_run,
                        answer_fn=answer_fn, log=log,  # relevance was judged on Seek already
                        **{f: getattr(st, f) for f in _PROFILE_FIELDS},
                    )
            else:
                log("Link-out jobs will be listed for manual apply: routing them to ATS Apply "
                    "needs first_name, last_name and email.")

        log("Loading Seek session and searching jobs...")
        result = run_seek_apply(
            keywords=st.keywords, site=st.site, location=st.location, profile=profile,
            max_applications=st.max_applications, max_pages=st.max_pages,
            cover_letter=st.cover_letter, resume_name=st.resume_name, dry_run=st.dry_run,
            relevance_fn=relevance_fn, answer_fn=answer_fn, ats_apply_fn=ats_apply_fn, log=log,
        )
        self.state.usage = usage_acc
        append_log(st.run_id, "Seek apply run complete, formatting result...")
        return json.dumps(result, ensure_ascii=False)
