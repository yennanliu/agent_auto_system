import json

from crewai.flow.flow import Flow, listen, start
from pydantic import BaseModel

from src.automation.crews.tw104_area_crew.crew import TW104AreaCrew
from src.automation.crews.tw104_relevance_crew.crew import TW104RelevanceCrew
from src.automation.flows.base import FlowMixin
from src.automation.flows.utils import extract_usage
from src.automation.progress import append_log
from src.automation.tools.tw104_apply_tool import run_tw104_apply
from src.automation.tools.tw104_area import CANONICAL_NAMES, resolve_area


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


def _parse_areas(text: str) -> list[str]:
    """Extract the ``areas`` list from a TW104AreaCrew JSON reply. Tolerates
    ```json fences / prose; returns [] if none found (caller then leaves the
    input unresolved)."""
    if not text:
        return []
    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start:end + 1])
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and isinstance(obj.get("areas"), list):
            return [str(a) for a in obj["areas"] if a]
    return []


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

        # The LLM (whichever provider/model the run selected) powers two optional
        # steps: enriching a free-form area into 104 codes, and the relevance
        # gate. It's resolved lazily and shared, so a run with neither need makes
        # no LLM call at all.
        llm_box: dict = {}

        def _get_llm():
            if "llm" not in llm_box:
                llm = None
                try:
                    from src.automation.harness.provider import resolve as resolve_llm
                    llm, _p, _m = resolve_llm(
                        self.state.llm_provider or None,
                        self.state.llm_model or None,
                        temperature=0.2,
                    )
                except Exception as exc:  # noqa: BLE001
                    append_log(self.state.run_id, f"No LLM available ({exc}).")
                llm_box["llm"] = llm
            return llm_box["llm"]

        def _acc_usage(result) -> None:
            u = extract_usage(result)
            usage_acc["prompt_tokens"] += u.get("prompt_tokens", 0)
            usage_acc["completion_tokens"] += u.get("completion_tokens", 0)

        # ── Area: name/typo/English → 104 code(s). Static table first; the LLM
        # only runs for tokens the table misses (rare), so common inputs are free.
        def _area_llm_fn(unresolved: str) -> str:
            llm = _get_llm()
            if llm is None:
                return ""
            result = TW104AreaCrew(llm=llm).crew().kickoff(inputs={
                "raw_input": unresolved,
                "valid_areas": ", ".join(CANONICAL_NAMES),
            })
            _acc_usage(result)
            text = (result.raw if hasattr(result, "raw") else str(result)) or ""
            names = _parse_areas(text)
            return ", ".join(names)

        area_codes, _note = resolve_area(
            self.state.area, llm_fn=_area_llm_fn,
            log=lambda m: append_log(self.state.run_id, m),
        )

        # ── Relevance gate (optional second filter before applying) ──
        task_filter = (self.state.task_filter or "").strip()
        relevance_fn = None
        if task_filter:
            llm = _get_llm()
            if llm is None:
                append_log(self.state.run_id,
                           "task_filter is set but no LLM is available; "
                           "applying to all eligible jobs.")
            else:
                append_log(self.state.run_id,
                           "Second gate active: filtering jobs by task_filter before applying.")

                def relevance_fn(title: str, meta: str) -> tuple[bool, str]:  # noqa: F811
                    try:
                        result = TW104RelevanceCrew(llm=llm).crew().kickoff(inputs={
                            "task_filter": task_filter,
                            "job_title": title,
                            "job_meta": meta,
                        })
                        _acc_usage(result)
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
            area=area_codes,
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
