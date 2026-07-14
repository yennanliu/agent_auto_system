"""
Auto-apply (應徵) to job openings on 104.com.tw (Taiwan's 104 人力銀行).

This is a faithful Playwright port of the reference browser script
(ai_experiment/104/gemini/104_auto_apply_with_controls.js). 104's apply flow is
DOM-driven, not a clean JSON API: clicking 應徵 opens the in-page application
form (?apply=form), which contains a free-text 自我推薦信 (cover letter) textarea
pre-filled with your 系統預設 letter, then a 確認送出 button. We drive that DOM.

The cover letter IS free text (a `<textarea>`, max 2000 chars) — so a custom
`cover_letter` string is typed straight into it, overriding the default. The
resume (履歷) is chosen via a separate multiselect which we leave on the user's
default. When `cover_letter` is empty the site's 系統預設 letter is left in place.

Auth is a persisted browser session (cookies), created ONCE via
`scripts/104_login.py` — same pattern as the Shopee scraper. 104 guards password
login with captcha / SMS-OTP, so we never automate the login itself.

Flow per run:
  1. Load the job-search listing with the saved session; bail if logged out.
  2. Page through /jobs/search/?keyword=...&area=...&order=...&page=N.
  3. For each job card: read title + company; skip if already applied (已應徵).
  4. Optional second gate: an LLM `relevance_fn(title, meta)` decides whether the
     job is worth applying to (natural-language task_filter). Fails open.
  5. Apply: click 應徵, switch to the application tab, select the cover letter
     (a saved 推薦信 by name, or the site default), click 確認送出, and confirm
     the URL lands on /job/apply/done/. Only a confirmed done-URL counts as
     applied — a plain click is never trusted as success.
  6. `dry_run` (default) does everything EXCEPT the final 確認送出, so you can
     preview which jobs would be applied to without sending real applications.

The LLM here is only the relevance judge (which is why the automation runs with
whichever provider/model the run selected — gemini, openai, anthropic, ...). The
apply itself is pure DOM automation.

Scraper policy (per CLAUDE.md): return partial results + a `warnings` list
instead of raising; one bad job must never abort the whole run.

`run_tw104_apply(...)` is the primary entry point (the flow calls it directly
with an LLM-backed relevance_fn). `TW104ApplyTool` is a thin BaseTool wrapper for
catalog/agent parity.
"""
import os
from collections.abc import Callable

from crewai.tools import BaseTool
from pydantic import BaseModel

_BASE = "https://www.104.com.tw"
_SEARCH = _BASE + "/jobs/search/"
DEFAULT_STATE_PATH = "data/tw104_state.json"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Job ids (/job/<id>) and the 已應徵 "already applied" flag are extracted inside
# the page.evaluate() block in _collect_page_jobs (JS-side), not with Python
# regex/string constants — see that function.

# DOM selectors, mirroring the reference script. Kept as fallbacks-in-order lists
# because 104's markup uses hashed/utility class names that shift over time; the
# first selector that matches wins. Text-based locators (:has-text) are the most
# durable, so they lead where possible.
_APPLY_BTN_SELECTORS = [
    "button.apply-button__button",
    ".apply-button__button",
    "button:has-text('應徵')",
    "a:has-text('應徵')",
]
_CONFIRM_SEND_SELECTORS = [
    "button:has-text('確認送出')",
    "button:has-text('送出應徵')",
    "button:has-text('確定送出')",
]
# The 自我推薦信 (cover letter) free-text field on the application form. It's the
# only <textarea> on the apply page; the placeholder text is the most stable hook.
_COVER_LETTER_SELECTORS = [
    "textarea[placeholder*='自我推薦']",
    "textarea.form-control",
    "textarea",
]
_COVER_LETTER_MAX = 2000  # site's own limit (the counter reads "n/2000")
_DONE_URL_FRAGMENT = "/job/apply/done"


def _noop(_msg: str) -> None:
    pass


# ── session / auth ───────────────────────────────────────────────────────────

def _looks_logged_out(page) -> bool:
    """Positive login signals beat the ever-present login link in 104's header.
    We only declare "logged out" when a login prompt is actually visible."""
    try:
        if "/login" in page.url or "/vip/login" in page.url:
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        for sel in (':text("登出")', ':text("我的104")', 'img[alt*="頭像"]',
                    'a[href*="/mylife"]'):
            if page.locator(sel).count() > 0:
                return False
    except Exception:  # noqa: BLE001
        pass
    try:
        loc = page.locator('a:has-text("會員登入"), a:has-text("登入")')
        return loc.count() > 0 and loc.first.is_visible()
    except Exception:  # noqa: BLE001
        return False


