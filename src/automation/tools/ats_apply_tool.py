"""
Auto-apply across applicant-tracking systems: Greenhouse, Lever, Ashby and
Workable (apply), SmartRecruiters (discovery only — listed for manual apply).

Phase 1 of ``doc/job-platforms-automation-plan.md``. Every platform plugs in as
an ``Adapter`` with four steps; everything else — title / location matching,
the "already applied" ledger, the LLM relevance gate, the answer rules, caps,
dry-run — is shared:

  1. ``discover(client, ref)``  → the board's open jobs (public JSON API)
  2. ``load_form(client, job)`` → the application form's questions, read
     WITHOUT a browser (Lever: server-rendered HTML; Ashby: public GraphQL;
     Workable: form API; Greenhouse: Job Board API)
  3. ``plan(form, job, profile, answer_fn)`` → answers + the labels of required
     questions we can't answer (→ the job is skipped, never guessed)
  4. ``apply(page, job, answers, dry_run, log)`` → fill in a real browser and,
     unless dry-run, submit; counted as applied only on the platform's
     confirmation.

``boards`` takes any mix of board / job URLs and ``platform:token`` shorthand
(``lever:palantir, https://jobs.ashbyhq.com/openai, greenhouse:figma``); see
``ats_router``. Scraper policy (CLAUDE.md): partial results + ``warnings``, one
bad job never aborts the run.
"""
import os
import tempfile
from collections.abc import Callable

from crewai.tools import BaseTool
from pydantic import BaseModel

from src.automation.tools.ats_common import (
    build_profile,
    first_visible,
    interleave,
    launch_browser,
    load_ledger,
    match_title,
    noop,
    off_loop,
    record_applied,
    split_list,
    url_path_has,
    visible_text,
)
from src.automation.tools.ats_router import SUPPORTED, BoardRef, parse_board_ref

DEFAULT_LEDGER_PATH = "data/ats_applied.json"
USER_AGENT = "agent-auto-system/ats-apply"


class Adapter:
    """One ATS. Subclasses set ``platform`` / ``label`` and implement the four
    steps. ``can_apply = False`` makes the runner list the platform's jobs under
    ``manual`` (with their apply link) instead of applying."""
    platform = ""
    label = ""
    can_apply = True

    def discover(self, client, ref: BoardRef) -> list[dict]:
        """All open jobs on the board as ``[{platform, token, job_id, title,
        company, location, url, apply_url}]``. Raise on failure; the runner
        turns that into a warning."""
        raise NotImplementedError

    def load_form(self, client, job: dict):
        raise NotImplementedError

    def plan(self, form, job: dict, profile: dict, answer_fn) -> tuple[list[dict], list[str]]:
        raise NotImplementedError

    def apply(self, page, job: dict, answers: list[dict], dry_run: bool, log: Callable) -> dict:
        raise NotImplementedError

    def ledger_key(self, job: dict) -> str:
        return f"{job['platform']}:{job['token']}/{job['job_id']}"

    def ledger_status(self, job: dict, ledger: dict) -> str | None:
        """"applied", "pending" (submitted, never confirmed) or None."""
        entry = ledger.get(self.ledger_key(job))
        if entry is None:
            return None
        return entry.get("status", "applied") if isinstance(entry, dict) else "applied"

    def already_applied(self, job: dict, ledger: dict) -> bool:
        return self.ledger_status(job, ledger) is not None

    def record(self, ledger_path: str, job: dict, status: str = "applied") -> None:
        record_applied(ledger_path, self.ledger_key(job), job, status)


def _adapters() -> dict[str, Adapter]:
    from src.automation.tools.ats_ashby import AshbyAdapter
    from src.automation.tools.ats_greenhouse import GreenhouseAdapter
    from src.automation.tools.ats_lever import LeverAdapter
    from src.automation.tools.ats_smartrecruiters import SmartRecruitersAdapter
    from src.automation.tools.ats_workable import WorkableAdapter
    return {a.platform: a for a in (GreenhouseAdapter(), LeverAdapter(), AshbyAdapter(),
                                    WorkableAdapter(), SmartRecruitersAdapter())}


# ── the browser half every adapter shares ────────────────────────────────────

def entry_for(job: dict, answers: list[dict]) -> dict:
    """The result row for one job: its identity plus the planned answers (a
    file is shown by basename only)."""
    entry = {k: job.get(k, "") for k in ("platform", "token", "job_id", "title", "company",
                                         "location", "url")}
    entry["answers"] = [{"label": str(a.get("label", ""))[:80],
                         "value": os.path.basename(str(a["value"])) if a.get("kind") == "file"
                         else a.get("value")}
                        for a in answers]
    return entry


