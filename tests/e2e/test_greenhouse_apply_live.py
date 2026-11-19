"""Live smoke test: dry-run ``run_greenhouse_apply`` against a real public board.

Opt-in (network + a browser): ``GREENHOUSE_LIVE_E2E=1``. Always ``dry_run``: the
real form is opened and filled but Submit is never clicked, so nothing is sent
to the employer. Catches drift in the Job Board API or the embedded form's DOM
that the hermetic fixture test (``test_greenhouse_apply_browser.py``) can't.

    GREENHOUSE_LIVE_E2E=1 uv run pytest tests/e2e/test_greenhouse_apply_live.py -v -s

``GREENHOUSE_LIVE_BOARD`` picks the board (default ``figma``, the canary's).
"""
import os

import pytest

from src.automation.tools import greenhouse_apply_tool as gh

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.getenv("GREENHOUSE_LIVE_E2E") != "1",
                       reason="live Greenhouse test is opt-in: set GREENHOUSE_LIVE_E2E=1"),
]


def _answer_fn(label, kind, options, job):
    """Stand-in for the LLM screening-answer crew: free-text questions ("Why do
    you want to join X?") get a fixed sentence, choices abstain."""
    return "" if options else "I admire the product and want to help build it."


def test_live_board_dry_run_prepares_an_application(tmp_path, monkeypatch):
    monkeypatch.setenv("GREENHOUSE_HEADLESS", "1")
    resume = tmp_path / "cv.pdf"
    resume.write_bytes(b"%PDF-1.4\n% live e2e dry-run resume\n")
    board = os.getenv("GREENHOUSE_LIVE_BOARD", "figma")

    out = gh.run_greenhouse_apply(
        boards=board, first_name="Test", last_name="DryRun", email="dry-run@example.com",
        phone="+1 415 555 0100", current_location="San Francisco, CA",
        authorized_countries="United States", years_experience=5,
        linkedin_url="https://www.linkedin.com/in/example",
        # Generic answers so most screening questions are answerable.
        extra_answers="pronouns: Prefer not to say\nhow did you hear: Company website",
        resume_path=str(resume), ledger_path=str(tmp_path / "ledger.json"),
        max_applications=1, max_scans_per_board=25, dry_run=True, log=print,
        answer_fn=_answer_fn,
    )

    assert "error" not in out, out.get("error")
    assert out["total_listed"] > 0, f"{board}: Job Board API listed no jobs"
    # Never sent: dry-run must not submit and must not touch the ledger.
    assert out["submitted_count"] == 0
    assert gh.load_ledger(str(tmp_path / "ledger.json")) == {}

    # A form we opened but could not fill / find Submit on means DOM drift.
    drift = [e for e in out["skipped"] if e.get("status") == "failed"
             and not str(e.get("reason", "")).startswith("could not load form questions")]
    assert not drift, f"form drift: {[(e['job_id'], e['reason']) for e in drift]}"

    if not out["applied"]:
        pytest.skip("every scanned job had a required question this profile can't answer: "
                    + "; ".join(e.get("reason", "")[:80] for e in out["skipped"][:3]))
    entry = out["applied"][0]
    assert entry["status"] == "prepared" and entry["submitted"] is False
    assert any(a["label"].lower().startswith("first name") for a in entry["answers"])
