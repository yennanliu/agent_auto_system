from crewai.flow.flow import Flow, listen, start
from pydantic import BaseModel

from src.automation.crews.form_crew.crew import FormFillerCrew
from src.automation.flows.base import FlowMixin
from src.automation.flows.utils import extract_usage
from src.automation.progress import append_log


class FormFillState(BaseModel):
    company_name: str = ""
    company_size: str = ""
    ai_problem: str = ""
    run_id: int = 0
    usage: dict = {}
    llm_provider: str = ""
    llm_model: str = ""
    previous_error: str = ""


class FormFillFlow(FlowMixin, Flow[FormFillState]):

    @start()
    def validate_payload(self):
        self._check_required("company_name", "company_size", "ai_problem")
        append_log(self.state.run_id, "Payload validated, launching form agent...")
        return self.state.model_dump()

    @listen(validate_payload)
    def execute_crew(self, _):
        from src.automation.harness.provider import resolve as resolve_llm
        llm, _, _ = resolve_llm(
            self.state.llm_provider or None,
            self.state.llm_model or None,
            temperature=0.0,
        )
        append_log(self.state.run_id, "Inspecting Google Form structure...")
        crew = FormFillerCrew(llm=llm)
        result = crew.crew().kickoff(inputs={
            "company_name": self.state.company_name,
            "company_size": self.state.company_size,
            "ai_problem": self.state.ai_problem,
            "previous_error": self.state.previous_error,
        })
        self.state.usage = extract_usage(result)
        append_log(self.state.run_id, "Form submission attempted, reading result...")
        return result.raw if hasattr(result, "raw") else str(result)
