"""extract_usage / tool_failure_messages against crewai's real result types.

Built from crewai's own models (not MagicMocks) so a crewai upgrade that reshapes
CrewOutput fails here instead of silently zeroing token cost.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

from crewai.crews.crew_output import CrewOutput
from crewai.tasks.task_output import TaskOutput
from crewai.tools.tool_failure import ToolFailure, ToolFailureReason, ToolFailureRecord
from crewai.types.usage_metrics import UsageMetrics

from src.automation.flows.utils import extract_usage, tool_failure_messages


def _crew_output(prompt=0, completion=0, failures=()):
    task = TaskOutput(description="d", agent="a", raw="ok", tool_failures=list(failures))
    return CrewOutput(
        raw="ok", tasks_output=[task],
        token_usage=UsageMetrics(prompt_tokens=prompt, completion_tokens=completion),
    )


def _failure(tool, msg):
    return ToolFailureRecord(
        tool_name=tool,
        failure=ToolFailure(message=msg, reason=ToolFailureReason.EXCEPTION),
    )


# ── extract_usage ────────────────────────────────────────────────────────────

def test_extract_usage_reads_crew_output():
    assert extract_usage(_crew_output(120, 30)) == {"prompt_tokens": 120, "completion_tokens": 30}


def test_extract_usage_reads_usage_metrics_dict():
    # crewai >= 1.15 shape (also LiteAgentOutput): usage_metrics is a plain dict.
    r = SimpleNamespace(usage_metrics={"prompt_tokens": 5, "completion_tokens": 2})
    assert extract_usage(r) == {"prompt_tokens": 5, "completion_tokens": 2}


def test_extract_usage_reads_legacy_usage_metrics_object():
    # crewai < 1.15 shape: usage_metrics was the UsageMetrics object itself.
    r = SimpleNamespace(usage_metrics=UsageMetrics(prompt_tokens=9, completion_tokens=4))
    assert extract_usage(r) == {"prompt_tokens": 9, "completion_tokens": 4}


def test_extract_usage_missing_or_mocked():
    assert extract_usage(SimpleNamespace()) == {}
    assert extract_usage("plain string") == {}
    assert extract_usage(MagicMock()) == {"prompt_tokens": 0, "completion_tokens": 0}


# ── tool_failure_messages ────────────────────────────────────────────────────

def test_tool_failure_messages_lists_each_failure():
    r = _crew_output(failures=[_failure("maps_search", "timeout"), _failure("scrape", "403")])
    assert tool_failure_messages(r) == [
        "Tool 'maps_search' failed: timeout",
        "Tool 'scrape' failed: 403",
    ]


def test_tool_failure_messages_empty_for_clean_or_foreign_results():
    assert tool_failure_messages(_crew_output()) == []
    assert tool_failure_messages(MagicMock()) == []
    assert tool_failure_messages("plain string") == []


# ── FlowMixin._run_crew surfaces failures in the run log ─────────────────────

def test_run_crew_logs_tool_failures(mocker):
    result = _crew_output(10, 5, failures=[_failure("hn_fetch", "HTTP 503")])
    crew_cls = MagicMock()
    crew_cls.return_value.crew.return_value.kickoff.return_value = result
    mocker.patch("src.automation.harness.provider.resolve", return_value=(None, "openai", "gpt-4o-mini"))
    mocker.patch("src.automation.flows.hn_digest_flow.HNDigestCrew", crew_cls)
    log = mocker.patch("src.automation.flows.base.append_log")

    from src.automation.flows.hn_digest_flow import HNDigestFlow
    flow = HNDigestFlow()
    flow.kickoff(inputs={"limit": 3, "run_id": 7})

    assert mocker.call(7, "⚠ Tool 'hn_fetch' failed: HTTP 503") in log.call_args_list
    assert flow.state.usage == {"prompt_tokens": 10, "completion_tokens": 5}
