import json
import re

from src.automation import checkpoint, spec
from src.automation.progress import append_log

# {{steps.N.result}} | {{steps.N.result.field}} | {{steps.N.result.field[].subfield}}
# The last form flattens a list-of-dicts field into a comma-joined string of one
# subfield — e.g. email_collect's `leads[].email` → "a@x.com,b@y.com" — so it can
# feed a later step's scalar input (email_sender's `to`). `field[]` (no subfield)
# joins the raw list items instead.
# A `.subfield` is only meaningful in the list-flatten branch, so it is gated
# behind `[]`: `.field.subfield` (no brackets) is not a supported token and is
# left literal rather than silently returning the stringified list.
_TOKEN_RE = re.compile(
    r'\{\{steps\.(\d+)\.result'
    r'(?:\.([a-zA-Z_]\w*)(?:(\[\])(?:\.([a-zA-Z_]\w*))?)?)?\}\}'
)


# Checkpoint keys (see src/automation/checkpoint.py). `started` is written before
# a step runs and `done` after, so "started without done" identifies the step
# that was in flight when the process died.
KEY_PREFIX = "pipeline:"
SIG_KEY = f"{KEY_PREFIX}sig"


def started_key(i: int) -> str:
    return f"{KEY_PREFIX}{i}:started"


def done_key(i: int) -> str:
    return f"{KEY_PREFIX}{i}:done"


def repeat_safe(step_type: str) -> bool:
    """Whether a step of this type may be executed again after it (maybe) ran.

    Only side-effect-free job types qualify; a nested pipeline never does (its
    own steps' markers are not tracked here).
    """
    return step_type != "pipeline" and spec.is_resumable(step_type)


def _interpolate(payload: dict, results: list) -> dict:
    """Substitute {{steps.N.result[...]}} tokens in payload string values."""
    def _sub(value: str) -> str:
        def replacer(m):
            idx = int(m.group(1))
            field, is_list, subfield = m.group(2), m.group(3), m.group(4)
            if idx >= len(results):
                return m.group(0)
            r = results[idx]
            if not field:
                return json.dumps(r) if isinstance(r, dict) else str(r)
            val = r.get(field, '') if isinstance(r, dict) else ''
            if not is_list:
                return str(val)
            # list-flatten: pull `subfield` from each item (or the item itself),
            # drop empties, comma-join.
            items = val if isinstance(val, list) else []
            out = []
            for it in items:
                v = it.get(subfield) if (subfield and isinstance(it, dict)) else it
                if v not in (None, ''):
                    out.append(str(v))
            return ','.join(out)
        return _TOKEN_RE.sub(replacer, value)

    def _walk(obj):
        if isinstance(obj, str):
            return _sub(obj)
        if isinstance(obj, dict):
            return {k: _walk(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_walk(v) for v in obj]
        return obj

    return _walk(payload)


async def execute_pipeline(
    run_id: int, steps: list, effective_provider: str, effective_model: str
) -> tuple[dict, dict]:
    """Execute automation steps in sequence, feeding each result into the next step's payload.

    Payload values may reference previous step results with {{steps.N.result}}
    or {{steps.N.result.field_name}} template variables.

    Each step is checkpointed: ``pipeline:<i>:started`` before it runs and
    ``pipeline:<i>:done`` (result + usage) after. When the run is resumed after a
    restart, steps with a matching ``done`` checkpoint are not executed again —
    their stored result is reused. Whether the step that was *in flight* may be
    re-run is decided before the resume, in ``resume._refusal``.

    Fail-closed rules for steps that are not :func:`repeat_safe` (apply, submit,
    send): such a step is never started if its ``started`` marker could not be
    persisted, and a completed one is never re-executed on resume because its
    inputs changed — both raise :class:`checkpoint.UnsafeToContinue`, which
    fails the run without an in-process retry.
    """
    from src.automation.executor import _run_flow  # late import to break circular dep

    n = len(steps)
    if n == 0:
        raise ValueError("Pipeline has no steps")

    results: list[dict] = []
    total_usage: dict = {"prompt_tokens": 0, "completion_tokens": 0}
    saved = checkpoint.load(run_id)
    # Recorded so a resume can tell the job was edited since this run started
    # (resume._refusal then fails the run rather than re-running changed steps).
    checkpoint.save(run_id, SIG_KEY, checkpoint.signature(steps))

    for i, step in enumerate(steps):
        step_type = step["job_type"]
        step_payload = _interpolate(dict(step.get("payload", {})), results)
        # Ties a stored result to the exact step that produced it: an edited
        # pipeline (or a changed upstream result) no longer matches and re-runs.
        sig = checkpoint.signature({"job_type": step_type, "payload": step_payload})

        done = saved.get(done_key(i))
        if isinstance(done, dict) and "result" in done and done.get("sig") != sig \
                and not repeat_safe(step_type):
            # This step already completed before the restart, but its inputs no
            # longer match — an upstream step whose completion marker was lost
            # re-ran and produced different output. Re-executing would repeat a
            # side effect (apply/submit/send) that already happened: refuse.
            raise checkpoint.UnsafeToContinue(
                f"Not resumed: step {i + 1} '{step_type}' already completed before the "
                "restart but its inputs changed on resume; it is not safe to repeat")
        if isinstance(done, dict) and done.get("sig") == sig and "result" in done:
            usage = done.get("usage") or {}
            total_usage["prompt_tokens"]     += usage.get("prompt_tokens", 0)
            total_usage["completion_tokens"] += usage.get("completion_tokens", 0)
            results.append(done["result"])
            append_log(run_id, f"[Step {i + 1}/{n}] Skipped {step_type} — "
                               "already completed before the restart (checkpoint reused)")
            continue

        if not checkpoint.save(run_id, started_key(i), {"job_type": step_type}) \
                and not repeat_safe(step_type):
            # Fail closed: without the marker a crash mid-step would look like
            # "never started" to resume, which would then execute it again.
            raise checkpoint.UnsafeToContinue(
                f"Step {i + 1} '{step_type}' was not started: its checkpoint marker "
                "could not be saved, and the step is not safe to repeat if the run "
                "were resumed")
        append_log(run_id, f"[Step {i + 1}/{n}] Starting {step_type}...")
        # _run_flow returns (result, usage, serve); the pipeline ignores serve.
        result, usage, _serve = await _run_flow(
            run_id, step_type, step_payload, effective_provider, effective_model
        )
        total_usage["prompt_tokens"]     += usage.get("prompt_tokens", 0)
        total_usage["completion_tokens"] += usage.get("completion_tokens", 0)
        results.append(result)
        # keep_prefix drops the finished step's own flow-level checkpoints in the
        # same write, so the next step can never inherit them.
        if not checkpoint.save(run_id, done_key(i),
                               {"sig": sig, "job_type": step_type, "result": result,
                                "usage": usage},
                               keep_prefix=KEY_PREFIX):
            # Still safe: a non-repeatable step stays "started without done", and a
            # later non-repeatable step completing after this gap makes resume
            # refuse (resume._refusal + the signature guard above).
            append_log(run_id, f"[Step {i + 1}/{n}] Warning: completion checkpoint not "
                               "saved — this run may not be resumable after a restart")
        append_log(run_id, f"[Step {i + 1}/{n}] Completed {step_type}")

    return (
        {
            "steps": [
                {"step": i + 1, "job_type": s["job_type"], "result": r}
                for i, (s, r) in enumerate(zip(steps, results))
            ],
            "final_result": results[-1],
        },
        total_usage,
    )
