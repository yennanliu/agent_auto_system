"""The custom-automation runner dispatches a no-tools crew and parses its JSON."""
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_run_custom_parses_result(mocker):
    from src import custom_automations
    from src.automation import executor

    definition = SimpleNamespace(
        name="Tagline Writer", instructions="write a tagline",
        output_hint="JSON with tagline", temperature=0.5, enabled=True,
    )
    mocker.patch.object(custom_automations, "get_by_job_type", return_value=definition)
    mocker.patch("src.automation.harness.provider.resolve",
                 return_value=("LLM", "openai", "gpt-4o-mini"))
    mocker.patch("src.automation.executor.append_log")

    fake_crew = SimpleNamespace(kickoff=lambda: SimpleNamespace(raw='{"tagline": "Boil smarter."}'))
    mocker.patch("src.automation.crews.dynamic_crew.DynamicCrew.crew", return_value=fake_crew)

    result, usage, serve = await executor._run_custom(
        1, "custom:tagline_writer",
        {"product": "kettle", "previous_error": ""}, "openai", "gpt-4o-mini",
    )
    assert result == {"tagline": "Boil smarter."}
    assert serve["served_model"] == "gpt-4o-mini"
    assert serve["fallback_used"] is False


@pytest.mark.asyncio
async def test_run_custom_rejects_unknown(mocker):
    from src import custom_automations
    from src.automation import executor

    mocker.patch.object(custom_automations, "get_by_job_type", return_value=None)
    with pytest.raises(ValueError, match="Unknown or disabled"):
        await executor._run_custom(1, "custom:nope", {}, "openai", "gpt-4o-mini")


def test_dynamic_crew_has_no_tools():
    """The safety boundary: the dynamically built agent gets an empty tool list."""
    from src.automation.crews.dynamic_crew import DynamicCrew

    definition = SimpleNamespace(name="X", instructions="do X", output_hint="")
    crew = DynamicCrew(definition, {"a": "1"}, llm=None).crew()
    assert crew.agents[0].tools == []
