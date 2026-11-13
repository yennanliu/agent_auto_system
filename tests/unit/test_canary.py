"""Scraper drift canaries (E7): runner, classification and alert transitions.

No live site is touched — pages, HTTP and the webhook are all fakes.
"""
import ast
import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.automation import browser_session as bs
from src.automation import canary, spec
from src.automation.canary import (
    ERROR,
    FAILED,
    OK,
    SESSION_EXPIRED,
    SKIPPED_NO_SESSION,
    LivePage,
    Outcome,
    decide_alert,
)
from src.automation.spec import Canary

ROOT = Path(__file__).resolve().parents[2]


# ── fakes ─────────────────────────────────────────────────────────────────────

class FakeLocator:
    def __init__(self, n, visible=True):
        self._n, self._visible = n, visible

    def count(self):
        return self._n

    def is_visible(self):
        return self._visible and self._n > 0

    @property
    def first(self):
        return self


class FakePage:
    """Selector → element count; counts may only appear after N waits."""

    def __init__(self, counts, url="https://site.test/list", appear_after=0):
        self.counts, self.url, self.appear_after, self.waits = counts, url, appear_after, 0

    def locator(self, selector):
        ready = self.waits >= self.appear_after
        return FakeLocator(self.counts.get(selector, 0) if ready else 0)


def live(page, blocked=()):
    def wait(_ms):
        page.waits += 1
    return LivePage(canary.ReadOnlyPage(page), wait, list(blocked))


def opener(page, blocked=(), seen=None):
    @contextmanager
    def _open(c, url, state_path):
        if seen is not None:
            seen.append((url, state_path))
        page.url = url
        yield live(page, blocked)
    return _open


BROWSER = Canary(kind="browser", url="https://site.test/list", expect=("a.card", "button.go"), wait_ms=1000)


def session_spec(logged_in):
    return bs.SessionSpec(name="fake", label="Fake Site", state_env="FAKE_STATE",
                          default_state_path="data/fake.json", login_url="https://site.test/login",
                          is_logged_in=lambda _p: logged_in)


# ── classification ────────────────────────────────────────────────────────────

def test_page_with_all_selectors_is_ok():
    out = canary.evaluate_page(BROWSER, live(FakePage({"a.card": 12, "button.go": 1})))
    assert out.status == OK


def test_missing_selector_is_drift_and_names_it():
    out = canary.evaluate_page(BROWSER, live(FakePage({"a.card": 12})))
    assert out.status == FAILED
    assert "button.go" in out.detail and "a.card" not in out.detail


def test_waits_for_client_side_render():
    page = FakePage({"a.card": 3, "button.go": 1}, appear_after=2)
    assert canary.evaluate_page(BROWSER, live(page)).status == OK
    assert page.waits == 2


def test_blocked_requests_are_reported_with_a_failure():
    out = canary.evaluate_page(BROWSER, live(FakePage({}), blocked=["POST api.site.test"]))
    assert out.status == FAILED and "POST api.site.test" in out.detail


def test_logged_out_page_is_session_expired_not_drift():
    # Selectors are missing too — but the cause is the login wall, so no drift page.
    out = canary.evaluate_page(BROWSER, live(FakePage({})), session_spec(logged_in=False))
    assert out.status == SESSION_EXPIRED
    assert "not site drift" in out.detail


def test_logged_in_page_with_missing_selectors_is_drift():
    out = canary.evaluate_page(BROWSER, live(FakePage({})), session_spec(logged_in=True))
    assert out.status == FAILED


def test_real_login_heuristics_work_through_the_read_only_page():
    """browser_session's is_logged_in checks only need url/count/is_visible."""
    page = FakePage({':text("登出")': 1}, url="https://www.tasker.com.tw/cases")
    assert bs.get_spec("tasker").is_logged_in(canary.ReadOnlyPage(page)) is True
    walled = FakePage({}, url="https://www.linkedin.com/authwall?x=1")
    assert bs.get_spec("linkedin").is_logged_in(canary.ReadOnlyPage(walled)) is False


# ── probe ─────────────────────────────────────────────────────────────────────

def test_no_saved_session_is_skipped_without_opening_a_browser(monkeypatch, tmp_path):
    monkeypatch.setenv("TASKER_STORAGE_STATE", str(tmp_path / "absent.json"))
    c = Canary(kind="browser", url="https://x.test/", expect=("a",), session="tasker")

    def boom(*_a):
        raise AssertionError("browser must not open")

    assert canary.probe(c, open_page=boom).status == SKIPPED_NO_SESSION


