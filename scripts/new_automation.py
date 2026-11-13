#!/usr/bin/env python3
"""Scaffold a new single-crew automation (job type) in one go.

    uv run python scripts/new_automation.py NAME [--title "Display Name"]
    make new-automation NAME=my_thing

Follows "Adding a New Job Type" in CLAUDE.md and mirrors the simplest existing
example (``hn_digest``). For ``NAME=my_thing`` it creates:

    src/automation/flows/my_thing_flow.py              Flow[StateModel] + _run_crew
    src/automation/crews/my_thing_crew/__init__.py
    src/automation/crews/my_thing_crew/crew.py         plain class, LLM via constructor
    src/automation/crews/my_thing_crew/config/agents.yaml
    src/automation/crews/my_thing_crew/config/tasks.yaml
    tests/unit/test_my_thing_flow.py                   unit-test skeleton

and edits two shared files:

    src/automation/spec.py     + register(AutomationSpec(...))   (custom_ui=False + fields
                                                                  → no UI edit needed)
    src/routers/system.py      + one row in _AGENT_DEFS

It refuses to touch anything if the name is invalid, already registered, or any
target file exists — every check runs before the first write, so a refused run
(including an accidental second run) leaves the tree unchanged.
"""
from __future__ import annotations

import argparse
import keyword
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")
_TITLE_RE = re.compile(r"^[\w][\w \-]*$")
_MAX_NAME_LEN = 40

SPEC_FILE = "src/automation/spec.py"
SYSTEM_FILE = "src/routers/system.py"
# New registrations go just above this trailing comment block in spec.py
# (falls back to appending at end of file if the comment is ever reworded).
_SPEC_ANCHOR = "# Third-party automations (opt-in"
_AGENT_DEFS_OPEN = "_AGENT_DEFS: list[dict] = ["


class ScaffoldError(Exception):
    """A refusal — nothing has been written when this is raised."""


def validate_name(name: str) -> None:
    if not _NAME_RE.match(name):
        raise ScaffoldError(
            f"invalid name {name!r}: use lower snake_case starting with a letter "
            "(e.g. my_thing)"
        )
    if len(name) > _MAX_NAME_LEN:
        raise ScaffoldError(f"invalid name {name!r}: longer than {_MAX_NAME_LEN} characters")
    if keyword.iskeyword(name) or keyword.issoftkeyword(name):
        raise ScaffoldError(f"invalid name {name!r}: Python keyword")
    if name.endswith(("_flow", "_crew")):
        raise ScaffoldError(
            f"invalid name {name!r}: drop the _flow/_crew suffix (it is added for you)"
        )


