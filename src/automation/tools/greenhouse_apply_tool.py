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
  6. ``dry_run`` (default) fills the whole form but never clicks Submit, and
     returns the planned answers so you can review exactly what would be sent.

Scraper policy (per CLAUDE.md): return partial results + a ``warnings`` list
instead of raising; one bad job must never abort the whole run.

``run_greenhouse_apply(...)`` is the primary entry point (the flow calls it with
LLM-backed ``relevance_fn`` / ``answer_fn``). ``GreenhouseApplyTool`` is a thin
BaseTool wrapper for catalog/agent parity.
"""
import asyncio
import contextvars
import json
import os
import re
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

from crewai.tools import BaseTool
from pydantic import BaseModel

_API = "https://boards-api.greenhouse.io/v1/boards"
_EMBED_APP = "https://job-boards.greenhouse.io/embed/job_app?for={token}&token={job_id}"
_HOSTED_JOB = "https://job-boards.greenhouse.io/{token}/jobs/{job_id}"
DEFAULT_LEDGER_PATH = "data/greenhouse_applied.json"

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_GH_HOSTS = ("greenhouse.io",)

# Same anti-bot launch config as the LinkedIn apply tool: Greenhouse forms carry
# invisible reCAPTCHA, which scores headless / automation-flagged browsers low
# and silently rejects the submission. Real Chrome, headed, by default.
_STEALTH_ARGS = ["--disable-blink-features=AutomationControlled"]
_STEALTH_IGNORE = ["--enable-automation"]

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


def _noop(_msg: str) -> None:
    pass


def _off_loop(fn: Callable, *args):
    """Call ``fn(*args)`` on a loop-free worker thread when an asyncio loop is
    running here. Playwright's sync API keeps one alive, and crewai's sync
    kickoff refuses to run inside a running loop — so the LLM callbacks
    (relevance gate / screening answers) must not run on this thread."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return fn(*args)
    ctx = contextvars.copy_context()
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(ctx.run, fn, *args).result()


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
    if not any(u.netloc.endswith(h) for h in _GH_HOSTS):
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


def _split_list(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"[,\n;]", text or "") if s.strip()]


def match_job(job: dict, keywords: list[str], locations: list[str]) -> bool:
    """Title matches ANY keyword phrase (every word of that phrase present,
    case-insensitive) and location matches ANY location substring. Empty lists
    match everything."""
    title = (job.get("title") or "").lower()
    loc = ((job.get("location") or {}).get("name") or "").lower()
    if keywords and not any(all(w in title for w in kw.lower().split()) for kw in keywords):
        return False
    if locations and not any(lc.lower() in loc for lc in locations):
        return False
    return True


def _interleave(groups: list[list[dict]]) -> list[dict]:
    out, i = [], 0
    while any(i < len(g) for g in groups):
        out.extend(g[i] for g in groups if i < len(g))
        i += 1
    return out


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
    return _interleave(groups), total


def fetch_questions(client, token: str, job_id: str) -> dict:
    resp = client.get(f"{_API}/{token}/jobs/{job_id}", params={"questions": "true"})
    resp.raise_for_status()
    return resp.json()


# ── answer planning (pure; unit-tested) ──────────────────────────────────────

_DECLINE_RE = re.compile(r"decline|don.?t wish|do not wish|prefer not|not to (say|answer|disclose)", re.I)
_AGREE_RE = re.compile(r"^(yes|i agree|i acknowledge|i consent|i understand|i confirm|i accept|agree|acknowledge|confirm|accept)", re.I)
_SOURCE_RE = re.compile(r"linkedin|job board|online|website|careers|internet|other", re.I)


def match_option(options: list[str], want: str) -> str | None:
    """The option label best matching ``want``: exact, then prefix, then
    substring (all case-insensitive). None if nothing matches."""
    w = (want or "").strip().lower()
    if not w:
        return None
    for test in (lambda o: o == w, lambda o: o.startswith(w), lambda o: w in o):
        for opt in options:
            if test(opt.strip().lower()):
                return opt
    return None


def _years_option(options: list[str], years: int) -> str | None:
    """Pick a range option ("0-2 years", "3-5", "10+") that contains ``years``."""
    for opt in options:
        nums = [int(n) for n in re.findall(r"\d+", opt)]
        if not nums:
            continue
        if "+" in opt or re.search(r"more|over|above", opt, re.I):
            if years >= nums[0]:
                return opt
        elif len(nums) >= 2 and nums[0] <= years <= nums[1]:
            return opt
        elif len(nums) == 1 and nums[0] == years:
            return opt
    return None


