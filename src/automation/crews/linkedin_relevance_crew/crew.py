from pathlib import Path

import yaml
from crewai import Agent, Crew, Process, Task

_CFG = Path(__file__).parent / "config"

with open(_CFG / "agents.yaml", encoding="utf-8") as _f:
    _AGENTS = yaml.safe_load(_f)
with open(_CFG / "tasks.yaml", encoding="utf-8") as _f:
    _TASKS = yaml.safe_load(_f)


class LinkedInRelevanceCrew:
    """Judges whether a single LinkedIn job posting matches the user's
    task_filter.

    Pure-LLM (no browser tools). This is the "second gate" that runs after the
    keyword search: the flow calls `.crew().kickoff(...)` once per scanned job
    and skips jobs the judge marks irrelevant, BEFORE spending an apply attempt.
    Returns a small JSON verdict ``{"relevant": bool, "reason": str}``.

    No `@CrewBase` (see CLAUDE.md): build Agent/Task/Crew fresh each call so a
    reused id(self) can't hand back a stale LLM.
    """

    def __init__(self, llm=None):
        self._llm = llm
        self._agents = _AGENTS
        self._tasks = _TASKS

    def crew(self) -> Crew:
        agent = Agent(
            config=self._agents["relevance_judge"],
            verbose=False,
            llm=self._llm,
        )
        task = Task(config={**self._tasks["judge_relevance_task"], "agent": agent})
        return Crew(
            agents=[agent],
            tasks=[task],
            process=Process.sequential,
            verbose=False,
        )
