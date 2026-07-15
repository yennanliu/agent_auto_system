import asyncio
import json
import logging
import re
import time
from datetime import UTC, datetime

from sqlmodel import Session

from src.automation import spec
from src.automation.harness import langfuse_tracer
from src.automation.harness.costs import estimate_cost
from src.automation.harness.evaluator import evaluate
from src.automation.harness.provider import (
    MAX_LLM_ATTEMPTS,
    _is_retriable,
    fallback_sequence,
)
from src.automation.harness.provider import normalize as normalize_llm
from src.automation.harness.validator import validate
from src.automation.pipeline import execute_pipeline
from src.automation.progress import append_log
from src.database import get_engine
from src.models import Run
from src.telemetry import record_run as _record_run

logger = logging.getLogger(__name__)


def _update_run(run_id: int, status: str, result: dict | None = None, **metrics):
    with Session(get_engine()) as s:
        run = s.get(Run, run_id)
        run.status = status
        if result is not None:
            run.result = json.dumps(result)
        if status in ("success", "failed"):
            run.finished_at = datetime.now(UTC)
        for k, v in metrics.items():
            setattr(run, k, v)
        s.add(run)
        s.commit()


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL | re.IGNORECASE)


def _parse_result(result_str: str) -> dict:
    """Parse a flow's raw string output into a dict.

    LLMs (notably Gemini) often wrap JSON in markdown code fences (```json ... ```),
    which makes a naive json.loads fail. Strip the fence and retry before falling
    back to wrapping the raw text in a {"message": ...} envelope.
    """
    try:
        return json.loads(result_str)
    except (json.JSONDecodeError, TypeError):
        pass

    if isinstance(result_str, str):
        m = _FENCE_RE.match(result_str)
        if m:
            try:
                return json.loads(m.group(1))
            except (json.JSONDecodeError, TypeError):
                pass

    return {"message": result_str}


# Dispatch table, derived from the automation registry (single source of truth).
# job_type → (flow_module, flow_class, start_log). "pipeline" has no flow entry;
# it is handled directly below. See src/automation/spec.py.
_FLOW_MAP = spec.flow_map()


async def _run_flow(run_id: int, job_type: str, payload: dict, effective_provider: str, effective_model: str):
    if job_type not in _FLOW_MAP:
        raise ValueError(f"Unknown job_type: {job_type}")

    module_path, class_name, log_msg = _FLOW_MAP[job_type]
    append_log(run_id, log_msg)

    import importlib
    flow_cls = getattr(importlib.import_module(module_path), class_name)

    # Resilience to transient provider outages (e.g. Gemini 503 "high demand"):
    # retry the kickoff with backoff, falling back through the other models in
    # the same provider, up to MAX_LLM_ATTEMPTS total. The requested model is
    # tried twice before advancing, so a brief spike doesn't lose your choice.
    candidates = fallback_sequence(effective_provider, effective_model)
    last_exc: Exception | None = None

    last_model: str | None = None
    tried: list[str] = []
    for attempt in range(1, MAX_LLM_ATTEMPTS + 1):
        # Try the requested model twice (attempts 1-2) before advancing through
        # the fallback list, one model per subsequent attempt.
        idx = 0 if attempt <= 2 else min(attempt - 2, len(candidates) - 1)
        model = candidates[idx]
        if last_model is not None and model != last_model:
            append_log(run_id, f"Falling back to {effective_provider} / {model}...")
        last_model = model
        if model not in tried:
            tried.append(model)

        flow = flow_cls()
        inputs = {
            **payload,
            "run_id": run_id,
            "llm_provider": effective_provider,
            "llm_model": model,
        }
        try:
            raw = await asyncio.to_thread(flow.kickoff, inputs=inputs)
        except Exception as exc:  # noqa: BLE001
            if not _is_retriable(exc):
                raise
            last_exc = exc
            logger.warning("run_id=%d model=%s unavailable (attempt %d/%d): %s",
                           run_id, model, attempt, MAX_LLM_ATTEMPTS, str(exc)[:160])
            if attempt < MAX_LLM_ATTEMPTS:
                append_log(run_id, f"Model unavailable ({model}), retrying (attempt {attempt}/{MAX_LLM_ATTEMPTS})...")
                await asyncio.sleep(min(2 ** (attempt - 1), 8))
                continue
            raise

        result_str = raw.raw if hasattr(raw, "raw") else str(raw)
        result = _parse_result(result_str)
        usage = getattr(flow.state, "usage", {})
        serve = {
            "served_model": model,
            "models_attempted": len(tried),
            "fallback_used": model != candidates[0],
        }
        return result, usage, serve

    raise last_exc  # exhausted retries + fallbacks (unreachable: loop raises first)