def _yes_no(flag: bool) -> str:
    return "Yes" if flag else "No"


def _rule_answer(label: str, profile: dict) -> str | None:
    """Heuristic answer for a custom question label, or None when unsure. The
    caller maps the answer onto a select's options when the field is a select."""
    lbl = label.lower()
    for kw, ans in profile.get("extra_answers") or []:
        if kw.lower() in lbl:
            return ans
    if "linkedin" in lbl:
        return profile.get("linkedin_url") or None
    if "github" in lbl:
        return profile.get("github_url") or None
    if re.search(r"website|portfolio|personal site|blog", lbl):
        return profile.get("website_url") or profile.get("github_url") or None
    needs = bool(profile.get("needs_sponsorship"))
    authorized = bool(profile.get("work_authorized", True))
    sponsor = re.search(r"sponsor|visa", lbl)
    # "Will you require sponsorship…" asks about sponsorship; "Are you authorized
    # to work … without sponsorship?" asks about authorization — opposite answers.
    if sponsor and "without" not in lbl and re.search(r"\b(require|need|will you|do you)\b", lbl):
        return _yes_no(needs)
    if re.search(r"authori[sz]ed|eligible to work|legally (able|permitted|allowed)|right to work|work permit", lbl):
        return _yes_no(authorized and not (sponsor and needs))
    if sponsor:
        return _yes_no(needs)
    if re.search(r"(worked|employed) (for|at|by)\b.*\b(before|previously|in the past)|"
                 r"previously (worked|been employed)|former employee|ever (worked|been employed)", lbl):
        return "No"
    if re.search(r"were you referred|referred by|referral", lbl):
        return "No"
    if re.search(r"how did you (hear|find|learn)|where did you (hear|find|learn)", lbl):
        return "LinkedIn"
    if re.search(r"how many years|years of (professional |relevant |work )?experience", lbl):
        return str(profile.get("years", 3))
    if re.search(r"salary|compensation|pay expectation|expected pay", lbl):
        return profile.get("salary") or None
    if re.search(r"(where|which (city|country)).*(located|based|live|reside|intend to work|work from)|"
                 r"current (location|city)|city of residence|country of residence", lbl):
        return profile.get("location") or None
    if re.search(r"preferred (first )?name", lbl):
        return profile.get("first_name") or None
    if re.search(r"full (legal )?name|legal name", lbl):
        return f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip() or None
    if re.search(r"consent|acknowledge|agree|privacy|certify|attest|confirm that|terms|i understand", lbl):
        return "__agree__"
    return None


def _to_option(answer: str, options: list[str]) -> str | None:
    """Map a heuristic/LLM answer onto one of a select's option labels."""
    if answer == "__agree__":
        return next((o for o in options if _AGREE_RE.match(o.strip())), None)
    hit = match_option(options, answer)
    if hit:
        return hit
    if answer.isdigit():
        return _years_option(options, int(answer))
    if answer == "LinkedIn":
        return next((o for o in options if _SOURCE_RE.search(o)), None)
    return None


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

    def add(field: str, kind: str, value, label: str) -> None:
        answers.append({"field": field, "kind": kind, "value": value, "label": label})

    for q in job_data.get("questions") or []:
        label = (q.get("label") or "").strip()
        required = bool(q.get("required"))
        fields = [f for f in q.get("fields") or [] if f.get("type") in _KIND_BY_TYPE]
        names = {f.get("name") for f in fields}
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
            ans = _rule_answer(label, profile)
            if kind in ("select", "multiselect"):
                pick = _to_option(ans, options) if ans else None
                if pick is None and required and len(options) == 1:
                    # A required single-option select ("I agree", "Aceito os
                    # termos…") is an acknowledgement — the only way to apply.
                    pick = options[0]
                if pick is None and required and answer_fn is not None:
                    pick = match_option(options, _call_answer_fn(answer_fn, label, "select", options))
                if pick is None and required:
                    pick = next((o for o in options if _DECLINE_RE.search(o)), None) \
                        if re.search(r"gender|race|ethnic|veteran|disabilit|pronoun", label, re.I) else None
                if pick is not None:
                    add(name, kind, [pick] if kind == "multiselect" else pick, label)
                    done = True
            elif kind == "text":
                if ans == "__agree__":
                    ans = None
                if not ans and required and answer_fn is not None:
                    ans = _call_answer_fn(answer_fn, label, "text", [])
                if ans:
                    add(name, "text", ans, label)
                    done = True
            break
        if required and not done and fields:
            unanswered.append(label or "(unlabelled question)")

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
                pick = next((o for o in options if _DECLINE_RE.search(o)), None)
                if pick:
                    add(f.get("name") or "", "select", pick, label)
                else:
                    unanswered.append(label)
                break

    demo = job_data.get("demographic_questions") or {}
    for q in (demo.get("questions") or []) if isinstance(demo, dict) else []:
        if q.get("required"):
            unanswered.append(f"{q.get('label') or 'demographic question'} (demographic)")

    return answers, unanswered


