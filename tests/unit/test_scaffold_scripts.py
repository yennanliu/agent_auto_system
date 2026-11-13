"""Tests for the developer scripts added with the CI coverage gate (E14):

* ``scripts/new_automation.py`` — scaffolds an automation into a throwaway copy
  of the tree (never the real repo) and checks the result imports, passes the
  spec-registry guards, and lints clean.
* ``scripts/coverage_ratchet.py`` — the "gate may only go up" comparison.
"""
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"

# The Docker test image ships src/ + tests/ only (no scripts/), so skip there.
pytestmark = pytest.mark.skipif(
    not (SCRIPTS / "new_automation.py").is_file(), reason="scripts/ not present in this image"
)

NAME = "zz_scaffold_probe"


def _load(script: str):
    spec = importlib.util.spec_from_file_location(f"_e14_{script}", SCRIPTS / f"{script}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gen():
    return _load("new_automation")


@pytest.fixture
def tree(tmp_path):
    """A throwaway copy of the parts of the repo the scaffold reads and writes."""
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(REPO / "src", tmp_path / "src", ignore=ignore)
    (tmp_path / "tests/unit").mkdir(parents=True)
    for rel in ("tests/__init__.py", "tests/conftest.py", "tests/unit/__init__.py",
                "tests/unit/test_spec_registry.py", "tests/unit/test_system_catalog.py",
                "pyproject.toml"):
        shutil.copy(REPO / rel, tmp_path / rel)
    return tmp_path


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _run(tree: Path, *args: str) -> subprocess.CompletedProcess:
    # Fresh interpreter: `src` is already imported (from the real repo) in this
    # process. Drop coverage's subprocess hooks so the copy isn't measured.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("COV_CORE_", "COVERAGE_"))}
    env["PYTHONPATH"] = str(tree)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, *args], cwd=tree, env=env, capture_output=True, text=True, timeout=300,
    )


# ── new_automation.py ────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    "", "MyThing", "my-thing", "1abc", "_x", "x_", "a__b", "my thing", "class",
    "foo_flow", "foo_crew", "a" * 41, "../evil", "x;import os",
])
def test_invalid_names_are_rejected(gen, bad):
    with pytest.raises(gen.ScaffoldError):
        gen.validate_name(bad)


@pytest.mark.parametrize("good", ["a", "my_thing", "seo_audit2", "x1_y2"])
def test_valid_names_are_accepted(gen, good):
    gen.validate_name(good)


def test_invalid_name_or_title_writes_nothing(gen, tree):
    before = _snapshot(tree)
    with pytest.raises(gen.ScaffoldError):
        gen.scaffold("Bad-Name", root=tree)
    with pytest.raises(gen.ScaffoldError):
        gen.scaffold(NAME, root=tree, title='x", icon="oops')
    assert _snapshot(tree) == before


def test_refuses_an_existing_automation(gen, tree):
    before = _snapshot(tree)
    with pytest.raises(gen.ScaffoldError, match="refusing to overwrite"):
        gen.scaffold("hn_digest", root=tree)  # flow + crew files already exist
    with pytest.raises(gen.ScaffoldError, match="already registers"):
        gen.scaffold("hacker_news_digest", root=tree)  # job_type already registered
    assert _snapshot(tree) == before


def test_refuses_when_any_target_file_exists(gen, tree):
    stray = tree / "tests/unit" / f"test_{NAME}_flow.py"
    stray.write_text("# mine\n")
    before = _snapshot(tree)
    with pytest.raises(gen.ScaffoldError, match="refusing to overwrite"):
        gen.scaffold(NAME, root=tree)
    assert _snapshot(tree) == before


def test_second_run_is_refused_and_changes_nothing(gen, tree):
    gen.scaffold(NAME, root=tree)
    after_first = _snapshot(tree)
    with pytest.raises(gen.ScaffoldError):
        gen.scaffold(NAME, root=tree)
    assert gen.main([NAME, "--root", str(tree)]) == 1
    assert _snapshot(tree) == after_first


