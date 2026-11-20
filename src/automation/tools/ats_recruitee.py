"""Recruitee adapter (``<company>.recruitee.com``, also behind custom domains).

  * Discover: the public careers-site API ``GET <company>.recruitee.com/api/offers/``
    (every published offer, with ``careers_apply_url``).
  * Form: the same API's ``GET /api/offers/<slug>`` describes the whole form, so
    it is planned without a browser:
      - built-ins ``options_cv`` / ``options_cover_letter`` / ``options_phone`` /
        ``options_photo``, each ``required`` / ``optional`` / ``off``;
      - ``open_questions``: ``kind`` (``string``, ``text``, ``boolean``,
        ``single_choice``, ``multi_choice``, ``salary``, ``file``, ``video``,
        ``date``, ``infobox``), ``required``, ``body`` (HTML) and
        ``open_question_options``.
  * Apply: the hosted form names every input after the API (verified live on
    Channable / Effectory, 2026-11-20): ``candidate.name``, ``candidate.email``,
    ``candidate.phone`` (typing "+886…" sets the country picker),
    ``candidate.cv``, ``candidate.coverLetterFile``, and per question
    ``candidate.openQuestionAnswers.<form id>.content`` (text, or a radio per
    option) / ``.flag`` (a Yes/No radio pair). The form's question ids are NOT
    the API's (server-rendered, no API exposes them), so each question is found
    by its text: the hidden ``….openQuestionId`` input's parent starts with the
    question. A question not found exactly fails the job; never a guess.
    Required GDPR boxes are ``candidate.agreements.<n>.consent``. Submit is "Send".

Some employers (bunq) send applicants to their own careers site instead of the
hosted form; that job fails with "form did not load", nothing is guessed.
"""
import html
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
    standard_value,
    with_job_location,
)
from src.automation.tools.ats_router import BoardRef

SUBMIT_SELECTORS = ["form button[type='submit']:has-text('Send')",
                    "button[type='submit']:has-text('Submit application')",
                    "button[type='submit']:has-text('Apply')"]
SUCCESS_TEXTS = ("Thank you for applying", "Thanks for applying", "Thank you for your application",
                 "application has been sent", "application has been submitted",
                 "We have received your application", "Application sent", "Application received")
ERROR_SELECTOR = "[role='alert'], [class*='error'], [id$='-error']"
COOKIE_DECLINE = ["button:has-text('Reject all')", "button:has-text('Decline all')",
                  "button:has-text('Only necessary')", "button:has-text('Use necessary cookies only')"]
# Cookiebot (custom-domain careers sites) may show only "Manage preferences" /
# "Allow all": open the preferences and save with the optional categories off.
# "Allow all" is never clicked.
COOKIEBOT_CUSTOMIZE = "#CybotCookiebotDialogBodyLevelButtonCustomize"
COOKIEBOT_SAVE = ["#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowallSelection",
                  "#CybotCookiebotDialogBodyButtonDecline"]
COOKIEBOT_OPTIONAL = ("#CybotCookiebotDialogBodyLevelButtonPreferencesInline, "
                      "#CybotCookiebotDialogBodyLevelButtonStatisticsInline, "
                      "#CybotCookiebotDialogBodyLevelButtonMarketingInline")
_ON = ("required", "optional")


def plain(body: str) -> str:
    """Question bodies are HTML ("<p>Do you…</p>", "&amp;")."""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", body or ""))).strip()


def api_base(company: str) -> str:
    return f"https://{company}.recruitee.com/api/offers"


