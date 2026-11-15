"""Langfuse LLM-observability integration.

Emits one Langfuse trace per automation run from the executor — the single
funnel that already knows the model, token usage, cost, eval score, and final
status. CrewAI 1.x calls native provider SDKs (OpenAI/Anthropic/Gemini), *not*
litellm, so tracing at the executor level is provider-agnostic and doesn't
couple us to CrewAI internals or a version-specific auto-instrumentor.

Enabled automatically when ``LANGFUSE_PUBLIC_KEY`` and ``LANGFUSE_SECRET_KEY``
are set (and ``LANGFUSE_ENABLED`` != "false"). A no-op otherwise, and it never
raises — observability must never break a run.

Env vars (see .env.example):
  LANGFUSE_PUBLIC_KEY   pk-lf-...                    (required)
  LANGFUSE_SECRET_KEY   sk-lf-...                    (required)
  LANGFUSE_HOST         https://cloud.langfuse.com   (optional; EU cloud default)
  LANGFUSE_ENABLED      "false" disables even when keys are present
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_HOST = "https://cloud.langfuse.com"

_client: Any = None
_init_attempted = False


def is_configured() -> bool:
    """True when both keys are present and Langfuse isn't explicitly disabled."""
    if os.getenv("LANGFUSE_ENABLED", "true").lower() == "false":
        return False
    return bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))


def get_client():
    """Lazily build a singleton Langfuse client, or return None when unconfigured
    or unavailable. Memoized so we construct (and authenticate) at most once."""
    global _client, _init_attempted
    if _init_attempted:
        return _client
    _init_attempted = True

    if not is_configured():
        return None

    host = os.getenv("LANGFUSE_HOST", _DEFAULT_HOST)
    try:
        from langfuse import Langfuse

        _client = Langfuse(
            public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
            secret_key=os.environ["LANGFUSE_SECRET_KEY"],
            host=host,
        )
        logger.info("Langfuse tracing enabled (host=%s).", host)
    except Exception:  # noqa: BLE001 — never let observability break startup
        logger.exception("Failed to initialise Langfuse client; tracing disabled.")
        _client = None
    return _client


def reset() -> None:
    """Testing hook: drop the memoized client so env changes take effect again."""
    global _client, _init_attempted
    _client, _init_attempted = None, False


def flush() -> None:
    """Block until buffered events are sent. Safe to call when disabled."""
    client = get_client()
    if client is not None:
        try:
            client.flush()
        except Exception:  # noqa: BLE001
            logger.debug("Langfuse flush failed; ignoring.", exc_info=True)


# Score schemas registered with Langfuse so our scores are typed, bounds-validated,
# and cleanly aggregatable in the UI (rather than free-floating numbers).
_SCORE_CONFIGS = [
    {"name": "eval_score", "data_type": "NUMERIC", "min_value": 0, "max_value": 100,
     "description": "LLM-judge overall quality (0-100)."},
    {"name": "eval_confidence", "data_type": "NUMERIC", "min_value": 0, "max_value": 1,
     "description": "Judge's confidence in eval_score (0-1)."},
    {"name": "eval_quality", "data_type": "CATEGORICAL",
     "categories": [{"value": 0, "label": "poor"}, {"value": 1, "label": "fair"},
                    {"value": 2, "label": "good"}, {"value": 3, "label": "excellent"}],
     "description": "eval_score bucketed for easy filtering."},
]


def ensure_score_configs() -> None:
    """Best-effort, idempotent registration of our score schemas. Never raises.

    Called once at startup. Skips configs that already exist (matched by name), so
    it's safe to call on every boot.
    """
    client = get_client()
    if client is None:
        return
    try:
        existing = {c.name for c in client.api.score_configs.get(limit=100).data}
    except Exception:  # noqa: BLE001
        logger.debug("Could not list Langfuse score configs; skipping registration.", exc_info=True)
        return

    for cfg in _SCORE_CONFIGS:
        if cfg["name"] in existing:
            continue
        try:
            client.api.score_configs.create(**cfg)
            logger.info("Registered Langfuse score config %r.", cfg["name"])
        except Exception:  # noqa: BLE001
            logger.debug("Failed to register score config %r; ignoring.", cfg["name"], exc_info=True)


def _quality_bucket(score: float) -> str:
    if score >= 85:
        return "excellent"
    if score >= 70:
        return "good"
    if score >= 40:
        return "fair"
    return "poor"


