"""
Auto-apply to job openings hosted on Greenhouse (boards.greenhouse.io).

Unlike LinkedIn / 104, Greenhouse is an applicant-tracking system with no
candidate login and no global job search: each employer has a public *job
board* identified by a token (``boards.greenhouse.io/<token>``). That makes the
funnel unusually deterministic:

  1. Discover — the public Job Board API (``boards-api.greenhouse.io``) lists a
     board's open jobs as JSON. We filter by title keywords + location text and
     interleave boards so one big employer can't eat the whole limit.
  2. Skip jobs already submitted from this machine (a small local ledger — the
     site itself has no "already applied" signal without a login).
  3. Optional second gate: an LLM ``relevance_fn(title, meta)`` decides whether
     the job is worth applying to (natural-language task_filter). Fails open.
  4. Plan — the same API returns the application form's questions (label,
     required flag, field name, select options). We answer them in Python from
     the candidate profile (``plan_answers``), BEFORE touching the browser. A
     required question we can't answer confidently skips the job instead of
     submitting a guess; an optional LLM ``answer_fn`` can answer screening
     questions from the candidate's own background text.
  5. Apply — open the embeddable application form
     (``job-boards.greenhouse.io/embed/job_app?for=<token>&token=<id>``, which
     never redirects to the employer's own careers site), fill each planned
     field by its id (the DOM ids equal the API field names), upload the resume,
     and Submit. Only Greenhouse's confirmation page counts as applied.
     A Submit with no confirmation is ledgered as "pending": not counted as
     applied, but never resent automatically (the employer may have it).
  6. ``dry_run`` (default) fills the whole form but never clicks Submit, and
     returns the planned answers so you can review exactly what would be sent.

Scraper policy (per CLAUDE.md): return partial results + a ``warnings`` list
instead of raising; one bad job must never abort the whole run.

The platform-neutral parts (answer rules, option matching, title matching,
ledger, browser helpers) live in ``ats_common`` and are shared with the other
ATS adapters; this module keeps only what is Greenhouse-specific: the Job
Board API, its question JSON → ``Question`` mapping, its structured
education / employment / EEOC blocks, and the react-select DOM.

``run_greenhouse_apply(...)`` is the primary entry point (the flow calls it with
LLM-backed ``relevance_fn`` / ``answer_fn``). ``GreenhouseApplyTool`` is a thin
BaseTool wrapper for catalog/agent parity.
"""
import os
import re
import tempfile
from collections.abc import Callable
from urllib.parse import parse_qs, urlparse

from crewai.tools import BaseTool
from pydantic import BaseModel

from src.automation.tools.ats_common import (
    Question,
    answer_question,
    build_profile,
    countries_in,  # noqa: F401 — re-exported for callers / tests
    decline_option,
    expand_keywords,  # noqa: F401 — re-exported for callers / tests
    first_visible,
    interleave,
    job_countries,  # noqa: F401 — re-exported for callers / tests
    launch_browser,
    ledger_claim,
    load_ledger,
    match_option,
    match_title,
    noop,
    off_loop,
    parse_extra_answers,
    parse_start,
    split_list,
    url_path_has,
    visible_text,
    with_job_location,
    years_option,
)
from src.automation.tools.ats_common import record_applied as _record_applied
from src.automation.tools.resume_tailor import Tailor, bind_tailor, tailored_text

# Pre-refactor private names, kept so existing callers / tests keep working.
_noop = noop
_years_option = years_option
_parse_start = parse_start
_parse_extra_answers = parse_extra_answers
# Looked up at call time so tests can patch it on this module.
_launch_browser = launch_browser

_API = "https://boards-api.greenhouse.io/v1/boards"
_EMBED_APP = "https://job-boards.greenhouse.io/embed/job_app?for={token}&token={job_id}"
_HOSTED_JOB = "https://job-boards.greenhouse.io/{token}/jobs/{job_id}"
DEFAULT_LEDGER_PATH = "data/greenhouse_applied.json"

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_GH_HOSTS = ("greenhouse.io",)

_SUBMIT_SELECTORS = [
    "button[type='submit']:has-text('Submit')",
    "button:has-text('Submit application')",
    "input[type='submit']",
]
_SUCCESS_TEXTS = ("Thank you for applying", "application has been submitted",
                  "Application submitted", "received your application")
# Newer boards e-mail a one-time code before accepting the application.
_VERIFY_TEXTS = ("security code", "verification code", "verify your email")
_ERROR_SELECTOR = "[id$='-error'], .helper-text--error, .error-message"



