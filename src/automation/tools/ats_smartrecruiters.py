"""SmartRecruiters adapter: discovery, and guest apply in the browser.

Jobs come from the public Posting API
(``api.smartrecruiters.com/v1/companies/<id>/postings``); each posting's detail
gives its ``applyUrl`` (the "one-click" apply page). No account is needed.

The apply page is behind DataDome: headless browsers get "Access is
temporarily restricted", headed real Chrome passes (the runner's default). The
form lives in web components (``spl-*``) with open shadow roots, which
Playwright's CSS locators pierce. There is no public form-definition API (the
page's own ``publication`` JSON lists only the standard field sets, and the
screening questions are rendered into the page), so the adapter plans in the
browser (``plans_in_browser``), as on Workday:

1. **Page 1, standard fields.** The résumé goes into the *Resume* drop zone
   FIRST: SmartRecruiters parses it and overwrites name / e-mail / phone, so
   the profile's values are typed after it. Then first / last name, e-mail
   (twice), the city (an autocomplete fed by its location API: only a
   suggestion whose city matches is picked), the phone (the full
   international number; the country code follows it), LinkedIn / website, and
   the cover letter as the message to the hiring team. *Next*.
2. **Screening questions** (``/screening``, when the job has any): every
   ``sr-question-field-<type>`` (TEXT, SELECT incl. multi-select, RADIO,
   LANGUAGES, INFO) is read (label, required, options) and answered with the
   shared rules. A required question with no confident answer skips the job
   (``unanswered``); nothing is guessed. Diversity questions get their
   "prefer not to answer" option. The required privacy-notice box is ticked.
3. **Submit**, unless dry-run (status ``prepared``, stopped before Submit).
   Counted only on the confirmation page; otherwise ``unconfirmed`` →
   ``pending`` in the ledger, as on every platform.
"""
import json
import re
from collections.abc import Callable

from src.automation.tools.ats_apply_tool import Adapter, entry_for, tick
from src.automation.tools.ats_common import (
    DEMOGRAPHIC_RE,
    Question,
    answer_question,
    decline_option,
    location_parts,
    noop,
    standard_value,
    url_path_has,
    visible_text,
    with_job_location,
)
from src.automation.tools.ats_router import BoardRef

_API = "https://api.smartrecruiters.com/v1/companies"
_PAGE = 100
_MAX_JOBS = 1000
BLOCKED_TEXTS = ("Access is temporarily restricted", "temporarily restricted")
# DataDome's interactive check ("Verification Required" + a slider). Never
# solved by the automation: a person may complete it in a headed browser
# when the run allows a wait (ATS_HUMAN_CHECK_WAIT seconds).
HUMAN_CHECK_TEXTS = ("Verification Required", "Slide right to secure your access")
SUCCESS_TEXTS = ("Thank you for applying", "Thanks for applying", "application has been sent",
                 "application has been submitted", "application was sent", "successfully applied",
                 "We have received your application", "Application submitted")
SUCCESS_PATHS = ("confirmation", "success", "thank-you", "thankyou")
CONSENT_RE = re.compile(r"privacy|consent|agree|acknowledge|terms", re.I)


