from pathlib import Path

import yaml
from crewai import Agent, Crew, Process, Task

_CFG = Path(__file__).parent / "config"

with open(_CFG / "agents.yaml") as _f:
    _AGENTS = yaml.safe_load(_f)
with open(_CFG / "tasks.yaml") as _f:
    _TASKS = yaml.safe_load(_f)


class FreelancerProposalCrew:
    """Drafts one Freelancer.com bid proposal from the candidate's background,
    or answers "SKIP: <reason>" when the background doesn't fit the project.

    Pure-LLM (no tools); the flow calls ``.crew().kickoff(...)`` once per
    project. Plain class, no @CrewBase (see CLAUDE.md).
    """

    def __init__(self, llm=None):
        self._llm = llm

    def crew(self) -> Crew:
        agent = Agent(config=_AGENTS["proposal_writer"], verbose=False, llm=self._llm)
        task = Task(config={**_TASKS["write_proposal_task"], "agent": agent})
        return Crew(agents=[agent], tasks=[task], process=Process.sequential, verbose=False)
