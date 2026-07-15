"""Browser-login session management for the storage-state automations.

Several automations (tasker.com.tw, 104.com.tw, Shopee) authenticate against
JS-rendered SPAs guarded by captcha / SMS-OTP, so we can't log in headlessly.
The established pattern is: log in ONCE in a real (headed) browser and persist
the Playwright ``storage_state`` (cookies) to a JSON file; every subsequent run
reuses that file headlessly. See ``scripts/tasker_login.py`` et al.

Those login scripts are terminal-only — the operator must SSH in and run them
whenever a session expires. This module exposes the same headed-login flow as an
**on-demand, UI-driven action** decoupled from any run:

  * ``session_status(name)`` — does a saved session exist, how old is it, is it
    still considered fresh? Drives the "needs refresh" hint in the UI.
  * ``start_login(name)`` — pop open a real Chromium window (on the machine
    running the server), let the operator log in, then poll until the site
    reports an authenticated session and save the ``storage_state``. Runs in a
    background thread; progress/result is tracked in-memory and polled via
    ``login_status(name)``.

This ONLY works when the server runs locally (the browser opens on the server's
display). Remote deployments should leave it disabled via
``BROWSER_LOGIN_ENABLED=0``; ``login_enabled()`` gates the HTTP entry point.
"""
from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

# ── spec registry ─────────────────────────────────────────────────────────────

# How long a saved session is trusted before the UI nudges you to refresh it.
# Cookies usually outlive this; the threshold is a conservative reminder, not a
# hard expiry (the run itself is the real check — it errors if the token is dead).
_DEFAULT_TTL = 7 * 24 * 3600  # 7 days


@dataclass(frozen=True)
class SessionSpec:
    """A browser-login-based automation and where its session lives."""

    name: str                # stable key, e.g. "tasker"
    label: str               # human label for the UI
    state_env: str           # env var holding the storage_state path
    default_state_path: str  # fallback path when the env var is unset
    login_url: str           # where the headed browser opens for login
    # Given a live Playwright page, return True once the site looks authenticated.
    is_logged_in: Callable[[object], bool] = field(repr=False, default=lambda _p: False)
    ttl_seconds: int = _DEFAULT_TTL


def _tasker_logged_in(page) -> bool:
    # Reuse the tool's own (battle-tested) logged-out heuristic, inverted.
    from src.automation.tools.tasker_apply_tool import _looks_logged_out
    return not _looks_logged_out(page)


# 104's login lives on dedicated hosts (login./signin.104.com.tw, plus the
# corporate OIDC flow). A plain "URL no longer contains /login" check is wrong
# here: those hosts don't have "/login" in the path, so it would fire the
# instant the page loads and slam the window shut before you can type. We also
# can't just invert the scraper's _looks_logged_out — on 104's current homepage
# the header login link is hidden in a collapsed menu, so that heuristic
# false-positives as "logged in". Instead require a *positive* account marker
# (logout / 我的104 / avatar / mylife) that is only present once authenticated.
_TW104_LOGIN_HOSTS = ("login.104.com.tw", "signin.104.com.tw", "/oidc/")
_TW104_AUTHED_SELECTORS = (
    ':text("登出")', ':text("我的104")', 'img[alt*="頭像"]',
    'a[href*="/mylife"]', 'a[href*="logout"]',
)


def _tw104_logged_in(page) -> bool:
    try:
        url = page.url or ""
    except Exception:  # noqa: BLE001 — page may be mid-navigation
        return False
    if any(h in url for h in _TW104_LOGIN_HOSTS):
        return False
    for sel in _TW104_AUTHED_SELECTORS:
        try:
            if page.locator(sel).count() > 0:
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _url_left_login(login_marker: str) -> Callable[[object], bool]:
    """Generic check: authenticated once the page has navigated away from the
    login route. Good enough for sites that redirect to a dashboard on success."""
    def _check(page) -> bool:
        try:
            return login_marker not in (page.url or "")
        except Exception:  # noqa: BLE001
            return False
    return _check