class SmartRecruitersAdapter(Adapter):
    platform = "smartrecruiters"
    label = "SmartRecruiters"
    plans_in_browser = True  # screening questions only exist in the page

    def discover(self, client, ref: BoardRef, search: tuple[str, ...] = ()) -> list[dict]:
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

    # ── apply (in the browser) ───────────────────────────────────────────────

    def apply_url(self, job: dict) -> str:
        """The one-click apply page: the posting detail's ``applyUrl``."""
        import httpx
        try:
            resp = httpx.get(f"{_API}/{job['token']}/postings/{job['job_id']}", timeout=25)
            resp.raise_for_status()
            url = resp.json().get("applyUrl") or ""
        except Exception:  # noqa: BLE001 — the job page's own "?oga=true" opens the same form
            url = ""
        return url or f"{job['url']}?oga=true"

    def apply_live(self, page, job: dict, profile: dict, answer_fn, dry_run: bool,
                   log: Callable = noop) -> dict:
        jid = f"{job['platform']}:{job['token']}/{job['job_id']}"
        entry = entry_for(job, [])
        profile = with_job_location(profile, job.get("location"), job.get("company"))
        form = OneClick(page, log)
        opened = form.open(self.apply_url(job), human_wait_s=human_check_wait())
        if opened != "ok":
            entry.update(submitted=False, **({
                "blocked": {"status": "blocked",
                            "reason": "SmartRecruiters' bot protection blocked the page "
                                      "(run headed: ATS_HEADLESS=0)"},
                "human_check": {"status": "blocked",
                                "reason": "SmartRecruiters asked for a human verification (slider). "
                                          "Run headed with ATS_HUMAN_CHECK_WAIT=300 and complete it "
                                          "in the browser window; it is never solved automatically"},
                "timeout": {"status": "failed", "reason": "the apply form didn't load"},
            }[opened]))
            return entry
        missing = form.fill_standard(profile)
        entry["answers"].extend(form.answers)
        if missing:
            entry.update(status="skipped", submitted=False, unanswered=missing,
                         reason="missing profile field(s) the form requires: " + ", ".join(missing))
            return entry
        step = form.next()
        if step == "rejected":
            entry.update(status="failed", submitted=False,
                         reason="page 1 rejected: " + (" | ".join(form.errors()) or "no error shown"))
            return entry
        if step == "screening":
            answers, unanswered = form.answer_screening(profile, answer_fn)
            entry["answers"].extend(answers)
            if unanswered:
                entry.update(status="skipped", submitted=False, unanswered=unanswered,
                             reason="unanswered required question(s): "
                                    + "; ".join(u[:70] for u in unanswered[:3]))
                log(f"↷ {jid}: {len(unanswered)} required question(s) we can't answer — next")
                return entry
        entry["answers"].extend({"label": t or "Consent", "value": "checked"} for t in form.tick_consent())
        return form.finish(entry, jid, dry_run)


# ── answering one screening question (pure: unit-tested) ─────────────────────

def plan_question(q: dict, profile: dict, answer_fn=None) -> dict | None:
    """``q`` is ``{type, label, required, options, multiple}`` as read by
    ``QUESTIONS_JS``. Returns ``{"value": str | list}`` or None (no confident
    answer). Diversity questions only ever get the decline option."""
    label, kind, required = q["label"], q["type"], q["required"]
    options = [o for o in q.get("options") or [] if o.strip()]
    if kind in ("INFO", "LANGUAGES"):
        return None  # LANGUAGES is a free-form pair list; left to the user
    if kind in ("SELECT", "RADIO") and DEMOGRAPHIC_RE.search(label):
        pick = decline_option(options)
        return {"value": [pick] if q.get("multiple") else pick} if pick else None
    if kind == "TEXT":
        if re.search(r"self[- ]describe", label, re.I):
            return None  # the free text behind a "self describe" choice
        ans = answer_question(Question(label, label, "text", required, []), profile, answer_fn)
        return {"value": str(ans["value"])} if ans else None
    if kind in ("SELECT", "RADIO"):
        qkind = "multiselect" if q.get("multiple") else "select"
        ans = answer_question(Question(label, label, qkind, required, options), profile, answer_fn)
        return {"value": ans["value"]} if ans else None
    return None


# ── the one-click form (browser) ─────────────────────────────────────────────

QUESTIONS_JS = r"""() => { const out = []; const count = {};
  const walk = (root) => root.querySelectorAll('*').forEach(e => {
    if (e.shadowRoot) walk(e.shadowRoot);
    if (!/^SR-QUESTION-FIELD-/.test(e.tagName)) return;
    const type = e.tagName.slice(18);
    const ordinal = count[type] = (count[type] === undefined ? 0 : count[type] + 1);
    const lines = (e.innerText || '').split('\n').map(x => x.trim()).filter(Boolean);
    const ctl = e.querySelector('[id^=question_]');
    const group = e.querySelector('spl-radio-group');
    const multi = !!e.querySelector('spl-multiselect-autocomplete');
    // spl-* option text is the element's ``label`` PROPERTY (not an attribute).
    const radios = Array.from(e.querySelectorAll('spl-radio')).map(r => (r.label || r.getAttribute('label') || r.innerText || '').trim());
    const checked = Array.from(e.querySelectorAll('spl-radio')).find(r => r.hasAttribute('checked') || r.getAttribute('aria-checked') === 'true');
    let value = '';
    if (ctl && 'value' in ctl) value = ctl.value || '';
    if (multi) value = Array.from(e.querySelectorAll('spl-tag, [data-test*=tag]')).map(t => t.innerText.trim()).join(', ');
    out.push({type: type === 'SELECT' && multi ? 'SELECT' : type, ordinal,
              label: (lines[0] || '').replace(/\s*\*\s*$/, ''),
              required: !!((ctl && ctl.hasAttribute('required')) || (group && group.hasAttribute('required')) ||
                           e.querySelector('[required]')),
              id: ctl ? ctl.id : '', multiple: multi, options: radios, value: checked ? 'checked' : value});
  });
  walk(document); return out; }"""


