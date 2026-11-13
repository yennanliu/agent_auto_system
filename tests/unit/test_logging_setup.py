"""Structured JSON logging + run/request correlation fields (src/logging_setup.py)."""
import asyncio
import json
import logging
import sys

import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel import Session

from src import logging_setup as ls
from src.models import Job, Run

_UVICORN = ("uvicorn", "uvicorn.error", "uvicorn.access")


class _Capture(logging.Handler):
    """Collects records as parsed JSON, through the real filter + formatter."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.setFormatter(ls.JsonFormatter())
        self.addFilter(ls.ContextFilter())
        self.lines: list[dict] = []

    def emit(self, record):
        self.lines.append(json.loads(self.format(record)))


@pytest.fixture
def capture():
    """JSON capture attached to the root logger (as the real handler is)."""
    root = logging.getLogger()
    handler = _Capture()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    yield handler
    root.removeHandler(handler)
    root.setLevel(old_level)


@pytest.fixture
def restore_logging():
    """Snapshot/restore global logging state mutated by setup_logging()."""
    names = (None, *_UVICORN)
    saved = {
        n: (lg.handlers[:], lg.level, lg.propagate)
        for n in names
        for lg in [logging.getLogger(n)]
    }
    yield
    for n, (handlers, level, propagate) in saved.items():
        lg = logging.getLogger(n)
        lg.handlers[:] = handlers
        lg.setLevel(level)
        lg.propagate = propagate


def _record(msg="hello %s", args=("world",), name="src.test", level=logging.INFO, **extra):
    rec = logging.LogRecord(name, level, __file__, 1, msg, args, None)
    rec.__dict__.update(extra)
    return rec


# ── formatter ────────────────────────────────────────────────────────────────

def test_formatter_emits_single_line_json_with_expected_keys():
    with ls.log_context(run_id=7, job_id=3, job_type="hacker_news_digest", user_id=2,
                        request_id="req-1"):
        rec = _record()
        ls.ContextFilter().filter(rec)
    line = ls.JsonFormatter().format(rec)

    assert "\n" not in line
    out = json.loads(line)
    assert out["message"] == "hello world"
    assert out["level"] == "INFO"
    assert out["logger"] == "src.test"
    assert out["ts"].endswith("+00:00")
    assert (out["run_id"], out["job_id"], out["job_type"], out["user_id"], out["request_id"]) == (
        7, 3, "hacker_news_digest", 2, "req-1")


def test_formatter_omits_unbound_context_fields_and_keeps_unicode():
    rec = _record("利潤健檢 done", ())
    ls.ContextFilter().filter(rec)
    line = ls.JsonFormatter().format(rec)
    out = json.loads(line)
    assert "利潤健檢" in line
    assert set(out) == {"ts", "level", "logger", "message"}


def test_formatter_includes_traceback():
    try:
        raise ValueError("boom")
    except ValueError:
        rec = logging.LogRecord("src.test", logging.ERROR, __file__, 1, "failed", (),
                                sys.exc_info())
    out = json.loads(ls.JsonFormatter().format(rec))
    assert "ValueError: boom" in out["exc_info"]


def test_formatter_redacts_sensitive_extras_and_survives_unserialisable_values():
    rec = _record(
        step="scrape", api_key="sk-live-123", payload={"password": "hunter2"},
        http={"headers": {"Authorization": "Bearer abc", "accept": "*/*"}}, obj=object(),
    )
    line = ls.JsonFormatter().format(rec)
    out = json.loads(line)
    assert out["step"] == "scrape"
    assert out["api_key"] == "[redacted]"
    assert out["payload"] == "[redacted]"
    assert out["http"]["headers"] == {"Authorization": "[redacted]", "accept": "*/*"}
    for secret in ("sk-live-123", "hunter2", "Bearer abc"):
        assert secret not in line


def test_access_log_drops_query_string():
    rec = _record(
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "GET", "/api/auth/oauth/google/callback?code=SECRET&state=x",
         "1.1", 302),
        name="uvicorn.access",
    )
    line = ls.JsonFormatter().format(rec)
    out = json.loads(line)
    assert "SECRET" not in line
    assert out["http_path"] == "/api/auth/oauth/google/callback"
    assert (out["http_method"], out["http_status"]) == ("GET", 302)


def test_only_whitelisted_fields_can_be_bound():
    with pytest.raises(ValueError):
        ls.bind_log_context(payload={"password": "x"})


def test_explicit_extra_wins_and_none_clears_inherited_field():
    with ls.log_context(run_id=1, user_id=9):
        rec = _record(run_id=42)
        ls.ContextFilter().filter(rec)
        assert rec.run_id == 42
        with ls.log_context(user_id=None):
            assert ls.get_log_context() == {"run_id": 1}
        assert ls.get_log_context() == {"run_id": 1, "user_id": 9}
    assert ls.get_log_context() == {}


# ── setup_logging ────────────────────────────────────────────────────────────

def test_text_mode_leaves_logging_untouched(restore_logging, monkeypatch):
    monkeypatch.delenv("LOG_FORMAT", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    names = (None, *_UVICORN)

    def snapshot():
        return [
            (lg.handlers[:], lg.level, lg.propagate, [h.formatter for h in lg.handlers])
            for lg in map(logging.getLogger, names)
        ]

    before = snapshot()
    assert ls.setup_logging() == "text"
    assert snapshot() == before


def test_unknown_format_falls_back_to_text(restore_logging, monkeypatch):
    monkeypatch.setenv("LOG_FORMAT", "yaml")
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    handlers = logging.getLogger().handlers[:]
    assert ls.setup_logging() == "text"
    assert logging.getLogger().handlers == handlers


def test_json_mode_installs_one_handler_and_routes_uvicorn(restore_logging, monkeypatch, capsys):
    monkeypatch.setenv("LOG_FORMAT", "JSON")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    uv_access = logging.getLogger("uvicorn.access")
    uv_access.addHandler(logging.StreamHandler())
    uv_access.propagate = False

    assert ls.setup_logging() == "json"
    assert ls.setup_logging() == "json"  # idempotent — no duplicate handlers

    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, ls.JsonFormatter)
    assert root.level == logging.DEBUG
    assert uv_access.handlers == [] and uv_access.propagate is True

    with ls.log_context(run_id=5, job_type="web_scraper"):
        logging.getLogger("some.third.party.lib").info("from a library")
    out = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert out["message"] == "from a library"
    assert (out["run_id"], out["job_type"]) == (5, "web_scraper")


def test_invalid_level_defaults_to_info_in_json_mode(restore_logging):
    assert ls.setup_logging("json", "LOUD") == "json"
    assert logging.getLogger().level == logging.INFO


# ── run correlation ──────────────────────────────────────────────────────────

@pytest.fixture
def two_runs(test_engine):
    with Session(test_engine) as s:
        job = Job(name="t", job_type="hacker_news_digest", payload="{}")
        s.add(job)
        s.commit()
        s.refresh(job)
        runs = [Run(job_id=job.id, status="pending") for _ in range(2)]
        s.add_all(runs)
        s.commit()
        return [r.id for r in runs]


async def test_run_fields_present_and_isolated_between_concurrent_runs(
    test_engine, two_runs, mocker, capture
):
    """Two interleaved runs: each record carries its own run's ids — on the event
    loop, in an asyncio.to_thread worker, and from a 'third-party' logger."""
    mocker.patch("src.automation.executor.get_engine", return_value=test_engine)
    mocker.patch("src.automation.executor.append_log")
    lib_log = logging.getLogger("thirdparty.lib")

    async def fake_flow(run_id, job_type, payload, provider, model):
        lib_log.info("loop-before run=%d", run_id)
        await asyncio.sleep(0.01)  # yield so the two runs interleave
        await asyncio.to_thread(lib_log.info, "thread run=%d", run_id)
        await asyncio.sleep(0.01)
        lib_log.info("loop-after run=%d", run_id)
        return ({"digest": "Top HN stories today about AI and Rust", "stories": [1]},
                {}, {"served_model": "m", "models_attempted": 1, "fallback_used": False})

    mocker.patch("src.automation.executor._run_flow", new=fake_flow)
    from src.automation.executor import execute_run

    a, b = two_runs
    await asyncio.gather(
        execute_run(a, "hacker_news_digest", {"limit": 3, "max_retries": 0}),
        execute_run(b, "web_scraper", {"limit": 3, "max_retries": 0}),
    )

    lib_lines = [ln for ln in capture.lines if ln["logger"] == "thirdparty.lib"]
    assert len(lib_lines) == 6
    expected_type = {a: "hacker_news_digest", b: "web_scraper"}
    for ln in lib_lines:
        said = int(ln["message"].rsplit("=", 1)[1])
        assert ln["run_id"] == said
        assert ln["job_type"] == expected_type[said]

    # the executor's own records are tagged too, and nothing leaks to the caller
    started = [ln for ln in capture.lines if ln["message"].startswith("Starting run_id=")]
    assert sorted(ln["run_id"] for ln in started) == sorted(two_runs)
    assert ls.get_log_context() == {}


async def test_launch_run_hands_job_and_user_ids_to_the_task_without_leaking(
    test_engine, mocker
):
    mocker.patch("src.automation.launcher.get_engine", return_value=test_engine)
    seen = {}

    async def fake_background(run_id, job_type, payload):
        seen.update(ls.get_log_context())

    mocker.patch("src.automation.launcher._run_in_background", side_effect=fake_background)
    from src.automation.launcher import launch_run

    with ls.log_context(request_id="req-9", user_id=1):  # the triggering request
        run_id = launch_run(11, "web_scraper", {"api_key": "nope"}, user_id=4)
        assert ls.get_log_context() == {"request_id": "req-9", "user_id": 1}
    await asyncio.sleep(0)

    assert seen == {"request_id": "req-9", "user_id": 4, "run_id": run_id, "job_id": 11,
                    "job_type": "web_scraper"}


async def test_context_survives_a_real_crewai_flow_kickoff(capture):
    """The executor runs `flow.kickoff` via asyncio.to_thread; CrewAI then runs
    each flow method on its own loop/threads. The fields must still be there."""
    from crewai.flow.flow import Flow, listen, start

    flow_log = logging.getLogger("tests.flow")

    class _Flow(Flow):
        @start()
        def first(self):
            flow_log.info("in start")
            return 1

        @listen(first)
        def second(self, _):
            flow_log.info("in listen")
            return "done"

    with ls.log_context(run_id=77, job_type="hacker_news_digest"):
        await asyncio.to_thread(_Flow().kickoff)

    lines = [ln for ln in capture.lines if ln["logger"] == "tests.flow"]
    assert [ln["message"] for ln in lines] == ["in start", "in listen"]
    assert all(ln["run_id"] == 77 and ln["job_type"] == "hacker_news_digest" for ln in lines)


# ── request middleware ───────────────────────────────────────────────────────

@pytest.fixture
def ctx_app():
    from starlette.applications import Starlette
    from starlette.middleware.sessions import SessionMiddleware
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def login(request):
        request.session["user_id"] = 5
        return JSONResponse(ls.get_log_context())

    async def whoami(request):
        return JSONResponse(ls.get_log_context())

    app = Starlette(routes=[Route("/login", login), Route("/whoami", whoami)])
    app.add_middleware(ls.RequestContextMiddleware)  # inner, as in src.main
    app.add_middleware(SessionMiddleware, secret_key="test")
    return app


async def test_middleware_binds_request_id_and_session_user(ctx_app):
    async with AsyncClient(transport=ASGITransport(app=ctx_app), base_url="http://t") as c:
        anon = await c.get("/whoami")
        assert set(anon.json()) == {"request_id"}
        assert anon.headers["x-request-id"] == anon.json()["request_id"]

        await c.get("/login")
        authed = await c.get("/whoami", headers={"X-Request-ID": "trace-abc.123"})
        assert authed.json() == {"request_id": "trace-abc.123", "user_id": 5}
        assert authed.headers["x-request-id"] == "trace-abc.123"

        # a malformed incoming id is replaced, never echoed into logs/headers
        bad = await c.get("/whoami", headers={"X-Request-ID": 'x" injected=1'})
        assert bad.json()["request_id"] != 'x" injected=1'
        assert len(bad.json()["request_id"]) == 32
    assert ls.get_log_context() == {}


async def test_app_responses_carry_request_id(anon_client):
    resp = await anon_client.get("/health")
    assert resp.status_code == 200
    assert len(resp.headers["x-request-id"]) == 32