class RecruiteeAdapter(Adapter):
    platform = "recruitee"
    label = "Recruitee"

    def discover(self, client, ref: BoardRef, search: tuple[str, ...] = ()) -> list[dict]:
        resp = client.get(f"{api_base(ref.company)}/")
        if resp.status_code == 404:
            raise RuntimeError("no such Recruitee company")
        resp.raise_for_status()
        jobs = []
        for o in resp.json().get("offers") or []:
            slug = str(o.get("slug") or "")
            if not slug or (o.get("status") and o.get("status") != "published"):
                continue
            where = o.get("location") or ", ".join(x for x in (o.get("city"), o.get("country")) if x)
            if o.get("remote"):
                where = f"{where} (Remote)".strip()
            jobs.append({
                "platform": self.platform, "token": ref.company, "job_id": slug,
                "title": (o.get("title") or "").strip()[:200] or slug,
                "company": (o.get("company_name") or ref.company)[:120],
                "location": (where or "")[:160],
                "url": o.get("careers_url") or f"https://{ref.company}.recruitee.com/o/{slug}",
                "apply_url": o.get("careers_apply_url") or f"https://{ref.company}.recruitee.com/o/{slug}/c/new",
            })
        return jobs

    def load_form(self, client, job: dict) -> dict:
        resp = client.get(f"{api_base(job['token'])}/{job['job_id']}")
        resp.raise_for_status()
        offer = resp.json().get("offer") or {}
        if not offer:
            raise RuntimeError("offer not found (closed?)")
        return offer

    def plan(self, form: dict, job: dict, profile: dict, answer_fn):
        profile = with_job_location(profile, job.get("location"), job.get("company"))
        answers: list[dict] = []
        unanswered: list[str] = []

        def want(key, label, kind, value):
            setting = form.get(key) or "off"
            if setting not in _ON:
                return
            if value:
                answers.append({"field": key, "kind": kind, "value": value, "label": label})
            elif setting == "required":
                unanswered.append(label)

        name = f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
        answers.append({"field": "name", "kind": "text", "value": name, "label": "Full name"})
        answers.append({"field": "email", "kind": "text", "value": profile.get("email", ""), "label": "Email"})
        want("options_phone", "Phone number", "phone", standard_value("phone", profile))
        want("options_cv", "CV or resume", "file", standard_value("resume", profile))
        # The cover letter is a FILE here; the run's letter is a .txt it wrote.
        cover = profile.get("cover_letter_path") or ""
        want("options_cover_letter", "Cover letter", "cover_file", cover if form.get("options_cover_letter") == "required" else "")
        if form.get("options_photo") == "required":
            unanswered.append("Photo")  # never guessed

        for q in form.get("open_questions") or []:
            qid, kind, req = str(q.get("id")), q.get("kind") or "", bool(q.get("required"))
            label = plain(q.get("body") or "")
            options = [plain(o.get("body") or "") for o in q.get("open_question_options") or []]
            ans = None
            if kind == "infobox":
                continue
            if kind == "boolean":
                ans = answer_question(Question(qid, label, "select", req, ["Yes", "No"]), profile, answer_fn)
                kind_out = "flag"
            elif kind == "single_choice":
                ans = answer_question(Question(qid, label, "select", req, options), profile, answer_fn)
                kind_out = "choice"
            elif kind == "multi_choice":
                ans = answer_question(Question(qid, label, "multiselect", req, options), profile, answer_fn)
                kind_out = "multi"
            elif kind in ("string", "text", "salary"):
                ans = answer_question(Question(qid, label, "text", req, []), profile, answer_fn)
                kind_out = "content"
            # file / video / date and unknown kinds: never guessed.
            if ans is not None and ans.get("value") not in (None, "", []):
                answers.append({"field": qid, "kind": kind_out, "value": ans["value"], "label": label})
            elif req:
                unanswered.append(label or qid)
        return answers, unanswered

    def apply(self, page, job: dict, answers: list[dict], dry_run: bool, log) -> dict:
        entry = entry_for(job, answers)
        if not open_form(page, job["apply_url"], "input[name='candidate.name']"):
            entry.update(status="failed", submitted=False,
                         reason="application form did not load (closed, or the employer uses "
                                "its own careers site)")
            return entry
        dismiss_cookie_banner(page)
        failed = fill(page, answers, log)
        failed += tick_agreements(page)
        if failed:
            entry.update(status="failed", submitted=False,
                         reason="could not fill: " + "; ".join(f[:60] for f in failed[:3]))
            return entry
        dismiss_cookie_banner(page, wait_s=1.2)  # a late banner would cover Send
        return submit_and_confirm(
            page, job, entry, dry_run=dry_run, label="Recruitee",
            submit_selectors=SUBMIT_SELECTORS, success_texts=SUCCESS_TEXTS,
            error_selector=ERROR_SELECTOR, log=log)


def dismiss_cookie_banner(page, wait_s: float = 2.5) -> None:
    """Refuse optional cookies: a "reject / necessary only" button, or
    Cookiebot's preferences saved with every optional category off. The
    banner can appear late and cover Send, so this runs again before submit."""
    for _ in range(max(1, int(wait_s / 0.4))):
        btn = first_visible(page, COOKIE_DECLINE)
        if btn is not None:
            try:
                btn.click(timeout=3000)
            except Exception:  # noqa: BLE001
                pass
            return
        customize = first_visible(page, [COOKIEBOT_CUSTOMIZE])
        if customize is not None:
            try:
                customize.click(timeout=3000)
                page.wait_for_timeout(600)
                boxes = page.locator(COOKIEBOT_OPTIONAL)
                for i in range(boxes.count()):
                    if boxes.nth(i).is_checked():
                        boxes.nth(i).evaluate("e => e.click()")
                save = first_visible(page, COOKIEBOT_SAVE)
                if save is not None:
                    save.click(timeout=3000)
            except Exception:  # noqa: BLE001
                pass
            return
        page.wait_for_timeout(400)


