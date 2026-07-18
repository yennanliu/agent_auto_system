# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: freeze the FastAPI backend into a standalone binary.

Ships inside the Electron .app so end users need no Python/uv/venv. Built via
`uv run python scripts/build_backend.py`. See doc/electron-desktop-app-design.md.

Two things make this non-trivial and are handled below:
  1. Crew configs load at import time via Path(__file__).parent/"config"/*.yaml,
     so those YAMLs must be bundled at their package-relative paths.
  2. The CrewAI stack (crewai, chromadb, tiktoken, onnxruntime, …) leans on
     dynamic imports + data files, so we collect_all() them wholesale.
"""

from PyInstaller.utils.hooks import collect_all, collect_data_files

datas = []
binaries = []
hiddenimports = []

# (1) Crew YAML configs — preserve the src/automation/crews/**/config/*.yaml layout
#     so Path(__file__).parent/"config" resolves inside the frozen bundle.
datas += collect_data_files("src", includes=["**/*.yaml", "**/*.yml"])

# (2) Dynamic-import-heavy packages. Wrapped so a missing optional package (e.g.
#     litellm on crewai 1.x) doesn't abort the whole build.
_COLLECT = [
    "crewai",
    "crewai_tools",
    "chromadb",
    "tiktoken",
    "tiktoken_ext",
    "litellm",
    "onnxruntime",
    "tokenizers",
    "langfuse",
    "opentelemetry",
    "openai",
    "sqlmodel",
    "authlib",
]
for _pkg in _COLLECT:
    try:
        _d, _b, _h = collect_all(_pkg)
        datas += _d
        binaries += _b
        hiddenimports += _h
    except Exception as _e:  # noqa: BLE001 — best-effort; real gaps surface at build
        print(f"[agent_backend.spec] skip collect_all({_pkg!r}): {_e}")

# uvicorn drives the app object directly (no reloader/workers), but its protocol
# and loop modules are imported by name — pin them so they're not tree-shaken out.
hiddenimports += [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
]

a = Analysis(
    ["src/desktop_entry.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Playwright ships its own node driver + browsers and is bundled separately
    # (Decision 7); browser flows are a follow-up. Excluding keeps this build
    # tractable — the app boots and non-browser flows work.
    excludes=["playwright"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="agent-auto-system",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # spawned headless by Electron; stdout/stderr → backend.log
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="agent-auto-system",
)
