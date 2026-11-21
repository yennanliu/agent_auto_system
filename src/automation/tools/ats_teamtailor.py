"""Teamtailor adapter (``<company>.teamtailor.com``, often behind a custom domain).

  * Discover: the career site's JSON Feed ``<company>.teamtailor.com/jobs.json``
    (``items[]``: ``url`` = ``…/jobs/<id>-<slug>``, ``title``, and a schema.org
    ``_jobposting`` with ``jobLocation`` / ``jobLocationType``).
  * Form: ``GET <job url>/applications/new`` returns the application form as
    server-rendered HTML, so it is planned without a browser:
      - built-ins ``candidate[first_name|last_name|email|phone]``, the résumé
        upload (``forms--inputs--upload``, ``…-required-value``), the cover
        letter textarea and the consent boxes; a field is required when its
        label says "Required";
      - questions: ``[data-question-mandatory]`` blocks, each with a hidden
        ``candidate[answers_attributes][N][question_id]`` and a
        ``forms--inputs--<kind>`` control: ``text`` (``[text]`` / ``[number]``),
        ``boolean`` (a Yes/No radio pair), ``choice`` (radios, or checkboxes
        when ``data-question-multiple-choice="true"``). Others (video, upload,
        range, date) are never guessed.
  * Apply: fill by name; each question is found in the page by its
    ``question_id`` (the ``[N]`` index is not trusted). The résumé goes into the
    upload component's file input. Only the REQUIRED consent box is ticked.
    The form's hidden ``verify_token`` / ``ctoken`` are set by the page's own
    script, as for any visitor; nothing is solved or bypassed.

Verified live as a dry run (Anyfin, 2026-11-21): planned over HTTP, filled,
Submit found and not clicked. The confirmation after Submit is not yet seen
live; its markers are below.
"""
import json
import re

from src.automation.tools.ats_apply_tool import (
    Adapter,
    entry_for,
    open_form,
    submit_and_confirm,
    tick,
)
from src.automation.tools.ats_common import (
    Question,
    answer_question,
    first_visible,
    match_option,
    parse_amount,
    standard_value,
    with_job_location,
)
from src.automation.tools.ats_router import BoardRef

SUBMIT_SELECTORS = ["input[type=submit][name=commit]", "button[type=submit]:has-text('Submit application')"]
SUCCESS_TEXTS = ("Thank you for applying", "Thanks for applying", "Thank you for your application",
                 "application has been sent", "application has been submitted", "We have received your application",
                 "Your application has been received")
ERROR_SELECTOR = "[role='alert'], .field_with_errors, [class*='error']"
COOKIE_DECLINE = ["button:has-text('Decline all')", "button:has-text('Reject all')",
                  "button:has-text('Only necessary')", "button:has-text('Decline')"]
_JOB_RE = re.compile(r"/jobs/(\d+)(?:-[^/?#]*)?/?$")


def _label_text(el) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip() if el else ""


def _clean(label: str) -> str:
    """'Do you have a work permit? * Required' → 'Do you have a work permit?' — also
    when a description follows ('Background check? * Required Due to …')."""
    return re.sub(r"\s+", " ", re.sub(r"\s*\*\s*Required\b", "", label)).strip(" *")


