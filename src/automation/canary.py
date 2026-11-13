"""Scraper drift canaries (E7): cheap, read-only probes of the sites we automate.

DOM changes on LinkedIn / Greenhouse / 104 / Shopee are the top failure source,
and without this the first person to notice is a user whose run came back empty.
Each automation declares its probe once, as an ``AutomationSpec.canary`` in
``spec.py``; this module runs them, records the outcome, and raises an alert when
an automation flips to failing.

Read-only by construction, not by flag
--------------------------------------
A canary must never submit an application or send anything. That is not left to
a ``dry_run`` default:

* ``spec.Canary`` is pure data (a URL + selectors) — it has no callable, so it
  cannot point at an apply / submit / send code path, and this module imports no
  tool, flow, launcher or executor (``test_canary.py`` enforces that).
* The browser context aborts every request that is not GET/HEAD/OPTIONS
  (``guard_route``), so even page script cannot POST a form.
* Probe logic only sees a ``ReadOnlyPage``: ``url`` plus ``locator().count()`` /
  ``is_visible()``. There is no click, fill, press or evaluate to call.

Outcomes
--------
``ok`` · ``failed`` (page loaded, expected selectors/markers gone → the site
changed) · ``session_expired`` (the saved login no longer authenticates — a
re-login chore, never paged as drift) · ``skipped_no_session`` (no saved login on
this machine) · ``error`` (the probe itself could not run: network, no browser).

Alerts go to the log and, when ``CANARY_ALERT_WEBHOOK`` is set, to that URL as a
JSON POST. There is no notification system in the app yet — this is deliberately
the minimum.
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from urllib.parse import urlsplit

from sqlmodel import Session, col, delete, select

from src.automation import browser_session as bs
from src.automation import spec as spec_mod
from src.automation.cron_utils import is_valid_cron, next_fire
from src.automation.spec import Canary
from src.models import CanaryResult

logger = logging.getLogger(__name__)

OK = "ok"
FAILED = "failed"
SESSION_EXPIRED = "session_expired"
SKIPPED_NO_SESSION = "skipped_no_session"
ERROR = "error"

# Only these say something about the site itself; the rest (no session, expired
# session) are operator chores and never move the alert state.
_CONCLUSIVE = frozenset({OK, FAILED, ERROR})

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

WINDOW = 7          # probes considered for the failure rate (a week of nightlies)
THRESHOLD = 0.5     # share of failed/error probes in the window that raises an alert
MIN_SAMPLES = 3     # ...once at least this many probes are in the window
_HISTORY = 60       # conclusive rows replayed to derive the alert state
_KEEP = 200         # rows kept per automation
_DEFAULT_SCHEDULE = "0 3 * * *"  # UTC, like job schedules

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class Outcome:
    status: str
    detail: str = ""
    duration_ms: int = 0


# ── read-only browser surface ─────────────────────────────────────────────────

class ReadOnlyLocator:
    """The only element operations a probe may perform: count and visibility."""

    __slots__ = ("_loc",)

    def __init__(self, loc) -> None:
        self._loc = loc

    def count(self) -> int:
        return self._loc.count()

    def is_visible(self) -> bool:
        return self._loc.is_visible()

    @property
    def first(self) -> ReadOnlyLocator:
        return ReadOnlyLocator(self._loc.first)


class ReadOnlyPage:
    """A Playwright page narrowed to observation. Enough for the selector checks
    and for ``browser_session``'s ``is_logged_in`` heuristics; nothing that acts."""

    __slots__ = ("_page",)

    def __init__(self, page) -> None:
        self._page = page

    @property
    def url(self) -> str:
        return self._page.url

    def locator(self, selector: str) -> ReadOnlyLocator:
        return ReadOnlyLocator(self._page.locator(selector))


@dataclass
class LivePage:
    """What ``open_page`` yields: the loaded page, a way to wait, and the list of
    requests the network guard refused."""

    page: ReadOnlyPage
    wait: Callable[[int], None]
    blocked: list[str] = field(default_factory=list)


def guard_route(route, blocked: list[str]) -> None:
    """Network-level write block: let reads through, abort everything else."""
    request = route.request
    method = (request.method or "").upper()
    if method in SAFE_METHODS:
        route.continue_()
        return
    blocked.append(f"{method} {urlsplit(request.url).netloc}")
    route.abort()


