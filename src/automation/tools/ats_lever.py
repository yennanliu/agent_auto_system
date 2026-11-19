"""Lever adapter (jobs.lever.co).

  * Discover: public Postings API ``api.lever.co/v0/postings/<site>?mode=json``
    (``api.eu.lever.co`` for EU sites).
  * Form: the apply page is server-rendered, so its questions are read from the
    HTML with no browser — every question is an ``li.application-question``
    whose label ends in ✱ when required. Radio / checkbox values are the option
    texts; custom questions are named ``cards[<card id>][field<n>]``.
  * Apply: resume first (Lever parses it and may overwrite text fields), then
    every planned field by ``name``; the location box is an autocomplete.
    Confirmation: the ``/thanks`` page.
"""
import re

from bs4 import BeautifulSoup

from src.automation.tools.ats_apply_tool import (
    Adapter,
    entry_for,
    open_form,
    pick_autocomplete,
    submit_and_confirm,
    tick,
)
from src.automation.tools.ats_common import (
    Question,
    answer_question,
    decline_option,
    location_parts,
    standard_value,
    with_job_location,
)
from src.automation.tools.ats_router import BoardRef

_API = {"": "https://api.lever.co/v0/postings", "eu": "https://api.eu.lever.co/v0/postings"}
_JOBS = {"": "https://jobs.lever.co", "eu": "https://jobs.eu.lever.co"}

# Lever's built-in contact fields → our canonical profile fields.
_STANDARD = {
    "name": "full_name", "email": "email", "phone": "phone", "org": "current_company",
    "urls[LinkedIn]": "linkedin", "urls[GitHub]": "github", "urls[Portfolio]": "website",
    "urls[Other]": "website", "comments": "cover_letter_text",
}

SUBMIT_SELECTORS = ["#btn-submit", "button:has-text('Submit application')",
                    "button[type='submit']:has-text('Submit')"]
SUCCESS_TEXTS = ("Application submitted", "Thanks for applying", "Thank you for applying")
ERROR_SELECTOR = ".application-error, .error-message, [data-qa='error-message']"


def _label(li) -> tuple[str, bool]:
    lab = li.select_one(".application-label")
    text = lab.get_text(" ", strip=True) if lab else ""
    required = "✱" in text
    return re.sub(r"\s*✱\s*", " ", text).strip(), required


def parse_form(html: str) -> list[dict]:
    """Every question on a Lever apply page as ``{name, label, required,
    control, options}`` with control one of text / textarea / select / radio /
    checkbox / file / location."""
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form", id="application-form") or soup.find("form")
    out: list[dict] = []
    if form is None:
        return out
    for li in form.select("li.application-question"):
        label, required = _label(li)
        inputs = [i for i in li.select("input, textarea, select")
                  if (i.get("type") or "").lower() != "hidden" and i.get("name")]
        if not inputs:
            continue
        el = inputs[0]
        name = el["name"]
        if any(i.has_attr("required") for i in inputs):
            required = True
        tag, typ = el.name, (el.get("type") or "").lower()
        options: list[str] = []
        if tag == "select":
            control = "select"
            options = [o.get_text(strip=True) for o in el.find_all("option")
                       if o.get("value") not in (None, "") and o.get_text(strip=True)]
        elif typ in ("radio", "checkbox"):
            control = typ
            options = [i.get("value") or "" for i in inputs if i.get("name") == name]
        elif typ == "file":
            control = "file"
        elif name == "location":
            control = "location"
        elif tag == "textarea":
            control = "textarea"
        else:
            control = "text"
        out.append({"name": name, "label": label, "required": required,
                    "control": control, "options": [o for o in options if o]})
    return out


