"""Workday adapter: discovery, and apply with a stored candidate account.

Workday is the largest pool of employer job boards (about 1,300 distinct
boards in the SimplifyJobs listings alone), and its job list is public:

    POST https://<tenant>.<wdN>.myworkdayjobs.com/wday/cxs/<tenant>/<site>/jobs
    {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "..."}

→ ``{"total": N, "jobPostings": [{title, externalPath, locationsText, …}]}``.
An unknown site answers 404, an unknown tenant 422, a closed board 410 and a
disabled one 403; only 429 means throttling. Pages hold at most 20,
so instead of listing a big board (Northrop Grumman has 3,000+ jobs) the run's
keywords are sent as ``searchText``.

Workday throttles hard and across tenants: quick requests to *different*
companies get HTTP 429, with no Retry-After header. Requests are paced and a
429 backs off (5 s, 10 s, 20 s, 40 s) before giving up with the HTTP error, so
the board scanner can pause too.

Applying (``apply_live``) needs a candidate account at each employer: Workday
has no guest apply. The password per tenant comes from ``password_for``: env
``WORKDAY_PASSWORD_<TENANT>_<WDN>``, the encrypted settings store, the local
accounts file (``data/workday_accounts.json``) or the macOS Keychain. With no
password, the job is listed under ``manual``, unless the run opted in to
``create_accounts``. Then a strong, unique password is generated and saved to
the local accounts file **before** the account is created, so the credentials
are never lost. That file is under ``data/`` (git-ignored) with mode 0600, and
must never be committed.

The questions only exist after sign-in, so planning happens in the browser,
one wizard step at a time (``plans_in_browser``): read each visible
``formField-*`` (label, kind, options), answer it with the shared rules
(``answer_question``), Save and Continue. A required question with no
confident answer stops the job (``skipped`` + ``unanswered``); Workday keeps
the draft, and the next run resumes it with "Continue Application". A dry run
stops at Review. Behaviours learned on a live tenant (3M):

* the sign-in cookie lasts only for the browser session, so sign in every run;
* with a draft, the job page shows ``continueButton``; re-opening
  ``/apply/applyManually`` while signed in gives "Something went wrong";
* field ids drift after the country changes, so every control is found inside
  its own ``formField-*`` box, never by id;
* each step first renders blank: wait for an enabled footer button + fields;
* the résumé is uploaded only when the step has no attachment yet.
"""
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime

from src.automation.tools.ats_apply_tool import Adapter, entry_for, tick
from src.automation.tools.ats_common import (
    Question,
    answer_question,
    match_option,
    noop,
    standard_for_label,
    standard_value,
    url_path_has,
    visible_text,
    with_job_location,
)
from src.automation.tools.ats_router import BoardRef

PAGE = 20                  # Workday's maximum page size
MAX_PAGES_LISTING = 10     # no search terms: the first 200 jobs
MAX_PAGES_PER_TERM = 5     # per search term: up to 100 jobs
# Search terms per board: the run's keywords with role shorthand expanded
# ("swe" → "software engineer", …), one alternative per keyword per round so
# every keyword is searched before any gets a second alternative.
MAX_TERMS = 5
PACE_S = 1.0               # between page requests
BACKOFF_S = (5, 10, 20, 40)


def split_token(token: str) -> tuple[str, str, str]:
    """``"ngc.wd1/Site"`` → ``("ngc", "wd1", "Site")``."""
    host, _, site = token.partition("/")
    tenant, _, wd = host.rpartition(".")
    if not (tenant and wd and site):
        raise ValueError(f"not a Workday board token: {token!r} (want <tenant>.<wdN>/<site>)")
    return tenant, wd, site


def search_terms(keywords) -> list[str]:
    """Workday search terms for the run's keywords. The runner's title filter
    expands role shorthand (ROLE_ALIASES), so the server-side search must too —
    Workday would answer "swe" with nothing, and a "Software Engineer" posting
    would never reach the filter. Keywords are taken round-robin over their
    alternatives, deduped, capped at MAX_TERMS."""
    from src.automation.tools.ats_common import expand_keywords
    per_kw = [expand_keywords([k.strip()]) for k in keywords if k and k.strip()]
    terms: list[str] = []
    for i in range(max((len(p) for p in per_kw), default=0)):
        for phrases in per_kw:
            if i < len(phrases) and phrases[i].lower() not in (t.lower() for t in terms):
                terms.append(phrases[i])
    return terms[:MAX_TERMS]