@contextmanager
def _open_page(canary: Canary, url: str, state_path: str | None):  # pragma: no cover - real browser
    from playwright.sync_api import sync_playwright

    headless = not canary.headed or os.getenv("CANARY_HEADLESS") == "1"
    blocked: list[str] = []
    with sync_playwright() as pw:
        args = ["--disable-blink-features=AutomationControlled"]
        if headless:
            browser = pw.chromium.launch(headless=True, args=args)
        else:
            # Same preference as the headed tools: real Chrome passes anti-bot
            # checks far more reliably than bundled Chromium.
            try:
                browser = pw.chromium.launch(channel="chrome", headless=False, args=args,
                                             ignore_default_args=["--enable-automation"])
            except Exception:  # noqa: BLE001 — Chrome not installed / not launchable
                browser = pw.chromium.launch(headless=False, args=args)
        try:
            options: dict = {"viewport": {"width": 1366, "height": 900}, "locale": "zh-TW",
                             "service_workers": "block"}  # workers would bypass routing
            if state_path:
                options["storage_state"] = state_path
            if headless:
                options["user_agent"] = _UA
            ctx = browser.new_context(**options)
            ctx.route("**/*", lambda route: guard_route(route, blocked))
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            yield LivePage(ReadOnlyPage(page), page.wait_for_timeout, blocked)
        finally:
            browser.close()


def _http_get(url: str) -> tuple[int, str]:  # pragma: no cover - real network
    import httpx

    resp = httpx.get(url, timeout=20, follow_redirects=True, headers={"User-Agent": _UA})
    return resp.status_code, resp.text


Fetch = Callable[[str], tuple[int, str]]
OpenPage = Callable[[Canary, str, "str | None"], AbstractContextManager[LivePage]]


# ── classification ────────────────────────────────────────────────────────────

def _missing(page: ReadOnlyPage, selectors: Iterable[str]) -> list[str]:
    out = []
    for sel in selectors:
        try:
            if page.locator(sel).count() == 0:
                out.append(sel)
        except Exception:  # noqa: BLE001 — mid-navigation; treat as not there yet
            out.append(sel)
    return out


def evaluate_page(canary: Canary, live: LivePage, session_spec: bs.SessionSpec | None = None) -> Outcome:
    """Classify a loaded listing page. Login state is judged before selectors so
    an expired session is reported as such instead of as site drift."""
    waited = 0
    while True:
        missing = _missing(live.page, canary.expect)
        if not missing or waited >= canary.wait_ms:
            break
        live.wait(500)
        waited += 500

    if session_spec is not None:
        try:
            logged_in = bool(session_spec.is_logged_in(live.page))
        except Exception:  # noqa: BLE001 — can't tell; let the selectors decide
            logged_in = True
        if not logged_in:
            age = (bs.session_status(session_spec.name) or {}).get("age_seconds")
            aged = f" (saved {age / 86400:.1f} days ago)" if age is not None else ""
            return Outcome(
                SESSION_EXPIRED,
                f"{session_spec.label} session{aged} is no longer logged in — refresh it "
                "(Admin → Sessions); this is not site drift.",
            )

    if missing:
        detail = f"no element matched {missing} at {live.page.url}"
        if live.blocked:
            hosts = sorted(set(live.blocked))
            detail += f"; read-only guard blocked {len(live.blocked)} request(s): {hosts[:5]}"
        return Outcome(FAILED, detail)
    return Outcome(OK, f"{len(canary.expect)} selector(s) matched")


def _dig(data, path: str):
    for part in path.split("."):
        try:
            data = data[int(part)] if isinstance(data, list) else data[part]
        except (KeyError, IndexError, ValueError, TypeError):
            return None
    return data


def _bad_status(what: str, code: int) -> Outcome | None:
    if code >= 500:
        return Outcome(ERROR, f"{what} returned HTTP {code}")
    if code != 200:
        return Outcome(FAILED, f"{what} returned HTTP {code}")
    return None


def _probe(canary: Canary, fetch: Fetch, open_page: OpenPage) -> Outcome:
    url = canary.url
    if canary.seed_url:
        code, body = fetch(canary.seed_url)
        if bad := _bad_status("seed endpoint", code):
            return bad
        try:
            seed = _dig(json.loads(body), canary.seed_path)
        except ValueError:
            return Outcome(FAILED, "seed endpoint no longer returns JSON")
        if seed in (None, ""):
            return Outcome(ERROR, f"seed endpoint had nothing at {canary.seed_path!r} to probe with")
        url = url.replace("{seed}", str(seed))

    if canary.kind == "http":
        code, body = fetch(url)
        if bad := _bad_status(url, code):
            return bad
        absent = [m for m in canary.expect if m not in body]
        if absent:
            return Outcome(FAILED, f"response no longer contains {absent}")
        return Outcome(OK, f"{len(canary.expect)} marker(s) present")

    session_spec = None
    state_path = None
    if canary.session:
        session_spec = bs.get_spec(canary.session)
        status = bs.session_status(canary.session)
        if session_spec is None or status is None:
            return Outcome(ERROR, f"unknown browser session {canary.session!r}")
        if not status["exists"]:
            return Outcome(SKIPPED_NO_SESSION,
                           f"no saved {session_spec.label} session at {status['state_path']}")
        state_path = status["state_path"]
    with open_page(canary, url, state_path) as live:
        return evaluate_page(canary, live, session_spec)