def open_form(page, url: str, ready_selector: str, settle_ms: int = 1000) -> bool:
    """Load ``url`` and wait until ``ready_selector`` exists and the page has
    hydrated (filling before hydration is silently reset on React forms)."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception:  # noqa: BLE001
        return False
    for _ in range(40):
        try:
            if page.locator(ready_selector).count() > 0:
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:  # noqa: BLE001 — long-polling page; carry on
                    pass
                page.wait_for_timeout(settle_ms)
                return True
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(500)
    return False


def submit_and_confirm(page, job: dict, entry: dict, *, dry_run: bool, label: str,
                       submit_selectors: list[str], success_texts: tuple[str, ...],
                       success_path_segments: tuple[str, ...] = (),
                       verify_texts: tuple[str, ...] = (),
                       error_selector: str = "", log: Callable = noop) -> dict:
    """Click Submit (unless dry-run) and wait for proof. Success is ONLY the
    platform's confirmation — a Submit click alone is never trusted."""
    jid = f"{job['platform']}:{job['token']}/{job['job_id']}"
    submit = first_visible(page, submit_selectors)
    if submit is None:
        entry.update(status="failed", submitted=False, reason="no Submit button on the form")
        return entry
    if dry_run:
        entry.update(status="prepared", submitted=False, reason="dry_run")
        log(f"✓ {jid}: application prepared (dry-run, NOT sent)")
        return entry
    submit.click(timeout=8000)
    for _ in range(40):
        page.wait_for_timeout(500)
        url = page.url or ""
        if url_path_has(url, success_path_segments) or visible_text(page, success_texts):
            entry.update(status="submitted", submitted=True,
                         confirmation=f"{label} confirmation shown")
            log(f"✓ {jid}: submitted AND confirmed")
            return entry
        if verify_texts and visible_text(page, verify_texts):
            entry.update(status="needs_verification", submitted=False,
                         reason=f"{label} asked for an e-mailed code; finish this one by hand")
            log(f"⚠ {jid}: verification required — NOT counting as applied")
            return entry
    errors = []
    if error_selector:
        try:
            errors = page.evaluate(
                """(sel) => Array.from(document.querySelectorAll(sel))
                     .filter(e => e.offsetParent !== null && (e.innerText || '').trim())
                     .map(e => e.innerText.trim().slice(0, 120)).slice(0, 3)""", error_selector)
        except Exception:  # noqa: BLE001
            errors = []
    entry.update(status="unconfirmed", submitted=False,
                 reason=("form rejected: " + " | ".join(errors)) if errors
                 else "clicked Submit but no confirmation")
    log(f"⚠ {jid}: submit unconfirmed — NOT counting as applied")
    return entry


def tick(locator) -> None:
    """Check a radio / checkbox and verify it took. A coordinate click can land
    on an overlay (an open dropdown, a captcha badge) and leave the box
    unchanged, so fall back to a DOM ``click()``, which fires the same events
    without hit-testing. Raises when the box still isn't checked."""
    try:
        locator.check(timeout=3000)
    except Exception:  # noqa: BLE001
        locator.evaluate("e => { if (!e.checked) e.click(); }")
    if not locator.is_checked():
        raise RuntimeError("box did not become checked")


def pick_autocomplete(page, input_locator, typed: str, option_selector: str,
                      want: str = "", also: tuple[str, ...] = ()) -> bool:
    """Type into an async autocomplete and click the suggestion matching
    ``want`` (then each of ``also``, e.g. just the city). With no ``want`` the
    first suggestion is taken. Returns False — clicking nothing — when no
    suggestion appears or none matches: a stranger's option is never picked."""
    from src.automation.tools.ats_common import match_option
    input_locator.click(timeout=5000)
    try:
        editable = input_locator.is_editable(timeout=1000)
    except Exception:  # noqa: BLE001
        editable = False
    if editable and typed:  # a read-only combobox is a click-to-open list
        input_locator.fill("")
        input_locator.press_sequentially(typed, delay=60)
    opts = page.locator(option_selector)
    for _ in range(16):
        if opts.count() > 0:
            break
        page.wait_for_timeout(400)
    texts = [t.strip() for t in opts.all_inner_texts()] if opts.count() else []
    if not texts:
        return False
    if want:
        def _match(w: str) -> str | None:
            # A fallback like a bare city prefers "Taipei, Taipei City, TWN"
            # (first part == the city) over a shorter "Taipei County, TWN".
            exact_head = [t for t in texts if t.split(",")[0].strip().lower() == w.strip().lower()]
            return exact_head[0] if exact_head else match_option(texts, w)
        pick = next((p for p in (_match(w) for w in (want, *also) if w) if p), None)
        if pick is None:
            try:
                page.keyboard.press("Escape")
            except Exception:  # noqa: BLE001
                pass
            return False
        idx = texts.index(pick)
    else:
        idx = 0
    opts.nth(idx).click(timeout=5000)
    page.wait_for_timeout(300)
    return True