def test_saved_session_is_loaded_and_expiry_detected(monkeypatch, tmp_path):
    state = tmp_path / "linkedin.json"
    state.write_text("{}")
    monkeypatch.setenv("LINKEDIN_STORAGE_STATE", str(state))
    c = Canary(kind="browser", url="https://www.linkedin.com/login", expect=("a",),
               session="linkedin", wait_ms=0)
    seen = []
    out = canary.probe(c, open_page=opener(FakePage({}), seen=seen))
    assert seen == [("https://www.linkedin.com/login", str(state))]
    assert out.status == SESSION_EXPIRED


def test_http_probe_classification():
    c = Canary(kind="http", url="https://x.test/form", expect=("MARKER",))
    assert canary.probe(c, fetch=lambda _u: (200, "… MARKER …")).status == OK
    assert canary.probe(c, fetch=lambda _u: (200, "redesigned")).status == FAILED
    assert canary.probe(c, fetch=lambda _u: (404, "")).status == FAILED
    assert canary.probe(c, fetch=lambda _u: (503, "")).status == ERROR


def test_seed_is_substituted_into_the_url():
    c = Canary(kind="browser", seed_url="https://api.test/jobs", seed_path="jobs.0.id",
               url="https://x.test/apply?token={seed}", expect=("a",), wait_ms=0)
    seen = []
    out = canary.probe(c, fetch=lambda _u: (200, json.dumps({"jobs": [{"id": 77}]})),
                       open_page=opener(FakePage({"a": 1}), seen=seen))
    assert out.status == OK
    assert seen == [("https://x.test/apply?token=77", None)]


def test_empty_seed_is_an_error_not_drift():
    c = Canary(kind="browser", seed_url="https://api.test/jobs", seed_path="jobs.0.id",
               url="https://x.test/{seed}", expect=("a",))
    assert canary.probe(c, fetch=lambda _u: (200, '{"jobs": []}')).status == ERROR
    assert canary.probe(c, fetch=lambda _u: (200, "<html>")).status == FAILED


def test_probe_never_raises_and_retries_once():
    calls = []

    def flaky(_u):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("slow")
        return 200, "MARKER"

    c = Canary(kind="http", url="https://x.test/", expect=("MARKER",))
    assert canary.probe(c, fetch=flaky).status == OK
    assert len(calls) == 2

    def dead(_u):
        raise ConnectionError("no route")

    out = canary.probe(c, fetch=dead)
    assert out.status == ERROR and "ConnectionError" in out.detail


# ── read-only guarantees ──────────────────────────────────────────────────────

class FakeRoute:
    def __init__(self, method, url="https://www.site.test/apply"):
        self.request = type("Req", (), {"method": method, "url": url})()
        self.action = None

    def continue_(self):
        self.action = "continue"

    def abort(self):
        self.action = "abort"


@pytest.mark.parametrize("method,action", [
    ("GET", "continue"), ("HEAD", "continue"), ("OPTIONS", "continue"),
    ("POST", "abort"), ("PUT", "abort"), ("PATCH", "abort"), ("DELETE", "abort"),
])
def test_network_guard_aborts_every_write(method, action):
    blocked = []
    route = FakeRoute(method)
    canary.guard_route(route, blocked)
    assert route.action == action
    assert blocked == ([] if action == "continue" else [f"{method} www.site.test"])


def test_read_only_page_exposes_no_way_to_act():
    page = canary.ReadOnlyPage(FakePage({"a": 1}))
    for surface in (page, page.locator("a"), page.locator("a").first):
        for verb in ("click", "fill", "press", "type", "evaluate", "goto", "check",
                     "set_input_files", "select_option", "keyboard", "mouse"):
            assert not hasattr(surface, verb), verb


