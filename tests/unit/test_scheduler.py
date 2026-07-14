from datetime import UTC, datetime, timedelta

from src.automation.scheduler import CronScheduler
from src.models import Job


def _job(job_id, schedule, job_type="google_form_fill"):
    return Job(id=job_id, name=f"job {job_id}", job_type=job_type, payload="{}", schedule=schedule)


def test_new_job_is_not_fired_immediately():
    sch = CronScheduler()
    now = datetime(2026, 1, 1, 10, 30, tzinfo=UTC)
    due = sch._sync_and_collect([_job(1, "0 * * * *")], now)
    assert due == []
    # It is anchored to the next future fire (11:00), not "now".
    assert sch.next_due_for(1) == datetime(2026, 1, 1, 11, 0, tzinfo=UTC)


def test_job_fires_once_its_due_time_passes():
    sch = CronScheduler()
    now = datetime(2026, 1, 1, 10, 30, tzinfo=UTC)
    job = _job(1, "0 * * * *")
    sch._sync_and_collect([job], now)  # anchors to 11:00

    later = datetime(2026, 1, 1, 11, 0, 5, tzinfo=UTC)  # just past 11:00
    due = sch._sync_and_collect([job], later)
    assert [j.id for j in due] == [1]
    # Next due advances to 12:00.
    assert sch.next_due_for(1) == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def test_not_fired_twice_in_the_same_slot():
    sch = CronScheduler()
    job = _job(1, "0 * * * *")
    sch._sync_and_collect([job], datetime(2026, 1, 1, 10, 30, tzinfo=UTC))
    sch._sync_and_collect([job], datetime(2026, 1, 1, 11, 0, 5, tzinfo=UTC))  # fires
    # A second tick a few seconds later must NOT re-fire the 11:00 slot.
    due = sch._sync_and_collect([job], datetime(2026, 1, 1, 11, 0, 30, tzinfo=UTC))
    assert due == []


def test_invalid_schedule_is_skipped():
    sch = CronScheduler()
    due = sch._sync_and_collect([_job(1, "not a cron")], datetime(2026, 1, 1, tzinfo=UTC))
    assert due == []
    assert sch.next_due_for(1) is None


def test_stale_jobs_are_forgotten():
    sch = CronScheduler()
    now = datetime(2026, 1, 1, 10, 30, tzinfo=UTC)
    sch._sync_and_collect([_job(1, "0 * * * *"), _job(2, "0 * * * *")], now)
    assert sch.next_due_for(1) is not None and sch.next_due_for(2) is not None
    # Job 2 loses its schedule / is deleted → dropped from the map next tick.
    sch._sync_and_collect([_job(1, "0 * * * *")], now)
    assert sch.next_due_for(1) is not None
    assert sch.next_due_for(2) is None


def test_multiple_due_jobs_collected_together():
    sch = CronScheduler()
    start = datetime(2026, 1, 1, 10, 30, tzinfo=UTC)
    jobs = [_job(1, "*/15 * * * *"), _job(2, "*/15 * * * *")]
    sch._sync_and_collect(jobs, start)  # both anchor to 10:45
    due = sch._sync_and_collect(jobs, datetime(2026, 1, 1, 10, 46, tzinfo=UTC))
    assert sorted(j.id for j in due) == [1, 2]