# ── board parsing / discovery (JSON API) ─────────────────────────────────────

def parse_board_ref(ref: str) -> tuple[str, str] | None:
    """A board token (and optional job id) from whatever the user pasted.

    Accepts a bare token (``figma``), a board URL
    (``https://boards.greenhouse.io/figma``, ``job-boards.greenhouse.io/figma``),
    a job URL (``…/figma/jobs/5426468004``) or an embed URL
    (``…/embed/job_board?for=figma``, ``…/embed/job_app?for=figma&token=123``).
    Returns ``(token, job_id)`` with job_id ``""`` for a whole board, or None
    when the input isn't a recognisable Greenhouse board."""
    ref = (ref or "").strip().strip("/")
    if not ref:
        return None
    if "://" not in ref and "/" not in ref and "." not in ref:
        return (ref.lower(), "") if _TOKEN_RE.match(ref) else None
    if "://" not in ref:
        ref = "https://" + ref
    u = urlparse(ref)
    host = u.netloc.lower().split(":")[0]
    if not any(host == h or host.endswith("." + h) for h in _GH_HOSTS):  # not evilgreenhouse.io
        return None
    q = parse_qs(u.query)
    parts = [p for p in u.path.split("/") if p]
    if parts and parts[0] == "embed":
        token = (q.get("for") or [""])[0]
        job_id = (q.get("token") or [""])[0]
    else:
        token = parts[0] if parts else ""
        job_id = parts[2] if len(parts) >= 3 and parts[1] == "jobs" else ""
    job_id = job_id or (q.get("gh_jid") or [""])[0]
    if not token or not _TOKEN_RE.match(token):
        return None
    return token.lower(), job_id if job_id.isdigit() else ""



def match_job(job: dict, keywords: list[str], locations: list[str]) -> bool:
    """``ats_common.match_title`` on a Job Board API job dict."""
    return match_title(job.get("title") or "", (job.get("location") or {}).get("name") or "",
                       keywords, locations)



def discover_jobs(client, boards: list[str], keywords: list[str],
                  locations: list[str], log: Callable,
                  warnings: list) -> tuple[list[dict], int]:
    """Matching jobs across boards as ``[{token, job_id, title, company,
    location, url}]`` (interleaved), plus the total number of jobs listed."""
    groups: list[list[dict]] = []
    total = 0
    for ref in boards:
        parsed = parse_board_ref(ref)
        if parsed is None:
            warnings.append(f"'{ref}': not a Greenhouse board token or URL")
            log(f"⚠ '{ref}': not a Greenhouse board token or URL — skipped")
            continue
        token, only_id = parsed
        try:
            resp = client.get(f"{_API}/{token}/jobs")
            if resp.status_code == 404:
                warnings.append(f"{token}: no such Greenhouse board")
                log(f"⚠ {token}: no such Greenhouse board")
                continue
            resp.raise_for_status()
            jobs = resp.json().get("jobs") or []
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{token}: could not list jobs ({exc})")
            log(f"⚠ {token}: could not list jobs ({exc})")
            continue
        total += len(jobs)
        picked = []
        for j in jobs:
            jid = str(j.get("id") or "")
            if not jid or (only_id and jid != only_id):
                continue
            # A pasted job URL is an explicit pick — don't let filters drop it.
            if not only_id and not match_job(j, keywords, locations):
                continue
            picked.append({
                "token": token,
                "job_id": jid,
                "title": (j.get("title") or "").strip()[:200] or jid,
                "company": (j.get("company_name") or token).strip()[:120],
                "location": ((j.get("location") or {}).get("name") or "").strip()[:160],
                "url": _HOSTED_JOB.format(token=token, job_id=jid),
            })
        log(f"{token}: {len(picked)} matching of {len(jobs)} open job(s)")
        groups.append(picked)
    return interleave(groups), total


def fetch_questions(client, token: str, job_id: str) -> dict:
    resp = client.get(f"{_API}/{token}/jobs/{job_id}", params={"questions": "true"})
    resp.raise_for_status()
    return resp.json()


# ── answer planning (pure; unit-tested) ──────────────────────────────────────

_STANDARD_TEXT = {"first_name": "first_name", "last_name": "last_name",
                  "email": "email", "phone": "phone"}
_KIND_BY_TYPE = {"input_text": "text", "textarea": "text", "input_file": "file",
                 "multi_value_single_select": "select",
                 "multi_value_multi_select": "multiselect"}