def probe(canary: Canary, *, fetch: Fetch | None = None, open_page: OpenPage | None = None,
          attempts: int = 2) -> Outcome:
    """Run one canary. Never raises: anything unexpected is an ``error`` outcome.
    A failed/error probe is retried once so a single throttled page load doesn't
    flip the automation to failing."""
    start = time.monotonic()
    outcome = Outcome(ERROR, "probe did not run")
    for _ in range(max(1, attempts)):
        try:
            outcome = _probe(canary, fetch or _http_get, open_page or _open_page)
        except Exception as exc:  # noqa: BLE001 — timeouts, no browser, network
            outcome = Outcome(ERROR, f"{type(exc).__name__}: {exc}"[:500])
        if outcome.status not in (FAILED, ERROR):
            break
    return replace(outcome, duration_ms=int((time.monotonic() - start) * 1000))


# ── alert transitions ─────────────────────────────────────────────────────────

def decide_alert(history: Sequence[str], new: str, *, window: int = WINDOW,
                 threshold: float = THRESHOLD, min_samples: int = MIN_SAMPLES) -> str | None:
    """The alert (if any) that recording ``new`` after ``history`` should raise.

    ``history`` is prior statuses, oldest first. Replays them as a small state
    machine so an automation alerts once when it starts failing, not every night:

    * ``failing``      — a ``failed`` probe while not already alerting (the flip).
    * ``failure_rate`` — ``error`` probes pushed the share of bad probes in the
      last ``window`` to ``threshold`` or more (with at least ``min_samples``).
    * ``recovered``    — an ``ok`` probe while alerting.

    ``session_expired`` / ``skipped_no_session`` are ignored entirely.
    """
    alerting = False
    recent: deque[str] = deque(maxlen=window)
    event = None
    for status in (*history, new):
        event = None
        if status not in _CONCLUSIVE:
            continue
        recent.append(status)
        if status == OK:
            event = "recovered" if alerting else None
            alerting = False
        elif not alerting:
            bad = sum(1 for s in recent if s != OK)
            if status == FAILED:
                event, alerting = "failing", True
            elif len(recent) >= min_samples and bad / len(recent) >= threshold:
                event, alerting = "failure_rate", True
    return event


def _history(session: Session, job_type: str, limit: int = _HISTORY) -> list[CanaryResult]:
    """Most recent conclusive rows for one automation, oldest first."""
    rows = session.exec(
        select(CanaryResult)
        .where(CanaryResult.job_type == job_type, col(CanaryResult.status).in_(_CONCLUSIVE))
        .order_by(col(CanaryResult.id).desc())
        .limit(limit)
    ).all()
    return list(reversed(rows))


def record(session: Session, job_type: str, outcome: Outcome) -> CanaryResult:
    """Persist one outcome, stamping the alert it raises (if any)."""
    event = decide_alert([r.status for r in _history(session, job_type)], outcome.status)
    row = CanaryResult(job_type=job_type, status=outcome.status, detail=outcome.detail,
                       duration_ms=outcome.duration_ms, alert=event)
    session.add(row)
    session.commit()
    session.refresh(row)
    # Bounded history: one row per automation per night needs no retention job.
    cutoff = session.exec(
        select(CanaryResult.id).where(CanaryResult.job_type == job_type)
        .order_by(col(CanaryResult.id).desc()).offset(_KEEP).limit(1)
    ).first()
    if cutoff is not None:
        session.exec(delete(CanaryResult).where(
            col(CanaryResult.job_type) == job_type, col(CanaryResult.id) <= cutoff))
        session.commit()
    return row


def send_alerts(alerts: list[dict], post: Callable[..., object] | None = None) -> bool:
    """Log each alert and POST them to ``CANARY_ALERT_WEBHOOK`` when configured.
    Returns whether a webhook delivery succeeded. Never raises."""
    if not alerts:
        return False
    lines = []
    for a in alerts:
        line = f"[{a['alert']}] {a['job_type']}: {a['status']} — {a['detail']}"
        lines.append(line)
        (logger.warning if a["alert"] == "recovered" else logger.error)("CANARY ALERT %s", line)
    url = os.getenv("CANARY_ALERT_WEBHOOK", "").strip()
    if not url:
        return False
    try:
        if post is None:
            import httpx
            post = httpx.post
        # "text" makes the payload readable by Slack-style incoming webhooks.
        post(url, json={"event": "canary_alert", "text": "Scraper canary alert\n" + "\n".join(lines),
                        "alerts": alerts}, timeout=10)
        return True
    except Exception as exc:  # noqa: BLE001 — alert delivery must not break the run
        logger.error("Canary alert webhook failed: %s: %s", type(exc).__name__, exc)
        return False


