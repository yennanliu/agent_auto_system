"""Multi-ATS auto-apply flow (Greenhouse, Lever, Ashby, Workable; SmartRecruiters
listed for manual apply). Same profile, LLM gates and safety rules as
``greenhouse_apply`` — only the runner differs (``run_ats_apply`` routes each
board to its platform adapter)."""
import json

from crewai.flow.flow import Flow, listen, start

from src.automation.flows.base import FlowMixin
from src.automation.flows.greenhouse_apply_flow import GreenhouseApplyState, build_llm_callbacks
from src.automation.progress import append_log
from src.automation.tools.ats_apply_tool import run_ats_apply


class ATSApplyState(GreenhouseApplyState):
    """``boards`` takes board / job URLs on any supported ATS, or
    ``platform:token`` shorthand (``lever:palantir``); ``sources`` names
    discovery sources (``hn, remoteok, remotive, weworkremotely``). At least
    one of the two is required."""
    sources: str = ""
    create_workday_accounts: bool = False


class ATSApplyFlow(FlowMixin, Flow[ATSApplyState]):

    @start()
    def validate_payload(self):
        self._check_required("first_name", "last_name", "email")
        if not (self.state.boards.strip() or self.state.sources.strip()):
            raise ValueError("Missing required fields: ['boards' or 'sources']")
        append_log(
            self.state.run_id,
            f"Payload validated — boards '{self.state.boards or '-'}', "
            f"sources '{self.state.sources or '-'}', "
            f"keywords '{self.state.keywords or 'any'}', "
            f"location '{self.state.location or 'any'}', "
            f"max {self.state.max_applications} application(s), "
            f"dry_run={self.state.dry_run}",
        )
        return self.state.model_dump()

    @listen(validate_payload)
    def execute_apply(self, _):
        relevance_fn, answer_fn, tailor_fn, usage_acc = build_llm_callbacks(self.state)
        st = self.state
        result = run_ats_apply(
            boards=st.boards, sources=st.sources, keywords=st.keywords, location=st.location,
            first_name=st.first_name, last_name=st.last_name, email=st.email, phone=st.phone,
            resume_path=st.resume_path, current_location=st.current_location,
            linkedin_url=st.linkedin_url, github_url=st.github_url, website_url=st.website_url,
            cover_letter=st.cover_letter, years_experience=st.years_experience,
            salary_expectation=st.salary_expectation, work_authorized=st.work_authorized,
            needs_sponsorship=st.needs_sponsorship, authorized_countries=st.authorized_countries,
            school=st.school, degree=st.degree, discipline=st.discipline,
            current_company=st.current_company, current_title=st.current_title,
            current_start=st.current_start, extra_answers=st.extra_answers,
            current_salary=st.current_salary, notice_period=st.notice_period,
            languages=st.languages, programming_languages=st.programming_languages,
            ai_experience=st.ai_experience, reason_for_leaving=st.reason_for_leaving,
            has_criminal_record=st.has_criminal_record,
            max_applications=st.max_applications, max_per_board=st.max_per_board,
            max_scans_per_board=st.max_scans_per_board, dry_run=st.dry_run,
            create_workday_accounts=st.create_workday_accounts,
            relevance_fn=relevance_fn, answer_fn=answer_fn, tailor_resume_fn=tailor_fn,
            log=lambda m: append_log(st.run_id, m),
        )
        self.state.usage = usage_acc
        append_log(self.state.run_id, "ATS apply run complete, formatting result...")
        return json.dumps(result, ensure_ascii=False)
