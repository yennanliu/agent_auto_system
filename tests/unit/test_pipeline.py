import json
from unittest.mock import AsyncMock, patch

import pytest

from src.automation.pipeline import _interpolate

# ── _interpolate ──────────────────────────────────────────────────────────────

def test_interpolate_passthrough_no_templates():
    payload = {"key": "value", "count": 5}
    assert _interpolate(payload, []) == {"key": "value", "count": 5}


def test_interpolate_steps_result_replaced_with_json():
    results = [{"summary": "hello", "count": 3}]
    payload = {"prompt": "{{steps.0.result}}"}
    out = _interpolate(payload, results)
    assert out["prompt"] == json.dumps(results[0])


def test_interpolate_steps_result_field_replaced():
    results = [{"summary": "great content", "count": 3}]
    payload = {"prompt": "{{steps.0.result.summary}}"}
    out = _interpolate(payload, results)
    assert out["prompt"] == "great content"


def test_interpolate_nested_dict_both_levels():
    results = [{"title": "top story"}]
    payload = {
        "outer": "{{steps.0.result.title}}",
        "inner": {"nested": "{{steps.0.result.title}}"},
    }
    out = _interpolate(payload, results)
    assert out["outer"] == "top story"
    assert out["inner"]["nested"] == "top story"


def test_interpolate_list_values_substituted():
    results = [{"val": "x"}]
    payload = {"items": ["{{steps.0.result.val}}", "static"]}
    out = _interpolate(payload, results)
    assert out["items"] == ["x", "static"]


def test_interpolate_list_flatten_subfield_comma_joined():
    # email_collect → email_sender: leads[].email → "a@x.com,b@y.com"
    results = [{"leads": [{"email": "a@x.com"}, {"email": "b@y.com"}]}]
    payload = {"to": "{{steps.0.result.leads[].email}}"}
    out = _interpolate(payload, results)
    assert out["to"] == "a@x.com,b@y.com"


def test_interpolate_list_flatten_skips_empty_and_missing():
    results = [{"leads": [{"email": "a@x.com"}, {"email": ""}, {"other": 1}]}]
    payload = {"to": "{{steps.0.result.leads[].email}}"}
    out = _interpolate(payload, results)
    assert out["to"] == "a@x.com"


def test_interpolate_list_flatten_raw_items_no_subfield():
    results = [{"emails": ["a@x.com", "b@y.com"]}]
    payload = {"to": "{{steps.0.result.emails[]}}"}
    out = _interpolate(payload, results)
    assert out["to"] == "a@x.com,b@y.com"


def test_interpolate_list_flatten_non_list_field_is_empty():
    results = [{"leads": "not-a-list"}]
    payload = {"to": "{{steps.0.result.leads[].email}}"}
    out = _interpolate(payload, results)
    assert out["to"] == ""


def test_interpolate_subfield_without_brackets_left_literal():
    # `.field.subfield` (no []) is not a supported token: it must NOT silently
    # return the stringified list — it stays literal.
    results = [{"leads": [{"email": "a@x.com"}]}]
    payload = {"to": "{{steps.0.result.leads.email}}"}
    out = _interpolate(payload, results)
    assert out["to"] == "{{steps.0.result.leads.email}}"


def test_interpolate_scalar_field_still_works_after_list_support():
    results = [{"summary": "great content"}]
    payload = {"prompt": "{{steps.0.result.summary}}"}
    out = _interpolate(payload, results)
    assert out["prompt"] == "great content"


def test_interpolate_out_of_range_index_left_as_is():
    payload = {"key": "{{steps.5.result}}"}
    out = _interpolate(payload, [])
    assert out["key"] == "{{steps.5.result}}"


def test_interpolate_partial_substitution():
    results = [{"field": "done"}]
    payload = {"a": "{{steps.0.result.field}}", "b": "{{steps.5.result.field}}"}
    out = _interpolate(payload, results)
    assert out["a"] == "done"
    assert out["b"] == "{{steps.5.result.field}}"


# ── execute_pipeline ──────────────────────────────────────────────────────────

async def test_execute_pipeline_empty_steps_raises():
    from src.automation.pipeline import execute_pipeline
    with pytest.raises(ValueError, match="no steps"):
        await execute_pipeline(0, [], "openai", "gpt-4o-mini")


