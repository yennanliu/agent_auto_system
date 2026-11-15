def extract_usage(result) -> dict:
    """Token usage of one crew kickoff. ``prompt_tokens`` is the whole prompt;
    the two cache keys are the parts of it read from / written to a prompt cache."""
    m = getattr(result, "usage_metrics", None)
    if not m:
        return {}
    return {
        "prompt_tokens":         getattr(m, "prompt_tokens", 0) or 0,
        "completion_tokens":     getattr(m, "completion_tokens", 0) or 0,
        "cached_prompt_tokens":  getattr(m, "cached_prompt_tokens", 0) or 0,
        "cache_creation_tokens": getattr(m, "cache_creation_tokens", 0) or 0,
    }


def add_usage(total: dict, usage: dict) -> None:
    """Add one ``extract_usage`` dict into a running total, in place."""
    for key in ("prompt_tokens", "completion_tokens", "cached_prompt_tokens", "cache_creation_tokens"):
        total[key] = total.get(key, 0) + (usage.get(key, 0) or 0)