def _call_answer_fn(answer_fn: Callable, label: str, kind: str, options: list[str]) -> str:
    try:
        return (_off_loop(answer_fn, label, kind, options) or "").strip()
    except Exception:  # noqa: BLE001 — an LLM failure just leaves it unanswered
        return ""


# ── local ledger (no site-side "already applied" without a login) ────────────

def _ledger_key(job: dict) -> str:
    return f"{job['token']}/{job['job_id']}"


def load_ledger(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record_applied(path: str, job: dict) -> None:
    ledger = load_ledger(path)
    ledger[_ledger_key(job)] = {
        "title": job.get("title", ""), "company": job.get("company", ""),
        "applied_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(ledger, f, ensure_ascii=False, indent=1)


# ── apply funnel (DOM) ────────────────────────────────────────────────────────

def _launch_browser(pw, headless: bool):
    try:
        return pw.chromium.launch(channel="chrome", headless=headless,
                                  args=_STEALTH_ARGS, ignore_default_args=_STEALTH_IGNORE)
    except Exception:  # noqa: BLE001 — Chrome not installed / not launchable
        return pw.chromium.launch(headless=headless, args=_STEALTH_ARGS)


def _by_id(page, field: str):
    # Attribute selector, not "#id": tolerates ids a CSS id selector would choke on.
    return page.locator(f"[id='{field}']").first


def _choose_option(page, field: str, labels: list[str], type_text: str = "") -> bool:
    """Pick option(s) in a Greenhouse react-select. Options are scoped by their
    ``react-select-<field>-option-N`` ids — the phone widget's hidden country
    list also uses role=option, so a bare [role=option] query picks strangers."""
    inp = _by_id(page, field)
    if inp.count() == 0:
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
        idx = texts.index(pick) if pick in texts else (0 if type_text else -1)
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
              const box = boxes.find(b => {
                const l = b.id && document.querySelector('label[for="' + b.id + '"]');
                return ((l && l.innerText) || b.value || '').trim().toLowerCase() === w;
              });
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


def _fill_form(page, answers: list[dict], log: Callable) -> list[str]:
    """Fill every planned answer. Returns labels of fields that could not be set."""
    failed: list[str] = []
    for a in answers:
        field, kind, value, label = a["field"], a["kind"], a["value"], a["label"]
        try:
            if kind == "text":
                el = _by_id(page, field)
                if el.count() == 0:
                    failed.append(label)
                    continue
                el.fill(str(value), timeout=5000)
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
    try:
        page.evaluate(_CONSENT_JS)
    except Exception:  # noqa: BLE001
        pass
    return failed


def _first_visible(page, selectors: list[str]):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                return loc
        except Exception:  # noqa: BLE001
            continue
    return None


def _visible_text(page, texts) -> str | None:
    for txt in texts:
        try:
            if page.get_by_text(txt, exact=False).first.is_visible():
                return txt
        except Exception:  # noqa: BLE001
            continue
    return None


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
                  log: Callable) -> dict:
    """Fill and (unless dry-run) submit one application. Success is ONLY the
    confirmation page — a Submit click alone is never trusted."""
    entry = {k: job[k] for k in ("token", "job_id", "title", "company", "location", "url")}
    entry["answers"] = [{"label": a["label"][:80],
                         "value": os.path.basename(a["value"]) if a["kind"] == "file" else a["value"]}
                        for a in answers]

    if not _open_form(page, job):
        entry.update(status="failed", submitted=False,
                     reason="application form did not load (job closed or not a Greenhouse form)")
        return entry

    failed = _fill_form(page, answers, log)
    if failed:
        entry.update(status="failed", submitted=False,
                     reason="could not fill: " + "; ".join(f[:60] for f in failed[:3]))
        return entry

    submit = _first_visible(page, _SUBMIT_SELECTORS)
    if submit is None:
        entry.update(status="failed", submitted=False, reason="no Submit button on the form")
        return entry
    if dry_run:
        entry.update(status="prepared", submitted=False, reason="dry_run")
        log(f"✓ {job['token']}/{job['job_id']}: application prepared (dry-run, NOT sent)")
        return entry

    submit.click(timeout=8000)
    for _ in range(30):
        page.wait_for_timeout(500)
        if "/confirmation" in (page.url or "") or _visible_text(page, _SUCCESS_TEXTS):
            entry.update(status="submitted", submitted=True,
                         confirmation="Greenhouse confirmation page shown")
            log(f"✓ {job['token']}/{job['job_id']}: submitted AND confirmed")
            return entry
        if _visible_text(page, _VERIFY_TEXTS):
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

def _parse_extra_answers(text: str) -> list[tuple[str, str]]:
    """``keyword: answer`` lines → [(keyword, answer)]."""
    out = []
    for line in (text or "").splitlines():
        if ":" in line:
            kw, ans = line.split(":", 1)
            if kw.strip() and ans.strip():
                out.append((kw.strip(), ans.strip()))
    return out


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
    extra_answers: str = "",
    max_applications: int = 5,
    dry_run: bool = True,
    relevance_fn: Callable[[str, str], tuple[bool, str]] | None = None,
    answer_fn: Callable[[str, str, list[str]], str] | None = None,
    log: Callable[[str], None] | None = None,
    ledger_path: str | None = None,
    client=None,
) -> dict:
    """Discover matching jobs on the given Greenhouse boards and apply to up to
    ``max_applications`` of them. See the module docstring."""
    import httpx

    log = log or _noop
    relevance_fn = relevance_fn or (lambda _t, _m: (True, ""))
    ledger_path = ledger_path or os.getenv("GREENHOUSE_LEDGER", DEFAULT_LEDGER_PATH)
    resume_path = (resume_path or os.getenv("GREENHOUSE_RESUME_PATH", "")).strip()
    max_applications = max(1, min(int(max_applications), 200))
    try:
        years = int(years_experience)  # preserve an explicit 0 (entry-level)
    except (TypeError, ValueError):
        years = 3
    board_list = _split_list(boards)
    keyword_list = _split_list(keywords)
    location_list = _split_list(location)

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
        profile = {
            "first_name": first_name.strip(), "last_name": last_name.strip(),
            "email": email.strip(), "phone": (phone or "").strip(),
            "location": (current_location or "").strip(),
            "linkedin_url": linkedin_url.strip(), "github_url": github_url.strip(),
            "website_url": website_url.strip(), "years": years,
            "salary": (salary_expectation or "").strip(),
            "work_authorized": bool(work_authorized),
            "needs_sponsorship": bool(needs_sponsorship),
            "resume_path": resume_path, "cover_letter_path": cover_tmp,
            "extra_answers": _parse_extra_answers(extra_answers),
        }
        ledger = load_ledger(ledger_path)

        applied: list[dict] = []
        skipped: list[dict] = []
        scanned = filtered = 0
        headless = os.getenv("GREENHOUSE_HEADLESS", "0") == "1"

        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = None
            page = None
            try:
                for job in candidates:
                    if len(applied) >= max_applications:
                        break
                    scanned += 1
                    jid = f"{job['token']}/{job['job_id']}"
                    log(f"[{len(applied)}/{max_applications} applied | scan #{scanned}] "
                        f"{jid} — {job['title'][:60]}")
                    try:
                        if _ledger_key(job) in ledger:
                            job.update(status="skipped", reason="already applied (ledger)")
                            log(f"↷ {jid}: already applied — next")
                            skipped.append(job)
                            continue

                        meta = f"Company: {job['company']}\nLocation: {job['location']}"
                        try:
                            keep, why = _off_loop(relevance_fn, job["title"], meta)
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
                        entry = _apply_to_job(page, job, answers, dry_run, log)
                        if entry.get("status") in ("prepared", "submitted"):
                            applied.append(entry)
                            if entry.get("submitted"):
                                record_applied(ledger_path, job)
                                ledger[_ledger_key(job)] = True
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
        summary = (f"{scanned} matching job(s) scanned on {len(board_list)} board(s), "
                   f"{len(applied)} {verb}, {len(skipped)} skipped")
        summary += f" ({filtered} filtered by task_filter)." if filtered else "."
        return {
            **base,
            "jobs_found": scanned,
            "total_listed": total_listed,
            "applied": applied,
            "skipped": skipped,
            "applied_count": len(applied),
            "submitted_count": submitted_n,
            "skipped_count": len(skipped),
            "filtered_count": filtered,
            "warnings": warnings[:50],
            "summary": summary,
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
