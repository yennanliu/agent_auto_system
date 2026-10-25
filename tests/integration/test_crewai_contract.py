"""Offline contract tests: real crewai Agent/Task/Crew kickoffs driven by a scripted LLM.

Every other test mocks the crew away, so nothing exercised crewai's own execution
loop. These run it for real (no network, no API key) and pin the parts of its
behaviour this codebase depends on — the ReAct loop, token-usage aggregation and
tool-failure recording — so a crewai upgrade that changes them fails here.
"""
from types import SimpleNamespace

from crewai import Agent, Crew, Process, Task
from crewai.llms.base_llm import BaseLLM
from crewai.tools import BaseTool

from src.automation.crews.dynamic_crew import DynamicCrew
from src.automation.flows.utils import extract_usage, tool_failure_messages


class ScriptedLLM(BaseLLM):
    """Returns canned replies in order and reports fixed usage per call."""

    def __init__(self, replies: list[str]):
        super().__init__(model="scripted-test-model")
        self._replies = list(replies)
        self.prompts: list = []

    def call(self, messages, *args, **kwargs):
        self.prompts.append(messages)
        self._track_token_usage_internal({"prompt_tokens": 11, "completion_tokens": 7})
        return self._replies.pop(0)

    def supports_function_calling(self) -> bool:
        return False


class FailingTool(BaseTool):
    name: str = "flaky_fetch"
    description: str = "Fetches a page. Always fails in this test."

    def _run(self, **kwargs):
        raise RuntimeError("upstream 503")


def test_dynamic_crew_kickoff_returns_answer_and_usage():
    llm = ScriptedLLM(['Thought: I know it.\nFinal Answer: {"summary": "hi"}'])
    definition = SimpleNamespace(
        name="Summarizer", instructions="Summarize the text.", output_hint="",
    )

    result = DynamicCrew(definition, {"text": "hello world"}, llm=llm).crew().kickoff()

    assert result.raw == '{"summary": "hi"}'
    assert extract_usage(result) == {"prompt_tokens": 11, "completion_tokens": 7}
    assert tool_failure_messages(result) == []
    # The declared inputs reach the prompt.
    assert "hello world" in str(llm.prompts[0])


def test_failed_tool_call_is_recorded_while_crew_still_finishes():
    llm = ScriptedLLM([
        "Thought: fetch it.\nAction: flaky_fetch\nAction Input: {}",
        'Thought: fetch failed, answer anyway.\nFinal Answer: {"ok": false}',
    ])
    agent = Agent(role="r", goal="g", backstory="b", tools=[FailingTool()],
                  llm=llm, max_iter=3, verbose=False)
    task = Task(description="Fetch the page.", expected_output="JSON", agent=agent)

    result = Crew(agents=[agent], tasks=[task], process=Process.sequential).kickoff()

    assert result.raw == '{"ok": false}'
    assert tool_failure_messages(result) == ["Tool 'flaky_fetch' failed: upstream 503"]
    # Usage aggregates across both LLM calls in the run.
    assert extract_usage(result) == {"prompt_tokens": 22, "completion_tokens": 14}
