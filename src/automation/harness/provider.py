import logging
import os

logger = logging.getLogger(__name__)

# Total LLM attempts before giving up (retries on the same model + fallbacks to
# other models in the same provider, combined).
MAX_LLM_ATTEMPTS = 5

# Substrings that mark a transient "model unavailable / overloaded" failure worth
# retrying or falling back from (vs. a hard error like a bad API key or 400).
_RETRIABLE_MARKERS = (
    "503", "unavailable", "overloaded", "high demand", "try again",
    "429", "rate limit", "rate_limit", "resource exhausted", "resourceexhausted",
    "temporarily", "timeout", "timed out", "502", "504",
)


def _is_retriable(exc: Exception) -> bool:
    # Bare TimeoutError/ConnectionError often stringify to "" or to messages that
    # don't contain our markers, so check the type before the substring scan.
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    return any(m in str(exc).lower() for m in _RETRIABLE_MARKERS)

# Model order matters: fallback_sequence() tries the requested model first, then the
# rest of its provider's list in this order. Newer models are appended after the
# existing ones so existing jobs keep their first fallbacks (lists are not price-ordered).
_CATALOG: dict = {
    "openai": {
        "models": ["gpt-4o-mini", "gpt-4o", "gpt-6-luna", "gpt-6-sol"],
        "default": "gpt-4o-mini",
        "env": "OPENAI_API_KEY",
    },
    "anthropic": {
        "models": [
            "claude-haiku-4-5-20251001",
            "claude-sonnet-4-6",
            "claude-haiku-5-5",
            "claude-sonnet-5-5",
            "claude-opus-5-5",
        ],
        "default": "claude-haiku-4-5-20251001",
        "env": "ANTHROPIC_API_KEY",
    },
    "gemini": {
        "models": [
            "gemini/gemini-3.5-flash",
            "gemini/gemini-3.1-flash-lite",
            "gemini/gemini-2.5-pro",
            "gemini/gemini-2.5-flash",
            "gemini/gemini-2.5-flash-lite",
            "gemini/gemini-3.5-flash-lite",
            "gemini/gemini-3.8-flash",
            # Paid tier only — a free-tier key gets 403 PERMISSION_DENIED.
            "gemini/gemini-4-argon",
            "gemini/gemini-4-carbon",
        ],
        "default": "gemini/gemini-2.5-flash",
        "env": "GEMINI_API_KEY",
    },
}

# Models that reject a `temperature` parameter (400 on Claude 5.x; OpenAI's GPT-5/6
# reasoning models accept only the default). Matched by prefix on the bare model id.
# Omitting temperature is always valid, so the list errs on the side of omitting.
_NO_TEMPERATURE_PREFIXES = (
    "claude-opus-4-7", "claude-opus-4-8", "claude-opus-5", "claude-sonnet-5",
    "claude-haiku-5", "claude-fable", "gpt-5", "gpt-6", "o1", "o3", "o4",
)

# Claude models that 400 on a forced tool_choice ({"type": "tool"|"any"}).
_NO_FORCED_TOOL_CHOICE_PREFIXES = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-")


# GPT-6 rejects function tools on /v1/chat/completions unless reasoning_effort is
# "none" ("use /v1/responses or set reasoning_effort to 'none'"). crewai 1.14's
# agent loop doesn't execute tool calls returned by /v1/responses, so stay on chat
# completions with reasoning off — every crew here is tool-driven.
_OPENAI_NO_REASONING_PREFIXES = ("gpt-6",)


def accepts_temperature(model: str) -> bool:
    return not model.split("/")[-1].startswith(_NO_TEMPERATURE_PREFIXES)


PROVIDER_MODELS: dict[str, list[str]] = {k: v["models"] for k, v in _CATALOG.items()}


def normalize(provider: str | None, model: str | None) -> tuple[str, str]:
    """Return (effective_provider, effective_model) without creating an LLM instance."""
    if not provider:
        provider = "openai"
    cfg = _CATALOG.get(provider, _CATALOG["openai"])
    effective_model = model if model and model != "default" else cfg["default"]
    return provider, effective_model


