import json

from crewai.flow.flow import Flow, listen, start
from pydantic import BaseModel

from src.automation.crews.linkedin_keyword_crew.crew import LinkedInKeywordCrew
from src.automation.crews.linkedin_relevance_crew.crew import LinkedInRelevanceCrew
from src.automation.flows.base import FlowMixin
from src.automation.flows.utils import add_usage, extract_usage
from src.automation.progress import append_log
from src.automation.tools.linkedin_apply_tool import (
    MAX_SEARCH_TERMS,
    run_linkedin_apply,
    split_keywords,
)

# How many LLM-suggested search terms `expand_keywords` may add on top of the
# user's own. Each extra term can cost up to `max_pages` listing pages.
MAX_EXPANDED_TERMS = 3


def _parse_verdict(text: str) -> dict | None:
    """Best-effort extract a {"relevant": bool, "reason": str} object from LLM
    text. Tolerates ```json fences and surrounding prose; returns None if no
    usable object with a boolean-ish ``relevant`` can be found (caller fails
    open). Mirrors the tw104_apply flow's parser."""
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
                    "true", "yes", "y", "1", "relevant", "match")
            return obj
    return None


def _parse_keyword_list(text: str) -> list[str]:
    """Best-effort extract the ``{"keywords": [...]}`` list (or a bare JSON
    list) from LLM text. Returns [] when nothing usable is found."""
    if not text:
        return []
    candidates = [text]
    for open_, close in (("{", "}"), ("[", "]")):
        start, end = text.find(open_), text.rfind(close)
        if 0 <= start < end:
            candidates.append(text[start:end + 1])
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            obj = obj.get("keywords")
        if isinstance(obj, list):
            return [str(x).strip() for x in obj if isinstance(x, str) and x.strip()]
    return []


class LinkedInApplyState(BaseModel):
    keywords: str = ""              # search keyword(s); "A, B、C" = several search terms
    expand_keywords: bool = False   # let the LLM add a few closely related search terms
    location: str = "Taiwan"       # location text, e.g. "Taipei" / "Remote" (default Taiwan)
    remote: bool = False           # only search remote jobs (LinkedIn f_WT=2)
    phone: str = ""                # phone for the Easy Apply contact question
    nationality: str = "Taiwanese"  # default answer to nationality/citizenship questions
    years_experience: int = 3      # default answer to "years of experience" questions
    task_filter: str = ""          # optional 2nd gate: LLM relevance filter (nat-lang)
    max_applications: int = 5
    max_pages: int = 10
    dry_run: bool = True           # safety: don't click final Submit unless explicitly false
    run_id: int = 0
    usage: dict = {}
    llm_provider: str = ""
    llm_model: str = ""
    previous_error: str = ""


class LinkedInApplyFlow(FlowMixin, Flow[LinkedInApplyState]):

    @start()
    def validate_payload(self):
        self._check_required("keywords")
        if not split_keywords(self.state.keywords):
            # e.g. " , ; " — non-empty, but no search term survives the split.
            # Never fall through to an unrestricted search (or let expansion
            # invent terms for an empty request).
            raise ValueError(f"No search terms in keywords {self.state.keywords!r}")
        append_log(
            self.state.run_id,
            f"Payload validated — keywords '{self.state.keywords}', "
            f"location '{self.state.location or 'any'}', "
            f"max {self.state.max_applications} application(s), "
            f"dry_run={self.state.dry_run}",
        )
        return self.state.model_dump()

    @listen(validate_payload)
    def execute_apply(self, _):
        usage_acc = {"prompt_tokens": 0, "completion_tokens": 0}

        # The LLM (whichever provider/model the run selected) powers two optional
        # steps: keyword expansion and the relevance gate. It's resolved lazily,
        # so a run with neither makes no LLM call at all.
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
            add_usage(usage_acc, extract_usage(result))

        # ── Search terms: the user's own (split on , 、 ; newline) + optional LLM expansion ──
        terms = split_keywords(self.state.keywords)
        if self.state.expand_keywords:
            terms = self._expand_terms(terms, _get_llm(), _acc_usage)
        append_log(self.state.run_id,
                   f"Search terms ({len(terms)}): " + ", ".join(f"'{t}'" for t in terms))

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
                        result = LinkedInRelevanceCrew(llm=llm).crew().kickoff(inputs={
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

        append_log(self.state.run_id, "Loading LinkedIn session and scanning jobs...")
        result = run_linkedin_apply(
            keywords=terms,
            location=self.state.location,
            remote=self.state.remote,
            phone=self.state.phone,
            nationality=self.state.nationality,
            years_experience=self.state.years_experience,
            max_applications=self.state.max_applications,
            max_pages=self.state.max_pages,
            dry_run=self.state.dry_run,
            relevance_fn=relevance_fn,
            log=lambda m: append_log(self.state.run_id, m),
        )

        self.state.usage = usage_acc
        append_log(self.state.run_id, "LinkedIn apply run complete, formatting result...")
        return json.dumps(result, ensure_ascii=False)

    def _expand_terms(self, terms: list[str], llm, acc_usage) -> list[str]:
        """Ask the LLM for up to MAX_EXPANDED_TERMS related search terms and append
        the new ones. Fails open: any error keeps the user's terms unchanged."""
        run_id = self.state.run_id
        if llm is None:
            append_log(run_id, "expand_keywords is on but no LLM is available; "
                               "searching the given keyword(s) only.")
            return terms
        room = min(MAX_EXPANDED_TERMS, MAX_SEARCH_TERMS - len(terms))
        if room <= 0:
            return terms
        try:
            result = LinkedInKeywordCrew(llm=llm).crew().kickoff(inputs={
                "keywords": ", ".join(terms),
                "location": self.state.location or "any",
                "task_filter": (self.state.task_filter or "").strip() or "(none)",
                "max_extra": room,
            })
            acc_usage(result)
            text = (result.raw if hasattr(result, "raw") else str(result)) or ""
        except Exception as exc:  # noqa: BLE001 — expansion must never fail the run
            append_log(run_id, f"Keyword expansion failed ({exc}); "
                               "searching the given keyword(s) only.")
            return terms
        known = {t.lower() for t in terms}
        extra = [t for t in split_keywords(_parse_keyword_list(text))
                 if t.lower() not in known][:room]
        if extra:
            append_log(run_id, "Keyword expansion added: " + ", ".join(f"'{t}'" for t in extra))
        else:
            append_log(run_id, f"Keyword expansion returned nothing usable ({text[:80]!r}).")
        return terms + extra