# ── orchestration ────────────────────────────────────────────────────────────

def _discover(client, refs: list[BoardRef], adapters: dict, keywords: list[str],
              locations: list[str], log: Callable, warnings: list) -> tuple[list[dict], int]:
    groups: list[list[dict]] = []
    total = 0
    for ref in refs:
        adapter = adapters.get(ref.platform)
        where = f"{ref.platform}:{ref.company}"
        if adapter is None:
            warnings.append(f"{where}: {ref.platform} is not supported yet")
            log(f"⚠ {where}: {ref.platform} is not supported yet — skipped")
            continue
        try:
            jobs = adapter.discover(client, ref)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{where}: could not list jobs ({exc})")
            log(f"⚠ {where}: could not list jobs ({exc})")
            continue
        total += len(jobs)
        picked = []
        for j in jobs:
            if ref.job_id:
                if j["job_id"].lower() != ref.job_id.lower():
                    continue
            elif not match_title(j.get("title", ""), j.get("location", ""), keywords, locations):
                continue
            picked.append(j)
        if ref.job_id and not picked:
            warnings.append(f"{where}/{ref.job_id}: job not found on the board (closed?)")
        log(f"{where}: {len(picked)} matching of {len(jobs)} open job(s)")
        groups.append(picked)
    return interleave(groups), total


