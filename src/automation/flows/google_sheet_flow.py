from crewai.flow.flow import Flow, listen, start
from pydantic import BaseModel

from src.automation.crews.google_sheet_crew.crew import GoogleSheetCrew
from src.automation.flows.base import FlowMixin
from src.automation.flows.utils import extract_usage
from src.automation.progress import append_log


class GoogleSheetState(BaseModel):
    url: str = ""
    limit: int = 200
    run_id: int = 0
    usage: dict = {}
    llm_provider: str = ""
    llm_model: str = ""
    previous_error: str = ""


class GoogleSheetFlow(FlowMixin, Flow[GoogleSheetState]):

    @start()
    def validate_payload(self):
        self._check_required("url")
        append_log(self.state.run_id, f"Validated sheet URL: {self.state.url}")
        return self.state.model_dump()

    @listen(validate_payload)
    def execute_crew(self, _):
        from src.automation.harness.provider import resolve as resolve_llm
        llm, _, _ = resolve_llm(
            self.state.llm_provider or None,
            self.state.llm_model or None,
            temperature=0.1,
        )
        append_log(self.state.run_id, "Fetching Google Sheet data...")
        crew = GoogleSheetCrew(llm=llm)
        result = crew.crew().kickoff(inputs={
            "url":            self.state.url,
            "limit":          self.state.limit,
            "previous_error": self.state.previous_error,
        })
        self.state.usage = extract_usage(result)
        append_log(self.state.run_id, "Analyzing sheet data, formatting result...")
        return result.raw if hasattr(result, "raw") else str(result)
