from crewai.flow.flow import Flow, listen, start
from pydantic import BaseModel

from src.automation.crews.hn_digest_crew.crew import HNDigestCrew
from src.automation.flows.base import FlowMixin
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
        return self._run_crew(
            HNDigestCrew, temperature=0.4,
            inputs={"limit": self.state.limit},
            working_log="HN analyst agent reading stories...",
            done_log="Digest generated, formatting result...",
        )