def _usage_details(tokens_in: int, tokens_out: int, cached: int = 0, cache_write: int = 0) -> dict:
    """Langfuse usage breakdown. ``tokens_in`` is the whole prompt, so when part of it
    hit the prompt cache, ``input`` is narrowed to the uncached remainder and the cache
    parts get their own keys (the parts still sum to ``total``)."""
    details = {"input": tokens_in, "output": tokens_out, "total": tokens_in + tokens_out}
    if cached or cache_write:
        details["input"] = max(tokens_in - cached - cache_write, 0)
        details["cache_read_input_tokens"] = cached
        details["cache_creation_input_tokens"] = cache_write
    return details


def record_run(
    *,
    run_id: int,
    job_type: str,
    status: str,
    provider: str,
    model: str,
    payload: dict | None = None,
    result: dict | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost_usd: float = 0.0,
    tokens_cached: int = 0,
    tokens_cache_write: int = 0,
    duration_secs: float | None = None,
    eval_score: float | None = None,
    eval_confidence: float | None = None,
    eval_notes: str = "",
    eval_method: str = "",
    judge_model: str = "",
) -> str | None:
    """Emit one trace (root span + nested LLM generation) for a finished run.

    ``status`` is "success" or "failed". Returns the Langfuse trace URL when a
    trace was sent, else None. Never raises — all errors are swallowed so a
    tracing failure can't fail the run.
    """
    client = get_client()
    if client is None:
        return None

    try:
        # Seed the trace id from run_id so a run maps to exactly one stable trace
        # (and we can build its URL) even if record_run is reached more than once.
        trace_id = client.create_trace_id(seed=str(run_id))
        level = "ERROR" if status == "failed" else "DEFAULT"
        status_message = None
        if status == "failed" and isinstance(result, dict):
            status_message = str(result.get("error") or "run failed")[:500]

        metadata: dict[str, Any] = {
            "run_id": run_id,
            "job_type": job_type,
            "provider": provider,
            "status": status,
        }
        if duration_secs is not None:
            metadata["duration_secs"] = round(duration_secs, 3)
        if eval_method:
            metadata["eval_method"] = eval_method
        if judge_model:
            metadata["judge_model"] = judge_model

        # Tags make runs filterable in the Langfuse UI. The root span's name and
        # input/output automatically become the trace's, so no set_trace_io.
        from langfuse import propagate_attributes

        with propagate_attributes(trace_name=f"run:{job_type}",
                                  tags=[job_type, provider, status]):
            root = client.start_observation(
                trace_context={"trace_id": trace_id},
                name=f"run:{job_type}",
                as_type="span",
                input=payload,
                output=result,
                metadata=metadata,
                level=level,
                status_message=status_message,
            )
            try:
                gen = root.start_observation(
                    name=f"{provider}/{model}",
                    as_type="generation",
                    model=model,
                    input=payload,
                    output=result,
                    usage_details=_usage_details(tokens_in, tokens_out, tokens_cached, tokens_cache_write),
                    cost_details={"total": cost_usd},
                    level=level,
                    status_message=status_message,
                )
                gen.end()

                # Attach the evaluator's quality signals as typed trace scores.
                # data_type is persisted and Langfuse matches these to the
                # registered score configs by name for validation/aggregation.
                if eval_score is not None:
                    root.score_trace(name="eval_score", value=float(eval_score),
                                     data_type="NUMERIC", comment=eval_notes or None)
                    root.score_trace(name="eval_quality",
                                     value=_quality_bucket(float(eval_score)),
                                     data_type="CATEGORICAL")
                if eval_confidence is not None:
                    root.score_trace(name="eval_confidence", value=float(eval_confidence),
                                     data_type="NUMERIC")
            finally:
                root.end()

        client.flush()
    except Exception:  # noqa: BLE001
        logger.exception("Langfuse record_run failed for run_id=%s; ignoring.", run_id)
        return None

    # The trace is already recorded above; the URL is a best-effort convenience
    # (it does a one-off project-id lookup that can fail on its own).
    try:
        return client.get_trace_url(trace_id=trace_id)
    except Exception:  # noqa: BLE001
        logger.debug("Langfuse get_trace_url failed for run_id=%s; ignoring.", run_id,
                     exc_info=True)
        return None
