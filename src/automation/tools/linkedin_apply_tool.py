"""
Auto-apply to LinkedIn "Easy Apply" job openings.

A Playwright port of the reference browser scripts (yennanliu/linkedin-skill and
ai_experiment/linkedin/gemini). LinkedIn's Easy Apply is a DOM-driven multi-step
modal, not a clean JSON API: clicking "Easy Apply" opens a dialog that may span
several pages (contact info → screening questions → resume → review → submit). We
drive that DOM: fill each page's fields with sensible defaults, click
Next/Review, and finally Submit — confirming the "application was sent" banner.

Auth is a persisted browser session (cookies), created ONCE via
`scripts/linkedin_login.py` — same pattern as the 104 / Shopee / tasker scrapers.
LinkedIn guards login with 2FA / CAPTCHA, so we never automate the login itself.

Flow per run:
  1. Load the jobs-search listing with the saved session; bail if logged out.
  2. Page through /jobs/search/?keywords=...&location=...&f_AL=true&start=N
     (`f_AL=true` is LinkedIn's own "Easy Apply" filter).
  3. For each job card: read title + company; skip if already applied.
  4. Optional second gate: an LLM `relevance_fn(title, meta)` decides whether the
     job is worth applying to (natural-language task_filter). Fails open.
  5. Apply: click "Easy Apply", walk the modal (fill defaults → Next/Review →
     Submit), and confirm the site shows "application was sent". Only a confirmed
     success banner counts as applied — a plain click is never trusted.
  6. `dry_run` (default) does everything EXCEPT the final Submit: it fills the
     form, then dismisses & discards the draft, so you can preview which jobs
     would be applied to without sending real applications.

The LLM here is only the relevance judge (so the automation runs with whichever
provider/model the run selected). The apply itself is pure DOM automation.

Scraper policy (per CLAUDE.md): return partial results + a `warnings` list
instead of raising; one bad job must never abort the whole run.

`run_linkedin_apply(...)` is the primary entry point (the flow calls it directly
with an LLM-backed relevance_fn). `LinkedInApplyTool` is a thin BaseTool wrapper
for catalog/agent parity.
"""
import os
from collections.abc import Callable

from crewai.tools import BaseTool
from pydantic import BaseModel

_BASE = "https://www.linkedin.com"
_SEARCH = _BASE + "/jobs/search/"
_JOB_VIEW = _BASE + "/jobs/view/"
DEFAULT_STATE_PATH = "data/linkedin_state.json"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# DOM selectors, kept as fallback-in-order lists because LinkedIn A/B-tests its
# hashed class names constantly; the first that matches wins. aria-label and
# text-based locators are the most durable, so they lead where possible.
_EASY_APPLY_SELECTORS = [
    "button.jobs-apply-button",
    "button[aria-label*='Easy Apply']",
    "button:has-text('Easy Apply')",
]
# LinkedIn migrated the Easy Apply modal to a native <dialog aria-labelledby=
# "dialog-header"> with fully obfuscated class names — it is NOT a div.jobs-easy-
# apply-modal and carries no role="dialog". The native <dialog> comes first; the
# legacy selectors stay as fallbacks for older layouts / A-B buckets.
_MODAL_SELECTOR = (
    "dialog[aria-labelledby='dialog-header'], "
    "div.jobs-easy-apply-modal, [role='dialog']"
)
_SUBMIT_SELECTORS = [
    "button[aria-label*='Submit application']",
    "button:has-text('Submit application')",
]
# Advance-a-step buttons, most-specific first. "Review" precedes "Next" because a
# review page still has a Next-less layout; matching Review first avoids a stall.
_NEXT_SELECTORS = [
    "button[aria-label*='Review your application']",
    "button[aria-label*='Continue to next step']",
    "button:has-text('Review')",
    "button:has-text('Next')",
    "button:has-text('Continue')",
]
_DISMISS_SELECTORS = [
    "button[aria-label='Dismiss']",
    "button[aria-label*='Dismiss']",
    "button[data-test-modal-close-btn]",
]
_DISCARD_SELECTORS = [
    "button[data-control-name='discard_application_confirm_btn']",
    "button:has-text('Discard')",
]
# Post-submit confirmation banner. Substring match is case-insensitive.
_SUCCESS_TEXTS = ("was sent", "Application sent", "已送出", "應徵已送出")
_MAX_MODAL_STEPS = 12  # safety valve against a bad screening question looping forever


