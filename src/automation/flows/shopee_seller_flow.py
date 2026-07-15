from crewai.flow.flow import Flow, listen, start
from pydantic import BaseModel

from src.automation.crews.shopee_seller_crew.crew import ShopeeSellerCrew
from src.automation.flows.base import FlowMixin
from src.automation.flows.utils import extract_usage
from src.automation.progress import append_log


class ShopeeSellerState(BaseModel):
    keyword: str = ""
    limit: int = 5
    run_id: int = 0
    usage: dict = {}
    llm_provider: str = ""
    llm_model: str = ""
    previous_error: str = ""


class ShopeeSellerFlow(FlowMixin, Flow[ShopeeSellerState]):

    @start()
    def validate_payload(self):
        self._check_required("keyword")
        append_log(self.state.run_id, f"Validated payload for keyword '{self.state.keyword}'")
        return self.state.model_dump()

    @listen(validate_payload)
    def execute_crew(self, _):
        from src.automation.harness.provider import resolve as resolve_llm
        llm, _, _ = resolve_llm(
            self.state.llm_provider or None,
            self.state.llm_model or None,
            temperature=0.2,
        )
        append_log(self.state.run_id, "Loading Shopee session and searching products...")
        crew = ShopeeSellerCrew(llm=llm)
        result = crew.crew().kickoff(inputs={
            "keyword": self.state.keyword,
            "limit": self.state.limit,
            "previous_error": self.state.previous_error,
        })
        self.state.usage = extract_usage(result)
        append_log(self.state.run_id, "Seller collection complete, formatting result...")
        return result.raw if hasattr(result, "raw") else str(result)