class WorkdayAdapter(Adapter):
    platform = "workday"
    label = "Workday"
    plans_in_browser = True  # questions only exist after sign-in
    job_scoped = True  # a job ref searches its requisition id, not the board

    # Opt-in per run (run_ats_apply(create_workday_accounts=True)): create the
    # candidate account at an employer with no stored password.
    create_accounts = False

    def __init__(self, sleep=time.sleep):
        self._sleep = sleep
        self._passwords: dict[tuple[str, str], str | None] = {}  # one lookup per tenant per run

    def password(self, host: str, email: str) -> str | None:
        key = (host, email.lower())
        if key not in self._passwords:
            self._passwords[key] = password_for(host, email)
        return self._passwords[key]

    def _post(self, client, url: str, body: dict) -> dict:
        for wait in (*BACKOFF_S, None):
            resp = client.post(url, json=body)
            # Workday throttles with 429 only. 404 (unknown site), 422 (unknown
            # tenant), 410 (closed board) and 403 (a disabled / locked site)
            # are this board's answer, not a rate limit — never wait on them.
            if resp.status_code in (403, 404, 410, 422):
                raise RuntimeError(f"no such Workday site (HTTP {resp.status_code})")
            if resp.status_code == 429 and wait is not None:
                self._sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        raise AssertionError("unreachable")

    def discover(self, client, ref: BoardRef, search: tuple[str, ...] = ()) -> list[dict]:
        tenant, wd, site = split_token(ref.company)
        api = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
        base = f"https://{tenant}.{wd}.myworkdayjobs.com/{site}"
        if ref.job_id:
            # One job: search its requisition id ("…_R10225937-1" → "R10225937-1").
            terms, max_pages = [ref.job_id.rsplit("_", 1)[-1]], 1
        elif search:
            terms, max_pages = search_terms(search), MAX_PAGES_PER_TERM
        else:
            terms, max_pages = [""], MAX_PAGES_LISTING
        jobs: dict[str, dict] = {}
        first = True
        for term in terms:
            for page in range(max_pages):
                if not first:
                    self._sleep(PACE_S)
                first = False
                data = self._post(client, api, {"appliedFacets": {}, "limit": PAGE,
                                                "offset": page * PAGE, "searchText": term})
                postings = data.get("jobPostings") or []
                for p in postings:
                    path = (p.get("externalPath") or "").rstrip("/")
                    jid = path.rsplit("/", 1)[-1]
                    if not jid or jid in jobs:
                        continue
                    url = base + path
                    jobs[jid] = {
                        "platform": self.platform, "token": ref.company, "job_id": jid,
                        "title": (p.get("title") or "").strip()[:200] or jid,
                        "company": tenant, "location": (p.get("locationsText") or "").strip()[:160],
                        "url": url, "apply_url": url,
                    }
                if len(postings) < PAGE or (page + 1) * PAGE >= int(data.get("total") or 0):
                    break
        return list(jobs.values())

    # ── apply (in the browser) ───────────────────────────────────────────────

    def manual_reason_for(self, job: dict, profile: dict | None = None) -> str | None:
        """None when this job can be applied to; else why it's listed as manual."""
        host = host_of(job["token"])
        email = (profile or {}).get("email", "")
        if self.password(host, email) or (self.create_accounts and email):
            return None
        return (f"no Workday account stored for {host}. Create one at apply_url with "
                f"{email or 'your e-mail'}, then store its password "
                f"({env_name(host)} or scripts/workday_password.py set {host})")

    def apply_live(self, page, job: dict, profile: dict, answer_fn, dry_run: bool,
                   log: Callable = noop) -> dict:
        """Sign in, open (or resume) the application, answer each step, and stop
        at Review (dry run) or submit. See the module docstring."""
        jid = f"{job['platform']}:{job['token']}/{job['job_id']}"
        entry = entry_for(job, [])
        host, email = host_of(job["token"]), profile.get("email", "")
        password = self.password(host, email)
        apply_url = job["url"].rstrip("/") + "/apply/applyManually"
        profile = with_job_location(profile, job_country(job.get("location") or ""), job.get("company"))
        wiz = Wizard(page, log)
        if not password and self.create_accounts and email:
            created = self._create_account(wiz, apply_url, host, email, log)
            if created is not True:
                entry.update(submitted=False, **created)
                return entry
        elif not password:
            entry.update(status="manual", submitted=False, reason=self.manual_reason_for(job, profile))
            return entry
        elif not wiz.sign_in(apply_url, email, password):
            entry.update(status="failed", submitted=False,
                         reason=f"could not sign in to Workday at {host}: "
                                + (" | ".join(wiz.errors()) or "check the stored password"))
            return entry
        opened = wiz.open_application(job["url"])
        if opened == "no_apply":
            entry.update(status="skipped", submitted=False,
                         reason="no Apply / Continue button on the job page (already applied, or closed)")
            return entry
        if opened != "wizard":
            entry.update(status="failed", submitted=False, reason="the application wizard didn't load")
            return entry
        for _ in range(MAX_STEPS):
            step = wiz.step()
            if "Review" in step:
                return wiz.finish(entry, jid, dry_run)
            log(f"{jid}: {step}")
            if "My Experience" in step:
                wiz.upload_resume(profile.get("resume_path", ""))
            answers, unanswered = wiz.answer_step(profile, answer_fn)
            entry["answers"].extend(answers)
            if unanswered:
                entry.update(status="skipped", submitted=False, unanswered=unanswered,
                             reason="unanswered required question(s): "
                                    + "; ".join(u[:70] for u in unanswered[:3])
                                    + " (the draft is kept on Workday; add them to extra_answers)")
                log(f"↷ {jid}: {len(unanswered)} required question(s) we can't answer — next")
                return entry
            if not wiz.save_and_continue():
                entry.update(status="failed", submitted=False,
                             reason=f"Workday rejected step '{step}': "
                                    + (" | ".join(wiz.errors()) or "no error shown"))
                return entry
        entry.update(status="failed", submitted=False, reason="never reached the Review step")
        return entry

    def _create_account(self, wiz, apply_url: str, host: str, email: str, log: Callable):
        """Create the candidate account; True when signed in, else the entry
        fields to report. The password is saved locally BEFORE the form is
        sent: whatever happens next, the account is never orphaned."""
        password = generate_password()
        save_account(host, email, password)
        self._passwords[(host, email.lower())] = password
        log(f"creating a Workday account at {host} for {email} (saved to {accounts_path()})")
        outcome = wiz.create_account(apply_url, email, password)
        if outcome == "signed_in":
            mark_account(host, email, "active")
            return True
        if outcome == "verify":
            mark_account(host, email, "needs_verification")
            return {"status": "needs_verification",
                    "reason": f"Workday account created at {host}; it asks to verify {email}. "
                              "Click the link in that e-mail; the next run signs in."}
        if outcome == "exists":
            forget_account(host, email)
            self._passwords.pop((host, email.lower()), None)
            return {"status": "failed",
                    "reason": f"an account for {email} already exists at {host}: store its password "
                              f"({env_name(host)} or scripts/workday_password.py set {host})"}
        mark_account(host, email, "create_failed")
        return {"status": "failed", "reason": f"could not create a Workday account at {host}: "
                                              + (" | ".join(wiz.errors()) or "no confirmation")}

    # Not used: plans_in_browser routes Workday through apply_live.
    def load_form(self, client, job):  # pragma: no cover
        raise NotImplementedError("Workday plans in the browser (apply_live)")

    def plan(self, form, job, profile, answer_fn):  # pragma: no cover
        raise NotImplementedError("Workday plans in the browser (apply_live)")

    def apply(self, page, job, answers, dry_run, log):  # pragma: no cover
        raise NotImplementedError("Workday plans in the browser (apply_live)")


