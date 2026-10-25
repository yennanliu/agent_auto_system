import json

from crewai.flow.flow import Flow, listen, start
from pydantic import BaseModel

from src.automation.crews.tasker_apply_crew.crew import TaskerProposalCrew
from src.automation.crews.tasker_relevance_crew.crew import TaskerRelevanceCrew
from src.automation.flows.base import FlowMixin
from src.automation.flows.utils import extract_usage, kickoff_crew
from src.automation.progress import append_log
from src.automation.tools.tasker_apply_tool import (
    _DEFAULT_TEMPLATE,
    _MIN_CHARGE_FLOOR,
    run_tasker_apply,
)


def _parse_verdict(text: str) -> dict | None:
    """Best-effort extract a {"relevant": bool, "reason": str} object from LLM text.

    Tolerates ```json fences and surrounding prose; returns None if no usable
    object with a boolean-ish ``relevant`` can be found (caller fails open)."""
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


class TaskerApplyState(BaseModel):
    category_ids: str = ""          # e.g. "110" or "110,101001"
    min_charge: int = 0
    max_charge: int = 0
    proposal_template: str = ""     # optional base/fallback for the LLM writer
    task_filter: str = ""           # optional 2nd gate: LLM relevance filter (nat-lang)
    max_cases: int = 5
    dry_run: bool = True            # safety: don't click 送出提案 unless explicitly false
    run_id: int = 0
    usage: dict = {}
    llm_provider: str = ""
    llm_model: str = ""
    previous_error: str = ""


class TaskerApplyFlow(FlowMixin, Flow[TaskerApplyState]):

    @start()
    def validate_payload(self):
        self._check_required("category_ids", "min_charge", "max_charge")
        if self.state.min_charge < _MIN_CHARGE_FLOOR:
            raise ValueError(f"min_charge must be >= {_MIN_CHARGE_FLOOR} (site minimum)")
        if self.state.min_charge > self.state.max_charge:
            raise ValueError("min_charge must be <= max_charge")
        append_log(
            self.state.run_id,
            f"Payload validated — category {self.state.category_ids}, "
            f"{self.state.min_charge}~{self.state.max_charge}, "
            f"max {self.state.max_cases} case(s), dry_run={self.state.dry_run}",
        )
        return self.state.model_dump()

    @listen(validate_payload)
    def execute_apply(self, _):
        template = self.state.proposal_template or _DEFAULT_TEMPLATE

        # Resolve the LLM used to write a tailored 提案說明 per case. If no key /
        # provider is available we fall back to the static template so the run
        # still works without an LLM.
        llm = None
        try:
            from src.automation.harness.provider import resolve as resolve_llm
            llm, _p, _m = resolve_llm(
                self.state.llm_provider or None,
                self.state.llm_model or None,
                temperature=0.5,
            )
        except Exception as exc:  # noqa: BLE001
            append_log(self.state.run_id,
                       f"No LLM available ({exc}); using default proposal template.")

        usage_acc = {"prompt_tokens": 0, "completion_tokens": 0}

        def proposal_fn(title: str, description: str) -> str:
            if llm is None:
                try:
                    return template.format(title=title)
                except Exception:  # noqa: BLE001
                    return template
            try:
                result = kickoff_crew(TaskerProposalCrew(llm=llm).crew(), {
                    "case_title": title,
                    "case_description": description,
                    "min_charge": self.state.min_charge,
                    "max_charge": self.state.max_charge,
                    "proposal_template": template,
                    "previous_error": self.state.previous_error,
                })
                u = extract_usage(result)
                usage_acc["prompt_tokens"] += u.get("prompt_tokens", 0)
                usage_acc["completion_tokens"] += u.get("completion_tokens", 0)
                text = result.raw if hasattr(result, "raw") else str(result)
                return (text or "").strip() or template
            except Exception as exc:  # noqa: BLE001 — never fail a case on text-gen
                append_log(self.state.run_id,
                           f"Proposal generation failed ({exc}); using template.")
                try:
                    return template.format(title=title)
                except Exception:  # noqa: BLE001
                    return template

        # Second gate: an optional natural-language task_filter. When set (and an
        # LLM is available) each scanned case is judged for relevance BEFORE we
        # spend a proposal-writing call; irrelevant cases are skipped. Fails open
        # (keeps the case) on any judge/parse error so a flaky LLM never silently
        # drops good cases — mirrors proposal_fn's degrade-gracefully policy.
        task_filter = (self.state.task_filter or "").strip()
        relevance_fn = None
        if task_filter and llm is None:
            append_log(self.state.run_id,
                       "task_filter is set but no LLM is available; skipping the "
                       "relevance gate (applying to all eligible cases).")
        elif task_filter:
            append_log(self.state.run_id,
                       "Second gate active: filtering cases by task_filter before proposing.")

            def relevance_fn(title: str, description: str) -> tuple[bool, str]:  # noqa: F811
                try:
                    result = kickoff_crew(TaskerRelevanceCrew(llm=llm).crew(), {
                        "task_filter": task_filter,
                        "case_title": title,
                        "case_description": description,
                    })
                    u = extract_usage(result)
                    usage_acc["prompt_tokens"] += u.get("prompt_tokens", 0)
                    usage_acc["completion_tokens"] += u.get("completion_tokens", 0)
                    text = (result.raw if hasattr(result, "raw") else str(result)) or ""
                    verdict = _parse_verdict(text)
                    if verdict is None:
                        append_log(self.state.run_id,
                                   f"Relevance verdict unparseable ({text[:80]!r}); "
                                   "keeping case (fail-open).")
                        return True, ""
                    return bool(verdict.get("relevant")), str(verdict.get("reason") or "")
                except Exception as exc:  # noqa: BLE001 — never fail a case on the gate
                    append_log(self.state.run_id,
                               f"Relevance judge failed ({exc}); keeping case (fail-open).")
                    return True, ""

        append_log(self.state.run_id, "Loading tasker.com.tw session and scanning cases...")
        result = run_tasker_apply(
            category_ids=self.state.category_ids,
            min_charge=self.state.min_charge,
            max_charge=self.state.max_charge,
            max_cases=self.state.max_cases,
            dry_run=self.state.dry_run,
            proposal_fn=proposal_fn,
            relevance_fn=relevance_fn,
            log=lambda m: append_log(self.state.run_id, m),
        )

        self.state.usage = usage_acc
        append_log(self.state.run_id, "Tasker apply run complete, formatting result...")
        return json.dumps(result, ensure_ascii=False)
