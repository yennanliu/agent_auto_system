"""
Auto-apply across applicant-tracking systems: Greenhouse, Lever, Ashby,
Workable, Recruitee, Teamtailor, SmartRecruiters (guest apply) and Workday (only with a
stored per-employer account). SmartRecruiters and Workday plan in the browser.
Personio and BambooHR are discovery only: a bot check guards their forms.

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
from src.automation.tools.resume_tailor import Tailor, bind_tailor

DEFAULT_LEDGER_PATH = "data/ats_applied.json"
USER_AGENT = "agent-auto-system/ats-apply"


class Adapter:
    """One ATS. Subclasses set ``platform`` / ``label`` and implement the four
    steps. ``can_apply = False`` makes the runner list the platform's jobs under
    ``manual`` (with their apply link) instead of applying."""
    platform = ""
    label = ""
    can_apply = True
    # True when discover() answers a job ref with only that job (a search),
    # not the whole board — the runner then caches per job, not per board.
    job_scoped = False
    # Shown on this platform's manual[] rows when can_apply is False.
    manual_reason = "apply isn't automated yet — open apply_url to apply by hand"
    # True when the questions can only be read after signing in (Workday): the
    # runner skips load_form / plan and calls apply_live, which plans each step
    # in the browser with the same answer rules.
    plans_in_browser = False

    def discover(self, client, ref: BoardRef, search: tuple[str, ...] = ()) -> list[dict]:
        """All open jobs on the board as ``[{platform, token, job_id, title,
        company, location, url, apply_url}]``. Raise on failure; the runner
        turns that into a warning. ``search`` is the run's keywords: a hint an
        adapter may use to search server-side instead of listing a huge board
        (Workday); the runner still filters what comes back."""
        raise NotImplementedError

    def load_form(self, client, job: dict):
        raise NotImplementedError

    def plan(self, form, job: dict, profile: dict, answer_fn) -> tuple[list[dict], list[str]]:
        raise NotImplementedError

    def apply(self, page, job: dict, answers: list[dict], dry_run: bool, log: Callable) -> dict:
        raise NotImplementedError

    def manual_reason_for(self, job: dict, profile: dict | None = None) -> str | None:
        """Why this job must be applied to by hand, or None to apply. Platform
        wide by default; an adapter can decide per job (Workday: is there a
        stored account for this employer?)."""
        return None if self.can_apply else self.manual_reason

    def apply_live(self, page, job: dict, profile: dict, answer_fn, dry_run: bool,
                   log: Callable) -> dict:
        """plans_in_browser adapters: plan and fill in one pass. Returns the
        same entry shape as apply(); a required question it can't answer gives
        ``status="skipped"`` with ``unanswered``."""
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


def discover_with_hint(adapter, client, ref: BoardRef, search: tuple[str, ...] = ()) -> list[dict]:
    """``adapter.discover`` with the ``search`` hint when it takes one. Adapters
    written before the hint existed (plugins, test doubles) keep working."""
    import inspect
    try:
        takes_hint = "search" in inspect.signature(adapter.discover).parameters
    except (TypeError, ValueError):
        takes_hint = False
    return adapter.discover(client, ref, search=search) if takes_hint else adapter.discover(client, ref)


def _adapters() -> dict[str, Adapter]:
    from src.automation.tools.ats_ashby import AshbyAdapter
    from src.automation.tools.ats_bamboohr import BambooHRAdapter
    from src.automation.tools.ats_greenhouse import GreenhouseAdapter
    from src.automation.tools.ats_lever import LeverAdapter
    from src.automation.tools.ats_personio import PersonioAdapter
    from src.automation.tools.ats_recruitee import RecruiteeAdapter
    from src.automation.tools.ats_smartrecruiters import SmartRecruitersAdapter
    from src.automation.tools.ats_teamtailor import TeamtailorAdapter
    from src.automation.tools.ats_workable import WorkableAdapter
    from src.automation.tools.ats_workday import WorkdayAdapter
    return {a.platform: a for a in (GreenhouseAdapter(), LeverAdapter(), AshbyAdapter(),
                                    WorkableAdapter(), SmartRecruitersAdapter(), WorkdayAdapter(),
                                    RecruiteeAdapter(), PersonioAdapter(), BambooHRAdapter(),
                                    TeamtailorAdapter())}


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
              locations: list[str], log: Callable, warnings: list,
              sourced: set | frozenset = frozenset()) -> tuple[list[dict], int]:
    """Matching jobs across ``refs``, interleaved by board. A job ref the user
    pasted is an explicit pick and skips the filters; a ref found by a discovery
    source (``sourced``) is still filtered by title and location. Each board is
    listed once, however many refs point at it."""
    by_board: dict[tuple, list[dict]] = {}  # picked jobs, grouped per board for round-robin
    total = 0
    listings: dict[tuple, list[dict] | None] = {}
    seen: set[tuple] = set()
    for ref in refs:
        adapter = adapters.get(ref.platform)
        where = f"{ref.platform}:{ref.company}"
        if adapter is None:
            warnings.append(f"{where}: {ref.platform} is not supported yet")
            log(f"⚠ {where}: {ref.platform} is not supported yet — skipped")
            continue
        # Tokens as written: a failed lookup of one spelling never shadows another.
        board_key = (ref.platform, ref.company, ref.region)
        # A job-scoped adapter (Workday) answers a job ref with just that job,
        # not the board: cache it per job so a second job on the same board is
        # still looked up. Everyone else lists the board once.
        query_key = board_key + ((ref.job_id,) if ref.job_id and adapter.job_scoped else ())
        if query_key not in listings:
            try:
                listings[query_key] = discover_with_hint(adapter, client, ref, tuple(keywords))
                total += len(listings[query_key])
            except Exception as exc:  # noqa: BLE001
                listings[query_key] = None
                warnings.append(f"{where}: could not list jobs ({exc})")
                log(f"⚠ {where}: could not list jobs ({exc})")
        jobs = listings[query_key]
        if jobs is None:
            continue
        explicit = ref not in sourced
        picked = []
        for j in jobs:
            if ref.job_id and j["job_id"].lower() != ref.job_id.lower():
                continue
            if not (ref.job_id and explicit) and \
                    not match_title(j.get("title", ""), j.get("location", ""), keywords, locations):
                continue
            key = (j["platform"], j["token"].lower(), j["job_id"].lower())
            if key in seen:
                continue
            seen.add(key)
            picked.append(j)
        if ref.job_id and explicit and not picked:
            warnings.append(f"{where}/{ref.job_id}: job not found on the board (closed?)")
        log(f"{where}{'/' + ref.job_id if ref.job_id else ''}: {len(picked)} matching of "
            f"{len(jobs)} open job(s)")
        by_board.setdefault(board_key, []).extend(picked)
    # One group per board, so three links into board A and one into board B
    # interleave A1, B1, A2, A3 — not A1, A2, A3, B1.
    return interleave(list(by_board.values())), total


def run_ats_apply(
    *,
    boards: str = "",
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
    sources: str = "",
    max_applications: int = 5,
    max_per_board: int = 0,
    max_scans_per_board: int = 0,
    dry_run: bool = True,
    create_workday_accounts: bool = False,
    relevance_fn: Callable[[str, str], tuple[bool, str]] | None = None,
    answer_fn: Callable[[str, str, list[str], dict], str] | None = None,
    resume_tailor: Tailor | None = None,
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
    for adapter in adapters.values():
        if hasattr(adapter, "create_accounts"):  # Workday: opt-in account creation
            adapter.create_accounts = bool(create_workday_accounts)

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

    source_list = split_list(sources)
    base = {"boards": board_list, "sources": source_list, "keywords": keywords,
            "location": location, "dry_run": dry_run, "applied": [], "skipped": [],
            "manual": [], "warnings": warnings}
    if not refs and not source_list:
        return {**base, "error": "No usable boards or sources given. Paste board URLs, use "
                                 f"platform:token with one of: {', '.join(SUPPORTED)}, or pick "
                                 "discovery sources (e.g. hn)."}

    owns_client = client is None
    client = client or httpx.Client(timeout=25, follow_redirects=True,
                                    headers={"User-Agent": USER_AGENT})
    cover_tmp = None
    try:
        source_manual: list[dict] = []
        sourced: set[BoardRef] = set()
        if source_list:
            from src.automation.tools.ats_sources import collect
            log(f"Collecting jobs from discovery sources: {', '.join(source_list)}...")
            found = collect(client, source_list, split_list(keywords), split_list(location), log)
            warnings.extend(found["warnings"])
            source_manual = found["manual"]
            for ref in found["refs"]:
                if ref not in refs:
                    refs.append(ref)
                    sourced.add(ref)
            base["manual"] = source_manual[:100]
        log("Discovering jobs via each ATS's public API...")
        candidates, total_listed = _discover(client, refs, adapters, split_list(keywords),
                                             split_list(location), log, warnings, sourced)
        # A résumé is only needed when something can actually be auto-applied
        # to — not for a run whose matches are all manual leads.
        if any(adapters[c["platform"]].manual_reason_for(c, {"email": email}) is None
               for c in candidates) \
                and (not resume_path or not os.path.isfile(resume_path)):
            return {**base, "manual_count": len(source_manual), "error": (
                f"Resume file not found ('{resume_path or 'unset'}'). Set resume_path to a "
                "PDF/DOCX on this server, or ATS_RESUME_PATH in .env.")}
        if not candidates:
            where = ", ".join(board_list + source_list)
            summary = f"No matching jobs to auto-apply to on {where}."
            if source_manual:
                summary += f" {len(source_manual)} lead(s) listed for manual apply."
            return {**base, "jobs_found": 0, "total_listed": total_listed,
                    "manual_count": len(source_manual), "warnings": warnings[:50],
                    "summary": summary}
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
        # Only forms with a paste-résumé option use it (Greenhouse today).
        profile["tailor_resume"] = bind_tailor(resume_tailor, resume_path, profile, log, warnings)
        ledger = load_ledger(ledger_path)
        # "Have you applied to <Company> before?" is a fact the ledger knows.
        profile["applied_companies"] = {
            (e.get("company") or "").lower() for e in ledger.values()
            if isinstance(e, dict) and e.get("status") in ("applied", "pending") and e.get("company")}

        applied: list[dict] = []
        skipped: list[dict] = []
        manual: list[dict] = list(source_manual)
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
                    why_manual = adapter.manual_reason_for(job, profile)
                    if why_manual:
                        # Listed even after the cap: they cost nothing to report.
                        manual.append({**job, "status": "manual",
                                       "reason": f"{adapter.label}: {why_manual}"})
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

                        def _ask(label, kind, options, _job=job):
                            return answer_fn(label, kind, options, _job) if answer_fn else ""

                        def _page():
                            nonlocal browser, page
                            if browser is None:
                                browser = launch_browser(pw, headless)
                                page = browser.new_context(
                                    locale="en-US", viewport={"width": 1366, "height": 900},
                                ).new_page()
                            return page

                        if adapter.plans_in_browser:
                            entry = adapter.apply_live(_page(), job, profile,
                                                       _ask if answer_fn else None, dry_run, log)
                        else:
                            try:
                                form = adapter.load_form(client, job)
                            except Exception as exc:  # noqa: BLE001
                                job.update(status="failed", reason=f"could not load form questions ({exc})")
                                skipped.append(job)
                                continue
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
                            entry = adapter.apply(_page(), job, answers, dry_run, log)
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
                        elif entry.get("status") == "manual":
                            manual.append(entry)
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
        summary = (f"{scanned} matching job(s) scanned on {len(refs)} board(s)"
                   + (f" (incl. {len(sourced)} found via {', '.join(source_list)})" if sourced else "")
                   + ", "
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
        "Auto-apply to jobs on company job boards hosted by Greenhouse, Lever, Ashby, "
        "Workable, Recruitee, SmartRecruiters or Workday (with a stored account). Args: boards (board "
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