class TeamtailorAdapter(Adapter):
    platform = "teamtailor"
    label = "Teamtailor"

    def discover(self, client, ref: BoardRef, search: tuple[str, ...] = ()) -> list[dict]:
        resp = client.get(f"https://{ref.company}.teamtailor.com/jobs.json")
        if resp.status_code == 404:
            raise RuntimeError("no such Teamtailor company")
        resp.raise_for_status()
        data = resp.json() or {}
        jobs = []
        for it in data.get("items") or []:
            url = (it.get("url") or "").split("?")[0].rstrip("/")
            m = _JOB_RE.search(url)
            if not m:
                continue
            jp = it.get("_jobposting") or {}
            if isinstance(jp, str):
                try:
                    jp = json.loads(jp)
                except ValueError:
                    jp = {}
            jobs.append({
                "platform": self.platform, "token": ref.company, "job_id": m.group(1),
                "title": (it.get("title") or jp.get("title") or "").strip()[:200] or m.group(1),
                "company": ((jp.get("hiringOrganization") or {}).get("name") or data.get("title") or ref.company)[:120],
                "location": posting_location(jp)[:160],
                "url": url, "apply_url": url,
            })
        return jobs

    def load_form(self, client, job: dict) -> dict:
        resp = client.get(f"{job['url']}/applications/new")
        resp.raise_for_status()
        form = parse_form(resp.text)
        if not form["fields"] and not form["questions"]:
            raise RuntimeError("no application form (closed, or the employer applies elsewhere)")
        return form

    def plan(self, form: dict, job: dict, profile: dict, answer_fn):
        profile = with_job_location(profile, job.get("location"), job.get("company"))
        answers: list[dict] = []
        unanswered: list[str] = []
        for name, f in form["fields"].items():
            std = {"candidate[first_name]": "first_name", "candidate[last_name]": "last_name",
                   "candidate[email]": "email", "candidate[phone]": "phone"}.get(name)
            if std:
                value = standard_value(std, profile)
                if value:
                    answers.append({"field": name, "kind": "text", "value": value, "label": f["label"]})
                elif f["required"]:
                    unanswered.append(f["label"])
        if form.get("resume_required") or form.get("resume"):
            resume = standard_value("resume", profile)
            if resume:
                answers.append({"field": "resume", "kind": "file", "value": resume, "label": "Resume"})
            elif form.get("resume_required"):
                unanswered.append("Resume")
        letter = (profile.get("cover_letter_text") or "").strip()
        if letter and form.get("cover_letter"):
            answers.append({"field": form["cover_letter"], "kind": "text", "value": letter[:5000],
                            "label": "Cover letter"})
        for q in form["questions"]:
            label, kind, req = q["label"], q["kind"], q["required"]
            ans = None
            if kind == "boolean":
                ans = answer_question(Question(q["id"], label, "select", req, ["Yes", "No"]), profile, answer_fn)
            elif kind == "choice":
                qkind = "multiselect" if q["multiple"] else "select"
                ans = answer_question(Question(q["id"], label, qkind, req, q["options"]), profile, answer_fn)
            elif kind == "text":
                ans = answer_question(Question(q["id"], label, "text", req, []), profile, answer_fn)
                if ans and q.get("number"):
                    # A [number] field takes just the amount, its value kept:
                    # "70,000" → 70000, never 70.000 (PR #126 review). An unclear
                    # format leaves the question unanswered.
                    amount = parse_amount(str(ans["value"]))
                    ans = {**ans, "value": amount} if amount is not None else None
            if ans is not None and ans.get("value") not in (None, "", []):
                answers.append({"field": q["id"], "kind": f"q_{kind}", "value": ans["value"], "label": label,
                                "multiple": q["multiple"], "number": q.get("number", False)})
            elif req:
                unanswered.append(label or q["id"])
        return answers, unanswered

    def apply(self, page, job: dict, answers: list[dict], dry_run: bool, log) -> dict:
        entry = entry_for(job, answers)
        if not open_form(page, f"{job['url']}/applications/new", "[name='candidate[first_name]']"):
            entry.update(status="failed", submitted=False, reason="application form did not load")
            return entry
        dismiss_cookie_banner(page)
        failed = fill(page, answers, log)
        failed += tick_required_consent(page)
        if failed:
            entry.update(status="failed", submitted=False,
                         reason="could not fill: " + "; ".join(f[:60] for f in failed[:3]))
            return entry
        return submit_and_confirm(
            page, job, entry, dry_run=dry_run, label="Teamtailor",
            submit_selectors=SUBMIT_SELECTORS, success_texts=SUCCESS_TEXTS,
            error_selector=ERROR_SELECTOR, log=log)


def posting_location(jp: dict) -> str:
    """'Stockholm, Sweden' from a schema.org JobPosting; '(Remote)' when it says so."""
    places = jp.get("jobLocation") or []
    places = places if isinstance(places, list) else [places]
    parts = []
    for p in places:
        a = (p or {}).get("address") or {}
        where = ", ".join(x for x in (a.get("addressLocality"), a.get("addressRegion")) if x)
        if where and where not in parts:
            parts.append(where)
    where = "; ".join(parts)
    if str(jp.get("jobLocationType") or "").upper() == "TELECOMMUTE":
        where = f"{where} (Remote)".strip()
    return where


