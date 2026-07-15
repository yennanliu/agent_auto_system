from crewai.flow.flow import Flow, listen, start
from pydantic import BaseModel

from src.automation.crews.hn_digest_crew.crew import HNDigestCrew
from src.automation.flows.base import FlowMixin
from src.automation.flows.utils import extract_usage
from src.automation.progress import append_log


class HNDigestState(BaseModel):
    limit: int = 5
    run_id: int = 0
    usage: dict = {}
    llm_provider: str = ""
    llm_model: str = ""
    previous_error: str = ""


class HNDigestFlow(FlowMixin, Flow[HNDigestState]):

    @start()
    def validate_payload(self):
        if not 1 <= self.state.limit <= 10:
            raise ValueError("limit must be between 1 and 10")
        append_log(self.state.run_id, f"Fetching top {self.state.limit} HN stories...")
        return self.state.model_dump()

    @listen(validate_payload)
    def execute_crew(self, _):
        from src.automation.harness.provider import resolve as resolve_llm
        llm, _, _ = resolve_llm(
            self.state.llm_provider or None,
            self.state.llm_model or None,
            temperature=0.4,
        )
        append_log(self.state.run_id, "HN analyst agent reading stories...")
        crew = HNDigestCrew(llm=llm)
        result = crew.crew().kickoff(inputs={
            "limit": self.state.limit,
            "previous_error": self.state.previous_error,
        })
        self.state.usage = extract_usage(result)
        append_log(self.state.run_id, "Digest generated, formatting result...")
        return result.raw if hasattr(result, "raw") else str(result)
