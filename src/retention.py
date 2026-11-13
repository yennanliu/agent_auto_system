"""Data retention + per-user export/erase (PII handling).

Runs (``log`` / ``result`` JSON, incl. email_collect leads that may name natural
persons), uploaded CSVs (``uploads/<upload_id>/``) and rendered PDFs
(``reports/<run_id>.pdf``) used to be kept forever. This module adds:

* a **retention policy** read from env (``policy()``),
* ``purge_expired()`` — the daily sweep, also callable on demand by an admin,
* ``RetentionTask`` — the asyncio loop started from ``main.lifespan``,
* ``export_user_data()`` / ``delete_user_data()`` — the admin "export and erase
  one user's data" operations.

Deleting is irreversible, so everything here is conservative:

* The automatic purge is **opt-in** (``RETENTION_ENABLED=1``). Upgrading an
  existing install never starts deleting data on its own.
* Active runs (``pending`` / ``running``) are never deleted.
* An upload still referenced by any existing job's payload is never deleted
  (that job could be re-run or fire on a schedule).
* Files are only ever removed as *direct children* of the configured uploads /
  reports directories, by a strictly validated name; symlinks are skipped.

See doc/data-retention.md.
"""
import asyncio
import io
import json
import logging
import os
import re
import shutil
import zipfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, func
from sqlmodel import Session, select

from src.database import get_engine
from src.models import Job, Run, User

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = ("pending", "running")

DEFAULT_RUN_DAYS = 90
DEFAULT_UPLOAD_DAYS = 30
DEFAULT_INTERVAL_HOURS = 24
# First sweep happens shortly after startup (not immediately, so boot stays
# fast); the in-memory timer re-anchors on restart, like the cron scheduler.
STARTUP_DELAY_SECS = 60

_UPLOAD_ID_RE = re.compile(r"[0-9a-f]{32}")  # uuid4().hex, see routers/uploads.py
_REPORT_RE = re.compile(r"(\d+)\.pdf")
_CHUNK = 500  # stay under SQLite's bound-parameter limit