def plan_answers(job_data: dict, profile: dict,
                 answer_fn: Callable[[str, str, list[str]], str] | None = None,
                 ) -> tuple[list[dict], list[str]]:
    """Answer an application form described by the Job Board API.

    Returns ``(answers, unanswered)``: ``answers`` is
    ``[{field, kind, value, label}]`` with kind one of text / select /
    multiselect / file / location, and ``unanswered`` lists the labels of
    REQUIRED questions we could not answer — the caller skips such a job rather
    than submitting a guess. Optional questions without a confident answer are
    simply left blank. ``answer_fn(label, kind, options) -> str`` is consulted
    only for required custom questions the heuristics miss; return "" to abstain.
    """
    answers: list[dict] = []
    unanswered: list[str] = []
    paste_resume = False  # the form offers "Enter manually" beside the résumé upload
    profile = with_job_location(profile, (job_data.get("location") or {}).get("name"),
                                job_data.get("company_name"))

    def add(field: str, kind: str, value, label: str) -> None:
        answers.append({"field": field, "kind": kind, "value": value, "label": label})

    for q in job_data.get("questions") or []:
        label = (q.get("label") or "").strip()
        required = bool(q.get("required"))
        fields = [f for f in q.get("fields") or [] if f.get("type") in _KIND_BY_TYPE]
        names = {f.get("name") for f in fields}
        paste_resume = paste_resume or {"resume", "resume_text"} <= names
        done = False
        for f in fields:
            name, kind = f.get("name") or "", _KIND_BY_TYPE[f["type"]]
            options = [str(v.get("label")) for v in f.get("values") or [] if v.get("label")]
            if name in _STANDARD_TEXT:
                val = profile.get(_STANDARD_TEXT[name]) or ""
                if val:
                    add(name, "text", val, label)
                    done = True
                break
            if name == "resume":
                if profile.get("resume_path"):
                    add(name, "file", profile["resume_path"], label)
                    done = True
                break
            if name == "cover_letter":
                if profile.get("cover_letter_path"):
                    add(name, "file", profile["cover_letter_path"], label)
                    done = True
                break
            if name in ("resume_text", "cover_letter_text") and names & {"resume", "cover_letter"}:
                continue  # the paste-instead alternative of a file field
            # A custom question: the shared, platform-neutral rules.
            ans = answer_question(Question(name, label, kind, required, options), profile, answer_fn)
            if ans is not None:
                answers.append(ans)
                done = True
            break
        if required and not done and fields:
            unanswered.append(label or "(unlabelled question)")

    # Structured Education / Employment blocks (not in `questions`; the API
    # flags them as "education_required" / "employment_required"). Field ids
    # are fixed: school--0, degree--0, discipline--0, company-name-0, title-0…
    edu_mode, emp_mode = str(job_data.get("education") or ""), str(job_data.get("employment") or "")
    edu = profile.get("education") or {}
    if edu_mode.endswith(("_required", "_optional")):
        if edu.get("school") and edu.get("degree"):
            edu_opt = edu_mode.endswith("_optional")
            answers.append({"field": "school--0", "kind": "combo", "value": edu["school"],
                            "label": "School", "optional": edu_opt})
            answers.append({"field": "degree--0", "kind": "select", "value": edu["degree"],
                            "label": "Degree", "optional": edu_opt})
            if edu.get("discipline"):
                answers.append({"field": "discipline--0", "kind": "combo", "value": edu["discipline"],
                                "label": "Discipline", "optional": True})
        elif edu_mode.endswith("_required"):
            unanswered.append("Education (school / degree)")
    emp = profile.get("employment") or {}
    if emp_mode.endswith(("_required", "_optional")):
        if emp.get("company") and emp.get("title") and emp.get("start_month") and emp.get("start_year"):
            emp_opt = emp_mode.endswith("_optional")
            for fld, knd, val, lab in (("company-name-0", "text", emp["company"], "Company name"),
                                       ("title-0", "text", emp["title"], "Title"),
                                       ("start-date-month-0", "select", emp["start_month"], "Start date month"),
                                       ("start-date-year-0", "text", emp["start_year"], "Start date year"),
                                       ("current-role-0_1", "check", True, "Current role")):
                answers.append({"field": fld, "kind": knd, "value": val, "label": lab, "optional": emp_opt})
        elif emp_mode.endswith("_required"):
            unanswered.append("Employment (company / title / start date)")

    for q in job_data.get("location_questions") or []:
        for f in q.get("fields") or []:
            if f.get("name") == "location":
                if profile.get("location"):
                    add("location", "location", profile["location"], q.get("label") or "Location")
                elif q.get("required"):
                    unanswered.append(q.get("label") or "Location")

    # EEOC / compliance questions are voluntary; answer a required one with the
    # "decline to self-identify" option, never a guessed identity.
    for block in job_data.get("compliance") or []:
        for q in block.get("questions") or []:
            if not q.get("required"):
                continue
            label = q.get("label") or ""
            for f in q.get("fields") or []:
                options = [str(v.get("label")) for v in f.get("values") or [] if v.get("label")]
                pick = decline_option(options)
                if pick:
                    add(f.get("name") or "", "select", pick, label)
                else:
                    unanswered.append(label)
                break

    # Newer boards' self-identification survey: input id = the question id, and
    # the API flags the decline option explicitly. Same policy as EEOC above.
    demo = job_data.get("demographic_questions") or {}
    for q in (demo.get("questions") or []) if isinstance(demo, dict) else []:
        if not q.get("required"):
            continue
        label = q.get("label") or "demographic question"
        opts = q.get("answer_options") or []
        pick = next((o.get("label") for o in opts if o.get("decline_to_answer")), None) \
            or decline_option([o.get("label") or "" for o in opts])
        if pick and q.get("id"):
            kind = "multiselect" if q.get("type") == "multi_value_multi_select" else "select"
            add(str(q["id"]), kind, [pick] if kind == "multiselect" else pick, label)
        else:
            unanswered.append(f"{label} (demographic)")

    # tailor_resume: paste a JD-tailored résumé where the form offers "Enter
    # manually" — only for a job we will apply to, so a skipped one costs no
    # LLM call. Any failure keeps the planned file upload.
    if paste_resume and not unanswered:
        for i, a in enumerate(answers):
            if a["field"] == "resume" and a["kind"] == "file":
                text = tailored_text(profile, job_data)
                if text:
                    answers[i] = {"field": "resume_text", "kind": "paste", "value": text,
                                  "label": a["label"], "fallback": a}
                break

    return answers, unanswered


