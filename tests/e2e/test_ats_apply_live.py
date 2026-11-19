"""Live smoke test: dry-run each new ATS adapter against a real public board.

Opt-in (network + a browser): ``ATS_LIVE_E2E=1``. Always ``dry_run``: real forms
are opened and filled but Submit is never clicked, so nothing is sent to any
employer. Catches drift in each platform's listing API, form definition (Lever
HTML / Ashby GraphQL / Workable form API) or form DOM that the hermetic fixture
test (``test_ats_apply_browser.py``) can't.

    ATS_LIVE_E2E=1 uv run pytest tests/e2e/test_ats_apply_live.py -v -s
"""
import os

import pytest

from src.automation.tools import ats_apply_tool as ats

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.getenv("ATS_LIVE_E2E") != "1",
                       reason="live ATS test is opt-in: set ATS_LIVE_E2E=1"),
]


def _answer_fn(label, kind, options, job):
    """Stand-in for the LLM screening-answer crew: free-text questions get a
    fixed sentence, choices abstain."""
    return "" if options else "I admire the product and want to help build it."


@pytest.mark.parametrize("boards", [
    os.getenv("ATS_LIVE_LEVER", "lever:palantir"),
    os.getenv("ATS_LIVE_ASHBY", "ashby:openai"),
    os.getenv("ATS_LIVE_WORKABLE", "workable:huggingface"),
])
def test_live_board_dry_run(boards, tmp_path, monkeypatch):
    monkeypatch.setenv("ATS_HEADLESS", "1")
    resume = tmp_path / "cv.pdf"
    resume.write_bytes(b"%PDF-1.4\n% live e2e dry-run resume\n")
    ledger = tmp_path / "ledger.json"

    out = ats.run_ats_apply(
        boards=boards, first_name="Test", last_name="DryRun", email="dry-run@example.com",
        phone="+1 415 555 0100", current_location="San Francisco, CA",
        authorized_countries="United States", years_experience=5, notice_period="2 weeks",
        languages="English, Chinese", school="Stanford University",
        linkedin_url="https://www.linkedin.com/in/example", github_url="https://github.com/example",
        extra_answers="office three days: Yes\npronouns: Prefer not to say",
        resume_path=str(resume), ledger_path=str(ledger),
        max_applications=1, max_scans_per_board=25, dry_run=True, log=print,
        answer_fn=_answer_fn,
    )

    assert "error" not in out, out.get("error")
    assert out["total_listed"] > 0, f"{boards}: listing API returned no jobs"
    assert out["submitted_count"] == 0 and not ledger.exists()   # never sent

    # A form we opened but could not fill / find Submit on means DOM drift.
    drift = [e for e in out["skipped"] if e.get("status") == "failed"
             and not str(e.get("reason", "")).startswith("could not load form questions")]
    assert not drift, f"form drift: {[(e['job_id'], e['reason']) for e in drift]}"

    if not out["applied"]:
        pytest.skip("every scanned job had a required question this profile can't answer: "
                    + "; ".join(e.get("reason", "")[:80] for e in out["skipped"][:3]))
    entry = out["applied"][0]
    assert entry["status"] == "prepared" and entry["submitted"] is False
