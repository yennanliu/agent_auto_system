#!/usr/bin/env python
"""Freeze the FastAPI backend into a standalone binary for the Electron desktop app.

    uv run python scripts/build_backend.py

Output: electron/backend-dist/agent-auto-system/  (a one-dir bundle; the executable
is `agent-auto-system` inside it). electron-builder copies this folder into the
app's resources as `backend/` (see electron/package.json extraResources).

See doc/electron-desktop-app-design.md (Phase 2).
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "electron" / "backend-dist"
WORK = ROOT / "build" / "pyinstaller"
SPEC = ROOT / "agent_backend.spec"


def main() -> int:
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--distpath",
        str(DIST),
        "--workpath",
        str(WORK),
        str(SPEC),
    ]
    print("$", " ".join(cmd))
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode == 0:
        exe = DIST / "agent-auto-system" / "agent-auto-system"
        print(f"\n✓ Backend frozen: {exe}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