# ── local ledger (no site-side "already applied" without a login) ────────────

def _ledger_key(job: dict) -> str:
    return f"{job['token']}/{job['job_id']}"


def record_applied(path: str, job: dict, status: str = "applied") -> None:
    """``status`` "pending" = Submit was clicked but never confirmed: the employer
    may have it, so it blocks an automatic resend just like "applied"."""
    _record_applied(path, _ledger_key(job), job, status)


def _ledger_status(ledger: dict, job: dict) -> str | None:
    """"applied", "pending" or None (older entries have no status → applied).
    A "claimed" entry (another run is filling the form) reads as None here —
    ``ledger_claim`` decides right before applying whether it is still live."""
    entry = ledger.get(_ledger_key(job))
    if entry is None:
        return None
    status = entry.get("status", "applied") if isinstance(entry, dict) else "applied"
    return None if status == "claimed" else status


# ── apply funnel (DOM) ────────────────────────────────────────────────────────

def _by_id(page, field: str):
    # Attribute selector, not "#id": tolerates ids a CSS id selector would choke on.
    return page.locator(f"[id='{field}']").first


def _choose_option(page, field: str, labels: list[str], type_text: str = "",
                   require_match: bool = False) -> bool:
    """Pick option(s) in a Greenhouse react-select. Options are scoped by their
    ``react-select-<field>-option-N`` ids — the phone widget's hidden country
    list also uses role=option, so a bare [role=option] query picks strangers."""
    inp = _by_id(page, field)
    # Multi-selects render as a <fieldset id="question_N[]"> of checkboxes, so
    # an element with the field's id exists but is not a react-select input.
    if inp.count() == 0 or page.locator(f"input[type='checkbox'][name='{field}']").count() > 0:
        return _check_boxes(page, field, labels)
    chosen = 0
    for want in labels:
        inp.click(timeout=5000)
        if type_text:
            inp.press_sequentially(type_text, delay=40)
        opts = page.locator(f"[id^='react-select-{field}-option']")
        for _ in range(12):
            if opts.count() > 0:
                break
            page.wait_for_timeout(400)
        texts = [t.strip() for t in opts.all_inner_texts()]
        if not texts:
            page.keyboard.press("Escape")
            continue
        pick = match_option(texts, want) if want else texts[0]
        idx = texts.index(pick) if pick in texts else (0 if type_text and not require_match else -1)
        if idx < 0:
            page.keyboard.press("Escape")
            continue
        opts.nth(idx).click(timeout=5000)
        chosen += 1
        page.wait_for_timeout(200)
    return chosen == len(labels)


