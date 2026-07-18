# Agent Auto System — Desktop Shell (Electron)

Wraps the whole system into one app: this Electron shell launches the FastAPI
backend as a hidden `127.0.0.1` sidecar and renders the existing `ui/` in-window.
No terminal, no browser tab. Design & plan: [`../doc/electron-desktop-app-design.md`](../doc/electron-desktop-app-design.md).

**Status: Phase 0 spike.** The shell drives the *dev* backend via `uv run uvicorn`
(so `uv` + the project `.venv` must be present). Freezing the backend with
PyInstaller and vendoring Chromium is Phase 2.

## Run the spike

```bash
# 1. Backend deps (once, from the repo root)
cd .. && uv sync && uv run playwright install chromium && cd electron

# 2. Shell deps (once)
npm install

# 3. Launch the app
npm start
```

What happens on launch (`main.js`):
1. Pick a free loopback port.
2. Spawn `uv run uvicorn src.main:app` with `DESKTOP_MODE=1` (single local admin,
   no login screen), writing data to the per-user app-data dir — never the repo.
3. Show a splash window; poll `GET /health` until the DB is up.
4. Load `http://127.0.0.1:<port>/` into the window.
5. On quit: SIGTERM → SIGKILL the backend; single-instance lock prevents dupes.

## Data & logs

Everything writable lives under Electron's `userData/data/`:

| Path | What |
|---|---|
| `app.db` | SQLite database (`DATABASE_URL`) |
| `uploads/` | uploaded files (`UPLOAD_DIR`) |
| `logs/backend.log` | sidecar stdout/stderr — **first place to look on failure** |

macOS: `~/Library/Application Support/agent-auto-system-desktop/data/`.
Also reachable via the app menu: **Help → Reveal Logs**.

## Files

| File | Role |
|---|---|
| `main.js` | app lifecycle: port pick, spawn sidecar, health-poll, window, teardown |
| `health.js` | poll `/health` with timeout + backoff |
| `preload.js` | locked-down preload (contextIsolation on, no nodeIntegration) |
| `splash.html` | "Starting…" shown while the backend boots |
| `package.json` | electron dep + `start` script |

## Not yet done (see design doc)

- Phase 2: PyInstaller-freeze the backend; vendor Playwright Chromium; `electron-builder` DMG.
- Phase 3: macOS code signing + notarization.
- Migrate WeasyPrint → headless-Chromium PDF for the packaged build.
