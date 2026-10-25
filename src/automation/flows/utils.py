def extract_usage(result) -> dict:
    """Prompt/completion token counts from a crew or agent kickoff result.

    crewai >= 1.15 keeps the typed ``UsageMetrics`` on ``token_usage`` and turned
    ``usage_metrics`` into a plain dict; older releases exposed the object as
    ``usage_metrics``. Read whichever shape is present so cost tracking never
    silently drops to zero on an upgrade.
    """
    m = getattr(result, "token_usage", None) or getattr(result, "usage_metrics", None)
    if not m:
        return {}
    get = m.get if isinstance(m, dict) else (lambda k, d=None: getattr(m, k, d))
    prompt, completion = get("prompt_tokens", 0), get("completion_tokens", 0)
    return {
        "prompt_tokens":     prompt if isinstance(prompt, int) else 0,
        "completion_tokens": completion if isinstance(completion, int) else 0,
    }


def tool_failure_messages(result) -> list[str]:
    """One human-readable line per tool failure crewai recorded during a kickoff.

    A crew can finish "successfully" after an agent narrates around a failed tool
    call; crewai >= 1.15 records those on ``CrewOutput.tool_failures``. Returns
    ``[]`` for results that don't carry the field.
    """
    failures = getattr(result, "tool_failures", None)
    if not isinstance(failures, list):
        return []
    return [f"Tool '{f.tool_name}' failed: {f.message}" for f in failures]
