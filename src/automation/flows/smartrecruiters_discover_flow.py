import json

from crewai.flow.flow import Flow, listen, start
from pydantic import BaseModel

# The relevance judge is site-agnostic: same crew and verdict parser as
# Greenhouse, so the fail-open gate behaves identically.
from src.automation.crews.greenhouse_relevance_crew.crew import GreenhouseRelevanceCrew
from src.automation.flows.base import FlowMixin
from src.automation.flows.greenhouse_apply_flow import _parse_verdict
from src.automation.flows.utils import extract_usage
from src.automation.progress import append_log
from src.automation.tools.smartrecruiters_discover_tool import run_smartrecruiters_discover


class SmartRecruitersDiscoverState(BaseModel):
    companies: str = ""      # company identifiers / URLs, comma-separated (required)
    keywords: str = ""       # title filter, comma-separated phrases (blank = all)
    location: str = ""       # job-location filter, comma-separated (blank = any)
    task_filter: str = ""    # optional 2nd gate: LLM relevance filter (nat-lang)
    max_results: int = 50
    only_new: bool = True    # skip postings returned by earlier runs
    run_id: int = 0
    usage: dict = {}
    llm_provider: str = ""
    llm_model: str = ""
    previous_error: str = ""


class SmartRecruitersDiscoverFlow(FlowMixin, Flow[SmartRecruitersDiscoverState]):

    @start()
    def validate_payload(self):
        self._check_required("companies")
        append_log(
            self.state.run_id,
            f"Payload validated — companies '{self.state.companies}', "
            f"keywords '{self.state.keywords or 'any'}', "
            f"location '{self.state.location or 'any'}', "
            f"max {self.state.max_results} result(s), only_new={self.state.only_new}",
        )
        return self.state.model_dump()

    @listen(validate_payload)
    def execute_discover(self, _):
        usage_acc = {"prompt_tokens": 0, "completion_tokens": 0}
        relevance_fn = None
        task_filter = (self.state.task_filter or "").strip()
        if task_filter:
            llm = None
            try:
                from src.automation.harness.provider import resolve as resolve_llm
                llm, _p, _m = resolve_llm(self.state.llm_provider or None,
                                          self.state.llm_model or None, temperature=0.2)
            except Exception as exc:  # noqa: BLE001
                append_log(self.state.run_id, f"No LLM available ({exc}).")
            if llm is None:
                append_log(self.state.run_id,
                           "task_filter is set but no LLM is available; returning all matches.")
            else:
                append_log(self.state.run_id,
                           "Second gate active: filtering postings by task_filter.")

                def relevance_fn(title: str, meta: str) -> tuple[bool, str]:
                    try:
                        result = GreenhouseRelevanceCrew(llm=llm).crew().kickoff(inputs={
                            "task_filter": task_filter, "job_title": title, "job_meta": meta,
                        })
                        u = extract_usage(result)
                        usage_acc["prompt_tokens"] += u.get("prompt_tokens", 0)
                        usage_acc["completion_tokens"] += u.get("completion_tokens", 0)
                        text = (result.raw if hasattr(result, "raw") else str(result)) or ""
                        verdict = _parse_verdict(text)
                        if verdict is None:
                            append_log(self.state.run_id,
                                       f"Relevance verdict unparseable ({text[:80]!r}); "
                                       "keeping posting (fail-open).")
                            return True, ""
                        return bool(verdict.get("relevant")), str(verdict.get("reason") or "")
                    except Exception as exc:  # noqa: BLE001
                        append_log(self.state.run_id,
                                   f"Relevance judge failed ({exc}); keeping posting (fail-open).")
                        return True, ""

        result = run_smartrecruiters_discover(
            companies=self.state.companies,
            keywords=self.state.keywords,
            location=self.state.location,
            max_results=self.state.max_results,
            only_new=self.state.only_new,
            relevance_fn=relevance_fn,
            log=lambda m: append_log(self.state.run_id, m),
        )
        self.state.usage = usage_acc
        append_log(self.state.run_id, "SmartRecruiters discovery run complete, formatting result...")
        return json.dumps(result, ensure_ascii=False)
