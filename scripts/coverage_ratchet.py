#!/usr/bin/env python3
"""Coverage ratchet: the gate in pyproject.toml may only be raised, never lowered.

The threshold lives in exactly one place — ``[tool.coverage.report] fail_under``
in pyproject.toml. CI runs this on pull requests to compare the PR's value with
the base branch's:

    uv run python scripts/coverage_ratchet.py --base-ref origin/main

Exit 0 when the gate is unchanged or raised (or the base has no gate yet);
exit 1 when a change lowers or removes it.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def read_fail_under(pyproject_text: str) -> float | None:
    """The configured gate, or None when the file sets none."""
    data = tomllib.loads(pyproject_text)
    value = data.get("tool", {}).get("coverage", {}).get("report", {}).get("fail_under")
    return None if value is None else float(value)


def check(current: float | None, base: float | None) -> tuple[bool, str]:
    """(ok, message) for moving the gate from ``base`` to ``current``."""
    if base is None:
        return True, f"base has no coverage gate; current fail_under={current}"
    if current is None:
        return False, f"coverage gate removed (base fail_under={base:g})"
    if current < base:
        return False, (
            f"coverage gate lowered: fail_under {base:g} -> {current:g}. "
            "The gate is a ratchet — add tests instead of lowering it."
        )
    return True, f"coverage gate ok: fail_under {base:g} -> {current:g}"


def _base_pyproject(base_ref: str) -> str | None:
    try:
        return subprocess.run(
            ["git", "show", f"{base_ref}:pyproject.toml"],
            cwd=REPO_ROOT, check=True, capture_output=True, text=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-ref", default="origin/main")
    args = parser.parse_args(argv)

    current = read_fail_under((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    base_text = _base_pyproject(args.base_ref)
    if base_text is None:
        print(f"error: cannot read pyproject.toml at {args.base_ref}", file=sys.stderr)
        return 1
    ok, message = check(current, read_fail_under(base_text))
    print(message, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