def _camel(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


def _default_title(name: str) -> str:
    return " ".join(part.capitalize() for part in name.split("_"))


# ── Templates ────────────────────────────────────────────────────────────────

def _flow_py(name: str, camel: str, title: str) -> str:
    return f'''from crewai.flow.flow import Flow, listen, start
from pydantic import BaseModel

from src.automation.crews.{name}_crew.crew import {camel}Crew
from src.automation.flows.base import FlowMixin


class {camel}State(BaseModel):
    topic: str = ""
    run_id: int = 0
    usage: dict = {{}}
    # llm_provider / llm_model must be declared to survive kickoff(inputs=...).
    llm_provider: str = ""
    llm_model: str = ""
    previous_error: str = ""


class {camel}Flow(FlowMixin, Flow[{camel}State]):

    @start()
    def validate_payload(self):
        self._check_required("topic")
        # The log strings below drive the step graph — keep them in sync with
        # `steps` in this automation's AutomationSpec (src/automation/spec.py).
        self._log("Payload validated")
        return self.state.model_dump()

    @listen(validate_payload)
    def execute_crew(self, _):
        return self._run_crew(
            {camel}Crew, temperature=0.4,
            inputs={{"topic": self.state.topic}},
            working_log="{title} agent working...",
            done_log="{title} result generated, formatting result...",
        )
'''


def _crew_py(name: str, camel: str) -> str:
    return f'''from pathlib import Path

import yaml
from crewai import Agent, Crew, Process, Task

_CFG = Path(__file__).parent / "config"

with open(_CFG / "agents.yaml") as _f:
    _AGENTS = yaml.safe_load(_f)
with open(_CFG / "tasks.yaml") as _f:
    _TASKS = yaml.safe_load(_f)


class {camel}Crew:
    """Plain class on purpose — no @CrewBase (see CLAUDE.md "Key Invariants")."""

    def __init__(self, llm=None):
        self._llm = llm
        self._agents = _AGENTS
        self._tasks = _TASKS

    def crew(self) -> Crew:
        # Build Agent/Task/Crew fresh on every call; add tools=[...] here if needed.
        agent = Agent(
            config=self._agents["{name}_agent"],
            tools=[],
            verbose=False,
            llm=self._llm,
        )
        task = Task(config={{**self._tasks["{name}_task"], "agent": agent}})
        return Crew(
            agents=[agent],
            tasks=[task],
            process=Process.sequential,
            verbose=False,
        )
'''


def _agents_yaml(name: str, title: str) -> str:
    return f"""{name}_agent:
  role: {title} Agent
  goal: >
    TODO: describe what this agent must achieve for the given topic.
  backstory: >
    TODO: describe the agent's expertise.
    You always produce clean, structured JSON output.
"""


def _tasks_yaml(name: str) -> str:
    return f"""{name}_task:
  description: >
    TODO: describe the task. The user-supplied topic is: {{topic}}
    If retrying, fix this issue from the previous attempt: {{previous_error}}
  expected_output: >
    A JSON object with exactly these keys:
    - "result": the main output (string)
    - "summary": a one-sentence summary
    Return ONLY the JSON object, no markdown fences.
  agent: {name}_agent
"""


def _test_py(name: str, camel: str) -> str:
    return f'''from unittest.mock import MagicMock

import pytest


def test_{name}_spec_is_registered_and_generic():
    from src.automation import spec

    s = spec.REGISTRY["{name}"]
    assert s.flow_class == "{camel}Flow"
    # Manifest-driven form: declared fields, no hand-written UI.
    assert s.custom_ui is False
    assert [f.name for f in s.fields] == ["topic"]
    assert spec.validate_registry() == []


def test_{name}_flow_raises_on_missing_topic():
    from src.automation.flows.{name}_flow import {camel}Flow
    with pytest.raises(Exception):
        {camel}Flow().kickoff(inputs={{"topic": "", "run_id": 0}})


def test_{name}_flow_calls_crew_with_topic(mocker):
    mock_result = MagicMock()
    mock_result.raw = '{{"result": "ok", "summary": "ok"}}'
    mock_result.usage_metrics = None

    mock_crew_instance = MagicMock()
    mock_crew_instance.crew.return_value.kickoff.return_value = mock_result

    mocker.patch("src.automation.harness.provider.resolve", return_value=(None, "openai", "gpt-4o-mini"))
    mock_cls = mocker.patch(
        "src.automation.flows.{name}_flow.{camel}Crew", return_value=mock_crew_instance
    )

    from src.automation.flows.{name}_flow import {camel}Flow
    result = {camel}Flow().kickoff(inputs={{"topic": "demo", "run_id": 0}})

    assert result == mock_result.raw
    # LLM is injected through the constructor, never post-init.
    assert "llm" in mock_cls.call_args.kwargs
    inputs = mock_crew_instance.crew.return_value.kickoff.call_args.kwargs["inputs"]
    assert inputs["topic"] == "demo"
    assert inputs["previous_error"] == ""


def test_{name}_crew_builds_fresh_objects_each_call():
    from src.automation.crews.{name}_crew.crew import {camel}Crew

    c = {camel}Crew(llm="gpt-4o-mini")
    first, second = c.crew(), c.crew()
    assert first is not second
    assert len(first.agents) == 1 and len(first.tasks) == 1
'''


def _spec_entry(name: str, camel: str, title: str) -> str:
    return f'''register(AutomationSpec(
    job_type="{name}", desc='TODO: one-line description', name="{title}", icon="🧩",
    flow_module="src.automation.flows.{name}_flow", flow_class="{camel}Flow",
    start_log="Launching {title} agent...", temperature=0.4,
    steps=(("Start", "Starting"), ("Validate", "Payload validated"),
           ("Generate", "result generated"), *_QA, _DONE),
    validate=lambda r: (bool(r.get("result")), "empty result"),
    # TODO: say what a *good* result looks like — this grounds the LLM judge.
    rubric="A non-empty result that directly and usefully addresses the requested topic.",
    name_template="{title}: {{topic}}",
    fields=(
        Field("topic", "text", "Topic", required=True, placeholder="What should the agent work on?"),
    ),
))

'''


def _agent_def_row(name: str, camel: str, title: str) -> str:
    return (
        f"    {{\"id\": '{name}_agent', \"name\": '{title} Agent', \"tools\": [], "
        f"\"crew\": '{camel}Crew', \"task\": '{name}_task', \"job_type\": '{name}', "
        f"\"source_file\": 'src/automation/crews/{name}_crew/config/agents.yaml'}},\n"
    )


# ── Shared-file edits (pure string → string) ─────────────────────────────────

def _insert_spec_entry(text: str, entry: str) -> str:
    idx = text.find(_SPEC_ANCHOR)
    if idx == -1:
        return text.rstrip("\n") + "\n\n" + entry.rstrip("\n") + "\n"
    # Keep two blank lines between the new entry and the trailing comment block.
    return text[:idx].rstrip("\n") + "\n\n" + entry + "\n" + text[idx:]


def _insert_agent_def(text: str, row: str) -> str:
    start = text.find(_AGENT_DEFS_OPEN)
    if start == -1:
        raise ScaffoldError(f"{SYSTEM_FILE}: cannot find {_AGENT_DEFS_OPEN!r}")
    close = text.find("\n]\n", start)
    if close == -1:
        raise ScaffoldError(f"{SYSTEM_FILE}: cannot find the end of _AGENT_DEFS")
    return text[: close + 1] + row + text[close + 1:]


# ── Entry points ─────────────────────────────────────────────────────────────

def scaffold(name: str, root: Path = REPO_ROOT, title: str | None = None) -> list[Path]:
    """Generate the automation under ``root``. Returns the paths written/edited.

    Raises :class:`ScaffoldError` *before writing anything* if it cannot proceed.
    """
    validate_name(name)
    title = title or _default_title(name)
    if not _TITLE_RE.match(title):
        raise ScaffoldError(
            f"invalid title {title!r}: letters, digits, spaces, '-' and '_' only"
        )
    camel = _camel(name)
    root = Path(root)

    crew_dir = root / "src/automation/crews" / f"{name}_crew"
    new_files: dict[Path, str] = {
        root / "src/automation/flows" / f"{name}_flow.py": _flow_py(name, camel, title),
        crew_dir / "__init__.py": "",
        crew_dir / "crew.py": _crew_py(name, camel),
        crew_dir / "config/agents.yaml": _agents_yaml(name, title),
        crew_dir / "config/tasks.yaml": _tasks_yaml(name),
        root / "tests/unit" / f"test_{name}_flow.py": _test_py(name, camel),
    }
    spec_path, system_path = root / SPEC_FILE, root / SYSTEM_FILE

    # ── Pre-flight: every refusal happens here, before the first write ──────
    for shared in (spec_path, system_path):
        if not shared.is_file():
            raise ScaffoldError(f"{shared} not found — is {root} the repo root?")
    existing = [p for p in (crew_dir, *new_files) if p.exists()]
    if existing:
        listing = "\n  ".join(str(p.relative_to(root)) for p in existing)
        raise ScaffoldError(f"refusing to overwrite existing path(s):\n  {listing}")

    spec_text = spec_path.read_text(encoding="utf-8")
    system_text = system_path.read_text(encoding="utf-8")
    if re.search(rf"""job_type\s*=\s*["']{name}["']""", spec_text):
        raise ScaffoldError(f"{SPEC_FILE} already registers job_type {name!r}")
    if re.search(rf"""["']job_type["']\s*:\s*["']{name}["']""", system_text):
        raise ScaffoldError(f"{SYSTEM_FILE} already has an _AGENT_DEFS row for {name!r}")
    if re.search(rf"\bclass {camel}(Flow|Crew)\b", spec_text + system_text):
        raise ScaffoldError(f"class name {camel}Flow/{camel}Crew is already in use")

    new_spec = _insert_spec_entry(spec_text, _spec_entry(name, camel, title))
    new_system = _insert_agent_def(system_text, _agent_def_row(name, camel, title))

    # ── Write ───────────────────────────────────────────────────────────────
    for path, content in new_files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "x", encoding="utf-8") as f:  # "x": never clobber
            f.write(content)
    spec_path.write_text(new_spec, encoding="utf-8")
    system_path.write_text(new_system, encoding="utf-8")
    return [*new_files, spec_path, system_path]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scaffold a new single-crew automation (spec + flow + crew + tests)."
    )
    parser.add_argument("name", help="job type in lower snake_case, e.g. my_thing")
    parser.add_argument("--title", help="display name (default: derived from NAME)")
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    try:
        written = scaffold(args.name, root=args.root, title=args.title)
    except ScaffoldError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    root = Path(args.root)
    print(f"Scaffolded automation {args.name!r}:")
    for p in written:
        print(f"  {p.relative_to(root)}")
    print(
        "\nNext steps:\n"
        f"  1. Fill in the TODOs (agents.yaml, tasks.yaml, the spec's desc/rubric/fields).\n"
        f"  2. uv run pytest tests/unit/test_spec_registry.py tests/unit/test_{args.name}_flow.py -v\n"
        "  3. No UI edit needed: custom_ui=False + fields renders the form from the manifest."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
