"""E13 — prompt caching: the Anthropic cache breakpoint and cache-aware cost accounting."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.automation.flows.utils import add_usage, extract_usage
from src.automation.harness import langfuse_tracer
from src.automation.harness.costs import estimate_cost

_TOOL = {"name": "lookup", "description": "Look something up", "parameters": {"type": "object", "properties": {}}}
_MARK = {"type": "ephemeral"}


def _anthropic(monkeypatch, model="claude-haiku-5-5"):
    from src.automation.harness.provider import resolve
    monkeypatch.setattr("src.settings_store.get_llm_key", lambda provider, env: "test-key")
    llm, _, _ = resolve("anthropic", model)
    return llm


def _params(llm, system="You are a careful analyst.", tools=None):
    return llm._prepare_completion_params([{"role": "user", "content": "task text"}], system, tools)


# ── request shape ────────────────────────────────────────────────────────────

def test_anthropic_system_prompt_gets_cache_breakpoint_by_default(monkeypatch):
    monkeypatch.delenv("PROMPT_CACHE_ENABLED", raising=False)
    params = _params(_anthropic(monkeypatch), tools=[_TOOL])
    assert params["system"] == [
        {"type": "text", "text": "You are a careful analyst.", "cache_control": _MARK}
    ]
    # One breakpoint on the system block covers the tools rendered before it.
    assert all("cache_control" not in t for t in params["tools"])
    # The task message carries per-run inputs and is never marked.
    assert params["messages"] == [{"role": "user", "content": "task text"}]


@pytest.mark.parametrize("flag", ["0", "false", "off", "No"])
def test_anthropic_request_is_plain_when_caching_disabled(monkeypatch, flag):
    monkeypatch.setenv("PROMPT_CACHE_ENABLED", flag)
    params = _params(_anthropic(monkeypatch), tools=[_TOOL])
    assert params["system"] == "You are a careful analyst."
    assert all("cache_control" not in t for t in params["tools"])


def test_breakpoint_falls_back_to_last_tool_without_a_system_prompt(monkeypatch):
    monkeypatch.delenv("PROMPT_CACHE_ENABLED", raising=False)
    params = _params(_anthropic(monkeypatch), system=None, tools=[_TOOL, {**_TOOL, "name": "second"}])
    assert "system" not in params
    assert [t.get("cache_control") for t in params["tools"]] == [None, _MARK]


def test_no_breakpoint_when_there_is_nothing_stable_to_cache(monkeypatch):
    monkeypatch.delenv("PROMPT_CACHE_ENABLED", raising=False)
    params = _params(_anthropic(monkeypatch), system=None)
    assert "system" not in params and "tools" not in params


def test_non_anthropic_llms_are_untouched_by_the_flag(monkeypatch):
    from src.automation.harness.provider import resolve
    monkeypatch.setenv("PROMPT_CACHE_ENABLED", "1")
    monkeypatch.setattr("src.settings_store.get_llm_key", lambda provider, env: "test-key")
    llm, _, _ = resolve("openai", "gpt-4o-mini")
    assert "cache_control" not in repr(llm._prepare_completion_params([{"role": "user", "content": "hi"}]))


# ── usage normalisation ──────────────────────────────────────────────────────

def test_anthropic_usage_folds_cache_tokens_into_the_prompt_count(monkeypatch):
    llm = _anthropic(monkeypatch)
    response = SimpleNamespace(usage=SimpleNamespace(
        input_tokens=40, output_tokens=10, cache_read_input_tokens=900, cache_creation_input_tokens=60,
    ))
    usage = llm._extract_anthropic_token_usage(response)
    assert usage["input_tokens"] == 1000  # 40 uncached + 900 read + 60 written
    assert usage["cached_prompt_tokens"] == 900
    assert usage["cache_creation_tokens"] == 60
    assert usage["total_tokens"] == 1010


def test_anthropic_usage_without_cache_fields_is_unchanged(monkeypatch):
    llm = _anthropic(monkeypatch)
    usage = llm._extract_anthropic_token_usage(
        SimpleNamespace(usage=SimpleNamespace(input_tokens=40, output_tokens=10))
    )
    assert (usage["input_tokens"], usage["output_tokens"], usage["total_tokens"]) == (40, 10, 50)
    assert llm._extract_anthropic_token_usage(SimpleNamespace(usage=None)) == {"total_tokens": 0}


def test_extract_usage_reads_cache_counts_and_tolerates_their_absence():
    full = SimpleNamespace(usage_metrics=SimpleNamespace(
        prompt_tokens=1000, completion_tokens=10, cached_prompt_tokens=900, cache_creation_tokens=60,
    ))
    assert extract_usage(full) == {
        "prompt_tokens": 1000, "completion_tokens": 10,
        "cached_prompt_tokens": 900, "cache_creation_tokens": 60,
    }
    bare = SimpleNamespace(usage_metrics=SimpleNamespace(prompt_tokens=7, completion_tokens=3))
    assert extract_usage(bare)["cached_prompt_tokens"] == 0
    assert extract_usage(SimpleNamespace()) == {}


def test_add_usage_accepts_dicts_without_cache_keys():
    total = {"prompt_tokens": 0, "completion_tokens": 0}
    add_usage(total, {"prompt_tokens": 10, "completion_tokens": 5})
    add_usage(total, {})
    add_usage(total, {"prompt_tokens": 100, "completion_tokens": 1, "cached_prompt_tokens": 80})
    assert total == {
        "prompt_tokens": 110, "completion_tokens": 6,
        "cached_prompt_tokens": 80, "cache_creation_tokens": 0,
    }


# ── cost ─────────────────────────────────────────────────────────────────────

def test_cost_prices_cache_reads_and_writes_at_their_own_rates():
    # Sonnet 5.5: $2 input, $0.10 cache read, $2.50 5m cache write, $10 output.
    cost = estimate_cost("claude-sonnet-5-5", 1_000_000, 100_000,
                         cached_tokens=600_000, cache_write_tokens=200_000)
    assert cost == pytest.approx(0.2 * 2.00 + 0.6 * 0.10 + 0.2 * 2.50 + 0.1 * 10.00)


def test_cost_without_cache_tokens_matches_the_two_argument_form():
    assert estimate_cost("gpt-4o", 1000, 500, 0, 0) == estimate_cost("gpt-4o", 1000, 500)


def test_cost_applies_openai_and_gemini_cached_input_rates():
    assert estimate_cost("gpt-4o-mini", 1_000_000, 0, cached_tokens=1_000_000) == pytest.approx(0.075)
    assert estimate_cost("gemini/gemini-2.5-pro", 1_000_000, 0, cached_tokens=400_000) == pytest.approx(
        0.6 * 1.25 + 0.4 * 0.125
    )


def test_cost_for_a_model_without_verified_cache_rates_never_undercharges():
    # No cache row: reads at the full input rate, writes at 1.25x input.
    base = estimate_cost("gemini-3.5-flash", 1_000_000, 0)
    assert estimate_cost("gemini-3.5-flash", 1_000_000, 0, cached_tokens=900_000) == base
    assert estimate_cost("mystery-model", 1_000_000, 0, cache_write_tokens=1_000_000) == pytest.approx(1.25)


def test_cost_clamps_cache_counts_that_exceed_the_prompt():
    assert estimate_cost("gpt-4o", 100, 0, cached_tokens=10_000) == estimate_cost("gpt-4o", 100, 0, cached_tokens=100)
    assert estimate_cost("gpt-4o", 100, 0, cached_tokens=-5) == estimate_cost("gpt-4o", 100, 0)


def test_every_cache_rate_belongs_to_a_priced_model_and_reads_cost_less_than_input():
    from src.automation.harness.costs import _CACHE_PRICING, _PRICING
    for model, (read, write) in _CACHE_PRICING.items():
        assert model in _PRICING
        assert read < _PRICING[model][0] <= write


# ── trace ────────────────────────────────────────────────────────────────────

def _generation_usage(monkeypatch, **kwargs):
    client = MagicMock()
    monkeypatch.setattr(langfuse_tracer, "get_client", lambda: client)
    langfuse_tracer.record_run(run_id=1, job_type="hacker_news_digest", status="success",
                               provider="anthropic", model="claude-sonnet-5-5", **kwargs)
    return client.start_observation.return_value.start_observation.call_args.kwargs["usage_details"]


def test_trace_reports_cache_tokens_separately_from_uncached_input(monkeypatch):
    usage = _generation_usage(monkeypatch, tokens_in=1000, tokens_out=10,
                              tokens_cached=900, tokens_cache_write=60)
    assert usage == {"input": 40, "output": 10, "total": 1010,
                     "cache_read_input_tokens": 900, "cache_creation_input_tokens": 60}


def test_trace_usage_is_unchanged_when_nothing_was_cached(monkeypatch):
    assert _generation_usage(monkeypatch, tokens_in=100, tokens_out=50) == {
        "input": 100, "output": 50, "total": 150,
    }


# ── through crewai's real call path ──────────────────────────────────────────

def test_call_sends_the_breakpoint_to_the_sdk_and_tracks_cache_usage(monkeypatch):
    """A list-valued ``system`` must survive crewai's own call() plumbing, and the
    cache counts on the response must land in the LLM's usage summary."""
    from anthropic.types import Message, TextBlock, Usage

    monkeypatch.delenv("PROMPT_CACHE_ENABLED", raising=False)
    llm = _anthropic(monkeypatch)
    client = MagicMock()
    client.messages.create.return_value = Message(
        id="msg_1", type="message", role="assistant", model="claude-haiku-5-5",
        content=[TextBlock(type="text", text="done")], stop_reason="end_turn",
        usage=Usage(input_tokens=40, output_tokens=10,
                    cache_read_input_tokens=900, cache_creation_input_tokens=60),
    )
    monkeypatch.setattr(type(llm), "_get_sync_client", lambda self: client)

    out = llm.call([{"role": "system", "content": "Stable prompt."}, {"role": "user", "content": "go"}])

    assert out == "done"
    sent = client.messages.create.call_args.kwargs
    assert sent["system"] == [{"type": "text", "text": "Stable prompt.", "cache_control": _MARK}]
    summary = llm.get_token_usage_summary()
    assert (summary.prompt_tokens, summary.cached_prompt_tokens, summary.cache_creation_tokens) == (1000, 900, 60)