def _check_boxes(page, field: str, labels: list[str]) -> bool:
    """Multi-select questions rendered as a checkbox group (name ``<field>[]``)."""
    return bool(page.evaluate(
        """([field, labels]) => {
            const boxes = Array.from(document.querySelectorAll("input[type='checkbox']"))
              .filter(b => (b.name || '').startsWith(field) || (b.id || '').startsWith(field));
            let n = 0;
            for (const want of labels) {
              const w = want.toLowerCase();
              const text = b => {
                const l = b.id && document.querySelector('label[for="' + CSS.escape(b.id) + '"]');
                return ((l && l.innerText) || (b.closest('label') || {}).innerText || b.value || '')
                  .replace(/\\s+/g, ' ').replace(/\\*$/, '').trim().toLowerCase();
              };
              const box = boxes.find(b => text(b) === w) || boxes.find(b => text(b).startsWith(w));
              if (box) { if (!box.checked) box.click(); n++; }
            }
            return n === labels.length;
        }""",
        [field, labels],
    ))


_CONSENT_JS = """
() => {
  let n = 0;
  document.querySelectorAll("form input[type='checkbox']").forEach(b => {
    if (b.checked) return;
    const l = b.id && document.querySelector('label[for="' + b.id + '"]');
    const txt = ((l && l.innerText) || (b.closest('label') || {}).innerText || '').toLowerCase();
    const req = b.required || b.getAttribute('aria-required') === 'true';
    if (req && /consent|agree|acknowledge|privacy|terms|certify/.test(txt)) { b.click(); n++; }
  });
  return n;
}
"""


def _paste(page, field: str, text: str) -> bool:
    """Fill a paste-instead box (``resume_text``), first clicking its "Enter
    manually" button (data-testid ``resume-text``) to reveal it. False when the
    form has neither, so the caller can upload the file instead."""
    box = _by_id(page, field)
    if not (box.count() and box.is_visible()):
        button = page.locator(f"[data-testid='{field.removesuffix('_text')}-text']").first
        if button.count() == 0:
            return False
        try:
            button.click(timeout=5000)
            box.wait_for(state="visible", timeout=5000)
        except Exception:  # noqa: BLE001 — no paste box: the caller uploads the file
            return False
    try:
        box.fill(text, timeout=5000)
    except Exception:  # noqa: BLE001
        return False
    return True


def _fill_form(page, answers: list[dict], log: Callable) -> list[str]:
    """Fill every planned answer. Returns labels of fields that could not be set."""
    failed: list[str] = []
    for a in answers:
        field, kind, value, label = a["field"], a["kind"], a["value"], a["label"]
        before = len(failed)
        try:
            if a.get("optional") and _by_id(page, field).count() == 0:
                continue  # e.g. "Discipline" exists on some boards' education block only
            if kind == "text":
                el = _by_id(page, field)
                if el.count() == 0:
                    failed.append(label)
                    continue
                el.fill(str(value), timeout=5000)
            elif kind == "check":
                el = _by_id(page, field)
                if el.count() == 0:
                    failed.append(label)
                    continue
                el.check(timeout=5000)
            elif kind == "combo":
                # Async autocomplete (school, discipline): type, then take the
                # matching suggestion — never an arbitrary first one.
                if not _choose_option(page, field, [str(value)], type_text=str(value), require_match=True):
                    failed.append(label)
            elif kind == "paste":
                if not _paste(page, field, str(value)):
                    fb = a["fallback"]  # the paste box is gone: upload the file instead
                    log(f"⚠ no '{label[:40]}' paste box — uploading the file instead")
                    page.locator(f"input[type='file'][id='{fb['field']}']").first.set_input_files(
                        fb["value"], timeout=10000)
                    page.wait_for_timeout(800)
                    a.update(fb)
            elif kind == "file":
                el = page.locator(f"input[type='file'][id='{field}']").first
                if el.count() == 0:
                    failed.append(label)
                    continue
                el.set_input_files(value, timeout=10000)
                page.wait_for_timeout(800)
            elif kind == "location":
                # Autocomplete: type the city, take the best suggestion. The
                # geocoder returns nothing for "Taipei, Taiwan" but suggests
                # "Taipei, Taipei City, Taiwan" for "Taipei" — so type only the
                # first part. If none appears the typed text stays.
                if _by_id(page, field).count() == 0:
                    field = "candidate-location"
                city = str(value).split(",")[0].strip() or str(value)
                if not _choose_option(page, field, [str(value)], type_text=city):
                    log(f"⚠ location '{value}': no suggestion picked")
            elif kind in ("select", "multiselect"):
                labels = value if isinstance(value, list) else [value]
                if not _choose_option(page, field, [str(v) for v in labels]):
                    failed.append(label)
        except Exception as exc:  # noqa: BLE001
            log(f"⚠ could not fill '{label[:60]}' ({type(exc).__name__})")
            failed.append(label)
        if a.get("optional") and len(failed) > before:
            # An optional block (education_optional, …) the board's own lists
            # can't take — e.g. a school missing from its list — stays blank
            # rather than failing the whole application.
            del failed[before:]
            log(f"↷ left optional '{label[:60]}' blank (no matching option)")
            if kind in ("combo", "select"):
                try:
                    page.keyboard.press("Escape")
                except Exception:  # noqa: BLE001
                    pass
    try:
        page.evaluate(_CONSENT_JS)
    except Exception:  # noqa: BLE001
        pass
    return failed