# ── runner ────────────────────────────────────────────────────────────────────

def run_all(only: Iterable[str] | None = None, *, engine=None, notify: bool = True,
            include_disabled: bool = False,
            probe_fn: Callable[[Canary], Outcome] = probe) -> list[dict]:
    """Probe every declared canary, record the results and deliver alerts.

    Automations switched off in Admin → Automations are skipped unless named in
    ``only`` or ``include_disabled`` is set. Returns one dict per probe run.
    """
    from src.database import get_engine
    from src.settings_store import is_automation_enabled

    wanted = set(only) if only else None
    results: list[dict] = []
    with Session(engine or get_engine()) as session:
        for job_type, canary in spec_mod.canaries().items():
            if wanted is not None:
                if job_type not in wanted:
                    continue
            elif not include_disabled and not is_automation_enabled(job_type):
                continue
            outcome = probe_fn(canary)
            row = record(session, job_type, outcome)
            logger.info("canary %s: %s (%d ms) — %s", job_type, row.status, row.duration_ms, row.detail)
            results.append(_row_dict(row))
    if notify:
        send_alerts([r for r in results if r["alert"]])
    return results


def _row_dict(row: CanaryResult) -> dict:
    return {
        "job_type": row.job_type, "status": row.status, "detail": row.detail,
        "duration_ms": row.duration_ms, "alert": row.alert,
        "checked_at": row.checked_at.isoformat() if row.checked_at else None,
    }


def run_all_safe() -> None:
    """Scheduler entry point: a canary run must never take the scheduler down."""
    try:
        run_all()
    except Exception:
        logger.exception("Canary run failed")


# ── opt-in nightly trigger (driven by CronScheduler.tick) ─────────────────────

class CanarySchedule:
    """Due-detection for the in-process trigger. Off unless ``CANARY_ENABLED=1``;
    fires on ``CANARY_SCHEDULE`` (cron, UTC; default 03:00). Same anchoring as job
    schedules: first sight only anchors, so enabling it never fires immediately."""

    def __init__(self) -> None:
        self.next_due: datetime | None = None

    @staticmethod
    def enabled() -> bool:
        return os.getenv("CANARY_ENABLED", "0") == "1"

    @staticmethod
    def expr() -> str:
        return os.getenv("CANARY_SCHEDULE", "").strip() or _DEFAULT_SCHEDULE

    def due(self, now: datetime) -> bool:
        expr = self.expr()
        if not self.enabled() or not is_valid_cron(expr):
            self.next_due = None
            return False
        if self.next_due is None:
            self.next_due = next_fire(expr, now)
            return False
        if now >= self.next_due:
            self.next_due = next_fire(expr, now)
            return True
        return False


schedule = CanarySchedule()


# ── status (admin API) ────────────────────────────────────────────────────────

def status_report(session: Session) -> dict:
    """Latest canary status per browser automation, plus trigger/alert config."""
    spec_mod.ensure_plugins_loaded()
    automations = []
    for job_type, s in spec_mod.REGISTRY.items():
        if s.canary is None and not s.browser:
            continue
        entry: dict = {
            "job_type": job_type, "name": s.name,
            "canary": None, "skip_reason": s.canary_skip or None,
            "latest": None, "last_ok_at": None, "alerting": False, "recent": [],
        }
        if s.canary is not None:
            c = s.canary
            entry["canary"] = {"kind": c.kind, "url": c.url, "session": c.session or None,
                               "headed": c.headed}
            latest = session.exec(
                select(CanaryResult).where(CanaryResult.job_type == job_type)
                .order_by(col(CanaryResult.id).desc()).limit(1)
            ).first()
            history = _history(session, job_type)
            statuses = [r.status for r in history]
            last_ok = next((r for r in reversed(history) if r.status == OK), None)
            entry.update(
                latest=_row_dict(latest) if latest else None,
                last_ok_at=last_ok.checked_at.isoformat() if last_ok else None,
                alerting=_is_alerting(statuses),
                recent=statuses[-WINDOW:],
            )
        automations.append(entry)
    return {
        "enabled": schedule.enabled(),
        "schedule": schedule.expr(),
        "next_run_at": schedule.next_due.isoformat() if schedule.next_due else None,
        "webhook_configured": bool(os.getenv("CANARY_ALERT_WEBHOOK", "").strip()),
        "automations": automations,
    }


def _is_alerting(statuses: Sequence[str]) -> bool:
    """Whether the automation is currently in the alerting state: the most recent
    alert it raised (replaying its history) was not a recovery."""
    alerting = False
    for i, status in enumerate(statuses):
        event = decide_alert(statuses[:i], status)
        if event:
            alerting = event != "recovered"
    return alerting
