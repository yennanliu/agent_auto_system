# Electron Desktop App — Design & Implementation Plan

Wrap the entire Agent Auto System into a single double-clickable desktop application
for macOS (and Windows/Linux later). The user launches **one app icon** — no `uvicorn`
in a terminal, no browser tab, no `localhost` URL to type. The Python/FastAPI backend
runs as a hidden child process ("sidecar"); the existing web UI (`ui/`) renders inside
an Electron window pointed at `http://127.0.0.1:<port>`.

**Goal:** ship the system "as an app," reusing ~100% of the current backend and frontend.
**Non-goal:** rewrite the UI natively, or port backend logic off Python (CrewAI, Playwright,
WeasyPrint cannot run anywhere but a real OS process — so we bundle them, not replace them).

## Decisions (locked)

| # | Decision | Choice |
|---|---|---|
| 1 | Shell framework | **Electron** — bundles its own Chromium (consistent rendering), trivial Python-sidecar via `child_process`, most battle-tested cross-platform packaging. Team is already JS + Python (no Rust toolchain, unlike Tauri). |
| 2 | Backend model | **Local bundled sidecar** — the app ships and auto-launches the FastAPI server on `127.0.0.1`. Fully self-contained, works offline, single-user per machine. |
| 3 | Same-origin auth | **Unchanged** — UI and BE share `127.0.0.1:<port>`, so the existing `SessionMiddleware` cookie auth and the no-CORS setup keep working as-is. No token/CORS rework needed. |
| 4 | Python packaging | **PyInstaller one-dir freeze** of the FastAPI app — no system Python required on the user's machine. (Alternative considered: ship a relocatable `uv`/venv; rejected as larger and more fragile to sign.) |
| 5 | Port selection | **Dynamic free port** chosen at launch (not hard-coded 8000) — avoids collisions with a dev server or a second instance. Passed to the renderer after health-check passes. |
| 6 | PDF engine | **Migrate WeasyPrint → headless-Chromium PDF** for the bundled build (see Risk R2). The Playwright Chromium is already vendored for `form_fill`; reuse it. WeasyPrint's Pango/Cairo native libs are the single biggest signing/bundling risk. |
| 7 | Playwright browsers | **Vendored into the bundle** at build time; `PLAYWRIGHT_BROWSERS_PATH` pinned to a path inside the app's resources at runtime. |
| 8 | Data location | **User app-data dir** (`app.getPath('userData')`) for the SQLite DB, `uploads/`, `reports/`, and `.env` — never inside the read-only `.app` bundle. |
| 9 | Distribution | **electron-builder**, macOS DMG first, code-signed + notarized. Windows NSIS target is a later config flip. |
| 10 | Browser-login sessions | **Disabled in the packaged app** (`BROWSER_LOGIN_ENABLED=0` semantics) initially — headed-login refresh (`src/routers/sessions.py`) is a v2 for desktop (see Open Questions). |
| 11 | Auth (v1) | **Single local admin, no login screen** — a `DESKTOP_MODE=1` env auto-provisions one admin user and auto-authenticates the session; the login overlay is skipped. RBAC code stays intact but serves one user. Keeps v1 simple. |

## Architecture

```
┌──────────────────────────── AgentAutoSystem.app ────────────────────────────┐
│                                                                              │
│  Electron main process (electron/main.js)                                    │
│    1. pick a free 127.0.0.1 port                                             │
│    2. spawn sidecar:  <resources>/backend/agent-auto-system  (PyInstaller)   │
│         env: PORT, DATABASE_URL=<userData>/app.db, UPLOAD_DIR,               │
│              PLAYWRIGHT_BROWSERS_PATH=<resources>/ms-playwright,             │
│              BROWSER_LOGIN_ENABLED=0, SCHEDULER_ENABLED=1                     │
│    3. poll GET /health until db_ok, with timeout + retry                     │
│    4. create BrowserWindow → loadURL(http://127.0.0.1:PORT/)                 │
│    5. on quit: SIGTERM the sidecar, wait, SIGKILL fallback                   │
│                                                                              │
│  ┌────────────────────┐   http (same origin)   ┌───────────────────────────┐│
│  │ Electron renderer  │ ─────────────────────► │ FastAPI sidecar (hidden)  ││
│  │ = your ui/ app.js  │ ◄───── SSE / JSON ───── │ uvicorn on 127.0.0.1:PORT ││
│  │ (Chromium window)  │                        │ CrewAI · Playwright ·     ││
│  └────────────────────┘                        │ scheduler · SQLite        ││
│                                                 └───────────────────────────┘│
│                                                                              │
│  Bundled resources: PyInstaller backend · ms-playwright/chromium · ui/       │
│  User data (writable, outside bundle): app.db · uploads/ · reports/ · .env   │
└──────────────────────────────────────────────────────────────────────────────┘
```

