"""Guards for the automation registry (SSOT) — see src/automation/spec.py.

These make a misregistered automation fail *loud* (a red test) instead of
silently vanishing from a dropdown or becoming un-runnable.
"""
import importlib

import pytest

from src.automation import spec


def test_registry_is_internally_consistent():
    assert spec.validate_registry() == []


def test_allowlist_matches_registry():
    from src import settings_store

    assert set(settings_store.ALL_AUTOMATIONS) == set(spec.REGISTRY)
    # order is preserved (registration order == allowlist order)
    assert settings_store.ALL_AUTOMATIONS == spec.job_types()


def test_flow_map_matches_executor():
    from src.automation import executor

    assert executor._FLOW_MAP == spec.flow_map()


def test_checks_and_rubrics_match_harness():
    from src.automation.harness import evaluator, validator

    assert validator._CHECKS == spec.checks()
    assert evaluator._RUBRICS == spec.rubrics()


def test_flow_steps_match():
    from src.automation import flow_steps

    assert flow_steps.FLOW_STEPS == spec.step_map()


def test_every_flow_backed_spec_imports():
    """Each (flow_module, flow_class) must actually resolve to a class."""
    for jt, (module, cls, _log) in spec.flow_map().items():
        mod = importlib.import_module(module)
        assert hasattr(mod, cls), f"{jt}: {module} has no attribute {cls}"


@pytest.mark.parametrize("job_type", list(spec.REGISTRY))
def test_every_spec_has_validate_and_rubric(job_type):
    s = spec.REGISTRY[job_type]
    passed, reason = s.validate({})
    assert isinstance(passed, bool) and isinstance(reason, str)
    assert s.rubric  # non-empty rubric grounds the LLM judge


def test_pipeline_has_no_flow_but_is_allowlisted():
    """Pipeline is dispatched directly by the executor — no flow, no step graph,
    but it is still a valid, runnable job type with a check + rubric."""
    s = spec.REGISTRY["pipeline"]
    assert s.flow_module is None and s.flow_class is None
    assert s.steps == ()
    assert "pipeline" not in spec.flow_map()
    assert "pipeline" not in spec.step_map()
    assert "pipeline" in spec.checks() and "pipeline" in spec.rubrics()


def test_manifest_shape():
    """The browser manifest exposes everything the UI needs to render forms."""
    m = spec.manifest()
    assert len(m) == len(spec.REGISTRY)
    by = {a["job_type"]: a for a in m}
    for a in m:
        assert {"job_type", "name", "icon", "desc", "custom_ui",
                "name_template", "steps", "fields"} <= set(a)
    # A generic (manifest-driven) automation carries its fields; hn has a number field.
    hn = by["hacker_news_digest"]
    assert hn["custom_ui"] is False
    assert any(f["name"] == "limit" and f["type"] == "number" for f in hn["fields"])
    assert hn["name_template"] == "HN Digest (top {limit})"
    # A bespoke automation is flagged custom_ui so the UI shows its hand-written form.
    assert by["profit_health_check"]["custom_ui"] is True
    assert by["pipeline"]["custom_ui"] is True