def _form_errors(page) -> list[str]:
    try:
        return page.evaluate(
            """(sel) => Array.from(document.querySelectorAll(sel))
                 .filter(e => e.offsetParent !== null && (e.innerText || '').trim())
                 .map(e => e.innerText.trim().slice(0, 120)).slice(0, 3)""",
            _ERROR_SELECTOR,
        )
    except Exception:  # noqa: BLE001
        return []


def _open_form(page, job: dict) -> bool:
    for url in (_EMBED_APP.format(**job), job["url"]):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception:  # noqa: BLE001
            continue
        for _ in range(20):
            if page.locator("[id='first_name']").count() > 0:
                # The form is server-rendered, so #first_name exists before React
                # hydrates — and hydration resets anything filled earlier (values
                # and the uploaded resume silently vanish). Let it settle first.
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:  # noqa: BLE001 — long-polling page; carry on
                    pass
                page.wait_for_timeout(1000)
                return True
            page.wait_for_timeout(500)
    return False


def _apply_to_job(page, job: dict, answers: list[dict], dry_run: bool,
                  log: Callable, on_submit: Callable | None = None) -> dict:
    """Fill and (unless dry-run) submit one application. Success is ONLY the
    confirmation page — a Submit click alone is never trusted. ``on_submit`` is
    called immediately before the Submit click (the ledger marks the job
    "pending" there, so nothing after the click can lead to a resend)."""
    entry = {k: job[k] for k in ("token", "job_id", "title", "company", "location", "url")}

    def _record_answers() -> None:  # after filling: a paste may have fallen back to the file
        entry["answers"] = [{"label": a["label"][:80],
                             "value": os.path.basename(a["value"]) if a["kind"] == "file" else a["value"]}
                            for a in answers]
        entry["resume_tailored"] = any(a["kind"] == "paste" for a in answers)

    _record_answers()
    if not _open_form(page, job):
        entry.update(status="failed", submitted=False,
                     reason="application form did not load (job closed or not a Greenhouse form)")
        return entry

    failed = _fill_form(page, answers, log)
    _record_answers()
    if failed:
        entry.update(status="failed", submitted=False,
                     reason="could not fill: " + "; ".join(f[:60] for f in failed[:3]))
        return entry

    submit = first_visible(page, _SUBMIT_SELECTORS)
    if submit is None:
        entry.update(status="failed", submitted=False, reason="no Submit button on the form")
        return entry
    if dry_run:
        entry.update(status="prepared", submitted=False, reason="dry_run")
        log(f"✓ {job['token']}/{job['job_id']}: application prepared (dry-run, NOT sent)")
        return entry

    if on_submit is not None and on_submit() is False:
        # Our ledger claim went stale and another run took the job.
        entry.update(status="skipped", submitted=False,
                     reason="claim lost to another run before Submit — not sent")
        log(f"↷ {job['job_id']}: claim lost before Submit — not sending")
        return entry
    submit.click(timeout=8000)
    for _ in range(30):
        page.wait_for_timeout(500)
        if url_path_has(page.url or "", ("confirmation",)) or visible_text(page, _SUCCESS_TEXTS):
            entry.update(status="submitted", submitted=True,
                         confirmation="Greenhouse confirmation page shown")
            log(f"✓ {job['token']}/{job['job_id']}: submitted AND confirmed")
            return entry
        if visible_text(page, _VERIFY_TEXTS):
            entry.update(status="needs_verification", submitted=False,
                         reason="Greenhouse e-mailed a security code; finish this one by hand")
            log(f"⚠ {job['job_id']}: e-mail security code required — NOT counting as applied")
            return entry
    errors = _form_errors(page)
    entry.update(status="unconfirmed", submitted=False,
                 reason=("form rejected: " + " | ".join(errors)) if errors
                 else "clicked Submit but no confirmation page")
    log(f"⚠ {job['job_id']}: submit unconfirmed — NOT counting as applied")
    return entry


