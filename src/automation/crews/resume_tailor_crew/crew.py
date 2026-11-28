from pathlib import Path

import yaml
from crewai import Agent, Crew, Process, Task

_CFG = Path(__file__).parent / "config"

with open(_CFG / "agents.yaml", encoding="utf-8") as _f:
    _AGENTS = yaml.safe_load(_f)
with open(_CFG / "tasks.yaml", encoding="utf-8") as _f:
    _TASKS = yaml.safe_load(_f)


class ResumeTailorCrew:
    """Rewrites the candidate's résumé toward one job description, as plain
    text for a form's paste-résumé box. Pure-LLM (no tools); the result is
    checked by ``resume_tailor.accept()`` before anything is sent.

    No `@CrewBase` (see CLAUDE.md): build Agent/Task/Crew fresh each call.
    """

    def __init__(self, llm=None):
        self._llm = llm

    def crew(self) -> Crew:
        agent = Agent(config=_AGENTS["resume_tailor"], verbose=False, llm=self._llm)
        task = Task(config={**_TASKS["tailor_resume_task"], "agent": agent})
        return Crew(agents=[agent], tasks=[task], process=Process.sequential, verbose=False)
