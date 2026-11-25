import json
import logging
from datetime import UTC, datetime

from sqlalchemy import text

from src.automation import cancellation

logger = logging.getLogger(__name__)


def append_log(run_id: int, message: str) -> None:
    if not run_id:
        return
    # A cancelled run's worker thread stops here (raises RunCancelled) instead
    # of carrying on in the background. No-op on the event-loop thread.
    cancellation.check(run_id)
    entry = json.dumps({"ts": datetime.now(UTC).strftime("%H:%M:%S"), "msg": message})
    try:
        from src.database import get_engine
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(
                text(
                    "UPDATE run SET log = json_insert(COALESCE(log, '[]'), '$[#]', json(:entry)) "
                    "WHERE id = :run_id"
                ),
                {"entry": entry, "run_id": run_id},
            )
            conn.commit()
    except Exception as exc:
        logger.warning("append_log failed for run_id=%s: %s", run_id, exc)