# ── Policy ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RetentionPolicy:
    enabled: bool        # automatic daily purge on/off (opt-in)
    run_days: int        # 0 = never purge runs / report PDFs
    upload_days: int     # 0 = never purge uploads
    interval_hours: int  # hours between automatic sweeps


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    """Parse a non-negative int env var. A malformed value disables that
    category (returns 0) rather than guessing — never delete on a bad config."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        logger.warning("Retention: %s=%r is not an integer; treating as 0 (disabled)", name, raw)
        return 0
    if value < minimum:
        logger.warning("Retention: %s=%r is below %d; treating as 0 (disabled)", name, raw, minimum)
        return 0
    return value


def policy() -> RetentionPolicy:
    """The current policy, read from env on every call (cheap; keeps tests simple)."""
    enabled = os.getenv("RETENTION_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")
    return RetentionPolicy(
        enabled=enabled,
        run_days=_env_int("RETENTION_RUN_DAYS", DEFAULT_RUN_DAYS),
        upload_days=_env_int("RETENTION_UPLOAD_DAYS", DEFAULT_UPLOAD_DAYS),
        interval_hours=_env_int("RETENTION_INTERVAL_HOURS", DEFAULT_INTERVAL_HOURS) or DEFAULT_INTERVAL_HOURS,
    )


# ── Safe file handling ────────────────────────────────────────────────────────

def uploads_root() -> Path:
    from src.routers import uploads
    return Path(uploads.UPLOAD_ROOT)


def reports_root() -> Path:
    from src.automation import report_render
    return Path(report_render.REPORTS_ROOT)


def _contained_child(root: Path, name: str) -> Path | None:
    """``root/name`` if — and only if — it is a real direct child of ``root``.

    Rejects separators, ``.``/``..``, NULs, symlinks, and anything whose resolved
    path does not sit directly inside the resolved root. Callers additionally
    validate ``name`` against a strict pattern; this is the last line of defence.
    """
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
        return None
    candidate = root / name
    try:
        if candidate.is_symlink():
            return None
        if candidate.resolve().parent != root.resolve():
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate


def _upload_dir(upload_id: object) -> Path | None:
    """The directory of a well-formed upload id, or None (malformed / unsafe)."""
    if not isinstance(upload_id, str) or not _UPLOAD_ID_RE.fullmatch(upload_id):
        return None
    return _contained_child(uploads_root(), upload_id)


def _report_path(run_id: object) -> Path | None:
    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id < 0:
        return None
    return _contained_child(reports_root(), f"{run_id}.pdf")


def delete_report(run_id: int) -> bool:
    """Remove ``reports/<run_id>.pdf`` if present. Never raises."""
    path = _report_path(run_id)
    try:
        if path is None or not path.is_file():
            return False
        path.unlink()
        return True
    except OSError:
        logger.warning("Retention: could not delete report for run_id=%s", run_id, exc_info=True)
        return False


def delete_upload(upload_id: str) -> bool:
    """Remove ``uploads/<upload_id>/`` if present. Never raises."""
    path = _upload_dir(upload_id)
    try:
        if path is None or not path.is_dir():
            return False
        shutil.rmtree(path)  # does not follow symlinks inside the tree
        return True
    except OSError:
        logger.warning("Retention: could not delete upload %s", upload_id, exc_info=True)
        return False


def _newest_mtime(directory: Path) -> float:
    """Most recent mtime of a directory or anything directly inside it."""
    newest = directory.stat().st_mtime
    for entry in directory.iterdir():
        try:
            newest = max(newest, entry.lstat().st_mtime)
        except OSError:
            continue
    return newest


def _referenced_upload_ids(payloads) -> set[str]:
    """Every upload-id-shaped token in the given job payloads.

    Scans the raw payload text rather than walking parsed JSON: it is a superset
    (errs towards keeping files) and covers nested pipeline steps and payloads
    that no longer parse.
    """
    found: set[str] = set()
    for payload in payloads:
        if payload:
            found.update(_UPLOAD_ID_RE.findall(payload))
    return found


def _chunks(items: list[int]):
    for i in range(0, len(items), _CHUNK):
        yield items[i:i + _CHUNK]


# ── Purge ─────────────────────────────────────────────────────────────────────

def purge_expired(
    *, now: datetime | None = None, dry_run: bool = False, pol: RetentionPolicy | None = None
) -> dict:
    """Delete expired runs, their report PDFs, and expired unreferenced uploads.

    Honors the *retention windows* of the policy but not its ``enabled`` flag —
    that flag only gates the automatic loop, so an admin can still run (or
    dry-run) a sweep by hand. With ``dry_run`` nothing is deleted and the counts
    say what *would* be.
    """
    pol = pol or policy()
    now = now or datetime.now(UTC)
    summary = {
        "dry_run": dry_run,
        "ran_at": now.isoformat(),
        "runs_deleted": 0,
        "reports_deleted": 0,
        "uploads_deleted": 0,
        "uploads_kept_referenced": 0,
    }

    if pol.run_days > 0:
        cutoff = now - timedelta(days=pol.run_days)
        _purge_runs(cutoff, dry_run, summary)
        _purge_orphan_reports(cutoff, dry_run, summary)
    if pol.upload_days > 0:
        _purge_uploads(now - timedelta(days=pol.upload_days), dry_run, summary)
    return summary


def _purge_runs(cutoff: datetime, dry_run: bool, summary: dict) -> None:
    age = func.coalesce(Run.finished_at, Run.started_at)
    with Session(get_engine()) as session:
        ids = list(session.exec(
            select(Run.id).where(Run.status.not_in(ACTIVE_STATUSES)).where(age < cutoff)
        ).all())
        if not dry_run:
            for chunk in _chunks(ids):
                # Re-assert "not active" in the DELETE itself: never race a run
                # that was (re)started between the SELECT and now.
                session.exec(
                    delete(Run).where(Run.id.in_(chunk)).where(Run.status.not_in(ACTIVE_STATUSES))
                )
            session.commit()
    summary["runs_deleted"] += len(ids)
    for run_id in ids:
        path = _report_path(run_id)
        if path is not None and path.is_file():
            if dry_run or delete_report(run_id):
                summary["reports_deleted"] += 1


def _purge_orphan_reports(cutoff: datetime, dry_run: bool, summary: dict) -> None:
    """Old PDFs whose run row is gone (runs deleted before this feature existed)."""
    root = reports_root()
    if not root.is_dir():
        return
    candidates: dict[int, Path] = {}
    for entry in root.iterdir():
        match = _REPORT_RE.fullmatch(entry.name)
        if not match:
            continue
        path = _report_path(int(match.group(1)))
        try:
            if path is None or not path.is_file() or path.name != entry.name:
                continue
            if datetime.fromtimestamp(path.stat().st_mtime, UTC) >= cutoff:
                continue
        except OSError:
            continue
        candidates[int(match.group(1))] = path
    if not candidates:
        return
    existing: set[int] = set()
    with Session(get_engine()) as session:
        for chunk in _chunks(list(candidates)):
            existing.update(session.exec(select(Run.id).where(Run.id.in_(chunk))).all())
    for run_id in candidates:
        if run_id not in existing and (dry_run or delete_report(run_id)):
            summary["reports_deleted"] += 1


def _purge_uploads(cutoff: datetime, dry_run: bool, summary: dict) -> None:
    root = uploads_root()
    if not root.is_dir():
        return
    with Session(get_engine()) as session:
        referenced = _referenced_upload_ids(session.exec(select(Job.payload)).all())
    for entry in root.iterdir():
        path = _upload_dir(entry.name)
        try:
            if path is None or not path.is_dir():
                continue  # not one of ours (or unsafe) — leave it alone
            if datetime.fromtimestamp(_newest_mtime(path), UTC) >= cutoff:
                continue
        except OSError:
            continue
        if entry.name in referenced:
            summary["uploads_kept_referenced"] += 1
            continue
        if dry_run or delete_upload(entry.name):
            summary["uploads_deleted"] += 1


# ── Per-user export / erase ───────────────────────────────────────────────────

class ActiveRunsError(Exception):
    """The user still has pending/running runs; cancel them before erasing."""

    def __init__(self, run_ids: list[int]):
        super().__init__(f"{len(run_ids)} run(s) still in progress")
        self.run_ids = run_ids


def _user_scope(session: Session, user_id: int) -> tuple[list[Job], list[Run]]:
    """A user's data: the jobs they created, the runs they triggered, and every
    run of their jobs (those results derive from the user's job payload).

    Rows with no owner (``created_by`` / ``user_id`` NULL — created before
    ownership was tracked) belong to nobody and are never included.
    """
    jobs = list(session.exec(select(Job).where(Job.created_by == user_id).order_by(Job.id)).all())
    job_ids = [j.id for j in jobs]
    runs: dict[int, Run] = {
        r.id: r for r in session.exec(select(Run).where(Run.user_id == user_id)).all()
    }
    for chunk in _chunks(job_ids):
        for r in session.exec(select(Run).where(Run.job_id.in_(chunk))).all():
            runs[r.id] = r
    return jobs, [runs[k] for k in sorted(runs)]


def _loads(raw: str | None):
    """Parsed JSON when possible, else the raw string (never lose data on export)."""
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def export_user_data(session: Session, user: User) -> bytes:
    """A zip of one user's data: ``data.json`` (account, jobs, runs) plus the
    upload CSVs their jobs reference and the report PDFs of their runs."""
    jobs, runs = _user_scope(session, user.id)

    doc = {
        "exported_at": datetime.now(UTC).isoformat(),
        "user": {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "oauth_provider": user.oauth_provider,
            "is_admin": user.is_admin,
            "is_active": user.is_active,
            "allowed_automations": _loads(user.allowed_automations),
            "created_at": _iso(user.created_at),
            "last_login_at": _iso(user.last_login_at),
        },
        "jobs": [
            {
                "id": j.id, "name": j.name, "job_type": j.job_type,
                "payload": _loads(j.payload), "schedule": j.schedule,
                "created_at": _iso(j.created_at),
            }
            for j in jobs
        ],
        "runs": [
            {**r.model_dump(mode="json"), "result": _loads(r.result), "log": _loads(r.log)}
            for r in runs
        ],
        "files": [],
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for upload_id in sorted(_referenced_upload_ids(j.payload for j in jobs)):
            directory = _upload_dir(upload_id)
            if directory is None or not directory.is_dir():
                continue
            for entry in sorted(directory.iterdir()):
                if entry.is_file() and not entry.is_symlink():
                    arcname = f"uploads/{upload_id}/{entry.name}"
                    zf.write(entry, arcname)
                    doc["files"].append(arcname)
        for r in runs:
            path = _report_path(r.id)
            if path is not None and path.is_file():
                arcname = f"reports/{r.id}.pdf"
                zf.write(path, arcname)
                doc["files"].append(arcname)
        zf.writestr("data.json", json.dumps(doc, ensure_ascii=False, indent=2))
    return buf.getvalue()


def delete_user_data(session: Session, user: User) -> dict:
    """Erase one user's jobs, runs and files. Keeps the account itself.

    Raises ``ActiveRunsError`` (nothing deleted) while any run in scope is still
    pending/running. An upload is removed only when no *remaining* job (i.e. one
    owned by someone else) still references it.
    """
    jobs, runs = _user_scope(session, user.id)
    active = [r.id for r in runs if r.status in ACTIVE_STATUSES]
    if active:
        raise ActiveRunsError(active)

    job_ids = [j.id for j in jobs]
    run_ids = [r.id for r in runs]
    upload_ids = _referenced_upload_ids(j.payload for j in jobs)

    for chunk in _chunks(run_ids):
        session.exec(delete(Run).where(Run.id.in_(chunk)))
    for chunk in _chunks(job_ids):
        session.exec(delete(Job).where(Job.id.in_(chunk)))
    session.commit()

    still_referenced = _referenced_upload_ids(session.exec(select(Job.payload)).all())
    reports = sum(1 for run_id in run_ids if delete_report(run_id))
    uploads = sum(1 for uid in sorted(upload_ids - still_referenced) if delete_upload(uid))
    logger.info(
        "Retention: erased data of user_id=%s — %d job(s), %d run(s), %d report(s), %d upload(s)",
        user.id, len(job_ids), len(run_ids), reports, uploads,
    )
    return {
        "user_id": user.id,
        "jobs_deleted": len(job_ids),
        "runs_deleted": len(run_ids),
        "reports_deleted": reports,
        "uploads_deleted": uploads,
        "uploads_kept_referenced": len(upload_ids & still_referenced),
    }


# ── Background task ───────────────────────────────────────────────────────────

class RetentionTask:
    """Runs ``purge_expired`` once a day (``RETENTION_INTERVAL_HOURS``).

    Started from ``main.lifespan`` next to the cron scheduler. ``start()`` is a
    no-op unless ``RETENTION_ENABLED=1``.
    """

    def __init__(self, startup_delay: float = STARTUP_DELAY_SECS):
        self.startup_delay = startup_delay
        self.last_result: dict | None = None
        self._task: asyncio.Task | None = None
        self._stopped = asyncio.Event()

    async def run_once(self) -> dict | None:
        """One sweep, off the event loop. Never raises."""
        try:
            self.last_result = await asyncio.to_thread(purge_expired)
            logger.info("Retention purge: %s", self.last_result)
            return self.last_result
        except Exception:
            logger.exception("Retention purge failed")
            return None

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stopped.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def _run_loop(self) -> None:
        await self._sleep(self.startup_delay)
        while not self._stopped.is_set():
            await self.run_once()
            await self._sleep(policy().interval_hours * 3600)

    def start(self) -> bool:
        pol = policy()
        if not pol.enabled:
            logger.info("Data retention purge is disabled (set RETENTION_ENABLED=1 to enable)")
            return False
        if self._task is not None:
            return True
        logger.info("Data retention purge enabled: %s", asdict(pol))
        self._stopped.clear()
        self._task = asyncio.create_task(self._run_loop())
        return True

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None


# Module-level singleton used by the app lifespan.
retention_task = RetentionTask()
