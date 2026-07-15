"""A single-agent, NO-TOOLS crew built at runtime from a custom automation.

The safety boundary for admin-authored no-code automations (Phase 3G): the agent
has an empty tool list, so it cannot reach the network, filesystem, or secrets —
it can only reason over the inputs and return JSON. Plain class, no @CrewBase
(same invariant as the built-in crews).
"""
from crewai import Agent, Crew, Process, Task

_BACKSTORY = (
    "You are a careful assistant. You have NO tools and must reason only from the "
    "inputs provided — never invent facts or claim to have fetched anything. You "
    "always return a single valid JSON object and nothing else."
)


class DynamicCrew:
    def __init__(self, definition, inputs: dict, previous_error: str = "", llm=None):
        self._d = definition
        self._inputs = inputs or {}
        self._prev = previous_error
        self._llm = llm

    def crew(self) -> Crew:
        d = self._d
        agent = Agent(
            role=d.name,
            goal=d.instructions,
            backstory=_BACKSTORY,
            tools=[],  # ← the safety boundary: no tools, ever
            llm=self._llm,
            verbose=False,
        )
        lines = "\n".join(f"- {k}: {v}" for k, v in self._inputs.items()) or "(none)"
        description = f"{d.instructions}\n\nInputs:\n{lines}"
        if self._prev:
            description += f"\n\nFix this issue from the previous attempt: {self._prev}"
        task = Task(
            description=description,
            expected_output=d.output_hint or "A single JSON object with the requested result.",
            agent=agent,
        )
        return Crew(agents=[agent], tasks=[task], process=Process.sequential, verbose=False)