def _noop(_msg: str) -> None:
    pass


# ── session / auth ───────────────────────────────────────────────────────────

_LOGIN_URL_MARKERS = ("/login", "/authwall", "/checkpoint", "/uas/login", "/signup")
_AUTHED_SELECTORS = (
    "img.global-nav__me-photo", ".global-nav__me", "a[href*='/feed/']",
    "input.jobs-search-box__text-input", ".jobs-search-results-list",
)


def _looks_logged_out(page) -> bool:
    """Positive login signals beat LinkedIn's ever-present marketing header. We
    only declare "logged out" when an auth wall is actually showing."""
    try:
        if any(m in (page.url or "") for m in _LOGIN_URL_MARKERS):
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        for sel in _AUTHED_SELECTORS:
            if page.locator(sel).count() > 0:
                return False
    except Exception:  # noqa: BLE001
        pass
    try:
        loc = page.locator("a:has-text('Sign in'), button:has-text('Sign in')")
        return loc.count() > 0 and loc.first.is_visible()
    except Exception:  # noqa: BLE001
        return False


# ── search / listing ─────────────────────────────────────────────────────────

def _search_url(keywords: str, location: str, page_num: int,
                remote: bool = False) -> str:
    from urllib.parse import urlencode
    # f_AL=true is LinkedIn's Easy Apply filter; results paginate by 25.
    params = {"keywords": keywords, "f_AL": "true", "start": (page_num - 1) * 25}
    if location:
        params["location"] = location
    if remote:
        # f_WT workplace-type filter: 1=On-site, 2=Remote, 3=Hybrid.
        params["f_WT"] = "2"
    return f"{_SEARCH}?{urlencode(params)}"


def _collect_page_jobs(page, keywords: str, location: str, page_num: int,
                       log: Callable, warnings: list,
                       remote: bool = False) -> list[dict] | None:
    """Job cards on a single listing page. Scrolls the results list to trigger
    LinkedIn's lazy load, then reads each card's job id / title / company /
    already-applied flag.

    Returns [{job_id, url, title, company, applied}], deduped by job id — or None
    if the page could not be read (transient load/parse failure). None is
    distinct from []: [] means the page loaded but had no jobs (end of results),
    so the caller stops; None means "try the next page" — a transient blip must
    not look like the end of the listing and abort the whole run."""
    url = _search_url(keywords, location, page_num, remote)
    for attempt in range(3):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)
            break
        except Exception as exc:  # noqa: BLE001
            if attempt == 2:
                warnings.append(f"page {page_num}: failed to load after 3 tries ({exc})")
                log(f"⚠ Page {page_num}: failed to load after 3 attempts ({exc})")
                return None
            log(f"⚠ Page {page_num}: load attempt {attempt + 1} failed, retrying ({exc})")
            page.wait_for_timeout(2000)

    # Scroll the results list so all ~25 cards render.
    prev = -1
    for _ in range(8):
        try:
            count = page.locator("[data-job-id]").count()
        except Exception:  # noqa: BLE001
            count = 0
        if count == prev:
            break
        prev = count
        try:
            page.evaluate(
                "() => { const l = document.querySelector('.jobs-search-results-list, "
                ".scaffold-layout__list'); if (l) l.scrollBy(0, 1500); "
                "else window.scrollBy(0, 1500); }"
            )
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(900)

    try:
        rows = page.evaluate(
            """() => {
                const out = [];
                const seen = new Set();
                const cards = Array.from(document.querySelectorAll('[data-job-id]'));
                for (const card of cards) {
                    const id = (card.getAttribute('data-job-id') || '').trim();
                    if (!id || id === '0' || seen.has(id)) continue;
                    const t = card.querySelector(
                        ".job-card-list__title, .job-card-list__title--link, "
                        + "a.job-card-container__link, [class*='job-card'][class*='title']");
                    let title = t ? (t.getAttribute('aria-label') || t.innerText || '') : '';
                    const c = card.querySelector(
                        ".artdeco-entity-lockup__subtitle, "
                        + ".job-card-container__primary-description, [class*='subtitle']");
                    let company = c ? (c.innerText || '') : '';
                    const cardText = card.innerText || '';
                    seen.add(id);
                    out.push({
                        job_id: id,
                        title: title.trim().slice(0, 200),
                        company: company.trim().slice(0, 120),
                        applied: /\\bApplied\\b/.test(cardText) || /已應徵/.test(cardText),
                    });
                }
                return out;
            }"""
        )
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"page {page_num}: could not read job cards ({exc})")
        log(f"⚠ Page {page_num}: could not read job cards ({exc})")
        return None

    jobs = []
    for r in rows or []:
        jid = r.get("job_id", "")
        jobs.append({
            "job_id": jid,
            "url": f"{_JOB_VIEW}{jid}/",
            "title": r.get("title", "") or jid,
            "company": r.get("company", ""),
            "applied": bool(r.get("applied")),
        })
    log(f"Page {page_num}: found {len(jobs)} job(s)")
    return jobs