def run_ats_apply(
    *,
    boards: str,
    first_name: str,
    last_name: str,
    email: str,
    keywords: str = "",
    location: str = "",
    phone: str = "",
    resume_path: str = "",
    linkedin_url: str = "",
    github_url: str = "",
    website_url: str = "",
    current_location: str = "",
    cover_letter: str = "",
    years_experience: int = 3,
    salary_expectation: str = "",
    work_authorized: bool = True,
    needs_sponsorship: bool = False,
    authorized_countries: str = "",
    school: str = "",
    degree: str = "",
    discipline: str = "",
    current_company: str = "",
    current_title: str = "",
    current_start: str = "",
    extra_answers: str = "",
    current_salary: str = "",
    notice_period: str = "",
    languages: str = "",
    programming_languages: str = "",
    ai_experience: str = "",
    reason_for_leaving: str = "",
    has_criminal_record: bool = False,
    max_applications: int = 5,
    max_per_board: int = 0,
    max_scans_per_board: int = 0,
    dry_run: bool = True,
    relevance_fn: Callable[[str, str], tuple[bool, str]] | None = None,
    answer_fn: Callable[[str, str, list[str], dict], str] | None = None,
    log: Callable[[str], None] | None = None,
    ledger_path: str | None = None,
    client=None,
    adapters: dict | None = None,
) -> dict:
    """Discover matching jobs on the given boards (any supported ATS) and apply
    to up to ``max_applications`` of them. See the module docstring."""
    import httpx

    log = log or noop
    relevance_fn = relevance_fn or (lambda _t, _m: (True, ""))
    ledger_path = ledger_path or os.getenv("ATS_LEDGER", DEFAULT_LEDGER_PATH)
    resume_path = (resume_path or os.getenv("ATS_RESUME_PATH", "")
                   or os.getenv("GREENHOUSE_RESUME_PATH", "")).strip()
    max_applications = max(1, min(int(max_applications), 200))
    adapters = adapters or _adapters()

    board_list = split_list(boards)
    refs: list[BoardRef] = []
    warnings: list[str] = []
    for b in board_list:
        ref = parse_board_ref(b)
        if ref is None:
            warnings.append(f"'{b}': not a recognised ATS board — use a board URL or "
                            "platform:token (e.g. lever:palantir)")
            log(f"⚠ '{b}': not a recognised ATS board — skipped")
            continue
        refs.append(ref)

    base = {"boards": board_list, "keywords": keywords, "location": location,
            "dry_run": dry_run, "applied": [], "skipped": [], "manual": [], "warnings": warnings}
    if not refs:
        return {**base, "error": "No usable boards given. Paste board URLs or use "
                                 f"platform:token with one of: {', '.join(SUPPORTED)}."}
    if not resume_path or not os.path.isfile(resume_path):
        return {**base, "error": (
            f"Resume file not found ('{resume_path or 'unset'}'). Set resume_path to a "
            "PDF/DOCX on this server, or ATS_RESUME_PATH in .env.")}

    owns_client = client is None
    client = client or httpx.Client(timeout=25, follow_redirects=True,
                                    headers={"User-Agent": USER_AGENT})
    cover_tmp = None
    try:
        log("Discovering jobs via each ATS's public API...")
        candidates, total_listed = _discover(client, refs, adapters, split_list(keywords),
                                             split_list(location), log, warnings)
        if not candidates:
            return {**base, "jobs_found": 0, "total_listed": total_listed,
                    "warnings": warnings[:50],
                    "summary": f"No matching jobs on {', '.join(board_list)}."}
        log(f"{len(candidates)} matching job(s) across {len(refs)} board(s)")

        if cover_letter.strip():
            fd, cover_tmp = tempfile.mkstemp(prefix="cover_letter_", suffix=".txt")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(cover_letter.strip())
        profile = build_profile(
            first_name=first_name, last_name=last_name, email=email, phone=phone,
            current_location=current_location, linkedin_url=linkedin_url,
            github_url=github_url, website_url=website_url, years_experience=years_experience,
            salary_expectation=salary_expectation, work_authorized=work_authorized,
            needs_sponsorship=needs_sponsorship, authorized_countries=authorized_countries,
            resume_path=resume_path, cover_letter_path=cover_tmp, cover_letter_text=cover_letter,
            extra_answers=extra_answers, current_salary=current_salary,
            notice_period=notice_period, languages=languages,
            programming_languages=programming_languages, ai_experience=ai_experience,
            reason_for_leaving=reason_for_leaving, has_criminal_record=has_criminal_record,
            school=school, degree=degree, discipline=discipline,
            current_company=current_company, current_title=current_title,
            current_start=current_start,
        )
        ledger = load_ledger(ledger_path)

        applied: list[dict] = []
        skipped: list[dict] = []
        manual: list[dict] = []
        pending: list[dict] = []
        scanned = filtered = 0
        per_board: dict[str, int] = {}
        scans: dict[str, int] = {}
        max_per_board = max(0, int(max_per_board or 0))
        max_scans_per_board = max(0, int(max_scans_per_board or 0))
        headless = os.getenv("ATS_HEADLESS", os.getenv("GREENHOUSE_HEADLESS", "0")) == "1"

        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = None
            page = None
            try:
                for job in candidates:
                    adapter = adapters[job["platform"]]
                    board = f"{job['platform']}:{job['token']}"
                    jid = f"{board}/{job['job_id']}"
                    if not adapter.can_apply:
                        # Listed even after the cap: they cost nothing to report.
                        manual.append({**job, "status": "manual",
                                       "reason": f"{adapter.label} apply isn't automated yet — "
                                                 "open apply_url to apply by hand"})
                        continue
                    # Unconfirmed submissions may have reached the employer, so
                    # they use up the cap like confirmed ones.
                    if len(applied) + len(pending) >= max_applications:
                        continue
                    if max_per_board and per_board.get(board, 0) >= max_per_board:
                        continue  # this company's quota is used; keep the others going
                    if max_scans_per_board and scans.get(board, 0) >= max_scans_per_board:
                        continue  # looked at enough of this company's jobs
                    scans[board] = scans.get(board, 0) + 1
                    scanned += 1
                    log(f"[{len(applied)}/{max_applications} applied | scan #{scanned}] "
                        f"{jid} — {job['title'][:60]}")
                    try:
                        seen = adapter.ledger_status(job, ledger)
                        if seen == "pending":
                            job.update(status="skipped", reason=(
                                "submitted on an earlier run but never confirmed (ledger) — check "
                                "your e-mail / the employer's portal; remove it from the ledger to retry"))
                            log(f"↷ {jid}: earlier submission unconfirmed — not resending")
                            skipped.append(job)
                            continue
                        if seen:
                            job.update(status="skipped", reason="already applied (ledger)")
                            log(f"↷ {jid}: already applied — next")
                            skipped.append(job)
                            continue

                        meta = f"Company: {job['company']}\nLocation: {job['location']}"
                        try:
                            keep, why = off_loop(relevance_fn, job["title"], meta)
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

                        try:
                            form = adapter.load_form(client, job)
                        except Exception as exc:  # noqa: BLE001
                            job.update(status="failed", reason=f"could not load form questions ({exc})")
                            skipped.append(job)
                            continue

                        def _ask(label, kind, options, _job=job):
                            return answer_fn(label, kind, options, _job) if answer_fn else ""

                        answers, unanswered = adapter.plan(form, job, profile,
                                                           _ask if answer_fn else None)
                        if unanswered:
                            job.update(status="skipped",
                                       reason="unanswered required question(s): "
                                       + "; ".join(u[:70] for u in unanswered[:3]),
                                       unanswered=unanswered)
                            log(f"↷ {jid}: {len(unanswered)} required question(s) we can't "
                                "answer — next (add them to extra_answers)")
                            skipped.append(job)
                            continue

                        if browser is None:
                            browser = launch_browser(pw, headless)
                            page = browser.new_context(
                                locale="en-US", viewport={"width": 1366, "height": 900},
                            ).new_page()
                        entry = adapter.apply(page, job, answers, dry_run, log)
                        if entry.get("status") in ("prepared", "submitted"):
                            applied.append(entry)
                            per_board[board] = per_board.get(board, 0) + 1
                            if entry.get("submitted"):
                                adapter.record(ledger_path, job)
                                ledger[adapter.ledger_key(job)] = {"status": "applied"}
                        elif not dry_run and entry.get("status") in ("unconfirmed", "needs_verification"):
                            # Submit was clicked: the employer may have it. Block a
                            # resend until the user reconciles; not counted as applied.
                            adapter.record(ledger_path, job, status="pending")
                            ledger[adapter.ledger_key(job)] = {"status": "pending"}
                            pending.append(entry)
                            skipped.append(entry)
                        else:
                            skipped.append(entry)
                        page.wait_for_timeout(2000)  # human-like pause between jobs
                    except Exception as exc:  # noqa: BLE001 — one bad job never stops the run
                        job.update(status="failed", reason=f"{type(exc).__name__}: {exc}")
                        log(f"✗ {jid}: {type(exc).__name__}: {exc}")
                        skipped.append(job)
            finally:
                if browser is not None:
                    browser.close()

        submitted_n = sum(1 for e in applied if e.get("submitted"))
        verb = "prepared (dry-run)" if dry_run else "submitted & confirmed"
        summary = (f"{scanned} matching job(s) scanned on {len(refs)} board(s), "
                   f"{len(applied)} {verb}, {len(skipped)} skipped")
        if pending:
            summary += f", {len(pending)} submitted but unconfirmed (check by hand)"
        if manual:
            summary += f", {len(manual)} listed for manual apply"
        summary += f" ({filtered} filtered by task_filter)." if filtered else "."
        # Summary and counts lead: the LLM judge reads only the first few KB.
        return {
            "summary": summary,
            "jobs_found": scanned,
            "total_listed": total_listed,
            "applied_count": len(applied),
            "submitted_count": submitted_n,
            "skipped_count": len(skipped),
            "filtered_count": filtered,
            "manual_count": len(manual),
            "unconfirmed_count": len(pending),
            **base,
            "applied": applied,
            "skipped": [{k: v for k, v in e.items() if k != "answers"} for e in skipped],
            "manual": manual[:100],
            "warnings": warnings[:50],
        }
    finally:
        if owns_client:
            client.close()
        if cover_tmp:
            try:
                os.remove(cover_tmp)
            except OSError:
                pass