async def _run_custom(run_id: int, job_type: str, payload: dict,
                      effective_provider: str, effective_model: str):
    """Run an admin-authored, no-code custom automation (custom:<slug>).

    A single LLM agent with no tools transforms the declared inputs into JSON,
    at the definition's temperature. Everything else (validate/evaluate/cost) is
    the same harness as built-in flows. See src/custom_automations.py.
    """
    from src import custom_automations
    from src.automation.crews.dynamic_crew import DynamicCrew
    from src.automation.flows.utils import extract_usage
    from src.automation.harness.provider import resolve as resolve_llm

    definition = custom_automations.get_by_job_type(job_type)
    if not definition or not definition.enabled:
        raise ValueError(f"Unknown or disabled custom automation: {job_type}")

    prev = payload.get("previous_error", "")
    inputs = {k: v for k, v in payload.items() if k != "previous_error"}
    append_log(run_id, f"Custom agent for '{definition.name}' working...")

    llm, _, eff_model = resolve_llm(
        effective_provider or None, effective_model or None,
        temperature=definition.temperature,
    )

    def _kick():
        return DynamicCrew(definition, inputs, previous_error=prev, llm=llm).crew().kickoff()

    raw = await asyncio.to_thread(_kick)
    result_str = raw.raw if hasattr(raw, "raw") else str(raw)
    result = _parse_result(result_str)
    usage = extract_usage(raw)
    serve = {"served_model": eff_model, "models_attempted": 1, "fallback_used": False}
    return result, usage, serve