# ── apply funnel (DOM) ────────────────────────────────────────────────────────

def _first_visible(scope, selectors: list[str]):
    """First matching, visible locator from a fallback list, or None."""
    for sel in selectors:
        try:
            loc = scope.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                return loc
        except Exception:  # noqa: BLE001
            continue
    return None


# JS that fills the current Easy Apply modal page with sensible defaults. Empty
# required fields are the ones LinkedIn blocks progress on; we answer them so the
# common flows (contact info, years-of-experience, yes/no eligibility) sail
# through. Values come from the user's `profile`; unknown text fields fall back to
# safe placeholders. React needs native-setter value + input/change events.
_FILL_JS = r"""
(profile) => {
  // Scope to the Easy Apply dialog (native <dialog> today, legacy fallbacks
  // after) so we never touch stray page fields (nav search box, footer, etc).
  const modal = document.querySelector("dialog[aria-labelledby='dialog-header']")
    || document.querySelector("div.jobs-easy-apply-modal, [role='dialog']")
    || document.body;
  const setVal = (el, val) => {
    const proto = el.tagName === 'TEXTAREA'
      ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    setter.call(el, val);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  };
  const labelFor = (el) => {
    let txt = '';
    if (el.id) { const l = document.querySelector('label[for="' + el.id + '"]'); if (l) txt = l.innerText; }
    if (!txt) { const g = el.closest("[data-test-form-element], .fb-dash-form-element, .jobs-easy-apply-form-element"); if (g) txt = g.innerText; }
    if (!txt && el.getAttribute('aria-label')) txt = el.getAttribute('aria-label');
    return (txt || '').toLowerCase();
  };
  let filled = 0;

  modal.querySelectorAll("input[type='text'], input[type='email'], input[type='tel'], input:not([type])").forEach(el => {
    if (el.value && el.value.trim()) return;
    const lbl = labelFor(el);
    let v = '';
    if (/phone|mobile|tel/.test(lbl)) v = profile.phone || '';
    else if (/email/.test(lbl)) v = profile.email || '';
    else if (/city|location|address/.test(lbl)) v = profile.location || '';
    else if (el.required) v = 'N/A';
    if (v) { setVal(el, v); filled++; }
  });

  modal.querySelectorAll("input[type='number']").forEach(el => {
    if (el.value && el.value.trim()) return;
    const lbl = labelFor(el);
    let v;
    if (/year|experience|exp/.test(lbl)) v = String(profile.years || 3);
    else if (/salary|rate|compensation|expected/.test(lbl)) v = profile.salary || '0';
    else v = '1';
    setVal(el, v); filled++;
  });

  modal.querySelectorAll('select').forEach(el => {
    const curText = (el.options[el.selectedIndex] || {}).text || '';
    if (el.value && !/select an option|^\s*$/i.test(curText)) return;
    const opts = Array.from(el.options).filter(o => o.value && !/select an option/i.test(o.text));
    if (!opts.length) return;
    const lbl = labelFor(el);
    const wantNo = /sponsor|require sponsorship/.test(lbl);
    let choice = opts.find(o => new RegExp(wantNo ? '^\\s*no' : '^\\s*yes', 'i').test(o.text)) || opts[0];
    el.value = choice.value;
    el.dispatchEvent(new Event('change', { bubbles: true }));
    filled++;
  });

  const optText = (r) => {
    let t = '';
    if (r.id) { const l = document.querySelector('label[for="' + r.id + '"]'); if (l) t = l.innerText; }
    return (t || r.value || '').trim().toLowerCase();
  };
  const seen = new Set();
  modal.querySelectorAll("input[type='radio']").forEach(el => {
    const name = el.name || '';
    if (seen.has(name)) return;
    seen.add(name);
    // Filter by name in JS — a CSS [name='..'] selector throws if the name has
    // special characters (LinkedIn uses urn:-style dynamic names).
    const group = Array.from(modal.querySelectorAll("input[type='radio']")).filter(r => r.name === name);
    if (group.some(r => r.checked)) return;
    // Mirror the <select> heuristic: default to "No" for sponsorship questions,
    // "Yes" otherwise. The question text comes from the field container, not the
    // option's own Yes/No label.
    const container = el.closest("[data-test-form-element], .fb-dash-form-element, .jobs-easy-apply-form-element, fieldset");
    const q = (container ? container.innerText : '').toLowerCase();
    const wantNo = /sponsor|require sponsorship/.test(q);
    const pick = group.find(r => new RegExp(wantNo ? '^no' : '^yes').test(optText(r))) || group[0];
    if (pick) { pick.click(); filled++; }
  });

  modal.querySelectorAll("textarea").forEach(el => {
    if ((el.value && el.value.trim()) || !el.required) return;
    setVal(el, 'Please see my LinkedIn profile and resume for details.');
    filled++;
  });

  modal.querySelectorAll("input[type='checkbox']").forEach(el => {
    if (el.required && !el.checked) { el.click(); filled++; }
  });

  return filled;
}
"""