def job_country(location: str) -> str:
    """Workday writes locations as ``<Country>-<Region>-<City>`` ("US-Minnesota-
    Maplewood", "Canada-Ontario-London"): the first segment is the country, and
    the city must not add another ("London" is not the UK here). Anything else
    ("2 Locations") is returned as is, so an unknown country stays unknown."""
    from src.automation.tools.ats_common import countries_in
    head = re.split(r"[-,]", location, maxsplit=1)[0].strip()
    if head.upper() in ("US", "USA"):
        return "United States"
    return head if countries_in(head) else location


# ── credentials ──────────────────────────────────────────────────────────────

KEYCHAIN_SERVICE = "agent-auto-system workday {host}"


def host_of(token: str) -> str:
    """``"3m.wd1/Search"`` → ``"3m.wd1"`` (one account per tenant)."""
    tenant, wd, _ = split_token(token)
    return f"{tenant}.{wd}".lower()


def env_name(host: str) -> str:
    return "WORKDAY_PASSWORD_" + re.sub(r"[^A-Z0-9]+", "_", host.upper())


# Accounts the automation created (or the user added): e-mail + password per
# tenant. Under data/, which is git-ignored; written 0600. NEVER commit it.
DEFAULT_ACCOUNTS_PATH = "data/workday_accounts.json"


