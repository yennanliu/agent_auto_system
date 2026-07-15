from crewai.flow.flow import Flow, listen, start
from pydantic import BaseModel

from src.automation.crews.web_scraper_crew.crew import WebScraperCrew
from src.automation.flows.base import FlowMixin
from src.automation.flows.utils import extract_usage
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
        from src.automation.harness.provider import resolve as resolve_llm
        llm, _, _ = resolve_llm(
            self.state.llm_provider or None,
            self.state.llm_model or None,
            temperature=0.1,
        )
        append_log(self.state.run_id, "Web scraper agent reading page content...")
        crew = WebScraperCrew(llm=llm)
        result = crew.crew().kickoff(inputs={
            "url": self.state.url,
            "previous_error": self.state.previous_error,
        })
        self.state.usage = extract_usage(result)
        append_log(self.state.run_id, "Agent generated summary, formatting result...")
        return result.raw if hasattr(result, "raw") else str(result)