def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _choose(page, name: str, value: str) -> None:
    """Tick the radio / checkbox named ``name`` whose label is ``value``."""
    boxes = page.locator(f"input[name={_q(name)}]")
    labels = [(boxes.nth(i).evaluate("e => (e.labels && e.labels[0] ? e.labels[0].innerText : e.value || '')")
               or "").strip() for i in range(boxes.count())]
    pick = match_option(labels, value) if value not in labels else value
    if pick is None:
        raise LookupError(f"no option {value!r} in {labels[:6]}")
    tick(boxes.nth(labels.index(pick)))


QUESTIONS_JS = r"""() => Array.from(document.querySelectorAll('input[type=hidden][name$=".openQuestionId"]'))
  .map(h => [h.name.replace(/\.openQuestionId$/, ''), (h.parentElement.innerText || '').split('\n')[0]])"""


def norm(text: str) -> str:
    """Question text as compared between the API and the page."""
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ").replace("*", "")).strip().lower()


def question_prefixes(page) -> dict[str, str]:
    """``{normalised question text: "candidate.openQuestionAnswers.<form id>"}``."""
    return {norm(label): prefix for prefix, label in page.evaluate(QUESTIONS_JS) if norm(label)}


def find_prefix(prefixes: dict[str, str], label: str) -> str | None:
    """The form prefix of the question whose text is ``label``. Equal text, or
    — the page shows only a long body's FIRST paragraph — the API text
    starting with a long (40+ characters) page text. Unique, or None: a short
    "Please describe your experience" never stands in for "…with Go"."""
    want = norm(label)
    if want in prefixes:
        return prefixes[want]
    hits = [p for text, p in prefixes.items() if len(text) >= 40 and want.startswith(text)]
    return hits[0] if len(hits) == 1 else None


def fill(page, answers: list[dict], log) -> list[str]:
    failed: list[str] = []
    prefixes = question_prefixes(page)
    for a in answers:
        field, kind, value, label = a["field"], a["kind"], a["value"], a["label"]
        try:
            if field == "name":
                page.locator("input[name='candidate.name']").fill(value)
            elif field == "email":
                page.locator("input[name='candidate.email']").fill(value)
            elif kind == "phone":
                # The international number sets the country-code picker itself.
                digits = re.sub(r"[^\d+]", "", value)
                page.locator("input[name='candidate.phone']").fill(digits if digits.startswith("+") else value)
            elif kind == "file":
                page.locator("input[type=file][name='candidate.cv']").set_input_files(value, timeout=15000)
                page.wait_for_timeout(1500)
            elif kind == "cover_file":
                page.locator("input[type=file][name='candidate.coverLetterFile']").set_input_files(value, timeout=15000)
            elif kind in ("content", "flag", "choice", "multi"):
                prefix = find_prefix(prefixes, label)
                if prefix is None:
                    raise LookupError("question not found on the form")
                if kind == "content":
                    box = page.locator(f"[name={_q(prefix + '.content')}]").first
                    if box.get_attribute("type") == "number":
                        # A salary box: just the amount ("5,500 EUR / month" → 5500).
                        m = re.search(r"\d[\d,. ]*", str(value))
                        if not m:
                            raise ValueError("no number in the answer")
                        value = re.sub(r"[ ,]", "", m.group(0)).rstrip(".")
                    box.fill(str(value))
                elif kind == "flag":
                    _choose(page, f"{prefix}.flag", str(value))
                elif kind == "choice":
                    _choose(page, f"{prefix}.content", str(value))
                else:
                    for v in value if isinstance(value, list) else [value]:
                        _choose(page, f"{prefix}.multiContent", str(v))
        except Exception as exc:  # noqa: BLE001 — reported, the job is not sent
            log(f"⚠ could not fill '{label[:60]}' ({type(exc).__name__}: {str(exc)[:80]})")
            failed.append(label)
    return failed


def tick_agreements(page) -> list[str]:
    """Tick the REQUIRED consent boxes (privacy policy / data processing).
    Optional ones (e.g. "keep me in your talent pool") are left alone."""
    failed = []
    boxes = page.locator("input[type=checkbox][name^='candidate.agreements'][required]")
    for i in range(boxes.count()):
        try:
            if not boxes.nth(i).is_checked():
                tick(boxes.nth(i))
        except Exception:  # noqa: BLE001
            failed.append("required consent box")
    return failed