class OneClick:
    """Drives SmartRecruiters' one-click apply page."""

    def __init__(self, page, log: Callable = noop):
        self.page, self.log = page, log
        self.answers: list[dict] = []

    def _wait(self, test, seconds: float) -> bool:
        for _ in range(int(seconds * 2)):
            try:
                if test():
                    return True
            except Exception:  # noqa: BLE001 — page mid-render
                pass
            self.page.wait_for_timeout(500)
        return False

    def open(self, url: str, human_wait_s: float = 0) -> str:
        """"ok", "blocked" (DataDome wall), "human_check" (DataDome's slider,
        not completed) or "timeout". With ``human_wait_s`` (headed runs only)
        a person may complete the check in the window; the automation waits
        and never interacts with it."""
        p = self.page
        p.goto(url, wait_until="domcontentloaded", timeout=45000)
        form = lambda: p.locator("input#first-name-input").count()  # noqa: E731
        ok = self._wait(lambda: form() or visible_text(p, BLOCKED_TEXTS)
                        or visible_text(p, HUMAN_CHECK_TEXTS), 30)
        if visible_text(p, BLOCKED_TEXTS):
            return "blocked"
        if not form() and visible_text(p, HUMAN_CHECK_TEXTS):
            if human_wait_s <= 0:
                return "human_check"
            self.log(f"⏸ SmartRecruiters asks for a human check: complete it in the browser window "
                     f"(waiting up to {int(human_wait_s)} s)")
            if not self._wait(form, human_wait_s):
                return "human_check"
            ok = True
        if not ok:
            return "timeout"
        p.wait_for_timeout(1500)
        return "ok"

    def errors(self) -> list[str]:
        try:
            texts = self.page.locator("spl-form-field-error, [role=alert], .error").all_inner_texts()
        except Exception:  # noqa: BLE001
            return []
        return [t.strip()[:160] for t in texts if t.strip()][:6]

    def _fill(self, selector: str, value: str, label: str) -> None:
        box = self.page.locator(selector).first
        box.fill(value)
        box.press("Tab")
        self.answers.append({"label": label, "value": value})

    def fill_standard(self, profile: dict) -> list[str]:
        """Page 1. Returns the labels of required fields the profile can't fill."""
        p = self.page
        missing = []
        resume = profile.get("resume_path") or ""
        if resume:
            # The LAST file input is the Resume drop zone; the first one (when
            # shown) is "Easy apply" résumé parsing, which rewrites the form.
            p.locator("input[type=file]").last.set_input_files(resume)
            name = resume.rsplit("/", 1)[-1]
            self._wait(lambda: p.locator(f"[aria-label='Delete file {name}']").count(), 30)
            p.wait_for_timeout(1500)  # the parse fills the form; type over it below
            self.answers.append({"label": "Resume", "value": name})
        for sel, key, label in (("input#first-name-input", "first_name", "First name"),
                                ("input#last-name-input", "last_name", "Last name"),
                                ("input#email-input", "email", "Email"),
                                ("input#confirm-email-input", "email", "Confirm your email")):
            value = standard_value(key, profile)
            if value:
                self._fill(sel, value, label)
            else:
                missing.append(label)
        if not self.pick_city(profile.get("location") or ""):
            missing.append("City (place of residence)")
        phone = re.sub(r"[^\d+]", "", profile.get("phone") or "")
        if phone:
            # The full international number: the country-code picker follows it.
            self._fill("input[type=tel]", phone if phone.startswith("+") else f"+{phone}", "Phone")
        for sel, key, label in (("input#linkedin-input", "linkedin", "LinkedIn"),
                                ("input#website-input", "website", "Website")):
            value = standard_value(key, profile)
            if value and p.locator(sel).count():
                self._fill(sel, value, label)
        letter = (profile.get("cover_letter_text") or "").strip()
        if letter and p.locator("textarea#hiring-manager-message-input").count():
            self._fill("textarea#hiring-manager-message-input", letter[:4000], "Message to the hiring team")
        return missing

    def pick_city(self, location: str) -> bool:
        """Type the city and pick the suggestion naming that city (and, when
        the profile says, its country). Never a stranger's city."""
        p = self.page
        parts = location_parts(location)
        if not parts:
            return False
        box = p.locator("spl-autocomplete[label='City'] input, spl-autocomplete[label='City'] >> input").first
        if not box.count():
            return False
        box.click()
        box.fill("")
        box.press_sequentially(parts[0], delay=60)
        lb = box.get_attribute("ariacontrols") or box.get_attribute("aria-controls")
        opts = p.locator(f"[id={json.dumps(lb)}] spl-select-option") if lb else p.locator("spl-select-option")
        self._wait(lambda: any(option_labels(opts)), 8)
        labels = option_labels(opts)
        city = parts[0].lower()
        same_city = [i for i, t in enumerate(labels) if t and t.split(",")[0].strip().lower() == city]
        country = parts[-1].lower() if len(parts) > 1 else ""
        # With a country in the profile, the suggestion must be in it: never
        # "London, Ontario, Canada" for London, UK (PR review).
        best = [i for i in same_city if t_has(labels[i], country)] if country else same_city
        if not best:
            p.keyboard.press("Escape")
            return False
        opts.nth(best[0]).click()
        p.wait_for_timeout(800)
        self.answers.append({"label": "City", "value": labels[best[0]]})
        return True

    def next(self) -> str:
        """Click Next: "screening", "final" (Submit already shown) or "rejected"."""
        p = self.page
        p.get_by_role("button", name="Next").last.click()
        if self._wait(lambda: "/screening" in (p.url or "")
                      or p.get_by_role("button", name="Submit").count(), 20):
            p.wait_for_timeout(2000)
            return "screening" if "/screening" in (p.url or "") else "final"
        return "rejected"

    def questions(self) -> list[dict]:
        return self.page.evaluate(QUESTIONS_JS)

    def _select_options(self, qid: str):
        p = self.page
        p.locator(f"input#{qid}").first.click()
        p.wait_for_timeout(1200)
        opts = p.locator(f"[id='menu-{qid}'] spl-select-option")
        self._wait(lambda: any(option_labels(opts)), 6)
        return option_labels(opts), opts

    def answer_screening(self, profile: dict, answer_fn=None) -> tuple[list[dict], list[str]]:
        p = self.page
        answers: list[dict] = []
        unanswered: list[str] = []
        for q in self.questions():
            if q["type"] == "INFO" or q["value"]:
                continue
            if q["type"] == "SELECT" and q["id"]:
                labels, opts = self._select_options(q["id"])
                q = {**q, "options": [t for t in labels if t]}
                plan = plan_question(q, profile, answer_fn)
                picks = (plan or {}).get("value")
                picks = picks if isinstance(picks, list) else [picks] if picks else []
                for pick in picks:
                    if pick in labels:
                        opts.nth(labels.index(pick)).click()
                        p.wait_for_timeout(500)
                p.keyboard.press("Escape")
                p.wait_for_timeout(300)
                if not picks:
                    plan = None
            else:
                plan = plan_question(q, profile, answer_fn)
                if plan and q["type"] == "RADIO":
                    field = p.locator("sr-question-field-radio").nth(q["ordinal"])
                    choose_radio(field.locator("spl-radio").nth(q["options"].index(plan["value"])))
                elif plan and q["type"] == "TEXT":
                    box = p.locator(f"input#{q['id']}, textarea#{q['id']}").first
                    box.fill(plan["value"])
                    box.press("Tab")
            if plan:
                value = plan["value"]
                answers.append({"label": q["label"][:80],
                                "value": ", ".join(value) if isinstance(value, list) else value})
            elif q["required"]:
                unanswered.append(q["label"])
        return answers, unanswered

    def tick_consent(self) -> list[str]:
        """Tick the required privacy-notice / consent box(es) next to Submit.
        ``spl-checkbox`` is usually the control itself (role=checkbox,
        aria-checked), like ``spl-radio``; an inner <input> is handled too.
        Only required boxes or ones worded as consent are touched."""
        p = self.page
        ticked = []
        boxes = p.locator("spl-checkbox")
        for i in range(boxes.count()):
            box = boxes.nth(i)
            text = (box.inner_text() or "").strip()
            if box.get_attribute("required") is None and not CONSENT_RE.search(text):
                continue
            inner = box.locator("input[type=checkbox]")
            if inner.count():
                if not inner.first.is_checked():
                    tick(inner.first)
            elif box.get_attribute("aria-checked") != "true":
                box.click()
                if box.get_attribute("aria-checked") != "true":
                    box.evaluate("e => e.click()")
                if box.get_attribute("aria-checked") != "true":
                    raise RuntimeError(f"consent box did not become checked: {text[:60]}")
            ticked.append(text[:80])
        return ticked

    def finish(self, entry: dict, jid: str, dry_run: bool) -> dict:
        p = self.page
        submit = p.get_by_role("button", name="Submit")
        if not submit.count():
            entry.update(status="failed", submitted=False, reason="no Submit button on the form")
            return entry
        if dry_run:
            entry.update(status="prepared", submitted=False, reason="dry_run (stopped before Submit)")
            self.log(f"✓ {jid}: application prepared (dry-run, NOT sent)")
            return entry
        submit.last.click()
        if self._wait(lambda: url_path_has(p.url or "", SUCCESS_PATHS)
                      or visible_text(p, SUCCESS_TEXTS), 30):
            entry.update(status="submitted", submitted=True,
                         confirmation="SmartRecruiters confirmation shown")
            self.log(f"✓ {jid}: submitted AND confirmed")
            return entry
        errs = self.errors()
        entry.update(status="unconfirmed", submitted=False,
                     reason=("form rejected: " + " | ".join(errs)) if errs
                     else "clicked Submit but no confirmation")
        self.log(f"⚠ {jid}: submit unconfirmed — NOT counting as applied")
        return entry


