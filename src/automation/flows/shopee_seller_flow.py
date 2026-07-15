from crewai.flow.flow import Flow, listen, start
from pydantic import BaseModel

from src.automation.crews.shopee_seller_crew.crew import ShopeeSellerCrew
from src.automation.flows.base import FlowMixin
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
        return self._run_crew(
            ShopeeSellerCrew, temperature=0.2,
            inputs={"keyword": self.state.keyword, "limit": self.state.limit},
            working_log="Loading Shopee session and searching products...",
            done_log="Seller collection complete, formatting result...",
        )
