"""In-process rate limiting for the login and run-trigger endpoints.

Two things are protected:

* ``POST /api/auth/login`` — password brute force. Only *failed* attempts are
  counted, per client IP and per submitted username (the latter blunts guessing
  one account from many IPs). While either budget is exhausted the endpoint
  answers 429 before the password is even checked.
* ``POST /api/jobs/{id}/run`` — runaway LLM cost. Counted per authenticated user
  id, never per IP. The cron scheduler calls ``launcher.launch_run`` directly and
  is deliberately not limited.

Built on the ``limits`` library (the engine underneath ``slowapi``) rather than
slowapi's decorators: slowapi key functions are sync and only see the
``Request``, so they cannot key on the username in the JSON body or count only
failed logins, and its ``Retry-After`` injection needs every limited endpoint to
return/accept a ``Response``.

Configuration (read on every call, so no restart-order surprises in tests):

* ``RATE_LIMIT_ENABLED``        — ``0`` turns every limit off (default on)
* ``RATE_LIMIT_LOGIN_IP``       — failed logins per client IP   (``10/minute``)
* ``RATE_LIMIT_LOGIN_USERNAME`` — failed logins per username    (``10/5minutes``)
* ``RATE_LIMIT_RUN``            — manual run triggers per user  (``20/minute;200/hour``)

Values use the ``limits`` syntax; ``;`` combines several windows. An empty value
disables that one limit; an unparseable value falls back to the default.

Counters live in this process's memory: they reset on restart and are **not**
shared between workers/replicas (N workers ⇒ up to N× the budget). Fine for the
single-process deployment; switch the storage to Redis before scaling out.

The client IP is ``request.client.host``. ``X-Forwarded-For`` is never parsed
here — behind a reverse proxy, tell uvicorn which peers may set it
(``FORWARDED_ALLOW_IPS`` / ``--forwarded-allow-ips``) and it rewrites
``request.client`` for us. Unconfigured, every client behind the proxy shares
the proxy's IP (safe, just coarse) instead of the key being spoofable.
"""
import logging
import math
import os
import time
from functools import lru_cache

from fastapi import HTTPException, Request
from limits import RateLimitItem, parse_many
from limits.storage import MemoryStorage
from limits.strategies import MovingWindowRateLimiter

logger = logging.getLogger(__name__)

_DEFAULTS = {
    "RATE_LIMIT_LOGIN_IP": "10/minute",
    "RATE_LIMIT_LOGIN_USERNAME": "10/5minutes",
    "RATE_LIMIT_RUN": "20/minute;200/hour",
}

_storage = MemoryStorage()
_limiter = MovingWindowRateLimiter(_storage)


def enabled() -> bool:
    return os.getenv("RATE_LIMIT_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")


def reset() -> None:
    """Forget every counter (tests)."""
    _storage.reset()


@lru_cache(maxsize=32)
def _parse(env: str, raw: str) -> tuple[RateLimitItem, ...]:
    try:
        return tuple(parse_many(raw))
    except ValueError:
        logger.warning("Invalid %s=%r; using default %s", env, raw, _DEFAULTS[env])
        return tuple(parse_many(_DEFAULTS[env]))


def _items(env: str) -> tuple[RateLimitItem, ...]:
    if not enabled():
        return ()
    raw = os.getenv(env, _DEFAULTS[env]).strip()
    return _parse(env, raw) if raw else ()


def _retry_after(item: RateLimitItem, key: str) -> int:
    reset_at = _limiter.get_window_stats(item, key).reset_time
    return max(1, math.ceil(reset_at - time.time()))


def _too_many(what: str, retry_after: int) -> HTTPException:
    return HTTPException(
        status_code=429,
        detail=f"Too many {what}. Try again in {retry_after} seconds.",
        headers={"Retry-After": str(retry_after)},
    )


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _login_keys(request: Request, username: str) -> list[tuple[str, str]]:
    return [
        ("RATE_LIMIT_LOGIN_IP", f"login-ip:{client_ip(request)}"),
        # Normalised + bounded so case tricks / huge strings can't dodge or bloat it.
        ("RATE_LIMIT_LOGIN_USERNAME", f"login-user:{username.strip().lower()[:128]}"),
    ]


def check_login(request: Request, username: str) -> None:
    """Raise 429 if this IP or this username has used up its failed-login budget."""
    for env, key in _login_keys(request, username):
        for item in _items(env):
            if not _limiter.test(item, key):
                logger.warning("Login rate limit hit (%s)", key)
                raise _too_many("failed sign-in attempts", _retry_after(item, key))


def record_login_failure(request: Request, username: str) -> None:
    for env, key in _login_keys(request, username):
        for item in _items(env):
            _limiter.hit(item, key)


def hit_run_trigger(user_id: int | None) -> None:
    """Count one manual run trigger for ``user_id``; raise 429 once over budget."""
    key = f"run:{user_id}"
    items = _items("RATE_LIMIT_RUN")
    # Test every window first so a rejected request doesn't consume the others.
    for item in items:
        if not _limiter.test(item, key):
            logger.warning("Run-trigger rate limit hit (user_id=%s)", user_id)
            raise _too_many("runs started", _retry_after(item, key))
    for item in items:
        _limiter.hit(item, key)