# ── BaseTool wrapper (no LLM gates; catalog/agent parity) ─────────────────────

class ATSApplyInput(BaseModel):
    boards: str
    first_name: str
    last_name: str
    email: str
    keywords: str = ""
    location: str = ""
    phone: str = ""
    resume_path: str = ""
    max_applications: int = 5
    dry_run: bool = True


class ATSApplyTool(BaseTool):
    name: str = "ats_apply"
    description: str = (
        "Auto-apply to jobs on company job boards hosted by Greenhouse, Lever, Ashby or "
        "Workable (SmartRecruiters jobs are listed for manual apply). Args: boards (board "
        "URLs or platform:token, comma-separated), first_name, last_name, email, keywords "
        "(title filter), location, phone, resume_path, max_applications, dry_run. Reads "
        "each form's questions without a browser, answers them from the profile, fills "
        "the form in a browser and submits only when dry_run is false."
    )
    args_schema: type[BaseModel] = ATSApplyInput

    def _run(self, boards: str, first_name: str, last_name: str, email: str,
             keywords: str = "", location: str = "", phone: str = "",
             resume_path: str = "", max_applications: int = 5,
             dry_run: bool = True) -> dict:
        try:
            return run_ats_apply(
                boards=boards, first_name=first_name, last_name=last_name,
                email=email, keywords=keywords, location=location, phone=phone,
                resume_path=resume_path, max_applications=max_applications,
                dry_run=dry_run,
            )
        except Exception as exc:  # noqa: BLE001
            return {"boards": boards, "applied": [], "skipped": [], "manual": [],
                    "warnings": [], "error": f"{type(exc).__name__}: {exc}"}
