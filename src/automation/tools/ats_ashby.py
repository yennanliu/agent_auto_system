"""Ashby adapter (jobs.ashbyhq.com).

  * Discover: public Job Board API
    ``api.ashbyhq.com/posting-api/job-board/<jobs page name>``.
  * Form: the hosted page's own public GraphQL query (``ApiJobPosting``) returns
    every field with its type, required flag and options, so the form is planned
    without a browser. Optional EEO surveys are left blank.
  * Apply: each field lives in ``.ashby-application-form-field-entry
    [data-field-path=<path>]`` (checkbox groups in a ``fieldset`` labelled for the
    path). Yes/No questions are ``button[data-option=yes|no]``; location is a
    combobox; dates are a react-datepicker text box (MM/DD/YYYY).
"""
import re

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
    call_answer_fn,
    location_parts,
    match_option,
    rule_answer,
    standard_for_label,
    standard_value,
    start_date_from_notice,
    with_job_location,
)
from src.automation.tools.ats_router import BoardRef

_BOARD_API = "https://api.ashbyhq.com/posting-api/job-board"
_GRAPHQL = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting"
_JOBS = "https://jobs.ashbyhq.com"
_QUERY = (
    "query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) {"
    " jobPosting(organizationHostedJobsPageName: $organizationHostedJobsPageName,"
    " jobPostingId: $jobPostingId) { id title locationName"
    " applicationForm { sections { fieldEntries { field isRequired } } } } }"
)

SUBMIT_SELECTORS = ["button.ashby-application-form-submit-button",
                    "button:has-text('Submit Application')", "button:has-text('Submit application')"]
SUCCESS_TEXTS = ("Thank you for applying", "Your application was successfully submitted",
                 "Application submitted", "application has been submitted")
VERIFY_TEXTS = ("verification code", "verify your email")
ERROR_SELECTOR = ".ashby-application-form-errors, [class*='_error_'], [role='alert']"

_TEXT_TYPES = {"String", "Email", "Phone", "LongText", "Number", "Url", "SocialLink"}


class AshbyAdapter(Adapter):
    platform = "ashby"
    label = "Ashby"

    def discover(self, client, ref: BoardRef, search: tuple[str, ...] = ()) -> list[dict]:
        resp = client.get(f"{_BOARD_API}/{ref.company}", params={"includeCompensation": "false"})
        if resp.status_code == 404:
            raise RuntimeError("no such Ashby job board")
        resp.raise_for_status()
        jobs = []
        for j in resp.json().get("jobs") or []:
            jid = str(j.get("id") or "")
            if not jid or j.get("isListed") is False:
                continue
            locs = [j.get("location") or ""] + [
                (s.get("location") if isinstance(s, dict) else str(s)) or ""
                for s in j.get("secondaryLocations") or []]
            where = "; ".join(x for x in locs if x)
            if j.get("isRemote") and "remote" not in where.lower():
                where = f"{where} (Remote)".strip()
            base = f"{_JOBS}/{ref.company}/{jid}"
            jobs.append({
                "platform": self.platform, "token": ref.company, "job_id": jid.lower(),
                "title": (j.get("title") or "").strip()[:200] or jid,
                "company": ref.company, "location": where[:160],
                "url": j.get("jobUrl") or base,
                "apply_url": j.get("applyUrl") or f"{base}/application",
            })
        return jobs

    def load_form(self, client, job: dict) -> list[dict]:
        resp = client.post(_GRAPHQL, json={
            "operationName": "ApiJobPosting",
            "variables": {"organizationHostedJobsPageName": job["token"], "jobPostingId": job["job_id"]},
            "query": _QUERY,
        })
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            raise RuntimeError(str(data["errors"][0].get("message", "GraphQL error"))[:120])
        posting = (data.get("data") or {}).get("jobPosting")
        if not posting:
            raise RuntimeError("job posting not found (closed?)")
        fields = []
        for section in (posting.get("applicationForm") or {}).get("sections") or []:
            for fe in section.get("fieldEntries") or []:
                f = fe.get("field") or {}
                if f.get("isDeactivated"):
                    continue
                fields.append({
                    "path": f.get("path") or "", "label": (f.get("title") or "").strip(),
                    "type": f.get("type") or "", "required": bool(fe.get("isRequired")),
                    "options": [str(v.get("label")) for v in f.get("selectableValues") or []
                                if v.get("label")],
                })
        return fields

    def plan(self, form: list[dict], job: dict, profile: dict, answer_fn):
        profile = with_job_location(profile, job.get("location"), job.get("company"))
        answers: list[dict] = []
        unanswered: list[str] = []
        for f in form:
            path, label, typ, req, opts = f["path"], f["label"], f["type"], f["required"], f["options"]
            ans = None
            std = {"_systemfield_name": "full_name", "_systemfield_email": "email",
                   "_systemfield_phone": "phone"}.get(path) \
                or {"Email": "email", "Phone": "phone"}.get(typ) \
                or (standard_for_label(label) if typ in _TEXT_TYPES else None)
            if path == "_systemfield_resume" or (typ == "File" and re.search(r"resume|\bcv\b", label, re.I)):
                val = standard_value("resume", profile)
                ans = {"field": path, "kind": "file", "value": val, "label": label} if val else None
            elif typ == "File":
                if re.search(r"cover letter", label, re.I) and standard_value("cover_letter_file", profile):
                    ans = {"field": path, "kind": "file",
                           "value": standard_value("cover_letter_file", profile), "label": label}
            elif typ == "Location" or path == "_systemfield_location":
                val = standard_value("location", profile)
                ans = {"field": path, "kind": "location", "value": val, "label": label} if val else None
            elif std:
                val = standard_value(std, profile)
                ans = {"field": path, "kind": "text", "value": val, "label": label} if val else None
            elif typ == "Date":
                ans = _date_answer(path, label, req, profile, answer_fn)
            elif typ == "Boolean":
                ans = answer_question(Question(path, label, "select", req, ["Yes", "No"]), profile, answer_fn)
                if ans:
                    ans["kind"] = "yesno"
            elif typ == "ValueSelect":
                ans = answer_question(Question(path, label, "select", req, opts), profile, answer_fn)
            elif typ == "MultiValueSelect":
                ans = answer_question(Question(path, label, "multiselect", req, opts), profile, answer_fn)
            elif typ in _TEXT_TYPES:
                ans = answer_question(Question(path, label, "text", req, []), profile, answer_fn)
            if ans is not None and ans.get("value") not in (None, "", []):
                answers.append(ans)
            elif req:
                unanswered.append(label or path)
        return answers, unanswered

    def apply(self, page, job: dict, answers: list[dict], dry_run: bool, log) -> dict:
        entry = entry_for(job, answers)
        if not open_form(page, job["apply_url"], ".ashby-application-form-field-entry"):
            entry.update(status="failed", submitted=False,
                         reason="application form did not load (job closed?)")
            return entry
        failed = fill(page, answers, log)
        if failed:
            entry.update(status="failed", submitted=False,
                         reason="could not fill: " + "; ".join(f[:60] for f in failed[:3]))
            return entry
        return submit_and_confirm(
            page, job, entry, dry_run=dry_run, label="Ashby",
            submit_selectors=SUBMIT_SELECTORS, success_texts=SUCCESS_TEXTS,
            verify_texts=VERIFY_TEXTS, error_selector=ERROR_SELECTOR, log=log)


