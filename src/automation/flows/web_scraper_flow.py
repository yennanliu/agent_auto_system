from crewai.flow.flow import Flow, listen, start
from pydantic import BaseModel

from src.automation.crews.web_scraper_crew.crew import WebScraperCrew
from src.automation.flows.base import FlowMixin
from src.automation.progress import append_log


class WebScraperState(BaseModel):
    url: str = ""
    run_id: int = 0
    usage: dict = {}
    llm_provider: str = ""
    llm_model: str = ""
    previous_error: str = ""


class WebScraperFlow(FlowMixin, Flow[WebScraperState]):

    @start()
    def validate_payload(self):
        self._check_required("url")
        append_log(self.state.run_id, f"Payload validated, fetching {self.state.url}...")
        return self.state.model_dump()

    @listen(validate_payload)
    def execute_crew(self, _):
        return self._run_crew(
            WebScraperCrew, temperature=0.1,
            inputs={"url": self.state.url},
            working_log="Web scraper agent reading page content...",
            done_log="Agent generated summary, formatting result...",
        )