def test_canary_module_imports_no_acting_code():
    """The runner must not be able to reach an apply/submit/send code path."""
    tree = ast.parse((ROOT / "src/automation/canary.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(f"{node.module}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    forbidden = ("src.automation.tools", "src.automation.flows", "src.automation.crews",
                 "src.automation.launcher", "src.automation.executor", "src.automation.pipeline")
    assert not [m for m in imported if m.startswith(forbidden)]


def test_canary_declarations_are_pure_data():
    for jt, c in spec.canaries().items():
        assert not [v for v in vars(c).values() if callable(v)], jt


# ── registry coverage ─────────────────────────────────────────────────────────

def test_every_browser_automation_declares_a_canary_or_a_reason():
    for jt, s in spec.REGISTRY.items():
        if s.browser:
            assert s.canary or s.canary_skip, f"{jt}: add a canary or a canary_skip reason"


def test_canary_sessions_exist():
    for jt, c in spec.canaries().items():
        if c.session:
            assert bs.get_spec(c.session) is not None, jt


def test_browser_selectors_mirror_the_tool_source():
    """A canary is only useful if it probes what the tool actually relies on."""
    def norm(text):
        return text.replace("\\", "").replace('"', "'")

    for jt, c in spec.canaries().items():
        assert c.source, jt
        source = norm((ROOT / (c.source.replace(".", "/") + ".py")).read_text(encoding="utf-8"))
        for marker in c.expect:
            parts = [marker] if norm(marker) in source else marker.split(", ")
            assert all(norm(p) in source for p in parts), f"{jt}: {marker!r} not in {c.source}"


def test_invalid_canary_is_reported_by_validate_registry(monkeypatch):
    bad = spec.AutomationSpec(
        job_type="bad", name="Bad", icon="x", rubric="r", validate=lambda r: (True, ""),
        canary=Canary(kind="carrier-pigeon", url="http://x.test/{seed}", expect=()),
        canary_skip="also skipped",
    )
    monkeypatch.setitem(spec.REGISTRY, "bad", bad)
    problems = [p for p in spec.validate_registry() if p.startswith("bad:")]
    assert len(problems) == 5


# ── alert transitions ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("history,new,expected", [
    ([], OK, None),
    ([OK, OK], FAILED, "failing"),                       # the flip
    ([], FAILED, "failing"),                             # never worked
    ([OK, FAILED], FAILED, None),                        # still failing → no repeat
    ([OK, FAILED, FAILED], OK, "recovered"),
    ([OK, FAILED, OK], FAILED, "failing"),               # a second flip pages again
    ([OK, OK], SESSION_EXPIRED, None),                   # never drift
    ([OK, OK], SKIPPED_NO_SESSION, None),
    ([OK, SESSION_EXPIRED, SESSION_EXPIRED], FAILED, "failing"),
    ([FAILED, SESSION_EXPIRED], FAILED, None),           # expiry doesn't reset the state
    ([FAILED, SESSION_EXPIRED], OK, "recovered"),
    ([OK, OK, OK], ERROR, None),                         # one flaky night
    ([OK, OK, OK, ERROR], ERROR, None),                  # 2/5
    ([OK, OK, OK, ERROR, ERROR], ERROR, "failure_rate"),  # 3/6 → rate jumped
    ([OK, OK, OK, ERROR, ERROR, ERROR], ERROR, None),    # already alerting
    ([ERROR, ERROR], ERROR, "failure_rate"),             # min samples reached
    ([ERROR], ERROR, None),
    ([OK, OK, OK, ERROR, ERROR, ERROR], OK, "recovered"),
    ([OK, ERROR], OK, None),                             # nothing to recover from
])
def test_decide_alert(history, new, expected):
    assert decide_alert(history, new) == expected


# ── recording + runner ────────────────────────────────────────────────────────

def _scripted(outcomes):
    it = iter(outcomes)
    return lambda _c: next(it)


def test_run_all_records_and_alerts_only_on_transitions(test_engine, mocker, monkeypatch):
    mocker.patch("src.settings_store.is_automation_enabled", return_value=True)
    sent = mocker.patch("src.automation.canary.send_alerts")
    one = {"tw104_apply": spec.canaries()["tw104_apply"]}
    monkeypatch.setattr(spec, "canaries", lambda: one)

    nights = [Outcome(OK, "fine"), Outcome(FAILED, "cards gone"), Outcome(FAILED, "cards gone"),
              Outcome(SESSION_EXPIRED, "login"), Outcome(OK, "fine")]
    alerts = []
    for outcome in nights:
        res = canary.run_all(engine=test_engine, probe_fn=_scripted([outcome]))
        alerts.append(res[0]["alert"])
        assert [a["alert"] for a in sent.call_args.args[0]] == [a for a in [res[0]["alert"]] if a]
    assert alerts == [None, "failing", None, None, "recovered"]

    from sqlmodel import Session
    with Session(test_engine) as s:
        report = canary.status_report(s)
    entry = next(a for a in report["automations"] if a["job_type"] == "tw104_apply")
    assert entry["latest"]["status"] == OK and entry["alerting"] is False
    assert entry["recent"] == [OK, FAILED, FAILED, OK]
    assert entry["last_ok_at"]


def test_run_all_skips_disabled_unless_asked(test_engine, mocker):
    mocker.patch("src.settings_store.is_automation_enabled", return_value=False)
    mocker.patch("src.automation.canary.send_alerts")
    ok = lambda _c: Outcome(OK)  # noqa: E731
    assert canary.run_all(engine=test_engine, probe_fn=ok) == []
    assert len(canary.run_all(engine=test_engine, probe_fn=ok, include_disabled=True)) == len(spec.canaries())
    only = canary.run_all(["greenhouse_apply"], engine=test_engine, probe_fn=ok)
    assert [r["job_type"] for r in only] == ["greenhouse_apply"]


