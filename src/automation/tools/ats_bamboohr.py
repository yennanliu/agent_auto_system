"""BambooHR adapter: discovery only.

Jobs come from the careers site's public JSON
``https://<company>.bamboohr.com/careers/list`` (``result[]`` with ``id``,
``jobOpeningName``, ``location`` / ``atsLocation``, ``isRemote``). An unknown
company redirects to bamboohr.com instead of answering 404.

Applying is NOT automated: the form at ``/careers/<id>`` is described by
``/careers/<id>/detail`` (``formFields``), but it carries a visible reCAPTCHA v2
"I'm not a robot" checkbox next to Submit (checked live on Scribd,
2026-11-21), and a CAPTCHA is never solved or bypassed. Matching jobs are
listed under ``manual`` with their link.
"""
from src.automation.tools.ats_apply_tool import Adapter
from src.automation.tools.ats_router import BoardRef


class BambooHRAdapter(Adapter):
    platform = "bamboohr"
    label = "BambooHR"
    can_apply = False
    manual_reason = ("its application form has a reCAPTCHA checkbox, which is never solved "
                     "automatically — open apply_url to apply by hand")

    def discover(self, client, ref: BoardRef, search: tuple[str, ...] = ()) -> list[dict]:
        base = f"https://{ref.company}.bamboohr.com/careers"
        resp = client.get(f"{base}/list", headers={"Accept": "application/json"})
        # An unknown company redirects to bamboohr.com (or, followed, answers
        # that page as HTML). Any OTHER failure (5xx, throttling, a Cloudflare
        # error page) is raised as itself: an existing board must never be
        # reported missing because of a transient error (PR #123 review).
        if resp.status_code in (301, 302, 404):
            raise RuntimeError("no such BambooHR company")
        resp.raise_for_status()
        ctype = (resp.headers.get("content-type") or "") if hasattr(resp, "headers") else "application/json"
        if "json" not in ctype:
            raise RuntimeError("no such BambooHR company")
        jobs = []
        for j in (resp.json() or {}).get("result") or []:
            jid = str(j.get("id") or "")
            title = (j.get("jobOpeningName") or "").strip()
            if not jid.isdigit() or not title:
                continue
            loc = j.get("atsLocation") or {}
            if not any(loc.values()):
                loc = j.get("location") or {}
            where = ", ".join(x for x in (loc.get("city"), loc.get("state") or loc.get("province"),
                                          loc.get("country")) if x)
            if j.get("isRemote"):
                where = f"{where} (Remote)".strip()
            url = f"{base}/{jid}"
            jobs.append({
                "platform": "bamboohr", "token": ref.company, "job_id": jid, "title": title[:200],
                "company": ref.company, "location": where[:160], "url": url, "apply_url": url,
            })
        return jobs