def parse_form(html: str) -> dict:
    """The application form (built-ins + questions) from ``/applications/new``."""
    from bs4 import BeautifulSoup
    s = BeautifulSoup(html, "html.parser")
    fields: dict[str, dict] = {}
    for name in ("candidate[first_name]", "candidate[last_name]", "candidate[email]", "candidate[phone]"):
        el = s.select_one(f"[name='{name}']:not([type=hidden])")
        if el is None:
            continue
        lab = s.select_one(f"label[for='{el.get('id')}']") if el.get("id") else None
        text = _label_text(lab)
        fields[name] = {"label": _clean(text) or name, "required": "Required" in text or el.has_attr("required")}
    upload = s.select_one("[data-controller~='forms--inputs--upload']")
    cover = s.select_one("textarea[name$='[cover_letter]']")
    questions = []
    for block in s.select("[data-question-mandatory]"):
        qid_el = block.select_one("input[name$='[question_id]']")
        ctl = block.select_one("[data-controller^='forms--inputs--']")
        if qid_el is None or ctl is None:
            continue
        kind = ctl["data-controller"].split()[0].replace("forms--inputs--", "")
        lab = block.select_one("legend") or block.select_one("label")
        options = [_label_text(o) for o in block.select("label[data-label-value], label[for*='_choice']")
                   if _label_text(o)] if kind == "choice" else []
        questions.append({
            "id": qid_el.get("value", ""), "kind": kind, "label": _clean(_label_text(lab)),
            "required": block.get("data-question-mandatory") == "true",
            "multiple": block.get("data-question-multiple-choice") == "true",
            "number": block.select_one("input[name$='[number]']") is not None,
            "options": list(dict.fromkeys(options)),
        })
    return {"fields": fields,
            "resume": upload is not None,
            "resume_required": bool(upload and upload.get("data-forms--inputs--upload-required-value") == "true"),
            "cover_letter": cover.get("name") if cover is not None else "",
            "questions": questions}


def dismiss_cookie_banner(page) -> None:
    for _ in range(5):
        btn = first_visible(page, COOKIE_DECLINE)
        if btn is not None:
            try:
                btn.click(timeout=3000)
            except Exception:  # noqa: BLE001
                pass
            return
        page.wait_for_timeout(400)


def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _prefix(page, question_id: str) -> str | None:
    """``candidate[answers_attributes][N]`` of the question with this id."""
    el = page.locator(f"input[type=hidden][name$='[question_id]'][value={_q(question_id)}]")
    if el.count() != 1:
        return None
    return el.first.get_attribute("name")[: -len("[question_id]")]


def _choose(page, name: str, value: str) -> None:
    boxes = page.locator(f"input[name={_q(name)}]")
    labels = [(boxes.nth(i).evaluate("e => (e.labels && e.labels[0] ? e.labels[0].innerText : '')") or "").strip()
              for i in range(boxes.count())]
    pick = value if value in labels else match_option(labels, value)
    if pick is None:
        raise LookupError(f"no option {value!r} in {labels[:6]}")
    tick(boxes.nth(labels.index(pick)))


def fill(page, answers: list[dict], log) -> list[str]:
    failed: list[str] = []
    for a in answers:
        field, kind, value, label = a["field"], a["kind"], a["value"], a["label"]
        try:
            if kind == "file":
                # The upload component (Dropzone) keeps its own file input.
                box = page.locator("[data-controller~='forms--inputs--upload'] input[type=file], "
                                   "input[type=file].dz-hidden-input").first
                box.set_input_files(value, timeout=15000)
                page.wait_for_timeout(3000)
            elif kind == "text":
                page.locator(f"[name={_q(field)}]").first.fill(str(value))
            else:
                prefix = _prefix(page, field)
                if prefix is None:
                    raise LookupError("question not found on the form")
                if kind == "q_text":
                    sub = "[number]" if a.get("number") else "[text]"
                    page.locator(f"[name={_q(prefix + sub)}]").first.fill(str(value))
                elif kind == "q_boolean":
                    _choose(page, prefix + "[boolean]", str(value))
                elif kind == "q_choice" and a.get("multiple"):
                    for v in value if isinstance(value, list) else [value]:
                        _choose(page, prefix + "[choice_ids][]", str(v))
                elif kind == "q_choice":
                    _choose(page, prefix + "[choice]", str(value[0] if isinstance(value, list) else value))
        except Exception as exc:  # noqa: BLE001 — reported; the job is not sent
            log(f"⚠ could not fill '{label[:60]}' ({type(exc).__name__}: {str(exc)[:80]})")
            failed.append(label)
    return failed


def tick_required_consent(page) -> list[str]:
    """Only ``candidate[consent_given]`` (the privacy notice the form requires);
    'contact me about future jobs' / SMS consent are left alone."""
    box = page.locator("input[type=checkbox][name='candidate[consent_given]']")
    if box.count() == 0:
        return []
    try:
        if not box.first.is_checked():
            tick(box.first)
        return []
    except Exception:  # noqa: BLE001
        return ["required consent box"]
