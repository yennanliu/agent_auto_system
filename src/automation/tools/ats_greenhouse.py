"""Greenhouse as an ``ats_apply`` adapter — a thin wrapper over
``greenhouse_apply_tool`` so both job types share one implementation.

Ledger: a Greenhouse job is "already applied" if either the ATS ledger or the
``greenhouse_apply`` ledger has it, and a confirmed submission is written to
both, so switching between the two job types never re-applies.
"""
import os

from src.automation.tools import greenhouse_apply_tool as gh
from src.automation.tools.ats_apply_tool import Adapter
from src.automation.tools.ats_common import ledger_entry_blocks, load_ledger
from src.automation.tools.ats_router import BoardRef

_API = "https://boards-api.greenhouse.io/v1/boards"


class GreenhouseAdapter(Adapter):
    platform = "greenhouse"
    label = "Greenhouse"

    def discover(self, client, ref: BoardRef, search: tuple[str, ...] = ()) -> list[dict]:
        resp = client.get(f"{_API}/{ref.company}/jobs")
        if resp.status_code == 404:
            raise RuntimeError("no such Greenhouse board")
        resp.raise_for_status()
        out = []
        for j in resp.json().get("jobs") or []:
            jid = str(j.get("id") or "")
            if not jid:
                continue
            url = gh._HOSTED_JOB.format(token=ref.company, job_id=jid)
            out.append({
                "platform": self.platform, "token": ref.company, "job_id": jid,
                "title": (j.get("title") or "").strip()[:200] or jid,
                "company": (j.get("company_name") or ref.company).strip()[:120],
                "location": ((j.get("location") or {}).get("name") or "").strip()[:160],
                "url": url, "apply_url": gh._EMBED_APP.format(token=ref.company, job_id=jid),
            })
        return out

    def load_form(self, client, job: dict):
        return gh.fetch_questions(client, job["token"], job["job_id"])

    def plan(self, form, job, profile, answer_fn):
        return gh.plan_answers(form, profile, answer_fn)

    def apply(self, page, job, answers, dry_run, log):
        entry = gh._apply_to_job(page, job, answers, dry_run, log)
        entry["platform"] = self.platform
        return entry

    # Shared with the greenhouse_apply job type's own ledger.
    def _gh_ledger_path(self) -> str:
        return os.getenv("GREENHOUSE_LEDGER", gh.DEFAULT_LEDGER_PATH)

    def ledger_status(self, job, ledger) -> str | None:
        return (super().ledger_status(job, ledger)
                or ("applied" if self._gh_entry_blocks(job) else None))

    def _gh_entry_blocks(self, job) -> bool:
        # A live "claimed" entry (greenhouse_apply is filling this form now)
        # blocks too; a stale claim left by a crashed run does not.
        entry = load_ledger(self._gh_ledger_path()).get(gh._ledger_key(job))
        return entry is not None and ledger_entry_blocks(entry)

    def record(self, ledger_path, job, status: str = "applied") -> None:
        super().record(ledger_path, job, status)
        # greenhouse_apply's ledger has no status: any entry blocks a resend,
        # which is what a pending (maybe-received) submission needs too.
        gh.record_applied(self._gh_ledger_path(), job)
