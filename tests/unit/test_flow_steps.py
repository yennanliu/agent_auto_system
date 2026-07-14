from src.automation.flow_steps import (
    infer_step_states,
    pipeline_step_states,
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