def _date_answer(path, label, required, profile, answer_fn) -> dict | None:
    """A date question ("When can you start?"): an explicit extra answer that is
    already a date wins; otherwise the notice period is turned into a date."""
    raw = rule_answer(label, profile)
    if isinstance(raw, str) and re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", raw.strip()):
        return {"field": path, "kind": "date", "value": raw.strip(), "label": label}
    if isinstance(raw, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw.strip()):
        y, m, d = raw.strip().split("-")
        return {"field": path, "kind": "date", "value": f"{m}/{d}/{y}", "label": label}
    if re.search(r"start|join|availab|notice", label, re.I):
        when = start_date_from_notice(raw if isinstance(raw, str) else profile.get("notice_period", ""))
        if when:
            return {"field": path, "kind": "date", "value": when.strftime("%m/%d/%Y"), "label": label}
    if required and answer_fn is not None:
        txt = call_answer_fn(answer_fn, label, "text", [])
        if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", txt):
            return {"field": path, "kind": "date", "value": txt, "label": label}
    return None


def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _container(page, path: str):
    return page.locator(f".ashby-application-form-field-entry[data-field-path={_q(path)}], "
                        f"fieldset:has(label[for={_q(path)}])").first


def _check_option(scope, text: str) -> None:
    """Tick the checkbox / radio in ``scope`` whose label best matches ``text``."""
    labels = scope.locator("label")
    texts = [t.strip() for t in labels.all_inner_texts()]
    pick = match_option([t for t in texts if t], text)
    if pick is None:
        raise LookupError(f"no option {text!r}")
    label = labels.nth(texts.index(pick))
    target = label.get_attribute("for")
    if target:
        tick(scope.locator(f"[id={_q(target)}]"))
    else:
        label.click(timeout=5000)


def fill(page, answers: list[dict], log) -> list[str]:
    failed: list[str] = []
    for a in answers:
        path, kind, value, label = a["field"], a["kind"], a["value"], a["label"]
        try:
            box = _container(page, path)
            if box.count() == 0:
                failed.append(label)
                continue
            if kind == "file":
                box.locator("input[type='file']").first.set_input_files(value, timeout=15000)
                page.wait_for_timeout(2500)
            elif kind == "yesno":
                box.locator(f"button[data-option={_q(str(value).lower())}]").first.click(timeout=5000)
            elif kind == "location":
                city = (location_parts(value) or [value])[0]
                if not pick_autocomplete(page, box.locator("input").first, city, "[role='option']",
                                         want=value, also=(city,)):
                    failed.append(label)
            elif kind == "date":
                el = box.locator("input").first
                el.click(timeout=5000)
                el.fill(str(value))
                el.press("Enter")
                page.keyboard.press("Escape")
            elif kind in ("select", "multiselect"):
                values = value if isinstance(value, list) else [value]
                if box.locator("input[type='checkbox'], input[type='radio']").count():
                    for v in values:
                        _check_option(box, str(v))
                else:  # long option lists render as a combobox
                    combo = box.locator("input").first
                    for v in values:
                        if not pick_autocomplete(page, combo, str(v)[:40], "[role='option']", want=str(v)):
                            raise LookupError(f"no option {v!r}")
            else:
                el = box.locator("input:not([type='file']):not([type='checkbox']), textarea").first
                el.fill(str(value), timeout=5000)
        except Exception as exc:  # noqa: BLE001
            log(f"⚠ could not fill '{label[:60]}' ({type(exc).__name__}: {str(exc)[:80]})")
            failed.append(label)
    return failed
