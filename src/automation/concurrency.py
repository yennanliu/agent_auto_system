"""Concurrency caps for runs (back-pressure for ``launcher.launch_run``).

Every run executes in this one process, so an unbounded ``asyncio.create_task``
lets a burst of triggers (several schedules firing together, a user clicking
"Run" ten times) start ten Chromium instances plus their LLM calls at once.

A run passes two gates, in this order:

1. **Its automation's own queue** — one semaphore per job type (``linkedin_apply``,
   ``job_104_apply``, ``greenhouse_apply``, ``pipeline``, ``custom:<slug>`` …), so a
   backlog of one automation never holds up another. Limit: ``MAX_CONCURRENT_RUNS_<TYPE>``
   (type upper-cased, non-alphanumerics → ``_``, e.g. ``MAX_CONCURRENT_RUNS_LINKEDIN_APPLY``),
   else the class default ``MAX_CONCURRENT_BROWSER_RUNS_PER_TYPE`` (default 1 — one
   Chromium per site/login session) / ``MAX_CONCURRENT_LLM_RUNS_PER_TYPE`` (default 4).
2. **The global class ceiling** — a machine-wide safety net on top of the
   per-type queues:

   - ``browser`` — the automation drives Playwright (``AutomationSpec.browser``,
     the single source of truth; a pipeline counts as browser if any step does).
     Limit: ``MAX_CONCURRENT_BROWSER_RUNS`` (default 4).
   - ``llm`` — everything else (LLM/API-only flows, custom no-tools automations).
     Limit: ``MAX_CONCURRENT_LLM_RUNS`` (default 8).

The per-type queue is taken first, so a run waiting behind its own siblings does
not sit on a global slot another automation could use.

A run that cannot get a slot stays in the ``queued`` status until one frees up;
slots are handed out in FIFO order within each queue. The caps are per process —
they do not coordinate across multiple server workers.
"""
import asyncio
import os
import re
import weakref

from src.automation import spec

BROWSER = "browser"
LLM = "llm"

_DEFAULTS = {BROWSER: 4, LLM: 8}
_ENV = {BROWSER: "MAX_CONCURRENT_BROWSER_RUNS", LLM: "MAX_CONCURRENT_LLM_RUNS"}

# Per-automation queue defaults, by class (overridable per type, see type_limit_for).
_TYPE_DEFAULTS = {BROWSER: 1, LLM: 4}
_TYPE_ENV = {
    BROWSER: "MAX_CONCURRENT_BROWSER_RUNS_PER_TYPE",
    LLM: "MAX_CONCURRENT_LLM_RUNS_PER_TYPE",
}

# Run statuses that mean "not finished yet". ``queued`` = waiting for a slot.
ACTIVE_STATUSES = ("pending", "queued", "running")

# asyncio primitives bind to the loop that first contends on them, so keep one
# set of semaphores per event loop (the server has exactly one; tests have many).
# Keys: ("class", run_class) for the global ceilings, ("type", queue_key) per automation.
_SEMAPHORES: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[tuple[str, str], asyncio.Semaphore]]" = (
    weakref.WeakKeyDictionary()
)


def _env_int(name: str, default: int) -> int:
    """Positive int from the environment (floor of 1 — a 0 cap would deadlock)."""
    try:
        value = int(os.getenv(name, ""))
    except ValueError:
        value = default
    return max(1, value)


def limit_for(run_class: str) -> int:
    """Configured global cap for a run class (env override, floor of 1)."""
    return _env_int(_ENV[run_class], _DEFAULTS[run_class])


def queue_key(job_type: str) -> str:
    """Name of the per-automation queue a run of ``job_type`` waits in."""
    return job_type or "unknown"


def type_env_var(job_type: str) -> str:
    """Env var that overrides one automation's queue size."""
    return "MAX_CONCURRENT_RUNS_" + re.sub(r"[^A-Z0-9]+", "_", queue_key(job_type).upper()).strip("_")


def type_limit_for(job_type: str, run_class: str) -> int:
    """Cap for one automation's own queue: per-type env, else the class default."""
    default = _env_int(_TYPE_ENV[run_class], _TYPE_DEFAULTS[run_class])
    return _env_int(type_env_var(job_type), default)


def _is_browser(job_type: str) -> bool:
    spec.ensure_plugins_loaded()
    s = spec.REGISTRY.get(job_type)
    return bool(s and s.browser)


def run_class(job_type: str, payload: dict | None = None) -> str:
    """Classify a run as ``browser`` or ``llm`` from the automation spec."""
    if job_type == "pipeline":
        steps = (payload or {}).get("steps") or []
        if any(isinstance(st, dict) and _is_browser(str(st.get("job_type", ""))) for st in steps):
            return BROWSER
        return LLM
    return BROWSER if _is_browser(job_type) else LLM


def _semaphore(key: tuple[str, str], limit: int) -> asyncio.Semaphore:
    per_loop = _SEMAPHORES.setdefault(asyncio.get_running_loop(), {})
    sem = per_loop.get(key)
    if sem is None:
        sem = per_loop[key] = asyncio.Semaphore(limit)
    return sem


def semaphore_for(run_class: str) -> asyncio.Semaphore:
    """The current event loop's global semaphore for ``run_class`` (created on demand)."""
    return _semaphore(("class", run_class), limit_for(run_class))


def type_semaphore_for(job_type: str, run_class: str) -> asyncio.Semaphore:
    """The current event loop's semaphore for ``job_type``'s own queue."""
    return _semaphore(("type", queue_key(job_type)), type_limit_for(job_type, run_class))


def gates_for(job_type: str, payload: dict | None = None) -> list[tuple[asyncio.Semaphore, str]]:
    """The semaphores a run must hold, in acquisition order, each with a
    human-readable reason shown in the run log while it waits on it."""
    rc = run_class(job_type, payload)
    key = queue_key(job_type)
    return [
        (
            type_semaphore_for(job_type, rc),
            f"waiting for a free slot in the {key} queue (limit {type_limit_for(job_type, rc)})",
        ),
        (
            semaphore_for(rc),
            f"waiting for a free {rc} slot (global limit {limit_for(rc)})",
        ),
    ]
