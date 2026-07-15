from crewai.flow.flow import Flow, listen, start
from pydantic import BaseModel

from src.automation.crews.form_crew.crew import FormFillerCrew
from src.automation.flows.base import FlowMixin
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
        return self._run_crew(
            FormFillerCrew, temperature=0.0,
            inputs={
                "company_name": self.state.company_name,
                "company_size": self.state.company_size,
                "ai_problem": self.state.ai_problem,
            },
            working_log="Inspecting Google Form structure...",
            done_log="Form submission attempted, reading result...",
        )