def accounts_path() -> str:
    return os.getenv("WORKDAY_ACCOUNTS_FILE", DEFAULT_ACCOUNTS_PATH)


def load_accounts(path: str | None = None) -> dict:
    """``{host: {email: {"password", "created_at", "status"}}}``."""
    try:
        with open(path or accounts_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_accounts(data: dict, path: str) -> None:
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".workday_accounts_", suffix=".json", dir=folder)
    try:
        os.chmod(tmp, 0o600)  # owner only, before a single secret is written
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def save_account(host: str, email: str, password: str, status: str = "creating",
                 path: str | None = None) -> None:
    path = path or accounts_path()
    data = load_accounts(path)
    data.setdefault(host, {})[email.lower()] = {
        "password": password, "status": status,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds")}
    _write_accounts(data, path)


def mark_account(host: str, email: str, status: str, path: str | None = None) -> None:
    path = path or accounts_path()
    data = load_accounts(path)
    entry = data.get(host, {}).get(email.lower())
    if entry is not None:
        entry["status"] = status
        _write_accounts(data, path)


def forget_account(host: str, email: str, path: str | None = None) -> None:
    path = path or accounts_path()
    data = load_accounts(path)
    if data.get(host, {}).pop(email.lower(), None) is not None:
        _write_accounts(data, path)


def generate_password(length: int = 20) -> str:
    """Meets Workday's rules (upper, lower, digit, special, 8+), unique per tenant."""
    pools = ("ABCDEFGHJKLMNPQRSTUVWXYZ", "abcdefghijkmnopqrstuvwxyz", "23456789", "!@#$%*-_=+?")
    chars = [secrets.choice(p) for p in pools]
    chars += [secrets.choice("".join(pools)) for _ in range(length - len(chars))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def password_for(host: str, email: str = "") -> str | None:
    """A tenant's password: env var, the encrypted settings store, the local
    accounts file, then (macOS) the Keychain under ``KEYCHAIN_SERVICE``."""
    if os.getenv(env_name(host)):
        return os.environ[env_name(host)]
    try:
        from src.settings_store import get_workday_password
        stored = get_workday_password(host)
        if stored:
            return stored
    except Exception:  # noqa: BLE001 — no DB here: try the file / Keychain
        pass
    if email:
        local = load_accounts().get(host, {}).get(email.lower()) or {}
        if local.get("password") and local.get("status") != "create_failed":
            return local["password"]
    if sys.platform == "darwin" and email:
        try:
            out = subprocess.run(["security", "find-generic-password", "-s",
                                  KEYCHAIN_SERVICE.format(host=host), "-a", email, "-w"],
                                 capture_output=True, text=True, timeout=10)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except Exception:  # noqa: BLE001
            pass
    return None


# ── answering one field (pure: unit-tested) ──────────────────────────────────

MAX_STEPS = 9
# Workday's own My Information fields the shared rules don't cover.
SYSTEM_DEFAULTS = ((r"phone device type", "Mobile"),)
_TICK_RE = re.compile(r"agree|acknowledge|certify|terms|consent|i have read|read and understand", re.I)
_PLACEHOLDER = ("", "select one")


def plan_field(f: dict, profile: dict, answer_fn=None) -> dict | None:
    """What to do with one scraped ``formField``: ``{"action": "pick"|"fill"|
    "tick", "value"}``, or None when there's no confident answer. ``f`` is
    ``{label, kind, required, options}`` (see ``FIELDS_JS``)."""
    label, kind, required = f["label"], f["kind"], f["required"]
    options = [o for o in f.get("options") or [] if o.strip().lower() not in _PLACEHOLDER]
    for pat, default in SYSTEM_DEFAULTS:
        if re.search(pat, label, re.I) and kind in ("listbox", "radio"):
            pick = match_option(options, default)
            return {"action": "pick", "value": pick} if pick else None
    if kind in ("listbox", "radio"):
        ans = answer_question(Question(label, label, "select", required, options), profile, answer_fn)
        return {"action": "pick", "value": ans["value"]} if ans else None
    if kind == "checkbox":
        # Only a required acknowledgement / terms box is ticked; nothing else.
        if required and _TICK_RE.search(label + " " + " ".join(options)):
            return {"action": "tick", "value": "checked"}
        return None
    if kind == "text":
        std = standard_for_label(label)
        value = standard_value(std, profile) if std else ""
        if std == "phone" and f.get("phone_code"):
            value = national_number(value, f["phone_code"])
        if not value:
            ans = answer_question(Question(label, label, "text", required, []), profile, answer_fn)
            value = ans["value"] if ans else ""
        return {"action": "fill", "value": str(value)} if value else None
    return None  # date widgets, multi-select prompts: left to the user


def national_number(phone: str, phone_code: str) -> str:
    """The number to type next to Workday's Country Phone Code widget, which
    holds the country code itself ("Taiwan (+886)"): a profile phone of
    "+886 963335868" becomes "963335868" (Workday rejects the "+886" as "not a
    valid format"). A phone in another country's code, or with none, is kept
    as typed, minus the formatting."""
    code = re.search(r"\+\s*(\d{1,4})", phone_code or "")
    digits = re.sub(r"\D", "", phone or "")
    if not phone.strip().startswith("+"):
        return phone.strip()
    if code and digits.startswith(code.group(1)):
        return digits[len(code.group(1)):]
    return phone.strip()


# ── the wizard (browser) ─────────────────────────────────────────────────────

FIELDS_JS = r"""() => Array.from(document.querySelectorAll('[data-automation-id^="formField"]'))
  .map((f, index) => ({f, index})).filter(({f}) => f.offsetParent).map(({f, index}) => {
    const lab = f.querySelector('label, legend');
    const label = lab ? lab.innerText.replace(/\s+/g, ' ').replace(/\*\s*$/, '').trim() : '';
    const required = !!(lab && /\*/.test(lab.innerText)) || !!f.querySelector('[aria-required=true],[required]');
    const btn = f.querySelector('button[aria-haspopup=listbox]');
    const radios = Array.from(f.querySelectorAll('input[type=radio]'));
    const checks = Array.from(f.querySelectorAll('input[type=checkbox]'));
    const text = f.querySelector('textarea, input[type=text]:not([data-uxi-widget-type]), input:not([type]), ' +
                                 'input[type=number], input[type=tel], input[type=email]');
    const optLabel = i => { const l = i.id && document.querySelector('label[for="' + CSS.escape(i.id) + '"]');
                            return (l ? l.innerText : (i.closest('label') || {}).innerText || i.value || '').trim(); };
    const chip = f.querySelector('[data-automation-id=promptOption], [data-automation-id=selectedItem]');
    let kind = 'other', value = '', options = [];
    const upload = f.querySelector('input[type=file]');
    if (upload) { kind = 'file'; value = Array.from(f.querySelectorAll('[data-automation-id=file-upload-item]'))
                    .map(e => e.innerText.trim().split('\n')[0]).filter(Boolean).join(', '); }
    else if (f.querySelector('input[data-uxi-widget-type=selectinput]')) { kind = 'selectinput'; value = chip ? chip.innerText.trim() : ''; }
    else if (btn) { kind = 'listbox'; value = btn.innerText.trim(); if (value === 'Select One') value = ''; }
    else if (radios.length) { kind = 'radio'; options = radios.map(optLabel);
                              const c = radios.find(r => r.checked); value = c ? optLabel(c) : ''; }
    else if (checks.length) { kind = 'checkbox'; options = checks.map(optLabel);
                              value = checks.every(c => c.checked) ? 'checked' : ''; }
    else if (text) { kind = 'text'; value = text.value; }
    return {index, field: f.getAttribute('data-automation-id'), label, required, kind, value, options};
  })"""
_BOXES = "[data-automation-id^='formField']"
_NEXT = "[data-automation-id='pageFooterNextButton']"
_SUBMIT = "[data-automation-id='pageFooterSubmitButton']"
_ERRORS = ("[data-automation-id='errorMessage'], [data-automation-id='inputErrorMessage'], "
           "[data-automation-id='inputAlert'], [role='alert']")
SUCCESS_TEXTS = ("Application Submitted", "application has been submitted",
                 "Thanks for applying", "Thank you for applying", "successfully submitted")


class Wizard:
    """Drives one tenant's apply wizard on a Playwright page."""

    def __init__(self, page, log: Callable = noop):
        self.page, self.log = page, log
        self.email = ""

    def _wait(self, test, seconds: float) -> bool:
        for _ in range(int(seconds * 2)):
            try:
                if test():
                    return True
            except Exception:  # noqa: BLE001 — page mid-navigation
                pass
            self.page.wait_for_timeout(500)
        return False

    def signed_in(self) -> bool:
        """The header's account menu shows the e-mail. (The menu button exists
        signed out too, so its presence alone proves nothing.)"""
        menu = self.page.locator("[data-automation-id='utilityMenuButton']")
        try:
            return bool(self.email) and any(self.email.lower() in t.lower() for t in menu.all_inner_texts())
        except Exception:  # noqa: BLE001
            return False

    def sign_in(self, url: str, email: str, password: str) -> bool:
        p = self.page
        self.email = email
        p.goto(url, wait_until="domcontentloaded", timeout=45000)
        pw_box = "input[data-automation-id='password']"
        if not self._wait(lambda: p.locator(pw_box).count() or self.signed_in(), 25):
            return False
        if self.signed_in():
            return True
        if p.locator("input[data-automation-id='verifyPassword']").count():
            # The create-account form: switch to Sign In (accounts are never created here).
            # A click before the form has settled is lost: retry until it switches.
            for _ in range(4):
                p.locator("[data-automation-id='signInLink']").first.click()
                if self._wait(lambda: not p.locator("input[data-automation-id='verifyPassword']").count(), 4):
                    break
        # The Sign In form re-renders as it mounts: values typed too early are
        # dropped from its state and an EMPTY form is submitted. Let it settle,
        # type like a user (focus, keys, blur), and check the values held.
        self._wait(lambda: p.locator("[data-automation-id='signInSubmitButton']").count(), 10)
        p.wait_for_timeout(1500)
        for sel, value in (("input[data-automation-id='email']", email), (pw_box, password)):
            box = p.locator(sel).first
            box.click()
            box.fill(value)
            box.press("Tab")
        p.wait_for_timeout(500)
        if p.locator("input[data-automation-id='email']").first.input_value() != email:
            return False
        # Workday covers the button with a transparent ``click_filter`` div
        # that takes the click; Enter in the password box is the fallback.
        for attempt in (self._click_sign_in, lambda: p.locator(pw_box).first.press("Enter")):
            try:
                attempt()
            except Exception:  # noqa: BLE001
                continue
            if self._wait(self.signed_in, 12):
                return True
        return False

    def create_account(self, url: str, email: str, password: str) -> str:
        """Fill Workday's Create Account form. Returns "signed_in", "verify"
        (Workday wants the e-mail confirmed first), "exists" or "failed"."""
        p = self.page
        self.email = email
        p.goto(url, wait_until="domcontentloaded", timeout=45000)
        create = "[data-automation-id='createAccountSubmitButton']"
        if not self._wait(lambda: p.locator(create).count() or self.signed_in(), 25):
            return "failed"
        if self.signed_in():
            return "signed_in"
        p.wait_for_timeout(1500)  # the form re-mounts as it settles, like Sign In
        for sel, value in (("input[data-automation-id='email']", email),
                           ("input[data-automation-id='password']", password),
                           ("input[data-automation-id='verifyPassword']", password)):
            box = p.locator(sel).first
            box.click()
            box.fill(value)
            box.press("Tab")
        # The account form's own consent box (terms / privacy) when it has one.
        boxes = p.locator("input[type=checkbox][data-automation-id*='createAccountCheckbox']")
        for i in range(boxes.count()):
            if not boxes.nth(i).is_checked():
                tick(boxes.nth(i))
        p.wait_for_timeout(500)
        overlay = p.locator("[data-automation-id='click_filter'][aria-label='Create Account']")
        (overlay.first.click() if overlay.count() else p.locator(create).first.click(force=True))
        verify = re.compile(r"verify your (account|e-?mail)|verification (e-?mail|link)|check your (e-?mail|inbox)", re.I)
        exists = re.compile(r"already (exists|in use|registered|been used)", re.I)
        for _ in range(40):
            p.wait_for_timeout(500)
            if self.signed_in():
                return "signed_in"
            try:
                body = p.locator("body").inner_text()
            except Exception:  # noqa: BLE001
                continue
            if exists.search(body):
                return "exists"
            if verify.search(body):
                return "verify"
        return "failed"

    def _click_sign_in(self) -> None:
        p = self.page
        overlay = p.locator("[data-automation-id='click_filter'][aria-label='Sign In']")
        if overlay.count():
            overlay.first.click()
        else:
            p.locator("[data-automation-id='signInSubmitButton']").first.click(force=True)

    def open_application(self, job_url: str) -> str:
        """"wizard" (on a fully rendered step), "no_apply" or "timeout"."""
        p = self.page
        p.goto(job_url, wait_until="domcontentloaded", timeout=45000)
        sel = "[data-automation-id='continueButton'], [data-automation-id='adventureButton']"
        if not self._wait(lambda: p.locator(sel).count(), 20):
            return "no_apply"
        # "Apply" renders first; with a saved draft it turns into "Continue
        # Application" a moment later. Clicking Apply → Apply Manually on a job
        # with a draft gives Workday's "Something went wrong".
        cont = p.locator("[data-automation-id='continueButton']").first
        self._wait(lambda: cont.count(), 6)
        if cont.count():
            self.log("resuming the saved draft (Continue Application)")
            cont.click()
        else:
            p.locator("[data-automation-id='adventureButton']").first.click()
            manual = p.locator("[data-automation-id='applyManually']").first
            if not self._wait(lambda: manual.count(), 10):
                return "timeout"
            manual.click()
        return "wizard" if self.wait_for_step() else "timeout"

    def step(self) -> str:
        bar = self.page.locator("[data-automation-id='progressBarActiveStep']")
        return bar.first.inner_text().replace("\n", " ") if bar.count() else ""

    def wait_for_step(self, seconds: float = 40) -> bool:
        """A wizard step fully rendered: an enabled footer button and visible
        fields (Review has none). Each step first renders blank."""
        p = self.page

        def ready():
            btn = p.locator(f"{_NEXT}, {_SUBMIT}").first
            fields = p.locator(f"{_BOXES} >> visible=true").count()
            return btn.count() and btn.is_enabled() and (fields or "Review" in self.step())
        ok = self._wait(ready, seconds)
        if ok:
            p.wait_for_timeout(1200)
        return ok

    def errors(self) -> list[str]:
        try:
            texts = self.page.locator(_ERRORS).all_inner_texts()
        except Exception:  # noqa: BLE001
            return []
        return [t.strip()[:160] for t in texts if t.strip()][:6]

    def fields(self) -> list[dict]:
        return self.page.evaluate(FIELDS_JS)

    def upload_resume(self, path: str) -> None:
        p = self.page
        if not path or p.locator("[data-automation-id='file-upload-item']").count():
            return  # a résumé is already attached (a resumed draft): never twice
        up = p.locator("input[type=file]").first
        if up.count():
            up.set_input_files(path)
            self._wait(lambda: p.locator("[data-automation-id='file-upload-successful']").count(), 30)
            self.log(f"résumé uploaded ({os.path.basename(path)})")

    def _listbox_options(self, btn):
        """Open a listbox button; its options, scoped by ``aria-controls`` (the
        phone-code widget keeps a stray role=option in the page)."""
        p = self.page
        btn.click()
        p.wait_for_timeout(900)
        lb = btn.get_attribute("aria-controls")
        opts = p.locator(f"[id={json.dumps(lb)}] [role='option']") if lb else \
            p.locator("[role='listbox'] >> visible=true").locator("[role='option']")
        return [t.strip() for t in opts.all_inner_texts()], opts

    def _act(self, f: dict, profile: dict, answer_fn) -> dict | None:
        box = self.page.locator(_BOXES).nth(f["index"])
        if f["kind"] == "listbox":
            btn = box.locator("button[aria-haspopup=listbox]").first
            texts, opts = self._listbox_options(btn)
            plan = plan_field({**f, "options": texts}, profile, answer_fn)
            if not plan:
                self.page.keyboard.press("Escape")
                return None
            opts.nth(texts.index(plan["value"])).click()
            self.page.wait_for_timeout(1200)  # dependent fields (state, phone code) re-render
            return plan
        plan = plan_field(f, profile, answer_fn)
        if not plan:
            return None
        if f["kind"] == "radio":
            tick(box.locator("input[type=radio]").nth(f["options"].index(plan["value"])))
        elif f["kind"] == "checkbox":
            boxes = box.locator("input[type=checkbox]")
            for i in range(boxes.count()):
                if not boxes.nth(i).is_checked():
                    tick(boxes.nth(i))
        elif f["kind"] == "text":
            box.locator("textarea, input:not([type=hidden]):not([type=file])").first.fill(plan["value"])
        return plan

    def answer_step(self, profile: dict, answer_fn=None) -> tuple[list[dict], list[str]]:
        """Answer every empty field of the current step. A choice (the country)
        re-renders the step, adding fields and shifting positions, so the
        fields are re-read after every action; each label is tried once.
        Returns (answers, labels of required fields still empty)."""
        answers: list[dict] = []
        tried: set[tuple[str, str]] = set()
        for _ in range(60):
            todo = next((f for f in self.fields()
                         if not f["value"] and (f["label"], f["kind"]) not in tried), None)
            if todo is None:
                break
            tried.add((todo["label"], todo["kind"]))
            if todo["field"] == "formField-phoneNumber":
                todo["phone_code"] = next((f["value"] for f in self.fields()
                                           if f["field"] == "formField-countryPhoneCode"), "")
            plan = self._act(todo, profile, answer_fn)
            if plan:
                answers.append({"label": todo["label"][:80], "value": plan["value"]})
        unanswered = [f["label"] for f in self.fields() if f["required"] and not f["value"]]
        return answers, unanswered

    def save_and_continue(self) -> bool:
        before = self.step()
        self.page.locator(_NEXT).first.click()
        if not self._wait(lambda: self.step() != before, 20):
            return False
        return self.wait_for_step(30)

    def finish(self, entry: dict, jid: str, dry_run: bool) -> dict:
        """On Review: stop (dry run), or submit and wait for Workday's confirmation."""
        p = self.page
        if dry_run:
            entry.update(status="prepared", submitted=False, reason="dry_run (stopped at Review)")
            self.log(f"✓ {jid}: application prepared up to Review (dry-run, NOT sent)")
            return entry
        # On Review the footer's Submit is usually the same pageFooterNextButton
        # as "Save and Continue", relabelled (3M); some tenants use
        # pageFooterSubmitButton. Click the one that SAYS Submit, never another.
        submit = p.locator(f"{_SUBMIT}, {_NEXT}").filter(has_text=re.compile(r"^\s*Submit\s*$"))
        if not submit.count():
            entry.update(status="failed", submitted=False, reason="no Submit button on the Review step")
            return entry
        submit.first.click()
        if self._wait(lambda: url_path_has(p.url or "", ("userHome",))
                      or visible_text(p, SUCCESS_TEXTS), 30):
            entry.update(status="submitted", submitted=True, confirmation="Workday confirmation shown")
            self.log(f"✓ {jid}: submitted AND confirmed")
            return entry
        errs = self.errors()
        entry.update(status="unconfirmed", submitted=False,
                     reason=("form rejected: " + " | ".join(errs)) if errs
                     else "clicked Submit but no confirmation")
        self.log(f"⚠ {jid}: submit unconfirmed — NOT counting as applied")
        return entry
