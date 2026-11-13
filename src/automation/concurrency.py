"""Global concurrency caps for runs (back-pressure for ``launcher.launch_run``).

Every run executes in this one process, so an unbounded ``asyncio.create_task``
lets a burst of triggers (several schedules firing together, a user clicking
"Run" ten times) start ten Chromium instances plus their LLM calls at once.

Runs are split into two classes, each with its own semaphore:

- ``browser`` — the automation drives Playwright (``AutomationSpec.browser``,
  the single source of truth; a pipeline counts as browser if any step does).
  Limit: ``MAX_CONCURRENT_BROWSER_RUNS`` (default 2).
- ``llm`` — everything else (LLM/API-only flows, custom no-tools automations).
  Limit: ``MAX_CONCURRENT_LLM_RUNS`` (default 8).

A run that cannot get a slot stays in the ``queued`` status until one frees up;
slots are handed out in FIFO order. The caps are per process — they do not
coordinate across multiple server workers.
"""
import asyncio
import os
import weakref

from src.automation import spec

BROWSER = "browser"
LLM = "llm"

_DEFAULTS = {BROWSER: 2, LLM: 8}
_ENV = {BROWSER: "MAX_CONCURRENT_BROWSER_RUNS", LLM: "MAX_CONCURRENT_LLM_RUNS"}

# Run statuses that mean "not finished yet". ``queued`` = waiting for a slot.
ACTIVE_STATUSES = ("pending", "queued", "running")

# asyncio primitives bind to the loop that first contends on them, so keep one
# set of semaphores per event loop (the server has exactly one; tests have many).
_SEMAPHORES: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Semaphore]]" = (
    weakref.WeakKeyDictionary()
)


def limit_for(run_class: str) -> int:
    """Configured cap for a run class (env override, floor of 1)."""
    try:
        value = int(os.getenv(_ENV[run_class], ""))
    except ValueError:
        value = _DEFAULTS[run_class]
    return max(1, value)


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


def semaphore_for(run_class: str) -> asyncio.Semaphore:
    """The current event loop's semaphore for ``run_class`` (created on demand)."""
    per_loop = _SEMAPHORES.setdefault(asyncio.get_running_loop(), {})
    sem = per_loop.get(run_class)
    if sem is None:
        sem = per_loop[run_class] = asyncio.Semaphore(limit_for(run_class))
    return sem