# ── orchestration ────────────────────────────────────────────────────────────

def run_greenhouse_apply(
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
    answer_fn: Callable[[str, str, list[str]], str] | None = None,
    resume_tailor: Tailor | None = None,
    log: Callable[[str], None] | None = None,
    ledger_path: str | None = None,
    client=None,
) -> dict:
    """Discover matching jobs on the given Greenhouse boards and apply to up to
    ``max_applications`` of them. See the module docstring."""
    import httpx

    log = log or noop
    relevance_fn = relevance_fn or (lambda _t, _m: (True, ""))
    ledger_path = ledger_path or os.getenv("GREENHOUSE_LEDGER", DEFAULT_LEDGER_PATH)
    resume_path = (resume_path or os.getenv("GREENHOUSE_RESUME_PATH", "")).strip()
    max_applications = max(1, min(int(max_applications), 200))
    try:
        years = int(years_experience)  # preserve an explicit 0 (entry-level)
    except (TypeError, ValueError):
        years = 3
    board_list = split_list(boards)
    keyword_list = split_list(keywords)
    location_list = split_list(location)

    base = {"boards": board_list, "keywords": keywords, "location": location,
            "dry_run": dry_run, "applied": [], "skipped": [], "warnings": []}
    if not board_list:
        return {**base, "error": "No Greenhouse boards given."}
    if not resume_path or not os.path.isfile(resume_path):
        return {**base, "error": (
            f"Resume file not found ('{resume_path or 'unset'}'). Set resume_path to a "
            "PDF/DOCX on this server, or GREENHOUSE_RESUME_PATH in .env.")}

    warnings: list[str] = []
    owns_client = client is None
    client = client or httpx.Client(timeout=20, follow_redirects=True,
                                    headers={"User-Agent": "agent-auto-system/greenhouse-apply"})
    cover_tmp = None
    try:
        log("Discovering Greenhouse jobs via the public Job Board API...")
        candidates, total_listed = discover_jobs(client, board_list, keyword_list,
                                                 location_list, log, warnings)
        if not candidates:
            return {**base, "jobs_found": 0, "total_listed": total_listed,
                    "warnings": warnings[:50],
                    "summary": f"No matching Greenhouse jobs on {', '.join(board_list)}."}
        log(f"{len(candidates)} matching job(s) across {len(board_list)} board(s)")

        if cover_letter.strip():
            fd, cover_tmp = tempfile.mkstemp(prefix="cover_letter_", suffix=".txt")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(cover_letter.strip())
        profile = build_profile(
            first_name=first_name, last_name=last_name, email=email, phone=phone,
            current_location=current_location, linkedin_url=linkedin_url,
            github_url=github_url, website_url=website_url, years_experience=years,
            salary_expectation=salary_expectation, work_authorized=work_authorized,
            needs_sponsorship=needs_sponsorship, authorized_countries=authorized_countries,
            resume_path=resume_path, cover_letter_path=cover_tmp, extra_answers=extra_answers,
            current_salary=current_salary, notice_period=notice_period, languages=languages,
            programming_languages=programming_languages, ai_experience=ai_experience,
            reason_for_leaving=reason_for_leaving, has_criminal_record=has_criminal_record,
            school=school, degree=degree, discipline=discipline,
            current_company=current_company, current_title=current_title,
            current_start=current_start,
        )
        profile["tailor_resume"] = bind_tailor(resume_tailor, resume_path, profile, log, warnings)
        ledger = load_ledger(ledger_path)

        applied: list[dict] = []
        skipped: list[dict] = []
        scanned = filtered = 0
        per_board: dict[str, int] = {}
        scans: dict[str, int] = {}
        max_per_board = max(0, int(max_per_board or 0))
        max_scans_per_board = max(0, int(max_scans_per_board or 0))
        headless = os.getenv("GREENHOUSE_HEADLESS", "0") == "1"

        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = None
            page = None
            try:
                for job in candidates:
                    if len(applied) >= max_applications:
                        break
                    if max_per_board and per_board.get(job["token"], 0) >= max_per_board:
                        continue  # this company's quota is used; keep the others going
                    if max_scans_per_board and scans.get(job["token"], 0) >= max_scans_per_board:
                        continue  # looked at enough of this company's jobs
                    scans[job["token"]] = scans.get(job["token"], 0) + 1
                    scanned += 1
                    jid = f"{job['token']}/{job['job_id']}"
                    log(f"[{len(applied)}/{max_applications} applied | scan #{scanned}] "
                        f"{jid} — {job['title'][:60]}")
                    try:
                        seen = _ledger_status(ledger, job)
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
                            job_data = fetch_questions(client, job["token"], job["job_id"])
                        except Exception as exc:  # noqa: BLE001
                            job.update(status="failed", reason=f"could not load form questions ({exc})")
                            skipped.append(job)
                            continue

                        def _ask(label, kind, options, _job=job):
                            return answer_fn(label, kind, options, _job) if answer_fn else ""

                        answers, unanswered = plan_answers(
                            job_data, profile, _ask if answer_fn else None)
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
                            browser = _launch_browser(pw, headless)
                            page = browser.new_context(
                                locale="en-US", viewport={"width": 1366, "height": 900},
                            ).new_page()
                        # Reserve the job in the ledger FILE: another run (any
                        # process) may have applied to — or be applying to — it
                        # since our snapshot was loaded. The claim turns
                        # "pending" right before the Submit click, so an
                        # unconfirmed submit, an exception or a crash after the
                        # click is never resent; a pre-Submit failure releases it.
                        with ledger_claim(ledger_path, _ledger_key(job), job,
                                          dry_run=dry_run) as claim:
                            if not claim:
                                job.update(status="skipped",
                                           reason="already applied (ledger, another run)")
                                log(f"↷ {jid}: applied / being applied by another run — next")
                                skipped.append(job)
                                continue
                            entry = _apply_to_job(page, job, answers, dry_run, log,
                                                  None if dry_run else claim.submitting)
                            if entry.get("status") in ("prepared", "submitted"):
                                applied.append(entry)
                                per_board[job["token"]] = per_board.get(job["token"], 0) + 1
                                if entry.get("submitted"):
                                    claim.confirmed()
                                    ledger[_ledger_key(job)] = {"status": "applied"}
                            else:
                                if not dry_run and entry.get("status") in (
                                        "unconfirmed", "needs_verification"):
                                    # Submit was clicked: the employer may have it.
                                    # Already "pending" via on_submit; re-assert it.
                                    claim.submitting()
                                    ledger[_ledger_key(job)] = {"status": "pending"}
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
        summary = (f"{scanned} matching job(s) scanned on {len(board_list)} board(s), "
                   f"{len(applied)} {verb}, {len(skipped)} skipped")
        summary += f" ({filtered} filtered by task_filter)." if filtered else "."
        # Summary and counts lead: the LLM judge only reads the first few KB of
        # the serialized result, and a long skipped[] used to push them past it.
        return {
            "summary": summary,
            "jobs_found": scanned,
            "total_listed": total_listed,
            "applied_count": len(applied),
            "submitted_count": submitted_n,
            "skipped_count": len(skipped),
            "filtered_count": filtered,
            **base,
            "applied": applied,
            "skipped": [{k: v for k, v in e.items() if k != "answers"} for e in skipped],
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

class GreenhouseApplyInput(BaseModel):
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


class GreenhouseApplyTool(BaseTool):
    name: str = "greenhouse_apply"
    description: str = (
        "Auto-apply to jobs on Greenhouse job boards. Args: boards (comma-separated "
        "board tokens or boards.greenhouse.io URLs), first_name, last_name, email, "
        "keywords (title filter), location, phone, resume_path, max_applications, "
        "dry_run. Lists jobs via the public Job Board API, answers each form's "
        "questions from the profile, fills the form in a browser and submits only "
        "when dry_run is false (Greenhouse's confirmation page is required)."
    )
    args_schema: type[BaseModel] = GreenhouseApplyInput

    def _run(self, boards: str, first_name: str, last_name: str, email: str,
             keywords: str = "", location: str = "", phone: str = "",
             resume_path: str = "", max_applications: int = 5,
             dry_run: bool = True) -> dict:
        try:
            return run_greenhouse_apply(
                boards=boards, first_name=first_name, last_name=last_name,
                email=email, keywords=keywords, location=location, phone=phone,
                resume_path=resume_path, max_applications=max_applications,
                dry_run=dry_run,
            )
        except Exception as exc:  # noqa: BLE001
            return {"boards": boards, "applied": [], "skipped": [],
                    "warnings": [], "error": f"{type(exc).__name__}: {exc}"}