def _has_form_error(page) -> bool:
    for sel in (".artdeco-inline-feedback--error",
                "[data-test-form-element-error-messages]",
                ".fb-dash-form-element__error-field"):
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _dismiss_and_discard(page, log: Callable) -> None:
    """Close the Easy Apply modal and discard any draft (dry-run / error path)."""
    x = _first_visible(page, _DISMISS_SELECTORS)
    if x is not None:
        try:
            x.click(timeout=4000)
            page.wait_for_timeout(700)
        except Exception:  # noqa: BLE001
            pass
    discard = _first_visible(page, _DISCARD_SELECTORS)
    if discard is not None:
        try:
            discard.click(timeout=4000)
            page.wait_for_timeout(500)
        except Exception:  # noqa: BLE001
            pass


def _apply_to_job(page, job: dict, profile: dict, dry_run: bool,
                  log: Callable, warnings: list) -> dict:
    """Drive the Easy Apply funnel for a single job. Returns a result entry. The
    final Submit is skipped in dry-run. Success is ONLY a confirmed "application
    was sent" banner — a click alone is never trusted."""
    entry = {"job_id": job["job_id"], "url": job["url"],
             "title": job["title"], "company": job["company"]}

    page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)

    # The job detail pane (which holds the Easy Apply button) hydrates after the
    # initial load, so poll for a few seconds before concluding it's absent —
    # a fixed short wait made this a flaky false "no Easy Apply button".
    apply_btn = None
    for _ in range(10):
        page.wait_for_timeout(600)
        apply_btn = _first_visible(page, _EASY_APPLY_SELECTORS)
        if apply_btn is not None:
            break
    if apply_btn is None:
        # No Easy Apply button: either already applied or an off-site apply.
        already = False
        try:
            already = page.get_by_text("Applied", exact=False).count() > 0
        except Exception:  # noqa: BLE001
            pass
        entry.update(
            status="skipped", submitted=False,
            reason="already applied" if already
            else "no Easy Apply button (external apply or job closed)")
        return entry

    try:
        apply_btn.click(timeout=6000)
    except Exception as exc:  # noqa: BLE001
        entry.update(status="failed", submitted=False,
                     reason=f"could not click Easy Apply ({exc})")
        return entry

    # Wait for the modal to open. LinkedIn fetches it over XHR, so it can take
    # several seconds — be patient rather than declaring a false "didn't open".
    modal = None
    for _ in range(20):
        modal = _first_visible(page, [_MODAL_SELECTOR])
        if modal is not None:
            break
        page.wait_for_timeout(500)
    if modal is None:
        entry.update(status="failed", submitted=False,
                     reason="Easy Apply modal did not open")
        return entry

    try:
        for _step in range(_MAX_MODAL_STEPS):
            page.wait_for_timeout(800)
            try:
                page.evaluate(_FILL_JS, profile)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"{job['job_id']}: form-fill error ({exc})")

            submit = _first_visible(page, _SUBMIT_SELECTORS)
            if submit is not None:
                if dry_run:
                    entry.update(status="prepared", submitted=False, reason="dry_run")
                    log(f"✓ {job['job_id']}: application prepared (dry-run, NOT sent)")
                    return entry
                submit.click(timeout=8000)
                applied = False
                for _ in range(16):
                    for txt in _SUCCESS_TEXTS:
                        try:
                            # A visible banner only — count() alone would match
                            # hidden/off-screen template text and false-positive.
                            if page.get_by_text(txt, exact=False).first.is_visible():
                                applied = True
                                break
                        except Exception:  # noqa: BLE001
                            pass
                    if applied:
                        break
                    page.wait_for_timeout(500)
                if applied:
                    entry.update(status="submitted", submitted=True,
                                 confirmation="LinkedIn confirmed 'application was sent'")
                    log(f"✓ {job['job_id']}: Easy Apply submitted AND confirmed")
                else:
                    entry.update(status="unconfirmed", submitted=False,
                                 reason="clicked Submit but no confirmation banner")
                    log(f"⚠ {job['job_id']}: submit unconfirmed — NOT counting as applied")
                return entry

            nxt = _first_visible(page, _NEXT_SELECTORS)
            if nxt is None:
                entry.update(status="failed", submitted=False,
                             reason="no Next/Review/Submit button (unexpected modal layout)")
                return entry
            nxt.click(timeout=8000)
            page.wait_for_timeout(1000)
            if _has_form_error(page):
                entry.update(status="skipped", submitted=False,
                             reason="required screening question we couldn't answer")
                log(f"↷ {job['job_id']}: unanswerable required question — skipping")
                return entry

        entry.update(status="failed", submitted=False,
                     reason=f"did not reach Submit within {_MAX_MODAL_STEPS} steps")
        return entry
    finally:
        # Always clean up an open/draft modal so it doesn't block the next job —
        # including the exception path, where status was never set (None). Only a
        # confirmed submit leaves nothing to discard.
        if entry.get("status") != "submitted":
            _dismiss_and_discard(page, log)


