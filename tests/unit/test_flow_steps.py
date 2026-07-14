from src.automation.flow_steps import (
    infer_step_states,
    pipeline_step_states,
    run_step_logs,
    step_labels,
)


def _logs(*msgs):
    return [{"ts": "00:00:00", "msg": m} for m in msgs]


def test_step_labels_known_type():
    labels = step_labels("web_scraper")
    assert labels[0] == "Start"
    assert "Verify" in labels and "Evaluate" in labels
    assert labels[-1] == "Done"


def test_step_labels_unknown_type_empty():
    assert step_labels("does_not_exist") == []


def test_infer_states_all_pending_when_no_logs():
    states = infer_step_states("web_scraper", [], "running")
    assert all(s["status"] == "pending" for s in states)


def test_infer_states_marks_reached_step_running():
    logs = _logs("Starting web_scraper...", "Payload validated")
    states = infer_step_states("web_scraper", logs, "running")
    by_name = {s["name"]: s["status"] for s in states}
    assert by_name["Start"] == "done"
    assert by_name["Validate"] == "running"
    assert by_name["Scrape"] == "pending"


def test_infer_states_success_marks_all_reached_done():
    logs = _logs(
        "Starting web_scraper...", "Payload validated", "scraper agent reading",
        "generated summary", "Validating result", "Evaluation complete",
        "Automation completed successfully!",
    )
    states = infer_step_states("web_scraper", logs, "success")
    assert all(s["status"] == "done" for s in states)


def test_infer_states_failed_marks_reached_step_failed():
    logs = _logs("Starting web_scraper...", "Payload validated", "scraper agent reading")
    states = infer_step_states("web_scraper", logs, "failed")
    by_name = {s["name"]: s["status"] for s in states}
    assert by_name["Scrape"] == "failed"
    assert by_name["Validate"] == "done"
    assert by_name["Analyze"] == "pending"


def test_infer_states_unknown_type_returns_empty():
    assert infer_step_states("nope", _logs("Starting"), "success") == []


def test_pipeline_states_running_and_pending():
    logs = _logs(
        "[Step 1/3] Starting web_scraper...",
        "[Step 1/3] Completed web_scraper",
        "[Step 2/3] Starting email_sender...",
    )
    states = pipeline_step_states(logs, "running")
    assert states[0]["status"] == "done"
    assert states[1]["status"] == "running"
    assert states[2]["status"] == "pending"
    assert states[0]["name"] == "web scraper"


def test_pipeline_states_last_step_failed():
    logs = _logs(
        "[Step 1/2] Starting web_scraper...",
        "[Step 1/2] Completed web_scraper",
        "[Step 2/2] Starting email_sender...",
    )
    states = pipeline_step_states(logs, "failed")
    assert states[1]["status"] == "failed"


def test_pipeline_states_empty_logs():
    assert pipeline_step_states([], "pending") == [{"name": "step 1", "status": "pending"}]


# ── run_step_logs (per-step drill-down) ───────────────────────────────────────

def test_run_step_logs_attributes_entries_to_steps():
    logs = _logs(
        "Starting web_scraper...",       # Start
        "Payload validated",             # Validate
        "scraper agent reading page",    # Scrape
        "fetched 8000 chars",            # still Scrape
        "generated summary",             # Analyze
    )
    steps = run_step_logs("web_scraper", logs, "running")
    by_name = {s["name"]: s for s in steps}
    scrape_msgs = [e["msg"] for e in by_name["Scrape"]["logs"]]
    assert "scraper agent reading page" in scrape_msgs
    assert "fetched 8000 chars" in scrape_msgs        # follow-on line stays in Scrape
    assert [e["msg"] for e in by_name["Analyze"]["logs"]] == ["generated summary"]
    assert by_name["Done"]["logs"] == []              # never reached → no logs


def test_run_step_logs_entries_before_first_trigger_go_to_step0():
    logs = _logs("some preamble", "Starting web_scraper...")
    steps = run_step_logs("web_scraper", logs, "running")
    assert steps[0]["name"] == "Start"
    assert [e["msg"] for e in steps[0]["logs"]] == ["some preamble", "Starting web_scraper..."]


def test_run_step_logs_pipeline_buckets_by_marker():
    logs = _logs(
        "[Step 1/2] Starting web_scraper...",
        "scraping...",
        "[Step 1/2] Completed web_scraper",
        "[Step 2/2] Starting email_sender...",
        "sending...",
    )
    steps = run_step_logs("pipeline", logs, "running")
    assert [e["msg"] for e in steps[0]["logs"]][0] == "[Step 1/2] Starting web_scraper..."
    assert "sending..." in [e["msg"] for e in steps[1]["logs"]]


def test_run_step_logs_unknown_type_empty():
    assert run_step_logs("nope", _logs("Starting"), "success") == []
