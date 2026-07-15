from crewai.flow.flow import Flow, listen, start
from pydantic import BaseModel

from src.automation.crews.x_scraper_crew.crew import XScraperCrew
from src.automation.flows.base import FlowMixin
from src.automation.progress import append_log


class XScraperState(BaseModel):
    username: str = ""
    limit: int = 5
    run_id: int = 0
    usage: dict = {}
    llm_provider: str = ""
    llm_model: str = ""
    previous_error: str = ""


class XScraperFlow(FlowMixin, Flow[XScraperState]):

    @start()
    def validate_payload(self):
        self._check_required("username")
        append_log(self.state.run_id, f"Validated payload for @{self.state.username}")
        return self.state.model_dump()

    @listen(validate_payload)
    def execute_crew(self, _):
        return self._run_crew(
            XScraperCrew, temperature=0.3,
            inputs={"username": self.state.username, "limit": self.state.limit},
            working_log="Fetching posts via nitter...",
            done_log="Analysis complete, formatting result...",
        )