### Why a sidecar (not "port the backend to JS")

The value of the system is Python-only: CrewAI orchestration, the harness
(`provider`/`validator`/`evaluator`), Playwright browser automation, the cron scheduler,
SQLModel. Rewriting any of it in Node would be a multi-month rewrite with no user-visible
benefit. Electron's job is purely: **launch the process, show its UI, tear it down.**
The backend code is unchanged except for reading config from env (port, data paths).

### Repository layout (new)

```
electron/
  main.js            # app lifecycle: port pick, spawn sidecar, health-poll, window, quit
  preload.js         # minimal, contextIsolation on; no nodeIntegration in renderer
  splash.html        # "Starting…" shown while the sidecar boots
  health.js          # poll http://127.0.0.1:PORT/health with backoff
  package.json       # electron + electron-builder, build targets, scripts
  build/
    entitlements.mac.plist
    icon.icns / icon.ico
scripts/
  build_backend.py   # PyInstaller spec runner → dist/backend/
  vendor_playwright.py  # copy the installed chromium into the build tree
```

No changes to `src/` beyond config-from-env (below). `ui/` is copied into the bundle
and served by FastAPI exactly as today (`app.mount("/ui", ...)`, `FileResponse("ui/index.html")`).

## Backend changes required (small, env-only)

The backend must not assume a hard-coded port, a CWD-relative DB, or a writable bundle dir.

| Area | Today | Change |
|---|---|---|
| Port | `--port 8000` (CLI) | Read `PORT` env; sidecar entrypoint calls `uvicorn.run(app, host="127.0.0.1", port=int(os.environ["PORT"]))` |
| DB path | CWD-relative SQLite | Honor `DATABASE_URL`/`APP_DATA_DIR`; default under `userData` when packaged |
| `uploads/`, `reports/` | CWD-relative | Resolve against `APP_DATA_DIR` env |
| `ui/` path | `FileResponse("ui/index.html")` | Resolve relative to a `resource_path()` helper (bundle-safe), not CWD |
| Playwright | uses default browser path | Respect `PLAYWRIGHT_BROWSERS_PATH` (Playwright already does; just set it) |
| PDF | WeasyPrint | Swap to headless-Chromium renderer for the packaged build (Decision 6) |
| Startup log | logs to console | Also log to a file under `APP_DATA_DIR/logs/` so we can debug packaged runs |

A new **`src/desktop_entry.py`** is the PyInstaller entrypoint: sets defaults for the env
vars above (deriving `APP_DATA_DIR` from an env passed by Electron), then boots uvicorn.
Dev flow (`uv run uvicorn ...`) stays exactly as documented in CLAUDE.md.

## Implementation plan (phased)

### Phase 0 — Spike / de-risk (½–1 week)  ← do this first
Prove the two riskiest bundling problems before committing.
1. `electron/main.js` that spawns **plain `uv run uvicorn`** (not yet frozen), health-polls, opens a window on the real UI. Confirms the whole loop works end-to-end.
2. PyInstaller-freeze the backend and confirm **a CrewAI run + a Playwright automation both work from the frozen binary** (imports, dynamic deps, browser path). This is where hidden-import surprises surface.
3. Confirm **PDF generation** works via the headless-Chromium path from the frozen binary (validates Decision 6, retires the WeasyPrint risk).

**Exit criteria:** double-clicking a dev-signed `.app` runs one real automation and produces a PDF, with no terminal.

### Phase 1 — Robust app shell (1 week)
- Dynamic free-port selection; `PORT` handed to the sidecar.
- Splash window during boot; health-poll with timeout + friendly error dialog on failure.
- Clean shutdown: SIGTERM → wait → SIGKILL; ensure no orphan uvicorn/Chromium processes.
- All data paths under `userData`; first-run creates the dir, `.env`, and DB.
- Single-instance lock (`app.requestSingleInstanceLock()`).
- File logging for the sidecar; a "Reveal logs" menu item.

### Phase 2 — Packaging & vendoring (1 week) — 🟡 backend freeze DONE
- ✅ **PyInstaller freeze** — `agent_backend.spec` + `scripts/build_backend.py` produce a
  one-dir bundle (`electron/backend-dist/agent-auto-system/`). Entry point:
  `src/desktop_entry.py`. Wired into `electron-builder` `extraResources` (→ `resources/backend/`).
- ✅ **Verified end-to-end:** the frozen binary (no Python/uv on PATH) boots and serves
  `/health`, `/api/auth/me` (DESKTOP_MODE auto-auth), the UI, and `/api/automations/manifest`
  — confirming every crew's `Path(__file__).parent/config/*.yaml` resolves *inside* the
  bundle (`collect_data_files("src", includes=["**/*.yaml"])`). Retires risk R1.