# ── orchestration ────────────────────────────────────────────────────────────

def run_linkedin_apply(
    *,
    keywords: str,
    location: str = "",
    remote: bool = False,
    phone: str = "",
    years_experience: int = 3,
    max_applications: int = 5,
    max_pages: int = 10,
    dry_run: bool = True,
    relevance_fn: Callable[[str, str], tuple[bool, str]] | None = None,
    log: Callable[[str], None] | None = None,
    state_path: str | None = None,
) -> dict:
    """Load the saved LinkedIn session and auto-apply to Easy Apply jobs matching
    a keyword.

    ``max_applications`` is the number of jobs to actually apply to (prepare, in
    dry-run); the scanner auto-advances through listing pages — skipping jobs
    already applied to or filtered out — until it reaches that target or runs out
    of new jobs (up to ``max_pages``). See the module docstring."""
    from playwright.sync_api import sync_playwright

    log = log or _noop
    relevance_fn = relevance_fn or (lambda _t, _m: (True, ""))
    state_path = state_path or os.getenv("LINKEDIN_STORAGE_STATE", DEFAULT_STATE_PATH)
    max_applications = max(1, min(int(max_applications), 1000))
    max_pages = max(1, min(int(max_pages), 500))
    try:
        years = int(years_experience)  # preserve an explicit 0 (entry-level)
    except (TypeError, ValueError):
        years = 3
    profile = {
        "phone": (phone or "").strip(),
        "email": "",
        "location": (location or "").strip(),
        "years": years,
        "salary": "0",
    }

    base = {"keywords": keywords, "location": location, "dry_run": dry_run,
            "applied": [], "skipped": [], "warnings": []}

    if not (state_path and os.path.exists(state_path)):
        return {**base, "error": (
            f"No saved LinkedIn session at '{state_path}'. Run "
            "`uv run python scripts/linkedin_login.py` once to log in and save it."
        )}

    warnings: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            # LinkedIn aggressively bot-blocks headless browsers (empty results /
            # auth walls). A visible window renders results reliably. (Headless
            # needs a virtual display, e.g. Xvfb, on a server.)
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            storage_state=state_path,
            user_agent=_UA,
            locale="en-US",
            viewport={"width": 1366, "height": 900},
        )
        page = ctx.new_page()
        try:
            first_url = _search_url(keywords, location, 1, remote)
            log(f"Opening {first_url}")
            page.goto(first_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)
            if _looks_logged_out(page):
                return {**base, "error": (
                    "Not logged in to LinkedIn. The saved session may have "
                    "expired — re-run `uv run python scripts/linkedin_login.py`."
                )}
            log("✓ Authenticated with LinkedIn")

            applied: list[dict] = []
            skipped: list[dict] = []
            seen: set[str] = set()
            scanned = 0
            filtered = 0
            pages_scanned = 0
            page_num = 1

            while len(applied) < max_applications and page_num <= max_pages:
                jobs = _collect_page_jobs(page, keywords, location, page_num,
                                          log, warnings, remote)
                if jobs is None:
                    page_num += 1
                    continue
                new_jobs = [j for j in jobs if j["job_id"] and j["job_id"] not in seen]
                if not new_jobs:
                    log(f"No new jobs on page {page_num}; reached the end.")
                    break
                for j in new_jobs:
                    seen.add(j["job_id"])
                pages_scanned += 1

                for job in new_jobs:
                    if len(applied) >= max_applications:
                        break
                    scanned += 1
                    jid = job["job_id"]
                    log(f"[{len(applied)}/{max_applications} applied | scan #{scanned}] "
                        f"{jid} — {job['title'][:60]}")
                    try:
                        if job["applied"]:
                            job.update(status="skipped", reason="already applied")
                            log(f"↷ {jid}: already applied — next")
                            skipped.append(job)
                            continue

                        # Second gate: LLM relevance. Fail open — a raising judge
                        # must never silently drop a good job.
                        meta = f"Company: {job['company']}" if job["company"] else ""
                        try:
                            keep, why = relevance_fn(job["title"], meta)
                        except Exception as exc:  # noqa: BLE001
                            keep, why = True, ""
                            log(f"⚠ {jid}: relevance filter errored ({exc}); keeping (fail-open)")
                        if not keep:
                            reason = f"filtered out: {why}" if why else "filtered out by task_filter"
                            job.update(status="skipped", reason=reason, filtered=True)
                            log(f"↷ {jid}: {reason} — next")
                            skipped.append(job)
                            filtered += 1
                            continue

                        entry = _apply_to_job(page, job, profile, dry_run, log, warnings)
                        if entry.get("status") in ("prepared", "submitted"):
                            applied.append(entry)
                        else:
                            skipped.append(entry)
                        page.wait_for_timeout(2500)  # human-like pause between jobs
                    except Exception as exc:  # noqa: BLE001 — one bad job never stops the run
                        job.update(status="failed",
                                   reason=f"{type(exc).__name__}: {exc}")
                        log(f"✗ {jid}: {type(exc).__name__}: {exc}")
                        skipped.append(job)

                page_num += 1

            warnings_out = warnings[:50]
            if scanned == 0:
                return {**base, "jobs_found": 0, "warnings": warnings_out,
                        "summary": f"No Easy Apply jobs found for '{keywords}'."}

            submitted_n = sum(1 for e in applied if e.get("submitted"))
            verb = "prepared (dry-run)" if dry_run else "submitted & confirmed"
            summary = (
                f"'{keywords}': {scanned} job(s) scanned across "
                f"{pages_scanned} page(s), {len(applied)} {verb}, "
                f"{len(skipped)} skipped"
            )
            summary += f" ({filtered} filtered by task_filter)." if filtered else "."
            return {
                **base,
                "jobs_found": scanned,
                "pages_scanned": pages_scanned,
                "applied": applied,
                "skipped": skipped,
                "applied_count": len(applied),
                "submitted_count": submitted_n,
                "skipped_count": len(skipped),
                "filtered_count": filtered,
                "warnings": warnings_out,
                "summary": summary,
            }
        finally:
            browser.close()


