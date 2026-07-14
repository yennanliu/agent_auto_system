"""Canonical flow-step definitions + status inference for the Task Overview grid.

Each job type maps to an ordered list of (label, trigger) pairs. ``trigger`` is a
substring searched for in a run's progress log; the last step whose trigger is
present is the furthest the run reached. This mirrors the client-side
``FLOW_STEPS`` / ``inferStepStates`` in ``ui/app.js`` but lives here so the
``/jobs/{id}/overview`` endpoint (and its tests) is the source of truth.
"""
from __future__ import annotations

# Validation + LLM-as-judge run centrally in the executor after every job.
_QA = [("Verify", "Validating result"), ("Evaluate", "Evaluation complete")]
_DONE = ("Done", "completed successfully")

FLOW_STEPS: dict[str, list[tuple[str, str]]] = {
    "google_form_fill": [
        ("Start", "Starting"), ("Validate", "Payload validated"),
        ("Inspect Form", "Inspecting Google Form"), ("Submit", "Form submission attempted"),
        *_QA, _DONE,
    ],
    "web_scraper": [
        ("Start", "Starting"), ("Validate", "Payload validated"),
        ("Scrape", "scraper agent reading"), ("Analyze", "generated summary"),
        *_QA, _DONE,
    ],
    "hacker_news_digest": [
        ("Start", "Starting"), ("Validate", "Fetching top"),
        ("Digest", "Digest generated"), *_QA, _DONE,
    ],
    "x_scraper": [
        ("Start", "Starting"), ("Validate", "Validated payload"),
        ("Fetch", "Fetching posts"), ("Analyze", "Analysis complete"), *_QA, _DONE,
    ],
    "email_sender": [
        ("Start", "Starting"), ("Validate", "Sending to"),
        ("Send", "Connecting to Gmail"), *_QA, _DONE,
    ],
    "google_sheet_reader": [
        ("Start", "Starting"), ("Validate", "Validated sheet URL"),
        ("Fetch", "Fetching Google Sheet"), ("Analyze", "Analyzing sheet data"), *_QA, _DONE,
    ],
    "shopee_seller_scraper": [
        ("Start", "Starting"), ("Validate", "Validated payload for keyword"),
        ("Search", "Loading Shopee session"), ("Collect", "Seller collection complete"),
        *_QA, _DONE,
    ],
    "profit_health_check": [
        ("Start", "Starting"), ("Load CSV", "Loaded CSVs"),
        ("驗證", "蝦皮資料驗證員"), ("修正", "蝦皮資料修正員"),
        ("分析", "蝦皮利潤分析師"), ("建議", "蝦皮營運行動建議員"),
        ("PDF", "PDF 報告"), *_QA, _DONE,
    ],
    "tasker_apply": [
        ("Start", "Starting"), ("Validate", "Payload validated"),
        ("Login", "Loading tasker.com.tw session"), ("Apply", "run complete"), *_QA, _DONE,
    ],
    "tw104_apply": [
        ("Start", "Starting"), ("Validate", "Payload validated"),
        ("Login", "Loading 104.com.tw session"), ("Apply", "run complete"), *_QA, _DONE,
    ],
    "email_collect": [
        ("Start", "Starting"), ("Validate", "Payload validated"),
        ("Discover", "Discovering businesses"), ("Extract", "Extracting email"),
        ("Collect", "Collected"), ("Qualify", "Qualifying"), *_QA, _DONE,
    ],
}


def step_labels(job_type: str) -> list[str]:
    return [label for label, _ in FLOW_STEPS.get(job_type, [])]


def infer_step_states(job_type: str, logs: list[dict], final_status: str) -> list[dict]:
    """Return one ``{name, status}`` per step for a run.

    status ∈ {done, running, failed, pending}. The furthest step whose trigger
    appears in the log is 'reached'; earlier steps are done, later are pending.
    The reached step's status depends on the run's terminal status.
    """
    steps = FLOW_STEPS.get(job_type)
    if not steps:
        return []
    msgs = [str(e.get("msg", "")) for e in (logs or [])]
    reached = -1
    for i, (_, trigger) in enumerate(steps):
        if any(trigger in m for m in msgs):
            reached = i

    out: list[dict] = []
    for i, (label, _) in enumerate(steps):
        if i < reached:
            status = "done"
        elif i == reached:
            if final_status == "failed":
                status = "failed"
            elif final_status == "success":
                status = "done"
            else:
                status = "running"
        else:
            status = "pending"
        out.append({"name": label, "status": status})
    return out


def run_step_logs(job_type: str, logs: list[dict], final_status: str) -> list[dict]:
    """Per-step view of a run: ``{name, status, logs}`` for each step, where ``logs``
    are the progress entries emitted while that step was active.

    An entry is attributed to the latest step whose trigger has appeared at or
    before it (entries before the first trigger fall under step 0). For pipeline
    runs, the ``[Step n/total]`` markers delimit steps.
    """
    logs = logs or []
    if job_type == "pipeline":
        states = pipeline_step_states(logs, final_status)
        buckets = _bucket_pipeline_logs(logs, len(states))
        return [{**states[i], "logs": buckets[i]} for i in range(len(states))]

    steps = FLOW_STEPS.get(job_type)
    if not steps:
        return []
    states = infer_step_states(job_type, logs, final_status)
    buckets: list[list[dict]] = [[] for _ in steps]
    current = 0
    for e in logs:
        msg = str(e.get("msg", ""))
        best = current
        for i, (_, trigger) in enumerate(steps):
            if i > best and trigger in msg:
                best = i
        current = best
        buckets[current].append(e)
    return [{**states[i], "logs": buckets[i]} for i in range(len(steps))]


def _bucket_pipeline_logs(logs: list[dict], n_steps: int) -> list[list[dict]]:
    import re

    step_re = re.compile(r"\[Step (\d+)/(\d+)\]")
    n = max(n_steps, 1)
    buckets: list[list[dict]] = [[] for _ in range(n)]
    current = 0
    for e in logs:
        m = step_re.search(str(e.get("msg", "")))
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < n:
                current = idx
        buckets[current].append(e)
    return buckets


def pipeline_step_states(logs: list[dict], final_status: str) -> list[dict]:
    """Derive per-step states for a ``pipeline`` run from its bracketed log lines
    (``[Step n/total] Starting <type>...`` / ``[Step n/total] Completed ...``)."""
    import re

    start_re = re.compile(r"\[Step (\d+)/(\d+)\] Starting (.+?)\.\.\.")
    done_re = re.compile(r"\[Step (\d+)/(\d+)\] Completed .+")
    info: dict[int, dict] = {}
    total = 0
    for e in logs or []:
        msg = str(e.get("msg", ""))
        if m := start_re.search(msg):
            idx = int(m.group(1)) - 1
            total = max(total, int(m.group(2)))
            info.setdefault(idx, {"type": m.group(3), "done": False})
        if m := done_re.search(msg):
            idx = int(m.group(1)) - 1
            total = max(total, int(m.group(2)))
            if idx in info:
                info[idx]["done"] = True
    total = total or 1

    out: list[dict] = []
    for i in range(total):
        entry = info.get(i)
        if entry and entry["done"]:
            status = "done"
        elif entry:
            status = "running"
        else:
            status = "pending"
        if i == total - 1 and status != "done":
            if final_status == "failed":
                status = "failed"
            elif final_status == "success":
                status = "done"
        name = entry["type"].replace("_", " ") if entry else f"step {i + 1}"
        out.append({"name": name, "status": status})
    return out
