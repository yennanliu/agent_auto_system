from crewai.flow.flow import Flow, listen, start
from pydantic import BaseModel

from src.automation.crews.google_sheet_crew.crew import GoogleSheetCrew
from src.automation.flows.base import FlowMixin
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
        return self._run_crew(
            GoogleSheetCrew, temperature=0.1,
            inputs={"url": self.state.url, "limit": self.state.limit},
            working_log="Fetching Google Sheet data...",
            done_log="Analyzing sheet data, formatting result...",
        )