# ── search / listing ─────────────────────────────────────────────────────────

def _search_url(keyword: str, area: str, order: str, page_num: int) -> str:
    from urllib.parse import urlencode
    params = {"keyword": keyword, "order": order or "1", "page": page_num}
    if area:
        params["area"] = area
    return f"{_SEARCH}?{urlencode(params)}"


def _collect_page_jobs(page, keyword: str, area: str, order: str, page_num: int,
                       log: Callable, warnings: list) -> list[dict] | None:
    """Job cards on a single listing page. Scrolls to trigger 104's lazy load,
    then reads each card's job id / title / company / already-applied flag.

    Returns [{job_id, url, title, company, applied}], deduped by job id — or
    None if the page could not be read (transient load/parse failure). None is
    distinct from []: [] means the page loaded but had no jobs (end of results),
    so the caller stops; None means "try the next page" — a transient blip must
    not look like the end of the listing and abort the whole run.
    The goto is the one unguarded external call; retry a few times first."""
    url = _search_url(keyword, area, order, page_num)
    for attempt in range(3):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1500)
            break
        except Exception as exc:  # noqa: BLE001
            if attempt == 2:
                warnings.append(f"page {page_num}: failed to load after 3 tries ({exc})")
                log(f"⚠ Page {page_num}: failed to load after 3 attempts ({exc})")
                return None
            log(f"⚠ Page {page_num}: load attempt {attempt + 1} failed, retrying ({exc})")
            page.wait_for_timeout(2000)

    prev = -1
    for _ in range(8):
        try:
            count = page.locator("a[href*='/job/']").count()
        except Exception:  # noqa: BLE001
            count = 0
        if count == prev:
            break
        prev = count
        try:
            page.evaluate("() => window.scrollBy(0, 3000)")
        except Exception:  # noqa: BLE001
            try:
                page.mouse.wheel(0, 3000)
            except Exception:  # noqa: BLE001
                pass
        page.wait_for_timeout(1000)

    # Extract one row per job link, reading the enclosing card for company +
    # 已應徵 state. Done in the page context in one pass for speed/robustness.
    try:
        rows = page.evaluate(
            """() => {
                const out = [];
                const seen = new Set();
                const anchors = Array.from(document.querySelectorAll("a[href*='/job/']"));
                for (const a of anchors) {
                    const href = a.getAttribute('href') || '';
                    const m = href.match(/\\/job\\/([0-9a-z]+)/i);
                    if (!m) continue;
                    const id = m[1].toLowerCase();
                    if (seen.has(id)) continue;
                    // walk up to the card container
                    let card = a;
                    for (let i = 0; i < 6 && card && card.parentElement; i++) {
                        const cls = (card.className || '') + '';
                        if (/job-list-container|job-summary|b-block--top/i.test(cls)) break;
                        card = card.parentElement;
                    }
                    const cardText = (card && card.innerText) || a.innerText || '';
                    const title = (a.getAttribute('title') || a.innerText || '').trim();
                    let company = '';
                    if (card) {
                        const c = card.querySelector("a[href*='/company/'], [class*='company']");
                        if (c) company = (c.getAttribute('title') || c.innerText || '').trim();
                    }
                    seen.add(id);
                    out.push({
                        job_id: id,
                        href,
                        title: title.slice(0, 200),
                        company: company.slice(0, 120),
                        applied: cardText.indexOf('已應徵') !== -1,
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
        href = r.get("href") or ""
        full = href if href.startswith("http") else _BASE + href
        jobs.append({
            "job_id": r.get("job_id", ""),
            "url": full.split("?")[0],
            "title": r.get("title", "") or r.get("job_id", ""),
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


def _fill_cover_letter(popup, cover_letter: str, log: Callable,
                       warnings: list, job_id: str) -> None:
    """Type a custom 自我推薦信 into the free-text textarea, overriding the site's
    系統預設 letter. When `cover_letter` is empty the default is left untouched.
    Never raises — a fill failure degrades to the default rather than aborting.

    104 is a Vue app, so we use fill() (which dispatches input events the model
    listens to) rather than setting .value directly."""
    text = (cover_letter or "").strip()
    if not text:
        return
    if len(text) > _COVER_LETTER_MAX:
        text = text[:_COVER_LETTER_MAX]
        warnings.append(f"{job_id}: cover letter truncated to {_COVER_LETTER_MAX} chars")
    box = _first_visible(popup, _COVER_LETTER_SELECTORS)
    if box is None:
        warnings.append(f"{job_id}: 自我推薦信 textarea not found; used site default")
        log("  · 自我推薦信 field not found — using site default letter")
        return
    try:
        box.fill(text, timeout=6000)
        log(f"  · 自我推薦信 set to custom text ({len(text)} chars)")
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"{job_id}: could not fill 自我推薦信 ({exc}); used default")
        log(f"  · could not fill 自我推薦信 ({exc}) — using site default")


def _apply_to_job(context, page, job: dict, cover_letter: str, dry_run: bool,
                  log: Callable, warnings: list) -> dict:
    """Drive the apply funnel for a single job. Returns a result entry. The final
    確認送出 is skipped in dry-run. Success is ONLY a confirmed /job/apply/done/
    URL — a click alone is never trusted."""
    entry = {"job_id": job["job_id"], "url": job["url"],
             "title": job["title"], "company": job["company"]}

    # Open the job page and find its 應徵 button.
    page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(1200)
    apply_btn = _first_visible(page, _APPLY_BTN_SELECTORS)
    if apply_btn is None:
        entry.update(status="skipped", submitted=False,
                     reason="no 應徵 button found (job closed or layout changed)")
        return entry

    # Clicking 應徵 usually opens the application in a new tab; handle both a
    # popup and same-tab navigation.
    popup = None
    try:
        with context.expect_page(timeout=6000) as pop_info:
            apply_btn.click(timeout=6000)
        popup = pop_info.value
        popup.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:  # noqa: BLE001 — no popup: the form likely opened in-page
        popup = page
        page.wait_for_timeout(1500)

    try:
        popup.wait_for_timeout(1000)
        _fill_cover_letter(popup, cover_letter, log, warnings, job["job_id"])

        if dry_run:
            entry.update(status="prepared", submitted=False, reason="dry_run")
            log(f"✓ {job['job_id']}: application prepared (dry-run, NOT sent)")
            return entry

        send = _first_visible(popup, _CONFIRM_SEND_SELECTORS)
        if send is None:
            entry.update(status="failed", submitted=False,
                         reason="no 確認送出 button found on application page")
            return entry
        send.click(timeout=8000)
        # Confirm the site navigated to the done page — the only trusted signal.
        applied = False
        for _ in range(12):
            try:
                if _DONE_URL_FRAGMENT in popup.url:
                    applied = True
                    break
            except Exception:  # noqa: BLE001
                pass
            popup.wait_for_timeout(500)
        if applied:
            entry.update(status="submitted", submitted=True,
                         confirmation="landed on /job/apply/done/")
            log(f"✓ {job['job_id']}: 應徵 submitted AND confirmed by site")
        else:
            entry.update(status="unconfirmed", submitted=False,
                         reason="clicked 確認送出 but never reached /job/apply/done/")
            log(f"⚠ {job['job_id']}: submit unconfirmed — NOT counting as applied")
        return entry
    finally:
        # Close the popup tab so it doesn't accumulate; keep the main page.
        if popup is not None and popup is not page:
            try:
                popup.close()
            except Exception:  # noqa: BLE001
                pass


# ── orchestration ────────────────────────────────────────────────────────────

def run_tw104_apply(
    *,
    keyword: str,
    area: str = "",
    order: str = "1",
    max_applications: int = 5,
    max_pages: int = 10,
    cover_letter: str = "",
    dry_run: bool = True,
    relevance_fn: Callable[[str, str], tuple[bool, str]] | None = None,
    log: Callable[[str], None] | None = None,
    state_path: str | None = None,
) -> dict:
    """Load the saved 104 session and auto-apply to open jobs matching a keyword.

    ``max_applications`` is the number of jobs to actually apply to (prepare, in
    dry-run); the scanner auto-advances through listing pages — skipping jobs
    already applied to or filtered out — until it reaches that target or runs out
    of new jobs (up to ``max_pages``). See the module docstring."""
    from playwright.sync_api import sync_playwright

    log = log or _noop
    relevance_fn = relevance_fn or (lambda _t, _m: (True, ""))
    state_path = state_path or os.getenv("TW104_STORAGE_STATE", DEFAULT_STATE_PATH)
    max_applications = max(1, min(int(max_applications), 200))
    max_pages = max(1, min(int(max_pages), 50))

    base = {"keyword": keyword, "area": area, "dry_run": dry_run,
            "applied": [], "skipped": [], "warnings": []}

    if not (state_path and os.path.exists(state_path)):
        return {**base, "error": (
            f"No saved 104.com.tw session at '{state_path}'. Run "
            "`uv run python scripts/104_login.py` once to log in and save it."
        )}

    warnings: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            storage_state=state_path,
            user_agent=_UA,
            locale="zh-TW",
            viewport={"width": 1366, "height": 900},
        )
        page = ctx.new_page()
        try:
            # Warm the session on the search page and confirm we're logged in.
            first_url = _search_url(keyword, area, order, 1)
            log(f"Opening {first_url}")
            page.goto(first_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1500)
            if _looks_logged_out(page):
                return {**base, "error": (
                    "Not logged in to 104.com.tw. The saved session may have "
                    "expired — re-run `uv run python scripts/104_login.py`."
                )}
            log("✓ Authenticated with 104.com.tw")

            applied: list[dict] = []
            skipped: list[dict] = []
            seen: set[str] = set()
            scanned = 0
            filtered = 0
            pages_scanned = 0
            page_num = 1

            while len(applied) < max_applications and page_num <= max_pages:
                jobs = _collect_page_jobs(page, keyword, area, order, page_num,
                                          log, warnings)
                if jobs is None:
                    # Transient load/parse failure — skip to the next page rather
                    # than treating it as end-of-results and aborting the run.
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
                            job.update(status="skipped", reason="already applied (已應徵)")
                            log(f"↷ {jid}: already applied — next")
                            skipped.append(job)
                            continue

                        # Second gate: LLM relevance. Fail open — a raising judge
                        # must never silently drop a good job.
                        meta = f"公司：{job['company']}" if job["company"] else ""
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

                        entry = _apply_to_job(ctx, page, job, cover_letter,
                                              dry_run, log, warnings)
                        if entry.get("status") in ("prepared", "submitted"):
                            applied.append(entry)
                        else:
                            skipped.append(entry)
                    except Exception as exc:  # noqa: BLE001 — one bad job never stops the run
                        job.update(status="failed",
                                   reason=f"{type(exc).__name__}: {exc}")
                        log(f"✗ {jid}: {type(exc).__name__}: {exc}")
                        skipped.append(job)

                page_num += 1

            warnings_out = warnings[:50]
            if scanned == 0:
                return {**base, "jobs_found": 0, "warnings": warnings_out,
                        "summary": f"No open jobs found for keyword '{keyword}'."}

            submitted_n = sum(1 for e in applied if e.get("submitted"))
            verb = "prepared (dry-run)" if dry_run else "submitted & confirmed"
            summary = (
                f"Keyword '{keyword}': {scanned} job(s) scanned across "
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

class TW104ApplyInput(BaseModel):
    keyword: str
    area: str = ""
    order: str = "1"
    max_applications: int = 5
    max_pages: int = 10
    cover_letter: str = ""
    dry_run: bool = True


class TW104ApplyTool(BaseTool):
    name: str = "tw104_apply"
    description: str = (
        "Log in to 104.com.tw (via a saved session) and auto-apply (應徵) to open "
        "jobs matching a keyword. Args: keyword, area (104 area codes, optional), "
        "order, max_applications, max_pages, cover_letter (custom 自我推薦信 free "
        "text, optional), dry_run. Skips already-applied jobs and submits only when "
        "dry_run is false (a confirmed /job/apply/done/ URL is required)."
    )
    args_schema: type[BaseModel] = TW104ApplyInput

    def _run(self, keyword: str, area: str = "", order: str = "1",
             max_applications: int = 5, max_pages: int = 10,
             cover_letter: str = "", dry_run: bool = True) -> dict:
        try:
            return run_tw104_apply(
                keyword=keyword, area=area, order=order,
                max_applications=max_applications, max_pages=max_pages,
                cover_letter=cover_letter, dry_run=dry_run,
            )
        except Exception as exc:  # noqa: BLE001
            return {"keyword": keyword, "applied": [], "skipped": [],
                    "warnings": [], "error": f"{type(exc).__name__}: {exc}"}
