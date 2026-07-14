from pathlib import Path

import yaml
from crewai import Agent, Crew, Process, Task

_CFG = Path(__file__).parent / "config"

with open(_CFG / "agents.yaml", encoding="utf-8") as _f:
    _AGENTS = yaml.safe_load(_f)
with open(_CFG / "tasks.yaml", encoding="utf-8") as _f:
    _TASKS = yaml.safe_load(_f)


class TW104AreaCrew:
    """Normalises a free-form area string (e.g. "台北", "taipei", a typo) into
    the canonical 104 city name(s) it refers to.

    Pure-LLM (no browser tools). Only invoked as a fallback for inputs the
    static alias table in tw104_area.resolve_area couldn't match, so it runs
    rarely and cheaply. Returns a JSON object ``{"areas": ["台北市", ...]}``;
    the caller maps those names back to 104 codes.

    No `@CrewBase` (see CLAUDE.md): build Agent/Task/Crew fresh each call.
    """

    def __init__(self, llm=None):
        self._llm = llm
        self._agents = _AGENTS
        self._tasks = _TASKS

    def crew(self) -> Crew:
        agent = Agent(
            config=self._agents["area_resolver"],
            verbose=False,
            llm=self._llm,
        )
        task = Task(config={**self._tasks["resolve_area_task"], "agent": agent})
        return Crew(
            agents=[agent],
            tasks=[task],
            process=Process.sequential,
            verbose=False,
        )
