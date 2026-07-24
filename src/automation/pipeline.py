import json
import re

from src.automation.progress import append_log

# {{steps.N.result}} | {{steps.N.result.field}} | {{steps.N.result.field[].subfield}}
# The last form flattens a list-of-dicts field into a comma-joined string of one
# subfield — e.g. email_collect's `leads[].email` → "a@x.com,b@y.com" — so it can
# feed a later step's scalar input (email_sender's `to`). `field[]` (no subfield)
# joins the raw list items instead.
_TOKEN_RE = re.compile(
    r'\{\{steps\.(\d+)\.result'
    r'(?:\.([a-zA-Z_]\w*)(\[\])?(?:\.([a-zA-Z_]\w*))?)?\}\}'
)


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
    """
    from src.automation.executor import _run_flow  # late import to break circular dep

    n = len(steps)
    if n == 0:
        raise ValueError("Pipeline has no steps")

    results: list[dict] = []
    total_usage: dict = {"prompt_tokens": 0, "completion_tokens": 0}

    for i, step in enumerate(steps):
        step_type = step["job_type"]
        step_payload = _interpolate(dict(step.get("payload", {})), results)

        append_log(run_id, f"[Step {i + 1}/{n}] Starting {step_type}...")
        result, usage = await _run_flow(
            run_id, step_type, step_payload, effective_provider, effective_model
        )
        total_usage["prompt_tokens"]     += usage.get("prompt_tokens", 0)
        total_usage["completion_tokens"] += usage.get("completion_tokens", 0)
        results.append(result)
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
