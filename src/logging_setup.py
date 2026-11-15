"""Structured logging: JSON output + per-run / per-request correlation fields.

Env:
  LOG_FORMAT=text|json   default ``text`` — logging is left exactly as uvicorn
                         configures it, so local dev output is unchanged.
  LOG_LEVEL=DEBUG|INFO|… default: untouched in text mode, ``INFO`` in json mode.

In json mode every record — ours, uvicorn's and third-party libraries' — is
written to stderr as one JSON object per line, carrying whichever of
``request_id`` / ``user_id`` / ``run_id`` / ``job_id`` / ``job_type`` are bound
in the current context.

Correlation uses a ``contextvars.ContextVar``, so it follows ``await``,
``asyncio.create_task`` and ``asyncio.to_thread`` (which is how the executor
runs flows) and CrewAI's own worker threads (it copies the context into them).
A thread started with a bare ``threading.Thread`` / ``loop.run_in_executor``
does NOT inherit it: records logged there are still JSON, just without the
correlation fields.

Secrets: only the fixed, non-sensitive ids in ``CONTEXT_FIELDS`` can be bound —
never payloads, API keys, passwords or cookies. ``extra=`` values under
sensitive-looking keys are redacted, and the query string is dropped from
access-log lines (it can carry OAuth codes). Message text itself is not
scrubbed: don't format secrets into log messages.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# The only fields that may be bound to the logging context (ids, never content).
CONTEXT_FIELDS = ("request_id", "user_id", "run_id", "job_id", "job_type")

_context: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("log_context")


def get_log_context() -> dict[str, Any]:
    """Return a copy of the correlation fields bound in the current context."""
    return dict(_context.get({}))


def bind_log_context(**fields: Any) -> contextvars.Token:
    """Bind correlation fields for the current task/thread context.

    A ``None`` value clears that field (so a run never keeps a stale inherited
    id). Returns the token to pass to ``_context.reset`` — prefer ``log_context``.
    """
    unknown = set(fields) - set(CONTEXT_FIELDS)
    if unknown:
        raise ValueError(f"Unknown log context field(s): {sorted(unknown)}")
    merged = {**_context.get({}), **fields}
    return _context.set({k: v for k, v in merged.items() if v is not None})


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Bind correlation fields for the duration of the ``with`` block."""
    token = bind_log_context(**fields)
    try:
        yield
    finally:
        _context.reset(token)


class ContextFilter(logging.Filter):
    """Copy the bound correlation fields onto every record passing a handler.

    Attached to the *handler* (logger-level filters don't see records from
    child loggers), so it covers third-party libraries too. An explicit
    ``extra={"run_id": …}`` on a log call wins over the context.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = _context.get({})
        for key in CONTEXT_FIELDS:
            if not hasattr(record, key):
                setattr(record, key, ctx.get(key))
        return True


# Attributes every LogRecord has; anything else on a record came from `extra=`.
_STD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__
) | {"message", "asctime", "taskName", "color_message"}

_SENSITIVE_KEY = re.compile(
    r"pass(word|wd)?|secret|token|api[_-]?key|authorization|cookie|credential|session|payload",
    re.IGNORECASE,
)
_REDACTED = "[redacted]"


def _scrub(value: Any, depth: int = 0) -> Any:
    """Redact sensitive-looking keys in nested ``extra`` values."""
    if depth > 4:
        return str(type(value).__name__)
    if isinstance(value, dict):
        return {
            str(k): _REDACTED if _SENSITIVE_KEY.search(str(k)) else _scrub(v, depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple | set | frozenset):
        return [_scrub(v, depth + 1) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts, level, logger, message + correlation fields."""

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # uvicorn access line: emit structured fields and drop the query string.
        args = record.args
        if record.name == "uvicorn.access" and isinstance(args, tuple) and len(args) == 5:
            client, method, full_path, _http_version, status = args
            path = str(full_path).split("?", 1)[0]
            out["message"] = f"{method} {path} {status}"
            out.update(client_addr=client, http_method=method, http_path=path, http_status=status)

        for key in CONTEXT_FIELDS:
            value = getattr(record, key, None)
            if value is not None:
                out[key] = value

        for key, value in record.__dict__.items():
            if key in _STD_ATTRS or key in out or key in CONTEXT_FIELDS or key.startswith("_"):
                continue
            out[key] = _REDACTED if _SENSITIVE_KEY.search(key) else _scrub(value)

        if record.exc_info:
            out["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            out["stack_info"] = self.formatStack(record.stack_info)
        return json.dumps(out, ensure_ascii=False, default=str)


_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def _resolve_level(name: str | None, default: int) -> int:
    if not name:
        return default
    level = logging.getLevelName(name.strip().upper())
    return level if isinstance(level, int) else default


def setup_logging(fmt: str | None = None, level: str | None = None) -> str:
    """Configure logging from ``LOG_FORMAT`` / ``LOG_LEVEL``. Idempotent.

    Returns the effective format (``"text"`` or ``"json"``). Called at import of
    ``src.main`` and again in the app lifespan, because ``uvicorn.run(app)``
    (desktop entry) applies uvicorn's own logging config *after* that import.
    """
    fmt = (fmt or os.getenv("LOG_FORMAT") or "text").strip().lower()
    level_name = level or os.getenv("LOG_LEVEL")
    root = logging.getLogger()

    if fmt != "json":
        # Text mode: leave handlers/formatters alone. Only an explicit LOG_LEVEL
        # changes anything (basicConfig is a no-op if a root handler exists).
        if level_name:
            resolved = _resolve_level(level_name, logging.INFO)
            logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s")
            root.setLevel(resolved)
        if fmt != "text":
            logger.warning("Unknown LOG_FORMAT=%r; using 'text'.", fmt)
        return "text"

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(ContextFilter())
    root.handlers[:] = [handler]
    root.setLevel(_resolve_level(level_name, logging.INFO))

    # Route uvicorn's loggers (own text handlers, propagate=False) through root.
    for name in _UVICORN_LOGGERS:
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    return "json"


_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class RequestContextMiddleware:
    """Pure-ASGI middleware binding ``request_id`` (+ session ``user_id``) per request.

    Honours a well-formed incoming ``X-Request-ID`` (else generates one) and
    echoes it on the response. Must sit *inside* ``SessionMiddleware`` so
    ``scope["session"]`` is populated. Only the user id is read from the
    session — never the cookie itself. Runs spawned by a request inherit its
    ``request_id``, tying a run back to the trigger call.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = ""
        for key, value in scope.get("headers") or ():
            if key == b"x-request-id":
                request_id = value.decode("latin-1")
                break
        if not _REQUEST_ID_RE.match(request_id):
            request_id = uuid.uuid4().hex

        user_id = (scope.get("session") or {}).get("user_id")
        if not isinstance(user_id, int):
            user_id = None

        async def send_with_request_id(message):
            if message["type"] == "http.response.start":
                headers = [h for h in message.get("headers", ()) if h[0].lower() != b"x-request-id"]
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        with log_context(request_id=request_id, user_id=user_id):
            await self.app(scope, receive, send_with_request_id)
