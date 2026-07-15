from src.automation.progress import append_log


class FlowMixin:
    """Shared helpers mixed into every automation flow."""

    def _check_required(self, *fields: str) -> None:
        missing = [f for f in fields if not getattr(self.state, f, "")]
        if missing:
            raise ValueError(f"Missing required fields: {missing}")

    def _log(self, message: str) -> None:
        append_log(self.state.run_id, message)