async def execute_run(run_id: int, job_type: str, payload: dict):
    logger.info("Starting run_id=%d job_type=%s", run_id, job_type)
    _update_run(run_id, "running")
    _t0 = time.monotonic()
    append_log(run_id, f"Starting {job_type}...")

    llm_provider = payload.pop("llm_provider", None)
    llm_model    = payload.pop("llm_model", None)
    max_retries  = int(payload.pop("max_retries", 1))

    effective_provider, effective_model = normalize_llm(llm_provider, llm_model)
    if llm_provider:
        append_log(run_id, f"Using {effective_provider} / {effective_model}")

    tokens_in = tokens_out = 0
    cost = 0.0
    vr = None
    ev = None  # set before every _trace_run call site; init keeps the closure safe

    async def _trace_run(status: str, result: dict) -> None:
        # Emit a Langfuse trace off the event loop (record_run flushes over the
        # network). No-op + never raises when Langfuse isn't configured.
        url = await asyncio.to_thread(
            langfuse_tracer.record_run,
            run_id=run_id, job_type=job_type, status=status,
            provider=effective_provider, model=effective_model,
            payload=payload, result=result,
            tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost,
            duration_secs=time.monotonic() - _t0,
            eval_score=ev.score if ev else None,
            eval_confidence=ev.confidence if ev else None,
            eval_notes=ev.notes if ev else "",
            eval_method=ev.method if ev else "",
            judge_model=ev.judge_model if ev else "",
        )
        if url:
            append_log(run_id, f"Langfuse trace: {url}")

    for attempt in range(max_retries + 1):
        if attempt > 0:
            append_log(run_id, f"Retrying (attempt {attempt + 1}/{max_retries + 1})...")

        current_payload = dict(payload)
        if attempt > 0 and vr is not None:
            current_payload["previous_error"] = vr.reason

        try:
            if job_type == "pipeline":
                pipeline_steps = current_payload.get("steps", [])
                result, usage = await execute_pipeline(run_id, pipeline_steps, effective_provider, effective_model)
                serve = {"served_model": effective_model, "models_attempted": 1, "fallback_used": False}
            elif job_type.startswith("custom:"):
                result, usage, serve = await _run_custom(run_id, job_type, current_payload, effective_provider, effective_model)
            else:
                result, usage, serve = await _run_flow(run_id, job_type, current_payload, effective_provider, effective_model)

            tokens_in  += usage.get("prompt_tokens", 0)
            tokens_out += usage.get("completion_tokens", 0)

            append_log(run_id, "Validating result...")
            vr = validate(job_type, result)
            if not vr.valid and attempt < max_retries:
                append_log(run_id, f"Validation failed ({vr.reason}), retrying...")
                continue
            append_log(run_id, f"Validation {'passed' if vr.valid else 'failed'}: {vr.reason or 'ok'}")

            append_log(run_id, "Evaluating result quality...")
            ev = await asyncio.to_thread(evaluate, job_type, result, effective_provider, effective_model)
            append_log(run_id, f"Evaluation complete — score {ev.score}/100, confidence {ev.confidence} ({ev.method})")

            cost    = estimate_cost(effective_model, tokens_in, tokens_out)
            metrics = dict(llm_provider=effective_provider, llm_model=effective_model,
                           served_model=serve["served_model"],
                           fallback_used=serve["fallback_used"],
                           models_attempted=serve["models_attempted"],
                           tokens_in=tokens_in, tokens_out=tokens_out,
                           cost_usd=cost, retry_count=attempt,
                           duration_secs=round(time.monotonic() - _t0, 3),
                           validation_passed=vr.valid, validation_reason=vr.reason,
                           eval_score=ev.score, eval_confidence=ev.confidence,
                           eval_notes=ev.notes, eval_method=ev.method)

            if not vr.valid:
                append_log(run_id, f"Automation reported failure: {result.get('error', vr.reason)}")
                _record_run(job_type=job_type, status="failed", duration_secs=time.monotonic() - _t0,
                            provider=effective_provider, tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost)
                _update_run(run_id, "failed", result, **metrics)
                await _trace_run("failed", result)
            else:
                append_log(run_id, "Automation completed successfully!")
                _record_run(job_type=job_type, status="success", duration_secs=time.monotonic() - _t0,
                            provider=effective_provider, tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost)
                _update_run(run_id, "success", result, **metrics)
                await _trace_run("success", result)
            return

        except asyncio.CancelledError:
            raise  # DB status already updated by the cancel endpoint
        except Exception as exc:
            if attempt < max_retries:
                append_log(run_id, f"Error (will retry): {str(exc)[:200]}")
                logger.warning("run_id=%d attempt=%d raised %s, retrying", run_id, attempt, exc)
                continue
            cost    = estimate_cost(effective_model, tokens_in, tokens_out)
            ev      = evaluate(job_type, {"error": str(exc)})  # heuristic; no LLM call for errors
            metrics = dict(llm_provider=effective_provider, llm_model=effective_model,
                           tokens_in=tokens_in, tokens_out=tokens_out,
                           cost_usd=cost, retry_count=attempt,
                           duration_secs=round(time.monotonic() - _t0, 3),
                           validation_passed=None, validation_reason=None,
                           eval_score=ev.score, eval_confidence=ev.confidence,
                           eval_notes=ev.notes, eval_method=ev.method)
            logger.error("run_id=%d failed after %d attempt(s): %s", run_id, attempt + 1, exc)
            append_log(run_id, f"Error: {str(exc)[:200]}")
            _record_run(job_type=job_type, status="failed", duration_secs=time.monotonic() - _t0,
                        provider=effective_provider, tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost)
            _update_run(run_id, "failed", {"error": str(exc)}, **metrics)
            await _trace_run("failed", {"error": str(exc)})