def fallback_sequence(provider: str | None, model: str | None) -> list[str]:
    """Ordered model attempt sequence for one job: the requested model first,
    then the other models in the SAME provider as fallbacks (de-duped)."""
    effective_provider, effective_model = normalize(provider, model)
    cfg = _CATALOG.get(effective_provider)
    if not cfg:
        # Unknown/custom provider: only try the requested model — never leak
        # another provider's models into the fallback sequence.
        return [effective_model]
    seq: list[str] = []
    for m in (effective_model, *cfg["models"]):
        if m and m not in seq:
            seq.append(m)
    return seq


def has_api_key(provider: str) -> bool:
    """True when an API key is available for ``provider`` (admin-set or env), without
    constructing an LLM or logging. Lets callers skip guaranteed-to-fail providers."""
    cfg = _CATALOG.get(provider)
    if not cfg:
        return False
    from src import settings_store
    return bool(settings_store.get_llm_key(provider, cfg["env"]))


def resolve(provider: str | None, model: str | None, temperature: float = 0.7):
    """Return (llm_instance, effective_provider, effective_model)."""
    from crewai import LLM

    effective_provider, effective_model = normalize(provider, model)
    cfg = _CATALOG.get(effective_provider, _CATALOG["openai"])

    # Prefer an admin-configured key from the DB, falling back to the env var.
    from src import settings_store
    api_key = settings_store.get_llm_key(effective_provider, cfg["env"])
    if api_key:
        # litellm / Gemini code paths read the provider env var directly, so make
        # the resolved key visible to them for this process too.
        os.environ[cfg["env"]] = api_key

    if not api_key:
        logger.error("API key %s not set for provider '%s'", cfg["env"], effective_provider)
        raise OSError(
            f"API key for provider '{effective_provider}' is not set. "
            f"Add {cfg['env']} to your .env file."
        )

    temp = temperature if accepts_temperature(effective_model) else None
    try:
        if effective_provider == "anthropic":
            # crewai only routes a bare "claude-*" id to Anthropic when the id is in
            # its built-in model list; anything newer silently falls through to the
            # OpenAI client. Build the native Anthropic client explicitly instead.
            llm = _anthropic_llm(effective_model, api_key, temp)
        elif effective_provider == "openai" and effective_model.startswith(_OPENAI_NO_REASONING_PREFIXES):
            llm = LLM(
                model=effective_model, api_key=api_key, temperature=temp,
                additional_params={"reasoning_effort": "none"},
            )
        else:
            llm = LLM(model=effective_model, api_key=api_key, temperature=temp)
    except ImportError as exc:
        raise ImportError(
            f"Provider '{effective_provider}' requires an extra package: {exc}. "
            "Run: uv add 'crewai[google-genai]' (Gemini), uv add 'crewai[anthropic]' "
            "(Claude) or check your install."
        ) from exc

    return llm, effective_provider, effective_model


def _anthropic_llm(model: str, api_key: str, temperature: float | None):
    """Native crewai Anthropic client, adjusted for models that reject forced tool use."""
    from crewai.llms.providers.anthropic.completion import AnthropicCompletion

    class _AnthropicLLM(AnthropicCompletion):
        def _prepare_completion_params(self, *args, **kwargs):
            params = super()._prepare_completion_params(*args, **kwargs)
            # crewai forces tool_choice when an agent has exactly one tool; newer
            # Claude models 400 on that, so let the model choose (it still calls it).
            choice = params.get("tool_choice")
            if isinstance(choice, dict) and choice.get("type") in ("tool", "any") and self.model.startswith(
                _NO_FORCED_TOOL_CHOICE_PREFIXES
            ):
                params["tool_choice"] = {"type": "auto"}
            return params

    return _AnthropicLLM(model=model, provider="anthropic", api_key=api_key, temperature=temperature)