class LeverAdapter(Adapter):
    platform = "lever"
    label = "Lever"

    def discover(self, client, ref: BoardRef) -> list[dict]:
        resp = client.get(f"{_API[ref.region]}/{ref.company}", params={"mode": "json"})
        if resp.status_code == 404:
            raise RuntimeError("no such Lever site")
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            raise RuntimeError(f"unexpected Postings API response ({str(data)[:80]})")
        jobs = []
        for p in data:
            jid = str(p.get("id") or "")
            if not jid:
                continue
            cats = p.get("categories") or {}
            base = f"{_JOBS[ref.region]}/{ref.company}/{jid}"
            jobs.append({
                "platform": self.platform, "token": ref.company, "job_id": jid,
                "region": ref.region,
                "title": (p.get("text") or "").strip()[:200] or jid,
                "company": ref.company,
                "location": (cats.get("location") or ", ".join(cats.get("allLocations") or [])
                             or "").strip()[:160],
                "url": p.get("hostedUrl") or base,
                "apply_url": p.get("applyUrl") or f"{base}/apply",
            })
        return jobs

    def load_form(self, client, job: dict) -> list[dict]:
        resp = client.get(job["apply_url"])
        resp.raise_for_status()
        fields = parse_form(resp.text)
        if not fields:
            raise RuntimeError("no application form on the page (job closed?)")
        return fields

    def plan(self, form: list[dict], job: dict, profile: dict, answer_fn):
        profile = with_job_location(profile, job.get("location"))
        answers: list[dict] = []
        unanswered: list[str] = []
        for f in form:
            name, label, req, control = f["name"], f["label"], f["required"], f["control"]
            ans = None
            if name == "resume":
                path = standard_value("resume", profile)
                ans = {"field": name, "kind": "file", "value": path, "label": label} if path else None
            elif name == "location":
                loc = standard_value("location", profile)
                ans = {"field": name, "kind": "location", "value": loc, "label": label} if loc else None
            elif name in _STANDARD:
                val = standard_value(_STANDARD[name], profile)
                ans = {"field": name, "kind": "text", "value": val, "label": label} if val else None
            elif name.startswith("eeo["):
                # Voluntary self-identification: only a required one is answered,
                # and only with the "decline" option — never a guessed identity.
                if req:
                    pick = decline_option(f["options"])
                    ans = {"field": name, "kind": "select", "value": pick, "label": label} if pick else None
            elif control == "file":
                if name in ("coverLetter", "cover_letter") and standard_value("cover_letter_file", profile):
                    ans = {"field": name, "kind": "file",
                           "value": standard_value("cover_letter_file", profile), "label": label}
            else:
                kind = {"radio": "select", "select": "select", "text": "text",
                        "textarea": "text"}.get(control)
                if control == "checkbox":
                    kind = "multiselect" if len(f["options"]) > 1 else "select"
                if kind:
                    ans = answer_question(Question(name, label, kind, req, f["options"]),
                                          profile, answer_fn)
            if ans is not None:
                ans["control"] = control
                answers.append(ans)
            elif req:
                unanswered.append(label or name)
        # Resume first: Lever parses it and may overwrite fields filled before.
        answers.sort(key=lambda a: a["kind"] != "file")
        return answers, unanswered

    def apply(self, page, job: dict, answers: list[dict], dry_run: bool, log) -> dict:
        entry = entry_for(job, answers)
        if not open_form(page, job["apply_url"], "form#application-form, input[name='name']"):
            entry.update(status="failed", submitted=False,
                         reason="application form did not load (job closed?)")
            return entry
        failed = fill(page, answers, log)
        if failed:
            entry.update(status="failed", submitted=False,
                         reason="could not fill: " + "; ".join(f[:60] for f in failed[:3]))
            return entry
        return submit_and_confirm(
            page, job, entry, dry_run=dry_run, label="Lever",
            submit_selectors=SUBMIT_SELECTORS, success_texts=SUCCESS_TEXTS,
            success_url_parts=("/thanks",), error_selector=ERROR_SELECTOR, log=log)


def _by_name(page, name: str, extra: str = ""):
    return page.locator(f"[name={_css_str(name)}]{extra}")


def _css_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def fill(page, answers: list[dict], log) -> list[str]:
    """Fill every planned answer; returns the labels that could not be set."""
    failed: list[str] = []
    for a in answers:
        name, kind, value, label = a["field"], a["kind"], a["value"], a["label"]
        control = a.get("control", "")
        try:
            if kind == "file":
                el = page.locator(f"input[type='file'][name={_css_str(name)}]").first
                if el.count() == 0:
                    failed.append(label)
                    continue
                el.set_input_files(value, timeout=15000)
                page.wait_for_timeout(3000)  # resume parsing may fill fields
            elif kind == "location":
                box = page.locator("#location-input, input[name='location']").first
                city = (location_parts(value) or [value])[0]
                if not pick_autocomplete(page, box, city, ".dropdown-results .dropdown-location, "
                                         ".dropdown-results > div", want=value):
                    box.fill(value)
                    log(f"⚠ location '{value}': no suggestion picked; typed text kept")
            elif control in ("radio", "checkbox"):
                values = value if isinstance(value, list) else [value]
                for v in values:
                    box = _by_name(page, name, f"[value={_css_str(str(v))}]").first
                    if box.count() == 0:
                        raise LookupError(f"no option {v!r}")
                    tick(box)
            elif control == "select" or kind == "select":
                _by_name(page, name).first.select_option(label=str(value), timeout=5000)
            else:
                el = _by_name(page, name).first
                if el.count() == 0:
                    failed.append(label)
                    continue
                el.fill(str(value), timeout=5000)
        except Exception as exc:  # noqa: BLE001
            log(f"⚠ could not fill '{label[:60]}' ({type(exc).__name__}: {str(exc)[:160]})")
            failed.append(label)
    return failed