_SPECS: dict[str, SessionSpec] = {
    "tasker": SessionSpec(
        name="tasker",
        label="tasker.com.tw",
        state_env="TASKER_STORAGE_STATE",
        default_state_path="data/tasker_state.json",
        login_url="https://www.tasker.com.tw/auth/login",
        is_logged_in=_tasker_logged_in,
    ),
    "tw104": SessionSpec(
        name="tw104",
        label="104.com.tw",
        state_env="TW104_STORAGE_STATE",
        default_state_path="data/tw104_state.json",
        login_url="https://login.104.com.tw/login",
        is_logged_in=_tw104_logged_in,
    ),
    "shopee": SessionSpec(
        name="shopee",
        label="Shopee (賣家)",
        state_env="SHOPEE_STORAGE_STATE",
        default_state_path="data/shopee_state.json",
        login_url="https://shopee.tw/buyer/login",
        is_logged_in=_url_left_login("/login"),
    ),
}


def get_spec(name: str) -> SessionSpec | None:
    return _SPECS.get(name)


def all_specs() -> list[SessionSpec]:
    return list(_SPECS.values())


def state_path(spec: SessionSpec) -> str:
    return os.getenv(spec.state_env) or spec.default_state_path


def profile_dir(name: str, state_path_str: str) -> str:
    """Persistent user-data-dir for the headed login browser.

    A warm profile keeps Cloudflare's ``cf_clearance`` cookie between logins, so
    after you solve the Turnstile challenge once, later refreshes reuse the
    cleared profile instead of being re-challenged. Defaults next to the session
    JSON (e.g. ``data/tw104_profile``) so the UI refresh and the terminal login
    script share ONE profile; override with ``<NAME>_USER_DATA_DIR``."""
    env = os.getenv(f"{name.upper()}_USER_DATA_DIR")
    return env or str(Path(state_path_str).parent / f"{name}_profile")


# Anti-bot-detection launch config — mirrors the scraper tools, plus strips the
# "controlled by automation" switch/banner that Cloudflare Turnstile keys on.
_STEALTH_ARGS = ["--disable-blink-features=AutomationControlled"]
_STEALTH_IGNORE = ["--enable-automation"]


def open_login_context(pw, *, user_data_dir, locale="zh-TW", viewport=None, on_progress=None):
    """Open a headed, persistent Chromium context tuned to clear Cloudflare
    Turnstile: a warm ``user_data_dir`` profile plus automation-flag suppression.
    Prefers the real installed Chrome (``channel="chrome"``) — its fingerprint
    passes Turnstile far more reliably than bundled Chromium — and falls back to
    bundled Chromium when Chrome isn't installed. Returns a ``BrowserContext``;
    the caller is responsible for closing it."""
    note = on_progress or (lambda _m: None)
    Path(user_data_dir).mkdir(parents=True, exist_ok=True)
    kwargs = dict(
        user_data_dir=user_data_dir,
        headless=False,
        locale=locale,
        viewport=viewport or {"width": 1366, "height": 900},
        args=_STEALTH_ARGS,
        ignore_default_args=_STEALTH_IGNORE,
    )
    try:
        return pw.chromium.launch_persistent_context(channel="chrome", **kwargs)
    except Exception as exc:  # noqa: BLE001 — no system Chrome → bundled Chromium
        note(f"System Chrome unavailable ({type(exc).__name__}); using bundled Chromium.")
        return pw.chromium.launch_persistent_context(**kwargs)


def login_enabled() -> bool:
    """Headed browser login only makes sense when the server is local. Remote
    deployments opt out with BROWSER_LOGIN_ENABLED=0."""
    return os.getenv("BROWSER_LOGIN_ENABLED", "1") != "0"


# ── status ────────────────────────────────────────────────────────────────────

def session_status(name: str) -> dict | None:
    """Freshness of a saved session, or None if the automation is unknown."""
    spec = get_spec(name)
    if spec is None:
        return None
    path = state_path(spec)
    p = Path(path)
    exists = p.is_file()
    mtime: float | None = None
    age: float | None = None
    fresh = False
    if exists:
        try:
            mtime = p.stat().st_mtime
            age = max(0.0, _now() - mtime)
            fresh = age < spec.ttl_seconds
        except OSError:
            exists = False
    task = login_status(name)
    return {
        "name": spec.name,
        "label": spec.label,
        "state_path": path,
        "exists": exists,
        "mtime": mtime,
        "age_seconds": age,
        "ttl_seconds": spec.ttl_seconds,
        "fresh": fresh,
        "login_in_progress": bool(task and task.get("status") == "running"),
        "last_login": task,
    }


def all_status() -> list[dict]:
    return [s for s in (session_status(spec.name) for spec in all_specs()) if s]


def _now() -> float:
    return time.time()


