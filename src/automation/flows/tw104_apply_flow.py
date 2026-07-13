import json

from crewai.flow.flow import Flow, listen, start
from pydantic import BaseModel

from src.automation.crews.tw104_relevance_crew.crew import TW104RelevanceCrew
from src.automation.flows.base import FlowMixin
from src.automation.flows.utils import extract_usage
from src.automation.progress import append_log
from src.automation.tools.tw104_apply_tool import run_tw104_apply


def _parse_verdict(text: str) -> dict | None:
    """Best-effort extract a {"relevant": bool, "reason": str} object from LLM
    text. Tolerates ```json fences and surrounding prose; returns None if no
    usable object with a boolean-ish ``relevant`` can be found (caller fails
    open). Mirrors the tasker_apply flow's parser."""
    if not text:
        return None
    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start:end + 1])
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and "relevant" in obj:
            val = obj["relevant"]
            if isinstance(val, str):
                obj["relevant"] = val.strip().lower() in (
                    "true", "yes", "y", "1", "相關", "符合", "是", "對")
            return obj
    return None


class TW104ApplyState(BaseModel):
    keyword: str = ""               # 104 search keyword (required)
    area: str = ""                  # optional 104 area code(s), e.g. 6001001000
    order: str = "1"                # listing sort order
    cover_letter: str = ""          # optional saved 推薦信 name; blank = site default
    task_filter: str = ""           # optional 2nd gate: LLM relevance filter (nat-lang)
    max_applications: int = 5
    max_pages: int = 10
    dry_run: bool = True            # safety: don't click 確認送出 unless explicitly false
    run_id: int = 0
    usage: dict = {}
    llm_provider: str = ""
    llm_model: str = ""
    previous_error: str = ""


class TW104ApplyFlow(FlowMixin, Flow[TW104ApplyState]):

    @start()
    def validate_payload(self):
        self._check_required("keyword")
        append_log(
            self.state.run_id,
            f"Payload validated — keyword '{self.state.keyword}', "
            f"area '{self.state.area or 'all'}', "
            f"max {self.state.max_applications} application(s), "
            f"dry_run={self.state.dry_run}",
        )
        return self.state.model_dump()

    @listen(validate_payload)
    def execute_apply(self, _):
        usage_acc = {"prompt_tokens": 0, "completion_tokens": 0}

        # The LLM is used only for the optional relevance gate, which is why this
        # automation runs with whichever provider/model the run selected. If no
        # task_filter is set (or no LLM is available) we apply to every eligible
        # job — no LLM call is made.
        task_filter = (self.state.task_filter or "").strip()
        relevance_fn = None
        if task_filter:
            llm = None
            try:
                from src.automation.harness.provider import resolve as resolve_llm
                llm, _p, _m = resolve_llm(
                    self.state.llm_provider or None,
                    self.state.llm_model or None,
                    temperature=0.2,
                )
            except Exception as exc:  # noqa: BLE001
                append_log(self.state.run_id,
                           f"task_filter is set but no LLM is available ({exc}); "
                           "applying to all eligible jobs.")
            if llm is not None:
                append_log(self.state.run_id,
                           "Second gate active: filtering jobs by task_filter before applying.")

                def relevance_fn(title: str, meta: str) -> tuple[bool, str]:  # noqa: F811
                    try:
                        result = TW104RelevanceCrew(llm=llm).crew().kickoff(inputs={
                            "task_filter": task_filter,
                            "job_title": title,
                            "job_meta": meta,
                        })
                        u = extract_usage(result)
                        usage_acc["prompt_tokens"] += u.get("prompt_tokens", 0)
                        usage_acc["completion_tokens"] += u.get("completion_tokens", 0)
                        text = (result.raw if hasattr(result, "raw") else str(result)) or ""
                        verdict = _parse_verdict(text)
                        if verdict is None:
                            append_log(self.state.run_id,
                                       f"Relevance verdict unparseable ({text[:80]!r}); "
                                       "keeping job (fail-open).")
                            return True, ""
                        return bool(verdict.get("relevant")), str(verdict.get("reason") or "")
                    except Exception as exc:  # noqa: BLE001 — never fail a job on the gate
                        append_log(self.state.run_id,
                                   f"Relevance judge failed ({exc}); keeping job (fail-open).")
                        return True, ""

        append_log(self.state.run_id, "Loading 104.com.tw session and scanning jobs...")
        result = run_tw104_apply(
            keyword=self.state.keyword,
            area=self.state.area,
            order=self.state.order or "1",
            max_applications=self.state.max_applications,
            max_pages=self.state.max_pages,
            cover_letter=self.state.cover_letter,
            dry_run=self.state.dry_run,
            relevance_fn=relevance_fn,
            log=lambda m: append_log(self.state.run_id, m),
        )

        self.state.usage = usage_acc
        append_log(self.state.run_id, "104 apply run complete, formatting result...")
        return json.dumps(result, ensure_ascii=False)
