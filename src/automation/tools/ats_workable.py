"""Workable adapter (apply.workable.com).

  * Discover: the careers page's own API ``POST apply.workable.com/api/v3/
    accounts/<account>/jobs`` (paginated with ``nextPage``).
  * Form: ``GET apply.workable.com/api/v1/jobs/<shortcode>/form`` lists every
    field (id, label, type, required, options), so the form is planned without a
    browser. Input ids equal the field ids.
  * Apply: text / paragraph by id; ``dropdown`` is a combobox
    (``#input_<id>_input``); ``boolean`` is a radio pair valued true / false;
    the resume input is ``input[type=file][data-ui=resume]``. The page sits
    behind a Cloudflare check and shows a cookie banner, declined first.
"""
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
    first_visible,
    standard_for_label,
    standard_value,
    with_job_location,
)
from src.automation.tools.ats_router import BoardRef

_BASE = "https://apply.workable.com"

# Workable's built-in profile fields → standard_value() names.
_STANDARD_IDS = {"firstname": "first_name", "lastname": "last_name", "email": "email",
                 "phone": "phone", "address": "location", "cover_letter": "cover_letter_text"}

SUBMIT_SELECTORS = ["button[data-ui='apply-button']", "button:has-text('Submit application')"]
SUCCESS_TEXTS = ("Application submitted", "application has been submitted",
                 "Thank you for applying", "Thanks for applying", "received your application")
VERIFY_TEXTS = ("verification code", "verify your email")
ERROR_SELECTOR = "[data-ui='error'], [role='alert'], [class*='error']"
_COOKIE_DECLINE = ["button[data-ui='cookie-consent-decline']", "button:has-text('Decline all')"]


class WorkableAdapter(Adapter):
    platform = "workable"
    label = "Workable"

    def discover(self, client, ref: BoardRef, search: tuple[str, ...] = ()) -> list[dict]:
        company = ref.company
        try:
            acct = client.get(f"{_BASE}/api/v1/accounts/{ref.company}")
            if acct.status_code == 200:
                company = (acct.json().get("name") or company).strip()
        except Exception:  # noqa: BLE001 — the name is cosmetic
            pass
        jobs: list[dict] = []
        body: dict = {}
        for _ in range(20):  # 20 pages is far beyond any real board
            resp = client.post(f"{_BASE}/api/v3/accounts/{ref.company}/jobs", json=body)
            if resp.status_code == 404:
                raise RuntimeError("no such Workable account")
            resp.raise_for_status()
            data = resp.json()
            for j in data.get("results") or []:
                code = str(j.get("shortcode") or "")
                if not code or j.get("state", "published") != "published":
                    continue
                loc = j.get("location") or {}
                where = ", ".join(x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x)
                if j.get("remote") or j.get("workplace") == "remote":
                    where = f"{where} (Remote)".strip()
                url = f"{_BASE}/{ref.company}/j/{code}/"
                jobs.append({
                    "platform": self.platform, "token": ref.company, "job_id": code,
                    "title": (j.get("title") or "").strip()[:200] or code,
                    "company": company[:120], "location": where[:160],
                    "url": url, "apply_url": f"{url}apply/",
                })
            if not data.get("nextPage"):
                break
            body = {"token": data["nextPage"]}
        return jobs

    def load_form(self, client, job: dict) -> list[dict]:
        resp = client.get(f"{_BASE}/api/v1/jobs/{job['job_id']}/form")
        resp.raise_for_status()
        fields = []
        for section in resp.json() or []:
            for f in section.get("fields") or []:
                fields.append({
                    "id": str(f.get("id") or ""), "label": (f.get("label") or "").strip(),
                    "type": f.get("type") or "", "required": bool(f.get("required")),
                    "single": f.get("singleOption", True),
                    "options": [str(o.get("value") or "").strip() for o in f.get("options") or []
                                if str(o.get("value") or "").strip()],
                })
        if not fields:
            raise RuntimeError("empty application form")
        return fields

    def plan(self, form: list[dict], job: dict, profile: dict, answer_fn):
        profile = with_job_location(profile, job.get("location"))
        answers: list[dict] = []
        unanswered: list[str] = []
        for f in form:
            fid, label, typ, req, opts = f["id"], f["label"], f["type"], f["required"], f["options"]
            ans = None
            std = _STANDARD_IDS.get(fid) or (
                standard_for_label(label) if typ in ("text", "paragraph", "email", "phone") else None)
            if fid == "resume" or (typ == "file" and "resume" in label.lower()):
                val = standard_value("resume", profile)
                ans = {"field": fid, "kind": "file", "value": val, "label": label} if val else None
            elif typ == "file":
                pass  # other uploads (avatar, portfolio files) are never guessed
            elif std:
                val = standard_value(std, profile)
                ans = {"field": fid, "kind": "text", "value": val, "label": label} if val else None
            elif typ == "boolean":
                ans = answer_question(Question(fid, label, "select", req, ["Yes", "No"]), profile, answer_fn)
                if ans:
                    ans["kind"] = "boolean"
            elif typ in ("dropdown", "multiple"):
                kind = "select" if f["single"] is not False else "multiselect"
                ans = answer_question(Question(fid, label, kind, req, opts), profile, answer_fn)
            elif typ in ("text", "paragraph", "email", "phone", "number"):
                ans = answer_question(Question(fid, label, "text", req, []), profile, answer_fn)
            # "complex" (education / experience blocks), "date" and unknown types
            # are left unanswered: a required one skips the job.
            if ans is not None and ans.get("value") not in (None, "", []):
                answers.append(ans)
            elif req:
                unanswered.append(label or fid)
        return answers, unanswered

    def apply(self, page, job: dict, answers: list[dict], dry_run: bool, log) -> dict:
        entry = entry_for(job, answers)
        if not open_form(page, job["apply_url"], "#firstname, button[data-ui='apply-button']"):
            entry.update(status="failed", submitted=False,
                         reason="application form did not load (job closed or Cloudflare check)")
            return entry
        dismiss_cookie_banner(page)
        failed = fill(page, answers, log)
        if failed:
            entry.update(status="failed", submitted=False,
                         reason="could not fill: " + "; ".join(f[:60] for f in failed[:3]))
            return entry
        return submit_and_confirm(
            page, job, entry, dry_run=dry_run, label="Workable",
            submit_selectors=SUBMIT_SELECTORS, success_texts=SUCCESS_TEXTS,
            verify_texts=VERIFY_TEXTS,
            error_selector=ERROR_SELECTOR, log=log)


