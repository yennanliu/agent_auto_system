"""Frozen-backend entrypoint for the Electron desktop app.

PyInstaller freezes THIS module (see agent_backend.spec) into a standalone binary
that ships inside the .app — no Python/uv/venv needed on the user's machine. The
Electron shell (electron/main.js) spawns the binary with PORT + DESKTOP_MODE + the
app-data paths already set in the environment.

Dev/spike still uses `uv run uvicorn src.main:app`; only the packaged app uses this.
See doc/electron-desktop-app-design.md.
"""

import multiprocessing
import os


def main() -> None:
    # Must run before anything spawns a process (CrewAI/tools may) so a frozen
    # child re-execs the bundle instead of trying to import a non-existent script.
    multiprocessing.freeze_support()

    import uvicorn

    # Import the app after freeze_support so startup ordering is deterministic.
    from src.main import app

    port = int(os.environ.get("PORT", "8000"))
    # Loopback only + no reloader/workers: this is a single local process behind
    # the desktop window, not a public server.
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
