from pathlib import Path

import yaml
from crewai import Agent, Crew, Process, Task

_CFG = Path(__file__).parent / "config"

with open(_CFG / "agents.yaml", encoding="utf-8") as _f:
    _AGENTS = yaml.safe_load(_f)
with open(_CFG / "tasks.yaml", encoding="utf-8") as _f:
    _TASKS = yaml.safe_load(_f)


class LinkedInKeywordCrew:
    """Expands the user's LinkedIn search keyword(s) into a few closely related
    search terms ("Forward Deployed Engineer" → "FDE", "AI Engineer", …).

    Pure-LLM (no browser tools), called once per run before the scan when the
    payload sets ``expand_keywords``. Returns ``{"keywords": [str, ...]}``.

    No `@CrewBase` (see CLAUDE.md): build Agent/Task/Crew fresh each call so a
    reused id(self) can't hand back a stale LLM.
    """

    def __init__(self, llm=None):
        self._llm = llm
        self._agents = _AGENTS
        self._tasks = _TASKS

    def crew(self) -> Crew:
        agent = Agent(
            config=self._agents["keyword_expander"],
            verbose=False,
            llm=self._llm,
        )
        task = Task(config={**self._tasks["expand_keywords_task"], "agent": agent})
        return Crew(
            agents=[agent],
            tasks=[task],
            process=Process.sequential,
            verbose=False,
        )
