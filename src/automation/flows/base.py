from src.automation.flows.utils import extract_usage
from src.automation.progress import append_log


class FlowMixin:
    """Shared helpers mixed into every automation flow."""

    def _check_required(self, *fields: str) -> None:
        missing = [f for f in fields if not getattr(self.state, f, "")]
        if missing:
            raise ValueError(f"Missing required fields: {missing}")

    def _log(self, message: str) -> None:
        append_log(self.state.run_id, message)

    def _run_crew(
        self,
        crew_cls,
        *,
        temperature: float,
        inputs: dict,
        working_log: str = "",
        done_log: str = "",
    ) -> str:
        """Resolve the run's LLM, run a single crew, record usage, return raw text.

        Collapses the identical ``resolve → kickoff → extract_usage → raw-extract``
        dance shared by every single-crew flow's ``execute_crew``. ``previous_error``
        is threaded into the crew inputs automatically so retries can self-correct.

        We keep this a mixin helper rather than a Flow *base class* on purpose:
        CrewAI's Flow metaclass does not reliably route results through inherited
        ``@start``/``@listen`` methods, so each flow still owns its own step
        methods (and their exact progress-log strings, which drive the step graph).
        """
        from src.automation.harness.provider import resolve as resolve_llm

        llm, _, _ = resolve_llm(
            self.state.llm_provider or None,
            self.state.llm_model or None,
            temperature=temperature,
        )
        if working_log:
            self._log(working_log)
        result = crew_cls(llm=llm).crew().kickoff(inputs={
            **inputs,
            "previous_error": self.state.previous_error,
        })
        self.state.usage = extract_usage(result)
        if done_log:
            self._log(done_log)
        return result.raw if hasattr(result, "raw") else str(result)
