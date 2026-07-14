from datetime import UTC, datetime, timedelta

import pytest

from src.automation.scheduler import CronScheduler
from src.models import Job


@pytest.fixture
def scheduled_job(db_session):
    job = Job(name="Cron Form", job_type="google_form_fill", payload="{}", schedule="* * * * *")
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    return job


async def test_tick_fires_due_job(scheduled_job, test_engine, mocker):
    mocker.patch("src.automation.scheduler.get_engine", return_value=test_engine)
    launched = mocker.patch("src.automation.scheduler.launch_run", return_value=123)
    mocker.patch("src.settings_store.is_automation_enabled", return_value=True)

    sch = CronScheduler()
    t0 = datetime(2026, 1, 1, 10, 0, 30, tzinfo=UTC)
    assert await sch.tick(now=t0) == []          # first tick only anchors, no fire
    launched.assert_not_called()

    t1 = t0 + timedelta(minutes=1, seconds=5)     # next minute has passed
    result = await sch.tick(now=t1)
    assert result == [123]
    launched.assert_called_once()
    kwargs = launched.call_args.kwargs
    assert kwargs["trigger"] == "schedule"
    assert launched.call_args.args[1] == "google_form_fill"


async def test_tick_skips_disabled_automation(scheduled_job, test_engine, mocker):
    mocker.patch("src.automation.scheduler.get_engine", return_value=test_engine)
    launched = mocker.patch("src.automation.scheduler.launch_run")
    mocker.patch("src.settings_store.is_automation_enabled", return_value=False)

    sch = CronScheduler()
    t0 = datetime(2026, 1, 1, 10, 0, 30, tzinfo=UTC)
    await sch.tick(now=t0)
    await sch.tick(now=t0 + timedelta(minutes=1, seconds=5))
    launched.assert_not_called()


async def test_tick_no_scheduled_jobs(test_engine, mocker):
    mocker.patch("src.automation.scheduler.get_engine", return_value=test_engine)
    launched = mocker.patch("src.automation.scheduler.launch_run")

    sch = CronScheduler()
    assert await sch.tick(now=datetime(2026, 1, 1, tzinfo=UTC)) == []
    launched.assert_not_called()