def test_history_is_pruned(db_session, monkeypatch):
    monkeypatch.setattr(canary, "_KEEP", 3)
    for _ in range(6):
        canary.record(db_session, "x", Outcome(OK))
    canary.record(db_session, "other", Outcome(OK))
    from sqlmodel import select

    from src.models import CanaryResult
    rows = db_session.exec(select(CanaryResult)).all()
    assert sorted(r.job_type for r in rows) == ["other", "x", "x", "x"]


# ── alert delivery ────────────────────────────────────────────────────────────

ALERT = {"job_type": "tw104_apply", "status": FAILED, "detail": "cards gone", "alert": "failing"}


def test_alert_logs_and_posts_to_webhook(monkeypatch, caplog):
    monkeypatch.setenv("CANARY_ALERT_WEBHOOK", "https://hooks.test/abc")
    posted = []
    with caplog.at_level("ERROR"):
        ok = canary.send_alerts([ALERT], post=lambda url, **kw: posted.append((url, kw)))
    assert ok is True
    assert "CANARY ALERT [failing] tw104_apply" in caplog.text
    url, kw = posted[0]
    assert url == "https://hooks.test/abc"
    assert kw["json"]["alerts"] == [ALERT] and "tw104_apply" in kw["json"]["text"]


def test_alert_without_webhook_only_logs(monkeypatch, caplog):
    monkeypatch.delenv("CANARY_ALERT_WEBHOOK", raising=False)

    def boom(*_a, **_k):
        raise AssertionError("no webhook configured")

    with caplog.at_level("ERROR"):
        assert canary.send_alerts([ALERT], post=boom) is False
    assert "CANARY ALERT" in caplog.text


def test_webhook_failure_never_raises(monkeypatch):
    monkeypatch.setenv("CANARY_ALERT_WEBHOOK", "https://hooks.test/abc")

    def boom(*_a, **_k):
        raise ConnectionError("down")

    assert canary.send_alerts([ALERT], post=boom) is False
    assert canary.send_alerts([], post=boom) is False


# ── opt-in nightly trigger ────────────────────────────────────────────────────

T0 = datetime(2026, 1, 1, 10, 30, tzinfo=UTC)


def test_schedule_is_off_by_default(monkeypatch):
    monkeypatch.delenv("CANARY_ENABLED", raising=False)
    sch = canary.CanarySchedule()
    assert sch.due(T0) is False and sch.due(T0 + timedelta(days=2)) is False
    assert sch.next_due is None


def test_schedule_anchors_then_fires_once_per_slot(monkeypatch):
    monkeypatch.setenv("CANARY_ENABLED", "1")
    monkeypatch.delenv("CANARY_SCHEDULE", raising=False)
    sch = canary.CanarySchedule()
    assert sch.due(T0) is False                                  # anchors to 03:00 tomorrow
    assert sch.next_due == datetime(2026, 1, 2, 3, 0, tzinfo=UTC)
    assert sch.due(T0 + timedelta(hours=10)) is False
    fire = datetime(2026, 1, 2, 3, 0, 20, tzinfo=UTC)
    assert sch.due(fire) is True
    assert sch.due(fire + timedelta(seconds=30)) is False


def test_invalid_schedule_never_fires(monkeypatch):
    monkeypatch.setenv("CANARY_ENABLED", "1")
    monkeypatch.setenv("CANARY_SCHEDULE", "not a cron")
    sch = canary.CanarySchedule()
    assert sch.due(T0) is False and sch.due(T0 + timedelta(days=2)) is False


async def test_scheduler_tick_launches_canaries_when_due(test_engine, mocker, monkeypatch):
    from src.automation.scheduler import CronScheduler

    mocker.patch("src.automation.scheduler.get_engine", return_value=test_engine)
    ran = mocker.patch("src.automation.canary.run_all_safe")
    monkeypatch.setattr(canary, "schedule", canary.CanarySchedule())
    monkeypatch.setenv("CANARY_SCHEDULE", "* * * * *")

    sch = CronScheduler()
    monkeypatch.delenv("CANARY_ENABLED", raising=False)
    await sch.tick(now=T0)
    await sch.tick(now=T0 + timedelta(minutes=2))
    assert sch._canary_task is None and not ran.called           # opt-in: off by default

    monkeypatch.setenv("CANARY_ENABLED", "1")
    await sch.tick(now=T0)                                       # anchors
    assert sch._canary_task is None
    await sch.tick(now=T0 + timedelta(minutes=2))
    await sch._canary_task
    ran.assert_called_once()
