from pathlib import Path

import yaml
from crewai import Agent, Crew, Process, Task

_CFG = Path(__file__).parent / "config"

with open(_CFG / "agents.yaml", encoding="utf-8") as _f:
    _AGENTS = yaml.safe_load(_f)
with open(_CFG / "tasks.yaml", encoding="utf-8") as _f:
    _TASKS = yaml.safe_load(_f)


class GreenhouseAnswerCrew:
    """Answers one required Greenhouse screening question the rule-based planner
    couldn't, grounded only in the candidate's own background text.

    Pure-LLM (no browser tools). The flow calls `.crew().kickoff(...)` per
    unanswered required question, before the browser opens. Returns a small JSON
    ``{"answer": str, "confident": bool}``; a non-confident answer leaves the
    question unanswered, so the job is skipped instead of sent with a guess.

    No `@CrewBase` (see CLAUDE.md): build Agent/Task/Crew fresh each call so a
    reused id(self) can't hand back a stale LLM.
    """

    def __init__(self, llm=None):
        self._llm = llm
        self._agents = _AGENTS
        self._tasks = _TASKS

    def crew(self) -> Crew:
        agent = Agent(
            config=self._agents["screening_answerer"],
            verbose=False,
            llm=self._llm,
        )
        task = Task(config={**self._tasks["answer_question_task"], "agent": agent})
        return Crew(
            agents=[agent],
            tasks=[task],
            process=Process.sequential,
            verbose=False,
        )
