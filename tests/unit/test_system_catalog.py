"""The System catalog builds agents from crew YAML — and must never crash startup."""
from src.routers import system


def test_build_agents_has_all_defs_with_structure():
    agents = system._build_agents()
    assert len(agents) == len(system._AGENT_DEFS)
    for a in agents:
        assert a["id"] and a["name"] and a["crew"] and a["job_type"]
    # real prose is pulled from YAML for a known agent
    hn = next(a for a in agents if a["id"] == "hn_analyst")
    assert hn["role"] and hn["backstory"]


def test_build_agents_survives_malformed_yaml(mocker):
    """A broken agents.yaml must degrade to empty prose, not raise at import time."""
    mocker.patch.object(system, "_read_file", return_value="::: not valid: yaml: [")
    agents = system._build_agents()
    assert len(agents) == len(system._AGENT_DEFS)
    assert all(a["role"] == "" and a["goal"] == "" and a["backstory"] == "" for a in agents)


def test_build_agents_survives_non_dict_yaml(mocker):
    """Top-level YAML that isn't a mapping (e.g. a list) must not raise."""
    mocker.patch.object(system, "_read_file", return_value="- one\n- two\n")
    agents = system._build_agents()
    assert len(agents) == len(system._AGENT_DEFS)
    assert all(a["role"] == "" for a in agents)


def test_build_agents_survives_missing_file(mocker):
    mocker.patch.object(system, "_read_file", return_value="")
    assert len(system._build_agents()) == len(system._AGENT_DEFS)