def choose_radio(radio) -> None:
    """``spl-radio`` is itself the control (role=radio, aria-checked); there is
    no inner <input>. Click it and check that it took."""
    radio.click()
    if radio.get_attribute("aria-checked") != "true":
        radio.evaluate("e => e.click()")
    if radio.get_attribute("aria-checked") != "true":
        raise RuntimeError("radio did not become checked")


def human_check_wait() -> float:
    """Seconds to wait for a person to complete DataDome's check: only in a
    headed run (nobody can see a headless window), from ATS_HUMAN_CHECK_WAIT."""
    import os
    if os.getenv("ATS_HEADLESS", os.getenv("GREENHOUSE_HEADLESS", "0")) == "1":
        return 0
    try:
        return max(0.0, min(float(os.getenv("ATS_HUMAN_CHECK_WAIT", "0") or 0), 900.0))
    except ValueError:
        return 0


def option_labels(opts) -> list[str]:
    """Texts of ``spl-select-option`` elements: their ``label`` property (an
    attribute read returns nothing), else their text."""
    try:
        return opts.evaluate_all("els => els.map(e => (e.label || e.getAttribute('label') || "
                                 "e.textContent || '').trim())")
    except Exception:  # noqa: BLE001
        return []


def t_has(label: str, country: str) -> bool:
    """A suggestion like "London, England, United Kingdom" names the country
    (a country name, or a US state code standing for the US)."""
    from src.automation.tools.ats_common import countries_in
    have = countries_in(label)
    want = countries_in(country)
    if want:
        return bool(have & want)
    # A code ("CA", "ON"): only as a whole comma-separated part, never a
    # substring ("ca" in "Jamaica").
    return country.strip().lower() in [p.lower() for p in location_parts(label)]