def dismiss_cookie_banner(page) -> None:
    """Decline the cookie banner; its backdrop otherwise swallows every click.
    It renders a moment after the form, so wait for it briefly."""
    for _ in range(8):
        decline = first_visible(page, _COOKIE_DECLINE)
        if decline is not None:
            try:
                decline.click(timeout=3000)
            except Exception:  # noqa: BLE001
                pass
            break
        page.wait_for_timeout(500)
    for _ in range(10):
        if page.locator("[data-ui='backdrop']").count() == 0:
            return
        page.wait_for_timeout(300)


def _field(page, fid: str):
    """A field's input: id = field id on most, name = id on the phone widget."""
    for sel in (f"[id={_q(fid)}]", f"[name={_q(fid)}]", f"[id={_q('input_' + fid)}]"):
        loc = page.locator(sel).first
        if loc.count() > 0:
            return loc
    return None


def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def fill(page, answers: list[dict], log) -> list[str]:
    failed: list[str] = []
    for a in answers:
        fid, kind, value, label = a["field"], a["kind"], a["value"], a["label"]
        try:
            if kind == "file":
                el = page.locator("input[type='file'][data-ui='resume']").first
                if el.count() == 0:
                    failed.append(label)
                    continue
                el.set_input_files(value, timeout=15000)
                page.wait_for_timeout(2500)
            elif kind == "boolean":
                val = "true" if str(value).lower().startswith("y") else "false"
                radio = page.locator(f"input[type='radio'][name={_q(fid)}][value={_q(val)}]").first
                if radio.count() == 0:
                    failed.append(label)
                    continue
                tick(radio)
            elif kind in ("select", "multiselect"):
                combo = page.locator(f"[id={_q(f'input_{fid}_input')}]").first
                # Scope options to this combobox's own listbox: the phone
                # widget's hidden country list also uses role=option.
                options = f"[id={_q(f'input_{fid}_listbox')}] [role='option']"
                values = value if isinstance(value, list) else [value]
                for v in values:
                    if not pick_autocomplete(page, combo, str(v).strip()[:40], options, want=str(v)):
                        raise LookupError(f"no option {v!r}")
            else:
                el = _field(page, fid)
                if el is None:
                    failed.append(label)
                    continue
                el.fill(str(value), timeout=5000)
        except Exception as exc:  # noqa: BLE001
            log(f"⚠ could not fill '{label[:60]}' ({type(exc).__name__}: {str(exc)[:80]})")
            failed.append(label)
    return failed
