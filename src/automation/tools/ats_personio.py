"""Personio adapter: discovery only.

Jobs come from the public XML feed ``https://<company>.jobs.personio.de/xml``
(some boards live on ``.jobs.personio.com``; both are tried). Each
``<position>`` has ``id``, ``name``, ``office`` (+ ``additionalOffices``),
``department``, ``schedule`` and ``createdAt``. Job descriptions are nested
``<jobDescription><name>`` blocks, so only DIRECT children are read: a
section heading ("DEIN TEAM") is never taken for a job title.

Applying is NOT automated: the hosted form (``/job/<id>/apply``) is simple and
server-rendered, but its Submit stays disabled until Cloudflare Turnstile
issues a token, which it does not do for an automated browser (checked live on
Clark and 1KOMMA5°, 2026-11-21). A bot check is never solved or bypassed, so
matching jobs are listed under ``manual`` with their apply link.
"""
import xml.etree.ElementTree as ET

from src.automation.tools.ats_apply_tool import Adapter
from src.automation.tools.ats_router import BoardRef

TLDS = ("de", "com")


class PersonioAdapter(Adapter):
    platform = "personio"
    label = "Personio"
    can_apply = False
    manual_reason = ("its application form is protected by Cloudflare Turnstile, which is never "
                     "solved automatically — open apply_url to apply by hand")

    def discover(self, client, ref: BoardRef, search: tuple[str, ...] = ()) -> list[dict]:
        # Every domain is tried: a failure on .de (not only a 404) must not stop
        # .com from being asked (PR #123 review). If none answers, a real error
        # wins over "no such company", so a transient failure isn't reported as
        # a missing board.
        errors: list[str] = []
        for tld in TLDS:
            base = f"https://{ref.company}.jobs.personio.{tld}"
            try:
                resp = client.get(f"{base}/xml")
                if resp.status_code == 404:
                    continue
                resp.raise_for_status()
                return parse_feed(resp.content, ref.company, base)
            except Exception as exc:  # noqa: BLE001 — try the next domain
                errors.append(f".{tld}: {exc}")
        if errors:
            raise RuntimeError("Personio feed failed (" + "; ".join(errors) + ")")
        raise RuntimeError("no such Personio company")


def parse_feed(xml: bytes, company: str, base: str) -> list[dict]:
    root = ET.fromstring(xml)
    jobs = []
    for pos in root.findall("position"):
        jid = (pos.findtext("id") or "").strip()
        title = (pos.findtext("name") or "").strip()
        if not jid.isdigit() or not title:
            continue
        offices = [(pos.findtext("office") or "").strip()]
        extra = pos.find("additionalOffices")
        if extra is not None:
            offices += [(o.text or "").strip() for o in extra.findall("office")]
        where = ", ".join(dict.fromkeys(o for o in offices if o))
        url = f"{base}/job/{jid}"
        jobs.append({
            "platform": "personio", "token": company, "job_id": jid, "title": title[:200],
            "company": company, "location": where[:160],
            "url": url, "apply_url": f"{url}/apply",
        })
    return jobs