async def test_execute_pipeline_single_step_result_shape():
    step_result = {"summary": "done", "count": 1}
    mock_run_flow = AsyncMock(return_value=(step_result, {"prompt_tokens": 10, "completion_tokens": 5}, {}))

    with patch("src.automation.pipeline.append_log"), \
         patch("src.automation.executor._run_flow", mock_run_flow):
        from src.automation.pipeline import execute_pipeline
        result, usage = await execute_pipeline(
            1,
            [{"job_type": "x_scraper", "payload": {"username": "test"}}],
            "openai",
            "gpt-4o-mini",
        )

    assert "steps" in result
    assert "final_result" in result
    assert len(result["steps"]) == 1
    assert result["final_result"] == step_result


async def test_execute_pipeline_two_steps_interpolation():
    first_result = {"summary": "first result summary"}
    second_result = {"digest": "used first result"}

    call_payloads: list[dict] = []

    async def fake_run_flow(run_id, job_type, payload, provider, model):
        call_payloads.append(dict(payload))
        if len(call_payloads) == 1:
            return first_result, {"prompt_tokens": 10, "completion_tokens": 5}, {}
        return second_result, {"prompt_tokens": 20, "completion_tokens": 8}, {}

    with patch("src.automation.pipeline.append_log"), \
         patch("src.automation.executor._run_flow", side_effect=fake_run_flow):
        from src.automation.pipeline import execute_pipeline
        result, usage = await execute_pipeline(
            1,
            [
                {"job_type": "x_scraper", "payload": {"username": "test"}},
                {"job_type": "hacker_news_digest", "payload": {"context": "{{steps.0.result.summary}}"}},
            ],
            "openai",
            "gpt-4o-mini",
        )

    assert call_payloads[1]["context"] == "first result summary"


async def test_execute_pipeline_collect_to_sender_list_flatten():
    # email_collect → email_sender: the sender's `to` is assembled from the
    # collected leads via {{steps.0.result.leads[].email}} — the real use case.
    collect_result = {
        "leads": [{"email": "a@x.com"}, {"email": "b@y.com"}, {"email": "c@z.com"}],
    }
    send_result = {"sent": True}
    call_payloads: list[dict] = []

    async def fake_run_flow(run_id, job_type, payload, provider, model):
        call_payloads.append(dict(payload))
        return (collect_result if len(call_payloads) == 1 else send_result), {}, {}

    with patch("src.automation.pipeline.append_log"), \
         patch("src.automation.executor._run_flow", side_effect=fake_run_flow):
        from src.automation.pipeline import execute_pipeline
        await execute_pipeline(
            1,
            [
                {"job_type": "email_collect", "payload": {"query": "marketing agency"}},
                {"job_type": "email_sender", "payload": {
                    "to": "{{steps.0.result.leads[].email}}",
                    "subject": "hi", "body": "hello"}},
            ],
            "openai",
            "gpt-4o-mini",
        )

    assert call_payloads[1]["to"] == "a@x.com,b@y.com,c@z.com"


async def test_execute_pipeline_usage_tokens_summed():
    mock_run_flow = AsyncMock(side_effect=[
        ({"out": "a"}, {"prompt_tokens": 100, "completion_tokens": 50}, {}),
        ({"out": "b"}, {"prompt_tokens": 200, "completion_tokens": 80}, {}),
    ])

    with patch("src.automation.pipeline.append_log"), \
         patch("src.automation.executor._run_flow", mock_run_flow):
        from src.automation.pipeline import execute_pipeline
        _, usage = await execute_pipeline(
            1,
            [
                {"job_type": "x_scraper", "payload": {}},
                {"job_type": "hacker_news_digest", "payload": {}},
            ],
            "openai",
            "gpt-4o-mini",
        )

    assert usage["prompt_tokens"] == 300
    assert usage["completion_tokens"] == 130


async def test_execute_pipeline_steps_have_required_keys():
    step_result = {"data": "value"}
    mock_run_flow = AsyncMock(return_value=(step_result, {}, {}))

    with patch("src.automation.pipeline.append_log"), \
         patch("src.automation.executor._run_flow", mock_run_flow):
        from src.automation.pipeline import execute_pipeline
        result, _ = await execute_pipeline(
            1,
            [{"job_type": "web_scraper", "payload": {"url": "https://example.com"}}],
            "openai",
            "gpt-4o-mini",
        )

    step = result["steps"][0]
    assert "step" in step
    assert "job_type" in step
    assert "result" in step
    assert step["step"] == 1
    assert step["job_type"] == "web_scraper"
    assert step["result"] == step_result