- ⏭️ **Playwright** excluded from the freeze for now (Decision 7) — browser-driven flows are a
  follow-up: vendor Chromium into resources + pin `PLAYWRIGHT_BROWSERS_PATH`. Non-browser
  (LLM) flows work today.
- ⏭️ macOS `.app` + DMG via electron-builder (CI `desktop.yml` build job does this) — verify
  cold-start time on a clean Mac.
- ⚠️ **Bundle size finding:** the frozen backend is **~570 MB** (5k files), driven by the
  CrewAI stack (`chromadb`, `onnxruntime`, `tokenizers`). Trimming candidates: drop unused
  embedding/vector deps if CrewAI memory is off. Track before GA.

### Phase 3 — Signing, notarization, polish (1 week)
- Apple Developer ID signing + notarization (`entitlements.mac.plist`, hardened runtime).
- App icon, menu bar, "About", auto-update (electron-updater) — optional.
- Smoke-test on a clean Mac with **no Python installed**.

### Phase 4 — Windows (later, ~½ week)
- PyInstaller on Windows + `electron-builder` NSIS target. Mostly config; native libs differ, re-verify Playwright + PDF.

**Total for shippable macOS app: ~3–4 weeks**, dominated by packaging/signing — the Electron code itself is small.

## Risks & mitigations

| # | Risk | Impact | Mitigation |
|---|---|---|---|
| R1 | ~~PyInstaller misses CrewAI/dynamic imports~~ | Frozen app crashes on run | ✅ **Retired** — frozen binary boots + serves all endpoints incl. crew manifest; `collect_all` for the crewai stack + `collect_data_files("src", **/*.yaml)` in `agent_backend.spec` |
| R2 | **WeasyPrint native libs** (Pango/Cairo) hard to bundle + sign | Notarization/runtime failures | **Decision 6:** replace with headless-Chromium PDF (Chromium already vendored). Retire WeasyPrint from the packaged build |
| R3 | Bundle size — measured **~570 MB** backend (crewai/chromadb/onnxruntime) + Electron Chromium | Large download | Accept for v1; trim unused embedding/vector deps; later evaluate Tauri (system webview) to drop the Electron Chromium |
| R4 | Orphan processes on crash/quit | Zombie uvicorn/browsers | Robust teardown + single-instance lock; track child PIDs; kill process group |
| R5 | Port collision / firewall prompt | Boot failure | Dynamic free port on `127.0.0.1` (loopback only → typically no firewall prompt) |
| R6 | Code signing gaps on bundled native libs | Gatekeeper blocks app | Sign all nested binaries (Playwright, PyInstaller `.so`s); notarize; verify on clean machine in Phase 3 |
| R7 | Long-running scheduler/SSE inside a desktop app | Battery/lifecycle quirks | Scheduler runs only while app is open (acceptable for single-user local); document the trade-off |

## Open questions (decide before Phase 1)

1. **Multi-user / RBAC in a single-user app.** ✅ **DECIDED (v1):** single local admin,
   no login screen — `DESKTOP_MODE=1` auto-provisions one admin and auto-authenticates,
   the login overlay is skipped (Decision 11). RBAC code stays intact but serves one user.
2. **API keys.** Each install needs LLM keys. First-run setup screen, or reuse the existing
   Admin → LLM keys UI? *Recommendation: reuse existing Admin UI; add a first-run nudge.*
3. **Browser-login sessions** (tasker/104/Shopee headed refresh). These already assume a
   local machine — desktop is actually a *better* fit than a remote server. Enable in a v2
   once core bundling is proven (currently disabled, Decision 10).
4. **Auto-update.** Ship electron-updater from v1, or manual DMG re-download for internal use?

## Alternatives considered (and why not)

- **Tauri instead of Electron** — smaller app (system webview, no bundled Chromium), but adds a Rust toolchain and per-OS webview rendering differences. Revisit for a v2 size reduction; Electron wins on speed-to-first-app and sidecar maturity for v1.
- **Thin client → hosted server** (Electron shell pointing at a remote deployment). Smaller app, shared data, keeps multi-user model, avoids all Python bundling. Rejected *for this goal* because the user explicitly wants the system to run **as a self-contained local app**, not against a server. (Kept on record: if offline/self-contained ever stops mattering, this is far cheaper — you already have `Dockerfile`/`docker-compose.yml`.)
- **PWA / "Add to Home Screen"** — no packaging pain, but still requires a running backend somewhere and isn't a real double-clickable macOS app. Doesn't meet the goal.

## Success criteria

- Double-click `AgentAutoSystem.app` on a **clean Mac with no Python** → app window opens, no terminal.
- Create and run at least one CrewAI automation and one Playwright automation to completion.
- Generate a PDF report.
- Quit leaves **no orphan processes**; relaunch preserves data (DB/uploads in `userData`).
- App is signed + notarized; Gatekeeper allows it without a right-click override.