# ── login task tracking (in-memory, thread-safe) ──────────────────────────────

# One login at a time per automation; the dict holds the latest task per name.
_TASKS: dict[str, dict] = {}
_LOCK = threading.Lock()


def login_status(name: str) -> dict | None:
    with _LOCK:
        task = _TASKS.get(name)
        return dict(task) if task else None


def _set_task(name: str, **fields) -> dict:
    with _LOCK:
        task = _TASKS.get(name) or {}
        task.update(fields)
        _TASKS[name] = task
        return dict(task)


class LoginError(Exception):
    """Raised by start_login for caller-actionable problems (unknown name,
    disabled, or already running). Carries the HTTP status the router should
    return, so the router need not string-match the message."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def start_login(
    name: str,
    *,
    timeout: int = 300,
    runner: Callable[[SessionSpec, Callable[[str], None], int], dict] | None = None,
) -> dict:
    """Kick off a headed-browser login in the background and return the task
    state immediately. Poll ``login_status(name)`` for progress/completion.

    ``runner`` is injectable so tests can drive the flow without a real browser;
    it defaults to the Playwright implementation. It receives
    ``(spec, on_progress, timeout)`` and must return a result dict.
    """
    spec = get_spec(name)
    if spec is None:
        raise LoginError(f"Unknown automation session '{name}'.", 404)
    if not login_enabled():
        raise LoginError(
            "Browser login is disabled on this server (BROWSER_LOGIN_ENABLED=0). "
            "It requires a local, headed environment.",
            403,
        )
    with _LOCK:
        existing = _TASKS.get(name)
        if existing and existing.get("status") == "running":
            raise LoginError(f"A login for '{name}' is already in progress.", 409)
        _TASKS[name] = {
            "name": name,
            "status": "running",
            "message": "Opening a browser window — log in there, then wait…",
            "started_at": _now(),
            "finished_at": None,
        }
        task = dict(_TASKS[name])

    run = runner or _browser_login

    def _worker() -> None:
        def on_progress(msg: str) -> None:
            _set_task(name, message=msg)

        try:
            result = run(spec, on_progress, timeout)
            ok = bool(result.get("ok"))
            _set_task(
                name,
                status="succeeded" if ok else "failed",
                message=result.get("message", "done" if ok else "login failed"),
                state_path=result.get("state_path"),
                finished_at=_now(),
            )
        except Exception as exc:  # noqa: BLE001 — surface any failure as task state
            _set_task(
                name,
                status="failed",
                message=f"{type(exc).__name__}: {exc}",
                finished_at=_now(),
            )

    threading.Thread(target=_worker, name=f"login-{name}", daemon=True).start()
    return task


# ── the real headed-browser login (not unit-tested) ───────────────────────────

def _browser_login(  # pragma: no cover - drives a real browser
    spec: SessionSpec, on_progress: Callable[[str], None], timeout: int
) -> dict:
    """Open a headed Chromium at the login page, poll until the site reports an
    authenticated session, then persist the storage_state.

    Uses a persistent ``user_data_dir`` profile (see ``open_login_context``) so
    the browser keeps Cloudflare's clearance cookie between logins and stops
    getting stuck on the Turnstile "verify you are human" challenge. Downstream
    scrapers still read the exported ``storage_state`` JSON — unchanged."""
    from playwright.sync_api import sync_playwright

    path = state_path(spec)
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    udd = profile_dir(spec.name, path)

    with sync_playwright() as pw:
        ctx = open_login_context(pw, user_data_dir=udd, on_progress=on_progress)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(spec.login_url, wait_until="domcontentloaded", timeout=30000)
            on_progress(f"Log in to {spec.label} in the browser window…")

            deadline = time.time() + max(30, timeout)
            authed = False
            while time.time() < deadline:
                try:
                    if spec.is_logged_in(page):
                        authed = True
                        break
                except Exception:  # noqa: BLE001 — page may be mid-navigation
                    pass
                page.wait_for_timeout(1000)

            if not authed:
                return {
                    "ok": False,
                    "message": (
                        "Timed out waiting for a successful login. Try again and "
                        "complete any captcha / OTP before the window closes."
                    ),
                }

            # Settle briefly so post-login cookies are written before we snapshot.
            page.wait_for_timeout(1500)
            ctx.storage_state(path=str(out))
            return {
                "ok": True,
                "message": f"Session saved to {out}",
                "state_path": str(out),
            }
        finally:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass
