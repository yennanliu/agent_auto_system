"""SmartRecruiters adapter — discovery only.

Jobs come from the public Posting API
(``api.smartrecruiters.com/v1/companies/<id>/postings``). Applying is NOT
automated yet: the one-click apply page sits behind a DataDome bot wall
("Access is temporarily restricted" for headless browsers), renders its form in
web components, and has no public form-definition API, so questions can't be
planned before the browser opens. Matching jobs are returned under ``manual``
with their ``apply_url`` instead. See doc/job-platforms-automation-plan.md.
"""
from src.automation.tools.ats_apply_tool import Adapter
from src.automation.tools.ats_router import BoardRef

_API = "https://api.smartrecruiters.com/v1/companies"
_PAGE = 100
_MAX_JOBS = 1000


class SmartRecruitersAdapter(Adapter):
    platform = "smartrecruiters"
    label = "SmartRecruiters"
    can_apply = False

    def discover(self, client, ref: BoardRef) -> list[dict]:
        jobs: list[dict] = []
        offset = 0
        while offset < _MAX_JOBS:
            resp = client.get(f"{_API}/{ref.company}/postings",
                              params={"limit": _PAGE, "offset": offset})
            resp.raise_for_status()
            data = resp.json()
            content = data.get("content") or []
            for p in content:
                jid = str(p.get("id") or "")
                if not jid:
                    continue
                loc = p.get("location") or {}
                where = loc.get("fullLocation") or ", ".join(
                    x for x in (loc.get("city"), loc.get("country")) if x)
                if loc.get("remote"):
                    where = f"{where} (Remote)".strip()
                url = f"https://jobs.smartrecruiters.com/{ref.company}/{jid}"
                jobs.append({
                    "platform": self.platform, "token": ref.company, "job_id": jid,
                    "title": (p.get("name") or "").strip()[:200] or jid,
                    "company": ((p.get("company") or {}).get("name") or ref.company)[:120],
                    "location": (where or "")[:160],
                    "url": url, "apply_url": url,
                })
            offset += len(content)
            if not content or offset >= int(data.get("totalFound") or 0):
                break
        if not jobs and offset == 0:
            # The API answers an unknown company with an empty list, not a 404.
            raise RuntimeError("no open postings (unknown company identifier?)")
        return jobs
