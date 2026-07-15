"""Quality evaluation ("evaluate node") for automation results.

Runs after validation and is purely informational — it never changes a run's
success/failed status. Produces a 0-100 quality score with a 0-1 confidence
using an LLM-as-judge, falling back to a deterministic heuristic when no LLM is
available (missing API key, network error, or unparseable judge output).

The judge is an **independent** model, never the one that produced the output —
a model grading its own homework inflates scores.

Which model judges is configurable, in precedence order:
  1. Admin setting (``settings_store.get_eval_judge()`` — set via the admin UI)
  2. ``EVAL_JUDGE_PROVIDER`` / ``EVAL_JUDGE_MODEL`` env vars
  3. Code default ``_DEFAULT_JUDGE`` (Gemini)

If the preferred judge is unavailable (no API key) we fall back to a *different*
model in the run's provider, and only as a last resort to the run's own model
(flagged in the result so downstream can discount it).
"""
import json
import logging
import os
import re
from dataclasses import dataclass

from src.automation import spec

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL | re.IGNORECASE)
_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)  # first {...} block anywhere in the text
_MAX_RESULT_CHARS = 6000

# Fixed default judge — independent of whatever model ran the job. Gemini Flash
# is a strong, cheap, fast judge; override via the admin UI or env when desired.
_DEFAULT_JUDGE = ("gemini", "gemini/gemini-2.5-flash")


@dataclass
class EvalResult:
    score: float           # 0-100 overall quality
    confidence: float      # 0-1 confidence in the score
    notes: str = ""
    method: str = "heuristic"  # "llm" | "heuristic"
    judge_model: str = ""      # provider/model that produced the score ("" for heuristic)


# Per-job-type rubric: a one-line description of what a *good* result looks like,
# injected into the judge prompt so scoring is grounded in each job's contract
# rather than a single generic yardstick. Derived from the automation registry
# (SSOT); unknown types fall back to _GENERIC_RUBRIC. See src/automation/spec.py.
_RUBRICS: dict[str, str] = spec.rubrics()
_GENERIC_RUBRIC = "The expected fields are present and the content is substantive, on-topic, and complete."


_JUDGE_PROMPT = """You are a strict quality evaluator for an automation system.
A "{job_type}" automation job produced the following JSON result:

{result}

What a good result looks like for this job type:
{rubric}

Rate the result's quality and completeness against that bar. Consider whether the
expected fields are present, the content is substantive and on-topic, and there
are no signs of failure, truncation, or hallucination.

Respond with ONLY a JSON object (no markdown fences) with exactly these keys:
- "score": integer 0-100 (100 = excellent, 0 = unusable)
- "confidence": number between 0.0 and 1.0 (how sure you are of the score)
- "notes": one short sentence explaining the score
"""


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _parse_json(text: str) -> dict | None:
    if not isinstance(text, str):
        return None
    # Try the raw text, then a fenced block, then the first {...} object anywhere.
    candidates = [text, _strip_fence(text)]
    m = _OBJECT_RE.search(text)
    if m:
        candidates.append(m.group(0))
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def _strip_fence(text: str) -> str | None:
    if not isinstance(text, str):
        return None
    m = _FENCE_RE.match(text)
    return m.group(1) if m else None


def _preferred_judge(run_provider: str) -> tuple[str, str | None]:
    """The configured judge (provider, model), in precedence order: admin setting
    → env vars → code default. A ``None`` model means the provider's default."""
    try:
        from src import settings_store
        provider, model = settings_store.get_eval_judge()
        if provider:
            return provider, model
    except Exception:  # noqa: BLE001 — settings are optional; never break scoring
        pass
    env_model = os.getenv("EVAL_JUDGE_MODEL")
    if env_model:
        return os.getenv("EVAL_JUDGE_PROVIDER") or run_provider, env_model
    return _DEFAULT_JUDGE