def test_scaffold_only_adds_to_shared_files(gen, tree):
    before = _snapshot(tree)
    written = gen.scaffold(NAME, root=tree, title="Scaffold Probe")
    after = _snapshot(tree)

    rel = {str(p.relative_to(tree)) for p in written}
    assert rel == {
        f"src/automation/flows/{NAME}_flow.py",
        f"src/automation/crews/{NAME}_crew/__init__.py",
        f"src/automation/crews/{NAME}_crew/crew.py",
        f"src/automation/crews/{NAME}_crew/config/agents.yaml",
        f"src/automation/crews/{NAME}_crew/config/tasks.yaml",
        f"tests/unit/test_{NAME}_flow.py",
        "src/automation/spec.py",
        "src/routers/system.py",
    }
    # Nothing else in the tree changed, and nothing pre-existing was removed.
    assert {k for k in after if after[k] != before.get(k)} == rel
    assert set(before) <= set(after)

    # Shared files are edited by pure insertion: every original line survives, in order.
    for shared in ("src/automation/spec.py", "src/routers/system.py"):
        old = before[shared].decode().splitlines()
        new = iter(after[shared].decode().splitlines())
        assert all(line in new for line in old), f"{shared}: an existing line was altered"

    crew_src = after[f"src/automation/crews/{NAME}_crew/crew.py"].decode()
    assert "@CrewBase" not in crew_src.replace("no @CrewBase", "")
    assert "def __init__(self, llm=None)" in crew_src
    spec_src = after["src/automation/spec.py"].decode()
    assert f'job_type="{NAME}"' in spec_src and 'name="Scaffold Probe"' in spec_src


def test_generated_automation_imports_and_passes_registry_checks(gen, tree):
    assert gen.main([NAME, "--root", str(tree)]) == 0

    # The repo's own registry + catalog guards, plus the generated test skeleton,
    # run against the scaffolded copy.
    result = _run(
        tree, "-m", "pytest", "-q", "-p", "no:cacheprovider",
        "tests/unit/test_spec_registry.py",
        "tests/unit/test_system_catalog.py",
        f"tests/unit/test_{NAME}_flow.py",
    )
    assert result.returncode == 0, result.stdout + result.stderr

    # The executor's dispatch table resolves to the generated flow, and the
    # manifest exposes a generic (no hand-written UI) form for it.
    probe = (
        "import importlib\n"
        "from src.automation import executor, spec\n"
        "from src import settings_store\n"
        "from src.routers import system\n"
        f"module, cls, _ = executor._FLOW_MAP['{NAME}']\n"
        "getattr(importlib.import_module(module), cls)\n"
        f"assert '{NAME}' in settings_store.ALL_AUTOMATIONS\n"
        f"m = next(a for a in spec.manifest() if a['job_type'] == '{NAME}')\n"
        "assert m['custom_ui'] is False and [f['name'] for f in m['fields']] == ['topic']\n"
        f"a = next(a for a in system._build_agents() if a['job_type'] == '{NAME}')\n"
        "assert a['role'] and a['goal'] and a['backstory']\n"
    )
    result = _run(tree, "-c", probe)
    assert result.returncode == 0, result.stdout + result.stderr

    # Generated code must satisfy the repo's lint config.
    result = _run(
        tree, "-m", "ruff", "check", "--no-cache",
        f"src/automation/flows/{NAME}_flow.py", f"src/automation/crews/{NAME}_crew",
        f"tests/unit/test_{NAME}_flow.py", "src/automation/spec.py", "src/routers/system.py",
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_real_repo_has_no_leftover_probe():
    """The scaffold tests work on a temp copy — nothing may leak into the repo."""
    assert not (REPO / f"src/automation/flows/{NAME}_flow.py").exists()
    assert not (REPO / f"src/automation/crews/{NAME}_crew").exists()
    assert NAME not in (REPO / "src/automation/spec.py").read_text(encoding="utf-8")
    assert NAME not in (REPO / "src/routers/system.py").read_text(encoding="utf-8")


# ── coverage_ratchet.py ──────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def ratchet():
    return _load("coverage_ratchet")


def test_ratchet_reads_gate_from_pyproject(ratchet):
    assert ratchet.read_fail_under("[tool.coverage.report]\nfail_under = 71\n") == 71.0
    assert ratchet.read_fail_under("[project]\nname = 'x'\n") is None
    # The real pyproject.toml carries the single source of truth for the gate.
    assert ratchet.read_fail_under((REPO / "pyproject.toml").read_text(encoding="utf-8")) is not None


@pytest.mark.parametrize("current, base, ok", [
    (71, 71, True),      # unchanged
    (75, 71, True),      # raised
    (70.5, 71, False),   # lowered
    (None, 71, False),   # removed
    (71, None, True),    # first introduction
])
def test_ratchet_only_allows_raising(ratchet, current, base, ok):
    assert ratchet.check(current, base)[0] is ok
