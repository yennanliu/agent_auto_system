"""In-process rate limiting for the login and run-trigger endpoints.

Two things are protected:

* ``POST /api/auth/login`` — password brute force. Only *failed* attempts are
  counted, against two budgets: per client IP, and per (username, client IP).
  While either is exhausted the endpoint answers 429 before the password is even
  checked. A successful sign-in clears that (username, IP) counter.

  The per-account counter is deliberately scoped to the client IP: a global
  per-username counter would let anyone lock any account out (send 10 bad
  passwords for ``admin`` every window). With this keying a third party can only
  exhaust budgets on *their own* IP, never the real user's. The price is that
  guessing one account from many IPs is bounded by the per-IP budget × the number
  of IPs, not by a per-account cap — use strong passwords / SSO for that threat.

  Check-and-record is atomic: ``login_attempt()`` reserves a slot (counting
  in-flight attempts) under a lock *before* bcrypt runs, so N concurrent requests
  can't all pass the check and only then record their failures.
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
* ``RATE_LIMIT_LOGIN_USERNAME`` — failed logins per (username, client IP) (``10/5minutes``)
* ``RATE_LIMIT_RUN``            — manual run triggers per user  (``20/minute;200/hour``)

Values use the ``limits`` syntax; ``;`` combines several windows. An empty value
disables that one limit; an unparseable value falls back to the default.

Counters live in this process's memory: they reset on restart and are **not**
shared between workers/replicas (N workers ⇒ up to N× the budget). Fine for the
single-process deployment; switch the storage to Redis before scaling out.
``limits``' MemoryStorage empties expired windows but never drops their keys, so
``_prune()`` sweeps empty keys once the table grows past ``_PRUNE_AT`` (bounds
memory under a username spray).

The client IP is ``request.client.host``. ``X-Forwarded-For`` is never parsed
here — behind a reverse proxy, tell uvicorn which peers may set it
(``FORWARDED_ALLOW_IPS`` / ``--forwarded-allow-ips``) and it rewrites
``request.client`` for us. Unconfigured, every client behind the proxy shares
the proxy's IP (safe, just coarse) instead of the key being spoofable.
"""
import logging
import math
import os
import threading
import time
from contextlib import contextmanager
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

# Login attempts currently between "reserved" and "outcome known" (bcrypt is slow
# and runs in a threadpool), per limiter key. Guarded by _login_lock.
_login_lock = threading.Lock()
_inflight: dict[str, int] = {}

_PRUNE_AT = 10_000


def enabled() -> bool:
    return os.getenv("RATE_LIMIT_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")


def reset() -> None:
    """Forget every counter (tests)."""
    _storage.reset()
    with _login_lock:
        _inflight.clear()


def _prune() -> None:
    """Drop keys whose moving window has fully expired, once there are many.

    MemoryStorage's own expiry timer empties those lists but leaves the keys.
    """
    events = getattr(_storage, "events", None)
    if not isinstance(events, dict) or len(events) < _PRUNE_AT:
        return
    for key in [k for k, v in list(events.items()) if not v]:
        if not events.get(key):
            events.pop(key, None)


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
    ip = client_ip(request)
    # Normalised + bounded so case tricks / huge strings can't dodge or bloat it.
    user = username.strip().lower()[:128]
    return [
        ("RATE_LIMIT_LOGIN_IP", f"login-ip:{ip}"),
        ("RATE_LIMIT_LOGIN_USERNAME", f"login-user:{ip}:{user}"),
    ]


class LoginAttempt:
    """Outcome holder for :func:`login_attempt`; call exactly one of these."""

    def __init__(self) -> None:
        self.outcome: str | None = None

    def failed(self) -> None:
        self.outcome = "failed"

    def succeeded(self) -> None:
        self.outcome = "succeeded"


@contextmanager
def login_attempt(request: Request, username: str):
    """Guard one password check against the failed-login budgets.

    Raises 429 if this IP, or this username from this IP, is over budget —
    counting attempts still in flight, so concurrent requests can't overshoot.
    On ``attempt.failed()`` the failure is recorded; on ``attempt.succeeded()``
    the (username, IP) counter is cleared. Neither (e.g. an unexpected error)
    just releases the reservation.
    """
    checks = [
        (env, key, item) for env, key in _login_keys(request, username) for item in _items(env)
    ]
    keys = list(dict.fromkeys(key for _, key, _ in checks))
    with _login_lock:
        for _, key, item in checks:
            if not _limiter.test(item, key, cost=_inflight.get(key, 0) + 1):
                logger.warning("Login rate limit hit (%s)", key)
                raise _too_many("failed sign-in attempts", _retry_after(item, key))
        for key in keys:
            _inflight[key] = _inflight.get(key, 0) + 1

    attempt = LoginAttempt()
    try:
        yield attempt
    finally:
        with _login_lock:
            for key in keys:
                left = _inflight.get(key, 0) - 1
                if left > 0:
                    _inflight[key] = left
                else:
                    _inflight.pop(key, None)
            for env, key, item in checks:
                if attempt.outcome == "failed":
                    _limiter.hit(item, key)
                # Never clear the per-IP counter: logging into your own account
                # must not refill the budget you use to guess someone else's.
                elif attempt.outcome == "succeeded" and env == "RATE_LIMIT_LOGIN_USERNAME":
                    _limiter.clear(item, key)
            _prune()


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