def _judge_candidates(run_provider: str, run_model: str, provider_models: dict) -> list[tuple[str, str | None]]:
    """Ordered independent-judge (provider, model) candidates.

    Precedence: the configured/preferred judge → a *different* model within the
    run's provider → (last resort) the run's own model. De-duped, order kept.
    """
    ordered: list[tuple[str, str | None]] = []
    # Only lead with the preferred judge if it's independent of the run model —
    # otherwise it'd self-grade even when an independent sibling is available.
    preferred = _preferred_judge(run_provider)
    if preferred != (run_provider, run_model):
        ordered.append(preferred)
    # A sibling model in the run's provider is still independent of run_model.
    for m in provider_models.get(run_provider, []):
        if m != run_model:
            ordered.append((run_provider, m))
    # Last resort: self-grade. Better than no score, but flagged by the caller.
    ordered.append((run_provider, run_model))

    seen: set[tuple[str, str | None]] = set()
    out: list[tuple[str, str | None]] = []
    for pair in ordered:
        if pair not in seen:
            seen.add(pair)
            out.append(pair)
    return out


def _heuristic(job_type: str, result) -> EvalResult:
    """Deterministic completeness-based score used when no LLM judge is available."""
    if not isinstance(result, dict):
        return EvalResult(0.0, 1.0, "result is not a dict", "heuristic")
    if result.get("error"):
        return EvalResult(0.0, 1.0, f"result contains error: {result['error']}", "heuristic")

    non_empty = [v for v in result.values() if v]
    content_len = len(" ".join(str(v) for v in non_empty))
    field_score = min(len(non_empty) * 12, 60)       # up to 60 for ~5 populated fields
    length_score = min(content_len / 40.0, 40.0)     # up to 40 for ~1600 chars
    score = round(min(field_score + length_score, 100.0), 1)
    return EvalResult(
        score, 0.4,
        f"heuristic: {len(non_empty)} populated field(s), {content_len} chars",
        "heuristic",
    )


def evaluate(job_type: str, result, provider: str | None = None, model: str | None = None) -> EvalResult:
    """Score the quality of a job result. Never raises — falls back to a heuristic."""
    # An errored result is a known failure; don't spend an LLM call to judge it.
    if isinstance(result, dict) and result.get("error"):
        return _heuristic(job_type, result)

    from src.automation.harness.provider import PROVIDER_MODELS, has_api_key, normalize, resolve

    run_provider, run_model = normalize(provider, model)
    result_json = json.dumps(result, ensure_ascii=False)[:_MAX_RESULT_CHARS]
    prompt = _JUDGE_PROMPT.format(
        job_type=job_type, result=result_json,
        rubric=_RUBRICS.get(job_type, _GENERIC_RUBRIC),
    )

    last_exc: Exception | None = None
    for jp, jm in _judge_candidates(run_provider, run_model, PROVIDER_MODELS):
        # Skip providers with no key so resolve() doesn't log a scary error for a
        # judge we already know is unavailable (common: default judge key unset).
        if not has_api_key(jp):
            continue
        try:
            llm, judge_provider, judge_model = resolve(jp, jm, temperature=0.0)
        except Exception as exc:  # noqa: BLE001 — judge unavailable (e.g. no key); try next
            last_exc = exc
            continue

        # A judge is available — spend exactly one call on it. If it answers but
        # the output is unusable, don't burn further calls; fall to the heuristic.
        try:
            raw = llm.call(prompt)
            data = _parse_json(raw if isinstance(raw, str) else str(raw))
            if not data:
                raise ValueError("judge returned unparseable output")

            score = _clamp(float(data.get("score", 0)), 0.0, 100.0)
            confidence = _clamp(float(data.get("confidence", 0)), 0.0, 1.0)
            notes = str(data.get("notes", "")).strip()[:500]

            independent = (judge_provider, judge_model) != (run_provider, run_model)
            if not independent:
                # Self-grading: keep the score but discount confidence and flag it.
                confidence = round(confidence * 0.5, 3)
                notes = (notes + " [self-graded: no independent judge available]").strip()
                logger.warning(
                    "Eval self-graded with run model %s/%s — no independent judge available",
                    run_provider, run_model,
                )
            # Some model ids already carry a provider prefix (e.g. "gemini/…");
            # don't double it in the display label.
            label = judge_model if "/" in judge_model else f"{judge_provider}/{judge_model}"
            return EvalResult(round(score, 1), round(confidence, 3), notes, "llm", label)
        except Exception as exc:  # noqa: BLE001 — evaluation must never break a run
            last_exc = exc
            break

    logger.warning("LLM evaluation failed (%s); using heuristic fallback", last_exc)
    ev = _heuristic(job_type, result)
    if not ev.notes:
        ev.notes = f"heuristic fallback ({last_exc})"
    return ev