# ── BaseTool wrapper (no relevance gate; catalog/agent parity) ────────────────

class LinkedInApplyInput(BaseModel):
    keywords: str
    location: str = ""
    remote: bool = False
    phone: str = ""
    years_experience: int = 3
    max_applications: int = 5
    max_pages: int = 10
    dry_run: bool = True


class LinkedInApplyTool(BaseTool):
    name: str = "linkedin_apply"
    description: str = (
        "Log in to LinkedIn (via a saved session) and auto-apply to 'Easy Apply' "
        "jobs matching a keyword. Args: keywords, location, remote (only remote "
        "jobs), phone, years_experience, max_applications, max_pages, dry_run. "
        "Pages through /jobs/search/?f_AL=true, opens each job, walks the Easy "
        "Apply modal (fill → Next/Review → Submit), skips already-applied jobs, "
        "and submits only when dry_run is false (a confirmed 'application was "
        "sent' banner is required)."
    )
    args_schema: type[BaseModel] = LinkedInApplyInput

    def _run(self, keywords: str, location: str = "", remote: bool = False,
             phone: str = "", years_experience: int = 3,
             max_applications: int = 5, max_pages: int = 10,
             dry_run: bool = True) -> dict:
        try:
            return run_linkedin_apply(
                keywords=keywords, location=location, remote=remote,
                phone=phone, years_experience=years_experience,
                max_applications=max_applications, max_pages=max_pages,
                dry_run=dry_run,
            )
        except Exception as exc:  # noqa: BLE001
            return {"keywords": keywords, "applied": [], "skipped": [],
                    "warnings": [], "error": f"{type(exc).__name__}: {exc}"}
