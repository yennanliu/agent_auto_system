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


def test_plugins_disabled_by_default(monkeypatch):
    monkeypatch.delenv("AUTOMATION_PLUGINS_ENABLED", raising=False)
    assert spec.load_plugins() == []


def test_ensure_plugins_loaded_runs_once(monkeypatch):
    """Plugins load lazily on first table derivation, and only once."""
    calls = []
    monkeypatch.setattr(spec, "_plugins_loaded", False)
    monkeypatch.setattr(spec, "load_plugins", lambda: calls.append(1))
    spec.ensure_plugins_loaded()
    spec.ensure_plugins_loaded()
    spec.job_types()  # a derivation helper — must not re-trigger
    assert calls == [1]


def test_importing_spec_does_not_load_plugins(monkeypatch):
    """Import is pure data: merely importing spec must not trigger plugin loading.

    (Enforced by construction — load_plugins() is not called at module scope.)
    """
    import pathlib
    src = pathlib.Path(spec.__file__).read_text()
    # the only call sites are inside ensure_plugins_loaded()/tests, never top-level
    assert "\nload_plugins()" not in src


def test_plugin_can_register_an_automation(monkeypatch):
    """A third-party entry point registers a spec when plugins are enabled."""
    monkeypatch.setenv("AUTOMATION_PLUGINS_ENABLED", "1")

    def _setup(register):
        register(spec.AutomationSpec(
            job_type="plugin_demo", name="Plugin Demo", icon="🔌",
            desc="from a plugin", rubric="demo",
            validate=lambda r: (True, ""),
        ))

    class _EP:
        name = "demo"
        def load(self):  # noqa: D401
            return _setup

    monkeypatch.setattr("importlib.metadata.entry_points", lambda group=None: [_EP()])
    try:
        loaded = spec.load_plugins()
        assert "demo" in loaded
        assert "plugin_demo" in spec.REGISTRY
        assert spec.manifest()[-1]["job_type"] in spec.REGISTRY  # serializes cleanly
    finally:
        spec.REGISTRY.pop("plugin_demo", None)
