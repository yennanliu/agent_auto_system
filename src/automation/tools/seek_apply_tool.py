"""
Seek / JobStreet **Quick Apply** — one adapter for every market on Seek's
platform (Seek AU / NZ, JobStreet SG / MY / PH / ID).

What is known from the public, logged-out side (checked live, Nov 2026):

  * Search: ``{base}/api/jobsearch/v5/search`` (JSON, no login) — reused from
    ``ats_sources.seek_search``. Job pages, search pages and ``/job/<id>/apply``
    are behind a Cloudflare challenge for plain HTTP clients.
  * Job details: Seek's GraphQL ``jobDetails`` query answers a plain GET with no
    login and no challenge. It says whether the job is a **link-out** (apply on
    the employer's own site: ``isLinkOut``), whether it is expired, and lists the
    employer's screening questions (``products.questionnaire.questions`` —
    labels only; their options are only shown inside the wizard). Job ids are
    one namespace across every market, so ``seek:<id>`` is the ledger key.

What needs the user's own logged-in account (NOT verified live — there is no
Seek session on the machine this was built on; see doc/seek_apply.md):

  * the Quick Apply wizard at ``{base}/job/<id>/apply``: choose documents
    (résumé / cover letter) → answer employer questions → update profile →
    review → submit → success. Its selectors and step markers are all kept in
    the ``_STEP_*`` / ``_*_SELECTORS`` tables below, and the walker treats any
    page it can't classify as "skip this job with a warning", never a guess.

Funnel per run (deterministic; the LLM only judges relevance and answers
required questions from the candidate's own background):

  1. search each keyword on the chosen market (``max_pages`` pages of 50),
     keep jobs whose title matches a keyword phrase;
  2. skip jobs in the ledger (applied / pending), then the optional relevance
     gate (fail-open);
  3. fetch the job's details over GraphQL (no browser): expired → skip;
     link-out → off-site routing (step 5); a screening question the shared
     rules refuse to guess (``rule_answer == "__skip__"``) → skip before any
     browser opens;
  4. Quick Apply in the browser with the saved session: documents → questions
     (read from the page, planned with the shared ``ats_common`` planner plus
     Seek's own guard rules; any unanswered required question skips the job) →
     profile → review. ``dry_run`` stops at review. Otherwise the ledger marks
     the job "pending" immediately before Submit, and only the success page
     turns it into "applied" — an unconfirmed submit stays "pending" and is
     never resent;
  5. link-out jobs: an ATS job link in the ad, or the page Seek's apply link
     lands on, is routed through ``ats_router.detect_ats``. Supported-ATS jobs
     are handed to ``ats_apply_fn`` (the flow wires ``run_ats_apply``, whose
     ledger is keyed by the ATS's canonical job id) with the remaining budget;
     everything else is a manual lead.

Returns partial results + ``warnings``; never raises out of ``run_seek_apply``.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from crewai.tools import BaseTool
from pydantic import BaseModel

from src.automation.tools.ats_common import (
    Question,
    answer_question,
    countries_in,
    expand_keywords,
    first_visible,
    ledger_claim,
    ledger_entry_blocks,
    load_ledger,
    match_option,
    match_title,
    noop,
    off_loop,
    parse_amount,
    rule_answer,
    split_list,
    with_job_location,
)
from src.automation.tools.ats_router import detect_ats
from src.automation.tools.ats_sources import _APPLY_SIGNAL, SEEK_SITES, _links, seek_search

# ── markets ──────────────────────────────────────────────────────────────────

# site code → (ats_sources.SEEK_SITES key, browser_session name). Seek AU / NZ
# and JobStreet are separate logins (separate cookie domains), so each family
# has its own saved session.
SITES: dict[str, tuple[str, str]] = {
    "au": ("seek_au", "seek"),
    "nz": ("seek_nz", "seek"),
    "sg": ("jobstreet_sg", "jobstreet"),
    "my": ("jobstreet_my", "jobstreet"),
    "ph": ("jobstreet_ph", "jobstreet"),
    "id": ("jobstreet_id", "jobstreet"),
}
_SITE_ALIASES = {"australia": "au", "seek_au": "au", "new zealand": "nz", "seek_nz": "nz",
                 "singapore": "sg", "jobstreet_sg": "sg", "malaysia": "my", "jobstreet_my": "my",
                 "philippines": "ph", "jobstreet_ph": "ph", "indonesia": "id", "jobstreet_id": "id"}
SESSION_STATE = {  # session name → (env var, default storage_state path)
    "seek": ("SEEK_STORAGE_STATE", "data/seek_state.json"),
    "jobstreet": ("JOBSTREET_STORAGE_STATE", "data/jobstreet_state.json"),
}
LOGIN_SCRIPT = "uv run python scripts/seek_login.py"
# Shared by every seek_apply run and market; keys are "seek:<job id>".
DEFAULT_LEDGER_PATH = "data/seek_applied.json"
_MAX_SEARCHES = 5     # undocumented API: keep the footprint small (as ats_sources)
_MAX_PAGES = 10


def resolve_site(site: str) -> str | None:
    """"AU" / "Australia" / "jobstreet_sg" → "au" / "sg"; None when unknown."""
    key = re.sub(r"[\s-]+", "_", (site or "au").strip().lower())
    key = _SITE_ALIASES.get(key.replace("_", " "), _SITE_ALIASES.get(key, key))
    return key if key in SITES else None


def site_info(site: str) -> dict:
    """Everything the run needs to know about one market."""
    source, session = SITES[site]
    base, site_key, label, country = SEEK_SITES[source]
    env, default = SESSION_STATE[session]
    return {"site": site, "source": source, "base": base, "site_key": site_key, "label": label,
            "country": country, "session": session, "state_env": env, "state_default": default}


def ledger_key(job_id: str) -> str:
    return f"seek:{job_id}"


# ── job details over GraphQL (no login, no browser) ──────────────────────────

_JOB_DETAILS_QUERY = (
    "query($jobId: ID!) { jobDetails(id: $jobId) { job { id title isExpired isLinkOut status "
    "advertiser { name } location { label } content(platform: WEB) "
    "products { questionnaire { questions } } } } }"
)


def fetch_job_details(client, base: str, job_id: str) -> dict | None:
    """The job's details from Seek's GraphQL (a GET: read-only), parsed by
    ``parse_job_details``. None when Seek doesn't know the job. Raises on an
    HTTP error (the caller turns that into a warning)."""
    resp = client.get(f"{base}/graphql", params={
        "query": _JOB_DETAILS_QUERY, "variables": json.dumps({"jobId": str(job_id)})})
    resp.raise_for_status()
    return parse_job_details(resp.json())


def parse_job_details(data: dict) -> dict | None:
    """GraphQL ``jobDetails`` response → ``{job_id, title, company, location,
    expired, link_out, questions, content}``; None for an unknown job."""
    job = (((data or {}).get("data") or {}).get("jobDetails") or {}).get("job")
    if not isinstance(job, dict) or not job.get("id"):
        return None
    products = job.get("products") or {}
    questionnaire = products.get("questionnaire") or {}
    questions = [q.strip() for q in questionnaire.get("questions") or [] if isinstance(q, str) and q.strip()]
    status = str(job.get("status") or "")
    return {
        "job_id": str(job["id"]),
        "title": (job.get("title") or "").strip(),
        "company": ((job.get("advertiser") or {}).get("name") or "").strip(),
        "location": ((job.get("location") or {}).get("label") or "").strip(),
        "expired": bool(job.get("isExpired")) or (bool(status) and status.lower() != "active"),
        "link_out": bool(job.get("isLinkOut")),
        "questions": questions,
        "content": job.get("content") or "",
    }


def offsite_ats_url(content_html: str) -> str:
    """An ATS *job* link the ad itself presents as how to apply ("Apply via
    https://jobs.lever.co/acme/<uuid>"), or "". A bare board link is not
    enough: it names every opening at the company, not this one."""
    for link in _links(content_html or "", need=_APPLY_SIGNAL):
        ref = detect_ats(link)
        if ref is not None and ref.supported and ref.job_id:
            return link
    return ""


# ── answer planning: Seek's guard rules + the shared planner ─────────────────

_RIGHT_TO_WORK = re.compile(r"right to work|work(ing)? rights|visa status|work visa|"
                            r"(eligible|eligibility|authori[sz]ed|entitled) to work", re.I)
_SPONSOR_OPT = re.compile(r"sponsor|require (a )?(work )?visa|need (a )?(work )?visa", re.I)
_NOTICE_Q = re.compile(r"\bnotice\b|how soon|when can you start|available to start", re.I)
_SALARY_Q = re.compile(r"salary|remuneration|compensation|pay expectation|expected (base )?pay", re.I)
_YES_NO = {"yes", "no"}


def _extra_answer(label: str, profile: dict) -> str | None:
    """The user's own "keyword: answer" line for this label, if any."""
    lbl = label.lower()
    return next((v for kw, v in profile.get("extra_answers") or [] if kw.lower() in lbl), None)


def _authorized_country(label: str, profile: dict, site_country: str) -> bool | None:
    """Is the candidate authorized to work in the country the question (or the
    market) is about? None when no authorized_countries list was given."""
    allowed = profile.get("authorized_countries") or set()
    if not allowed:
        return None
    named = countries_in(label) or countries_in(site_country)
    return bool(named) and named <= allowed


def _amount_k(text: str) -> float | None:
    """"$80k" → 80000, "$120,000" → 120000, "100000 AUD / year" → 100000."""
    m = re.search(r"(\d[\d,.\s]*)\s*(k)?\b", text or "", re.I)
    if not m:
        return None
    num = parse_amount(m.group(1).strip())
    if num is None:
        return None
    value = float(num)
    return value * 1000 if m.group(2) else value


def salary_option(options: list[str], salary: str) -> str | None:
    """The salary-band option nearest the expected salary ("$90k" for 92000).
    None unless the expectation parses and the options are amounts."""
    want = _amount_k(salary)
    if not want:
        return None
    priced = [(abs(v - want), o) for o in options if (v := _amount_k(o))]
    return min(priced)[1] if priced else None


def seek_rule(q: Question, profile: dict, site_country: str):
    """Seek-specific rules, consulted BEFORE the shared planner. Returns an
    answer value, ``"__skip__"`` (leave unanswered — the job is skipped if the
    question is required) or None (no Seek rule: use the shared planner)."""
    label, options = q.label, q.options
    statements = q.kind in ("select", "multiselect") and options \
        and not {o.strip().lower() for o in options} <= _YES_NO
    if _RIGHT_TO_WORK.search(label) and statements:
        # "Which statement best describes your right to work in Australia?"
        # Seek lists citizen / PR / visa kinds / sponsorship. The shared
        # planner would read an inferred "No" into "…visa with NO restrictions",
        # so this question is ONLY answered from the user's own extra answer
        # or, when they are not authorized there, the sponsorship statement.
        own = _extra_answer(label, profile)
        if own:
            return match_option(options, own) or "__skip__"
        if _authorized_country(label, profile, site_country) is False:
            return next((o for o in options if _SPONSOR_OPT.search(o)), None) or "__skip__"
        return "__skip__"  # citizen vs PR vs which visa: not ours to guess
    if _NOTICE_Q.search(label) and profile.get("notice_period") and not _extra_answer(label, profile):
        if q.kind == "text":
            return profile["notice_period"]
        hit = match_option(options, profile["notice_period"])
        if hit:
            return hit
    if _SALARY_Q.search(label) and q.kind in ("select", "multiselect") and profile.get("salary") \
            and not _extra_answer(label, profile):
        hit = salary_option(options, profile["salary"])
        if hit:
            return hit
    return None


def plan_seek_questions(questions: list[Question], profile: dict, site_country: str,
                        answer_fn: Callable[[str, str, list[str]], str] | None = None,
                        ) -> tuple[list[dict], list[str]]:
    """Like ``ats_common.plan_questions``, with ``seek_rule`` first. Returns
    ``(answers, unanswered required labels)`` — skip the job when the second
    list is not empty."""
    answers: list[dict] = []
    unanswered: list[str] = []
    for q in questions:
        rule = seek_rule(q, profile, site_country)
        if rule == "__skip__":
            a = None
        elif rule is not None:
            value = [rule] if q.kind == "multiselect" and not isinstance(rule, list) else rule
            a = {"field": q.key, "kind": q.kind, "value": value, "label": q.label}
        else:
            a = answer_question(q, profile, answer_fn)
        if a is not None:
            answers.append(a)
        elif q.required:
            unanswered.append(q.label or "(unlabelled question)")
    return answers, unanswered


def prescreen(labels: list[str], profile: dict) -> list[str]:
    """Questionnaire labels (from GraphQL, before any browser) that the shared
    rules refuse to guess — e.g. work authorization for a job in a country
    that can't be determined. Such a job is skipped without opening it."""
    return [lbl for lbl in labels if rule_answer(lbl, profile) == "__skip__"]


# ── wizard page reading (pure; HTML → step / questions) ──────────────────────

# URL path suffixes after /job/<id>/apply, most specific first. UNVERIFIED
# against a logged-in session (see module docstring) — the heading table below
# is the fallback when a path is not recognised.
_STEP_PATHS = (
    ("success", ("/apply/success", "/apply/confirmation", "/apply/submitted")),
    ("review", ("/apply/review",)),
    ("questions", ("/apply/role-requirements", "/apply/questions", "/apply/employer-questions")),
    ("profile", ("/apply/profile", "/apply/update-profile")),
    ("documents", ("/apply/documents", "/apply")),
)
_STEP_HEADINGS = (
    ("success", re.compile(r"application (has been |was )?(sent|submitted)|you.?ve applied", re.I)),
    ("review", re.compile(r"review (and|&) submit|review your application", re.I)),
    ("questions", re.compile(r"answer employer questions|employer questions", re.I)),
    ("profile", re.compile(r"update (your )?(seek|jobstreet) profile|update your profile", re.I)),
    ("documents", re.compile(r"choose documents|choose your documents", re.I)),
)
_LOGIN_HOSTS = ("login.seek.com", "login.jobstreet.com", "auth.seek.com")
_LOGIN_PATHS = ("/oauth/login", "/sign-in", "/signin", "/login")
_CHALLENGE_MARKERS = ("Just a moment", "Verify you are human", "Checking your browser")
_ALREADY_APPLIED = re.compile(r"you(?: have|'ve|’ve)? already applied|you applied (on|for this)", re.I)
_CLOSED = re.compile(r"no longer (advertised|available|accepting)|this job (has )?expired|"
                     r"job (ad )?(is )?no longer", re.I)
_ERROR_SELECTORS = ("[aria-invalid='true']", "[role='alert']", "[data-automation*='error']")


def _page_text(soup) -> str:
    return " ".join(soup.get_text(" ").split())


def _headings(soup) -> str:
    return " | ".join(" ".join(h.get_text(" ").split()) for h in soup.find_all(["h1", "h2", "h3"]))


def classify_step(url: str, html: str) -> str:
    """Which wizard step a page is: ``documents`` · ``questions`` · ``profile``
    · ``review`` · ``success`` — or ``login`` (session expired), ``challenge``
    (bot check), ``already_applied``, ``closed``, ``unknown``."""
    soup = BeautifulSoup(html or "", "html.parser")
    title = (soup.title.get_text() if soup.title else "") or ""
    text = _page_text(soup)
    if any(m in title or m in text[:600] for m in _CHALLENGE_MARKERS):
        return "challenge"
    u = urlparse(url or "")
    host, path = (u.netloc or "").lower(), (u.path or "").rstrip("/").lower()
    if any(h in host for h in _LOGIN_HOSTS) or any(path.endswith(p) or f"{p}/" in path for p in _LOGIN_PATHS):
        return "login"
    heads = _headings(soup)
    if _ALREADY_APPLIED.search(heads) or _ALREADY_APPLIED.search(text[:2000]):
        return "already_applied"
    for step, suffixes in _STEP_PATHS:
        if any(path.endswith(s) for s in suffixes) and "/apply" in path:
            return step
    for step, pat in _STEP_HEADINGS:
        if pat.search(heads):
            return step
    if _CLOSED.search(text[:3000]):
        return "closed"
    return "unknown"


def form_errors(html: str) -> list[str]:
    """Visible validation messages on a wizard page (best-effort)."""
    soup = BeautifulSoup(html or "", "html.parser")
    out: list[str] = []
    for el in soup.select("[role='alert'], [data-automation*='error'], .error, [id$='-message']"):
        t = " ".join(el.get_text(" ").split())
        if t and t not in out and len(t) < 300:
            out.append(t)
    return out[:5]


_STAR = re.compile(r"\s*[*✱]\s*$|\s*\((required|optional)\)\s*$", re.I)


def _clean_label(text: str) -> str:
    t = " ".join((text or "").split())
    for _ in range(2):
        t = _STAR.sub("", t)
    return t.strip()


def _label_text(soup, el) -> str:
    ids = (el.get("aria-labelledby") or "").split()
    if ids:
        parts = [soup.find(id=i) for i in ids]
        txt = " ".join(p.get_text(" ") for p in parts if p is not None)
        if txt.strip():
            return txt
    if el.get("id"):
        lab = soup.find("label", attrs={"for": el["id"]})
        if lab is not None:
            return lab.get_text(" ")
    parent = el.find_parent("label")
    if parent is not None:
        return parent.get_text(" ")
    return el.get("aria-label") or el.get("placeholder") or ""


def _group_label(soup, inputs) -> str:
    """The question text of a radio / checkbox group: its fieldset legend, or
    the label of the element named by the group's aria-labelledby."""
    first = inputs[0]
    group = first.find_parent(attrs={"role": re.compile("radiogroup|group")})
    if group is not None and group.get("aria-labelledby"):
        txt = " ".join(t.get_text(" ") for i in group["aria-labelledby"].split()
                       if (t := soup.find(id=i)) is not None)
        if txt.strip():
            return txt
    fs = first.find_parent("fieldset")
    if fs is not None and fs.find("legend") is not None:
        return fs.find("legend").get_text(" ")
    if group is not None and group.get("aria-label"):
        return group["aria-label"]
    return ""


def _optional(text: str) -> bool:
    return bool(re.search(r"\(optional\)", text or "", re.I))


def _required(el, label_raw: str, default: bool) -> bool:
    if el.has_attr("required") or (el.get("aria-required") or "").lower() == "true":
        return True
    if re.search(r"[*✱]\s*$|\(required\)", label_raw or "", re.I):
        return True
    return default and not _optional(label_raw)


def read_questions(html: str, required_default: bool = True) -> list[Question]:
    """The employer questions on a wizard page, as shared ``Question``s.

    ``key`` encodes how to fill it: ``"<control>|<attr>=<value>"`` where control
    is select / radio / checkbox / text and attr is ``id`` or ``name``. A field
    with no required marker counts as required unless it says "(optional)"
    (``required_default``): Seek's employer questions are compulsory, and a
    false "required" only ever skips a job, never sends a guess."""
    soup = BeautifulSoup(html or "", "html.parser")
    scope = max(soup.find_all("form"), key=lambda f: len(f.find_all(["input", "select", "textarea"])),
                default=None) or soup
    out: list[Question] = []
    seen_groups: set[str] = set()
    for el in scope.find_all(["select", "input", "textarea"]):
        tag = el.name
        typ = (el.get("type") or "text").lower() if tag == "input" else tag
        if typ in ("hidden", "submit", "button", "file", "search", "image", "reset"):
            continue
        if el.has_attr("disabled"):
            continue
        if typ in ("radio", "checkbox"):
            name = el.get("name") or el.get("id") or ""
            if not name or name in seen_groups:
                continue
            seen_groups.add(name)
            inputs = [i for i in scope.find_all("input", attrs={"type": typ}) if (i.get("name") or i.get("id")) == name]
            options = [_clean_label(_label_text(soup, i)) or (i.get("value") or "") for i in inputs]
            raw = _group_label(soup, inputs)
            if not raw and len(inputs) == 1:
                raw = _label_text(soup, inputs[0])  # a lone "I agree…" box
            attr = "name" if el.get("name") else "id"
            kind = "multiselect" if typ == "checkbox" and len(inputs) > 1 else "select"
            req = any(_required(i, raw, required_default and typ == "radio") for i in inputs) or \
                (typ == "checkbox" and len(inputs) > 1 and required_default and not _optional(raw))
            out.append(Question(f"{typ}|{attr}={name}", _clean_label(raw), kind, req,
                                [o for o in options if o]))
            continue
        ident = ("id", el["id"]) if el.get("id") else ("name", el.get("name") or "")
        if not ident[1]:
            continue
        raw = _label_text(soup, el)
        if tag == "select":
            opts = [" ".join(o.get_text(" ").split()) for o in el.find_all("option")
                    if (o.get("value") or "").strip() and not o.has_attr("disabled")]
            opts = [o for o in opts if o and not re.match(r"^(select|choose|please select)\b", o, re.I)]
            out.append(Question(f"select|{ident[0]}={ident[1]}", _clean_label(raw), "select",
                                _required(el, raw, required_default), opts))
        else:
            out.append(Question(f"text|{ident[0]}={ident[1]}", _clean_label(raw), "text",
                                _required(el, raw, required_default)))
    return out


# ── the wizard walker (pure logic over a driver; unit-tested with a fake) ────

_MAX_STEPS = 10
STOP_STATUSES = ("session_expired", "blocked")  # every later job would fail the same way


class SessionError(RuntimeError):
    """The browser / saved session could not be opened: no job can be applied."""


def walk_wizard(driver, job: dict, plan_fn: Callable[[list[Question]], tuple[list[dict], list[str]]],
                *, cover_letter: str = "", resume_name: str = "", dry_run: bool = True,
                on_submit: Callable[[], bool] | None = None, log: Callable = noop) -> dict:
    """Walk one Quick Apply wizard. ``driver`` does the browser work
    (``open``, ``snapshot``, ``choose_documents``, ``fill``, ``click``,
    ``wait_success``); this function decides. Returns a result entry whose
    ``status`` is prepared · submitted · unconfirmed · skipped · failed ·
    session_expired · blocked. ``on_submit`` is called immediately before the
    Submit click (the ledger marks the job "pending"); False → not sent."""
    entry = {k: job.get(k, "") for k in ("job_id", "url", "title", "company", "location")}
    driver.open(job)
    last = None
    repeats = 0
    for _ in range(_MAX_STEPS):
        url, html = driver.snapshot()
        step = classify_step(url, html)
        log(f"  · step: {step}")
        key = (step, (url or "").split("?")[0])
        repeats = repeats + 1 if key == last else 0
        last = key
        if step == "login":
            return {**entry, "status": "session_expired", "submitted": False,
                    "reason": f"Seek asked to sign in — the saved session expired; re-run `{LOGIN_SCRIPT}`"}
        if step == "challenge":
            return {**entry, "status": "blocked", "submitted": False,
                    "reason": "Seek answered with its bot check (Cloudflare) instead of the form"}
        if step == "already_applied":
            return {**entry, "status": "skipped", "submitted": False, "reason": "already applied (Seek)"}
        if step == "closed":
            return {**entry, "status": "skipped", "submitted": False, "reason": "job no longer advertised"}
        if step == "unknown":
            heads = _headings(BeautifulSoup(html or "", "html.parser"))[:120]
            return {**entry, "status": "skipped", "submitted": False, "unknown_step": True,
                    "reason": f"unrecognised Quick Apply page ({urlparse(url or '').path or url!r}, "
                              f"headings: {heads or 'none'}) — layout changed? skipped, nothing sent"}
        if repeats >= 2:
            errs = form_errors(html)
            return {**entry, "status": "skipped", "submitted": False,
                    "reason": f"stuck on the '{step}' step"
                              + (f" — the form says: {'; '.join(errs)}" if errs else "")}
        if step == "success":
            # Reached without our Submit: the wizard's success page is only
            # ever shown after a submission (ours or an earlier one).
            return {**entry, "status": "skipped", "submitted": False,
                    "reason": "Seek shows this job as already applied (success page)"}
        if step == "documents":
            for note in driver.choose_documents(resume_name=resume_name,
                                                cover_letter=_personalise(cover_letter, job)) or []:
                log(f"  · {note}")
            if repeats == 1 and form_errors(html):
                return {**entry, "status": "skipped", "submitted": False,
                        "reason": "documents step rejected: " + "; ".join(form_errors(html))}
            driver.click("continue")
            continue
        if step == "questions":
            questions = read_questions(html)
            answers, unanswered = plan_fn(questions)
            if unanswered:
                return {**entry, "status": "skipped", "submitted": False, "unanswered": unanswered,
                        "reason": "unanswered required question(s): "
                                  + "; ".join(u[:70] for u in unanswered[:3])}
            if repeats == 1:
                errs = form_errors(html)
                if errs:
                    return {**entry, "status": "skipped", "submitted": False,
                            "reason": "the employer form rejected our answers: " + "; ".join(errs)}
            driver.fill(answers)
            entry["answers"] = [{"label": a["label"], "value": a["value"]} for a in answers]
            driver.click("continue")
            continue
        if step == "profile":
            driver.click("continue")  # never edit the user's Seek profile
            continue
        if step == "review":
            if dry_run:
                return {**entry, "status": "prepared", "submitted": False, "reason": "dry_run"}
            if on_submit is not None and on_submit() is False:
                return {**entry, "status": "skipped", "submitted": False,
                        "reason": "claim lost to another run before Submit — not sent"}
            if not driver.click("submit"):
                return {**entry, "status": "failed", "submitted": False,
                        "reason": "no Submit button on the review page"}
            if driver.wait_success():
                return {**entry, "status": "submitted", "submitted": True,
                        "confirmation": "Seek showed its application-sent page"}
            return {**entry, "status": "unconfirmed", "submitted": False,
                    "reason": "clicked Submit but never saw Seek's success page"}
    return {**entry, "status": "failed", "submitted": False,
            "reason": f"did not reach review within {_MAX_STEPS} steps"}


def _personalise(text: str, job: dict) -> str:
    """``{company}`` / ``{title}`` in the cover letter → this job's."""
    out = (text or "").strip()
    for k in ("company", "title"):
        out = out.replace("{" + k + "}", job.get(k) or "")
    return out


# ── Playwright driver (needs a live, logged-in session — not unit-tested) ────

_CONTINUE_SELECTORS = [
    "button[data-testid='continue-button']",
    "button[data-automation='continue-button']",
    "button:has-text('Continue')",
    "button:has-text('Next')",
]
_SUBMIT_SELECTORS = [
    "button[data-testid='review-submit-application']",
    "button[data-automation='submit-application']",
    "button:has-text('Submit application')",
    "button:has-text('Submit')",
]
_SUCCESS_URL_PARTS = ("/apply/success", "/apply/confirmation", "/apply/submitted")
_SUCCESS_TEXTS = ("Your application has been sent", "Application sent", "application was submitted",
                  "Lamaran Anda telah dikirim")
_READY_SELECTORS = ", ".join(["h1", "h2", "form", "button:has-text('Continue')",
                              "button:has-text('Submit')"])
# Fills one planned answer. key = "<control>|<attr>=<value>"; value is a str,
# or a list for a checkbox group. Uses the native setter + input/change events
# so React picks the value up.
_FILL_JS = r"""
([key, value]) => {
  const [ctrl, ident] = key.split('|');
  const eq = ident.indexOf('=');
  const attr = ident.slice(0, eq), val = ident.slice(eq + 1);
  const byAttr = (sel) => Array.from(document.querySelectorAll(sel))
      .filter(e => e.getAttribute(attr) === val);
  const labelOf = (el) => {
    let t = '';
    if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) t = l.innerText; }
    if (!t) { const p = el.closest('label'); if (p) t = p.innerText; }
    return (t || el.value || '').replace(/\s+/g, ' ').trim().toLowerCase();
  };
  const want = (Array.isArray(value) ? value : [value]).map(v => String(v).replace(/\s+/g, ' ').trim().toLowerCase());
  if (ctrl === 'select') {
    const el = byAttr('select')[0]; if (!el) return false;
    const opt = Array.from(el.options).find(o => o.text.replace(/\s+/g, ' ').trim().toLowerCase() === want[0]);
    if (!opt) return false;
    el.value = opt.value;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    return true;
  }
  if (ctrl === 'radio' || ctrl === 'checkbox') {
    const group = byAttr("input[type='" + ctrl + "']"); if (!group.length) return false;
    let hit = 0;
    for (const el of group) {
      const on = want.includes(labelOf(el)) || (group.length === 1 && want.length === 1);
      if (on !== el.checked && (on || ctrl === 'checkbox')) el.click();
      if (on) hit++;
    }
    return hit > 0;
  }
  const el = byAttr('input, textarea')[0]; if (!el) return false;
  const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, String(value));
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  el.dispatchEvent(new Event('blur', { bubbles: true }));
  return true;
}
"""
# Documents step: résumé (keep Seek's pre-selected one, or pick a saved one by
# name) and cover letter ("Write a cover letter" + text, or "Don't include").
_DOCUMENTS_JS = r"""
([resumeName, coverText]) => {
  const notes = [];
  const lab = (el) => {
    let t = '';
    if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) t = l.innerText; }
    if (!t) { const p = el.closest('label'); if (p) t = p.innerText; }
    return (t || '').replace(/\s+/g, ' ').trim();
  };
  const radios = Array.from(document.querySelectorAll("input[type='radio']"));
  const pick = (re) => radios.find(r => re.test(lab(r)));
  if (resumeName) {
    const sel = pick(/select.*r[eé]sum[eé]|choose.*r[eé]sum[eé]/i); if (sel && !sel.checked) sel.click();
    const box = Array.from(document.querySelectorAll('select')).find(s =>
        Array.from(s.options).some(o => o.text.toLowerCase().includes(resumeName.toLowerCase())));
    if (box) {
      const o = Array.from(box.options).find(o => o.text.toLowerCase().includes(resumeName.toLowerCase()));
      box.value = o.value; box.dispatchEvent(new Event('change', { bubbles: true }));
      notes.push('résumé: ' + o.text.trim());
    } else { notes.push("résumé '" + resumeName + "' not found — Seek's default kept"); }
  }
  if (coverText) {
    const w = pick(/write.*cover letter/i);
    if (w) {
      if (!w.checked) w.click();
      notes.push('cover letter: written (' + coverText.length + ' chars)');
      return {notes, needText: true};
    }
    notes.push("no 'Write a cover letter' option — cover letter not added");
  } else {
    const none = pick(/don.?t include.*cover letter|no cover letter/i);
    if (none && !none.checked) { none.click(); notes.push('cover letter: none'); }
  }
  return {notes, needText: false};
}
"""


class PlaywrightWizard:
    """The browser half of ``walk_wizard`` on a logged-in Seek page."""

    def __init__(self, page, base: str):
        self.page, self.base = page, base

    def open(self, job: dict) -> None:
        self.page.goto(f"{self.base}/job/{job['job_id']}/apply", wait_until="domcontentloaded",
                       timeout=45000)

    def snapshot(self) -> tuple[str, str]:
        page = self.page
        try:
            page.wait_for_selector(_READY_SELECTORS, timeout=15000)
        except Exception:  # noqa: BLE001 — classify_step reports what it sees
            pass
        page.wait_for_timeout(1200)  # let the step's fields hydrate
        for _ in range(15):  # a Cloudflare check often clears by itself in a headed Chrome
            html = page.content()
            if not any(m in html[:5000] for m in _CHALLENGE_MARKERS):
                break
            page.wait_for_timeout(2000)
        return page.url, page.content()

    def choose_documents(self, *, resume_name: str, cover_letter: str) -> list[str]:
        out = self.page.evaluate(_DOCUMENTS_JS, [resume_name or "", cover_letter or ""]) or {}
        if out.get("needText"):
            self.page.wait_for_timeout(600)
            box = first_visible(self.page, ["textarea[name*='cover' i]", "textarea[id*='cover' i]", "textarea"])
            if box is not None:
                box.fill(cover_letter[:5000], timeout=8000)
            else:
                out.setdefault("notes", []).append("cover-letter text box not found")
        return out.get("notes") or []

    def fill(self, answers: list[dict]) -> None:
        for a in answers:
            if not self.page.evaluate(_FILL_JS, [a["field"], a["value"]]):
                raise RuntimeError(f"could not fill '{a['label'][:60]}'")
            self.page.wait_for_timeout(150)

    def click(self, what: str) -> bool:
        btn = first_visible(self.page, _SUBMIT_SELECTORS if what == "submit" else _CONTINUE_SELECTORS)
        if btn is None:
            return False
        btn.click(timeout=8000)
        self.page.wait_for_timeout(1500)
        return True

    def wait_success(self) -> bool:
        for _ in range(30):
            try:
                if any(p in (self.page.url or "") for p in _SUCCESS_URL_PARTS):
                    return True
                for txt in _SUCCESS_TEXTS:
                    if self.page.get_by_text(txt, exact=False).first.is_visible():
                        return True
            except Exception:  # noqa: BLE001
                pass
            self.page.wait_for_timeout(500)
        return False

    def resolve_link_out(self, job: dict) -> str:
        """Where Seek's apply link for a link-out job lands (a GET; nothing is
        filled in). "" when it stays on Seek."""
        page = self.page
        try:
            with page.context.expect_page(timeout=8000) as pop:
                page.goto(f"{self.base}/job/{job['job_id']}/apply", wait_until="domcontentloaded",
                          timeout=30000)
            target = pop.value
        except Exception:  # noqa: BLE001 — no new tab: the redirect happened here
            target = page
        try:
            for _ in range(10):
                if not _is_seek_host(target.url):
                    return target.url
                target.wait_for_timeout(800)
            return ""
        finally:
            if target is not page:
                try:
                    target.close()
                except Exception:  # noqa: BLE001
                    pass


def _is_seek_host(url: str) -> bool:
    host = urlparse(url or "").netloc.lower()
    return not host or host.endswith(("seek.com", "seek.com.au", "seek.co.nz", "jobstreet.com",
                                      "jobstreet.co.id", "jobsdb.com", "cloudflare.com"))


_LOGGED_OUT_SELECTORS = ["a[href*='/oauth/login']", "a[data-automation='sign in']",
                         "a:has-text('Sign in')", "button:has-text('Sign in')"]


def looks_logged_out(page) -> bool:
    """True when Seek shows a sign-in page or a visible "Sign in" link (the
    logged-in header shows the account menu instead). UNVERIFIED selectors —
    the wizard's own redirect to sign-in (step ``login``) is the backstop."""
    try:
        u = urlparse(page.url or "")
        if any(h in u.netloc for h in _LOGIN_HOSTS) or any(p in u.path for p in _LOGIN_PATHS):
            return True
    except Exception:  # noqa: BLE001
        pass
    return first_visible(page, _LOGGED_OUT_SELECTORS) is not None


# ── orchestration ────────────────────────────────────────────────────────────

def _search(client, info: dict, keywords: list[str], location: str, max_pages: int,
            log: Callable, warnings: list) -> list[dict]:
    """Every distinct job the market's search returns for the keywords whose
    title matches a keyword phrase (ROLE_ALIASES expanded)."""
    terms = expand_keywords(keywords)[:_MAX_SEARCHES] or [""]
    if len(expand_keywords(keywords)) > _MAX_SEARCHES:
        warnings.append(f"searched only the first {_MAX_SEARCHES} keyword phrases")
    jobs: list[dict] = []
    seen: set[str] = set()
    for term in terms:
        for page_no in range(1, max_pages + 1):
            try:
                found = seek_search(client, info["source"], term, location, page_no)
            except Exception as exc:  # noqa: BLE001 — one failed page never sinks the run
                warnings.append(f"search '{term}' page {page_no}: {exc}")
                log(f"⚠ search '{term}' page {page_no} failed ({exc})")
                break
            new = [j for j in found if j["job_id"] not in seen]
            for j in new:
                seen.add(j["job_id"])
                if match_title(j["title"], j["location"], keywords, []):
                    jobs.append(j)
            if len(found) < 50 or not new:
                break
    log(f"{info['label']}: {len(seen)} listing(s) found, {len(jobs)} with a matching title")
    return jobs


def _state_path(info: dict, state_path: str | None) -> str:
    return state_path or os.getenv(info["state_env"], info["state_default"])


def run_seek_apply(
    *,
    keywords: str,
    site: str = "au",
    location: str = "",
    profile: dict | None = None,
    max_applications: int = 5,
    max_pages: int = 3,
    cover_letter: str = "",
    resume_name: str = "",
    dry_run: bool = True,
    relevance_fn: Callable[[str, str], tuple[bool, str]] | None = None,
    answer_fn: Callable[[str, str, list[str], dict], str] | None = None,
    ats_apply_fn: Callable[..., dict] | None = None,
    log: Callable[[str], None] | None = None,
    state_path: str | None = None,
    ledger_path: str | None = None,
    client=None,
    wizard_factory: Callable[[], object] | None = None,
) -> dict:
    """Search one Seek / JobStreet market and Quick Apply to up to
    ``max_applications`` matching jobs (see the module docstring).

    ``profile`` is ``ats_common.build_profile(...)`` output (it answers the
    employer questions). ``wizard_factory`` returns a driver for
    ``walk_wizard`` and is called lazily — at most once — the first time a job
    needs the browser; it defaults to a headed Chrome with the saved session.
    ``ats_apply_fn(boards=..., max_applications=...)`` applies to link-out jobs
    on a supported ATS; without it they are listed as manual leads."""
    import httpx

    log = log or noop
    relevance_fn = relevance_fn or (lambda _t, _m: (True, ""))
    code = resolve_site(site)
    kw_list = split_list(keywords)
    base = {"keywords": keywords, "site": site, "location": location, "dry_run": dry_run,
            "applied": [], "skipped": [], "manual": [], "warnings": []}
    if code is None:
        return {**base, "error": f"Unknown site '{site}' — use one of: {', '.join(SITES)}"}
    if not kw_list:
        return {**base, "error": "No keywords given — an empty Seek search matches every job."}
    info = site_info(code)
    base["site"] = code
    state = _state_path(info, state_path)
    if wizard_factory is None and not os.path.exists(state):
        return {**base, "error": (
            f"No saved {info['label'].split()[0]} session at '{state}'. Run "
            f"`{LOGIN_SCRIPT} --site {code}` once to log in and save it.")}
    ledger_path = ledger_path or os.getenv("SEEK_LEDGER", DEFAULT_LEDGER_PATH)
    max_applications = max(1, min(int(max_applications), 200))
    max_pages = max(1, min(int(max_pages), _MAX_PAGES))
    profile = dict(profile or {})
    warnings: list[str] = []

    owns_client = client is None
    client = client or httpx.Client(timeout=25, follow_redirects=True, headers={"User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")})
    pw_box: dict = {}

    def _wizard():
        if "error" in pw_box:
            raise SessionError(pw_box["error"])
        if "wizard" not in pw_box:
            try:
                pw_box["wizard"] = (wizard_factory or (lambda: _open_browser(pw_box, info, state, log)))()
            except Exception as exc:  # noqa: BLE001 — opened once; every later job would fail too
                pw_box["error"] = str(exc) if isinstance(exc, SessionError) else f"{type(exc).__name__}: {exc}"
                raise SessionError(pw_box["error"]) from exc
        return pw_box["wizard"]

    try:
        log(f"Searching {info['label']} for: {', '.join(kw_list)}"
            + (f" in '{location}'" if location else ""))
        candidates = _search(client, info, kw_list, location, max_pages, log, warnings)
        ledger = load_ledger(ledger_path)
        applied: list[dict] = []
        skipped: list[dict] = []
        manual: list[dict] = []
        pending: list[dict] = []
        routed: list[dict] = []
        scanned = filtered = 0
        stop = ""

        for job in candidates:
            if len(applied) + len(pending) >= max_applications or stop:
                break
            jid = job["job_id"]
            key = ledger_key(jid)
            scanned += 1
            log(f"[{len(applied)}/{max_applications} applied | scan #{scanned}] {jid} — {job['title'][:60]}")
            try:
                prior = ledger.get(key)
                claimed = isinstance(prior, dict) and prior.get("status") == "claimed"
                if prior is not None and not claimed and ledger_entry_blocks(prior):
                    is_pending = isinstance(prior, dict) and prior.get("status") == "pending"
                    skipped.append({**job, "status": "skipped", "reason": (
                        "submitted on an earlier run but never confirmed (ledger) — check Seek → "
                        "Applied jobs; remove it from the ledger to retry") if is_pending
                        else "already applied (ledger)"})
                    log(f"↷ {jid}: {'earlier submission unconfirmed' if is_pending else 'already applied'} — next")
                    continue

                meta = f"Company: {job['company']}\nLocation: {job['location']}" \
                       + (f"\nSummary: {job['teaser']}" if job.get("teaser") else "")
                try:
                    keep, why = off_loop(relevance_fn, job["title"], meta)
                except Exception as exc:  # noqa: BLE001
                    keep, why = True, ""
                    log(f"⚠ {jid}: relevance filter errored ({exc}); keeping (fail-open)")
                if not keep:
                    reason = f"filtered out: {why}" if why else "filtered out by task_filter"
                    skipped.append({**job, "status": "skipped", "reason": reason, "filtered": True})
                    filtered += 1
                    log(f"↷ {jid}: {reason} — next")
                    continue

                try:
                    details = fetch_job_details(client, info["base"], jid)
                except Exception as exc:  # noqa: BLE001 — the wizard can still tell
                    details = None
                    warnings.append(f"{jid}: job details unavailable ({exc})")
                if details is not None:
                    job["company"] = job["company"] or details["company"]
                    job["employer_questions"] = details["questions"]
                    if details["expired"]:
                        skipped.append({**job, "status": "skipped", "reason": "job expired"})
                        log(f"↷ {jid}: expired — next")
                        continue
                    if details["link_out"]:
                        target = offsite_ats_url(details["content"])
                        if not target:
                            try:
                                target = _wizard().resolve_link_out(job)
                            except Exception as exc:  # noqa: BLE001
                                warnings.append(f"{jid}: could not follow the link-out ({exc})")
                        _route_offsite(job, target, routed, manual, log)
                        continue

                job_profile = with_job_location(profile, job["location"], job["company"])
                refused = prescreen((details or {}).get("questions") or [], job_profile)
                if refused:
                    skipped.append({**job, "status": "skipped", "unanswered": refused,
                                    "reason": "employer question we won't guess: " + "; ".join(r[:70] for r in refused[:3])})
                    log(f"↷ {jid}: {len(refused)} question(s) we won't guess — next")
                    continue

                def _plan(questions, _job=job, _profile=job_profile):
                    ask = (lambda label, kind, options: answer_fn(label, kind, options, _job)) if answer_fn else None
                    return plan_seek_questions(questions, _profile, info["country"], ask)

                with ledger_claim(ledger_path, key, job, dry_run=dry_run) as claim:
                    if not claim:
                        skipped.append({**job, "status": "skipped", "reason": "already applied (ledger, another run)"})
                        log(f"↷ {jid}: applied / being applied by another run — next")
                        continue
                    submitted_clicked = {"v": False}

                    def _on_submit(_claim=claim, _flag=submitted_clicked):
                        _flag["v"] = _claim.submitting()  # "pending" on disk before the click
                        return _flag["v"]

                    entry = walk_wizard(_wizard(), job, _plan, cover_letter=cover_letter,
                                        resume_name=resume_name, dry_run=dry_run,
                                        on_submit=None if dry_run else _on_submit, log=log)
                    entry["employer_questions"] = job.get("employer_questions", [])
                    status = entry.get("status")
                    if status in ("prepared", "submitted"):
                        applied.append(entry)
                        if entry.get("submitted"):
                            claim.confirmed()
                            ledger[key] = {"status": "applied"}
                        log(f"✓ {jid}: {'submitted AND confirmed' if entry.get('submitted') else 'prepared (dry-run, NOT sent)'}")
                    elif status == "unconfirmed":
                        pending.append(entry)
                        skipped.append(entry)
                        ledger[key] = {"status": "pending"}
                        log(f"⚠ {jid}: submit unconfirmed — ledgered as pending, never resent")
                    else:
                        skipped.append(entry)
                        log(f"↷ {jid}: {entry.get('reason', status)}")
                        if entry.get("unknown_step"):
                            warnings.append(f"{jid}: {entry['reason']}")
                        if status in STOP_STATUSES:
                            stop = entry["reason"]
                    if not dry_run and submitted_clicked["v"] and status != "submitted":
                        ledger[key] = {"status": "pending"}  # claim stays "pending" on disk too
            except SessionError as exc:
                stop = str(exc)
                skipped.append({**job, "status": "failed", "reason": f"browser session unavailable: {exc}"})
            except Exception as exc:  # noqa: BLE001 — one bad job never stops the run
                skipped.append({**job, "status": "failed", "reason": f"{type(exc).__name__}: {exc}"})
                log(f"✗ {jid}: {type(exc).__name__}: {exc}")

        if stop:
            warnings.append(f"run stopped early: {stop}")
            log(f"⛔ {stop}")

        ats_result = None
        remaining = max_applications - len(applied) - len(pending)
        if routed and ats_apply_fn is not None and remaining > 0 and not stop:
            urls = [r["ats_url"] for r in routed]
            log(f"Handing {len(urls)} link-out job(s) on a supported ATS to ATS Apply "
                f"(budget {remaining})...")
            try:
                ats_result = ats_apply_fn(boards=", ".join(urls), max_applications=remaining)
            except Exception as exc:  # noqa: BLE001
                ats_result = {"error": f"{type(exc).__name__}: {exc}"}
            if ats_result.get("error"):
                warnings.append(f"ATS Apply on link-out jobs: {ats_result['error']}")
                manual.extend(_manual_from_routed(routed, "ATS Apply could not run: " + ats_result["error"]))
            else:
                applied.extend({**e, "via": "ats_apply"} for e in ats_result.get("applied") or [])
                skipped.extend({**e, "via": "ats_apply"} for e in ats_result.get("skipped") or [])
                manual.extend({**e, "via": "ats_apply"} for e in ats_result.get("manual") or [])
                warnings.extend(f"ats_apply: {w}" for w in (ats_result.get("warnings") or [])[:10])
        elif routed:
            why = ("run stopped early" if stop else "application budget used up" if remaining <= 0
                   else "ATS routing is off")
            manual.extend(_manual_from_routed(routed, why))

        submitted_n = sum(1 for e in applied if e.get("submitted"))
        verb = "prepared (dry-run)" if dry_run else "submitted & confirmed"
        summary = (f"{info['label']} '{keywords}': {len(candidates)} matching job(s), {scanned} scanned, "
                   f"{len(applied)} {verb}, {len(skipped)} skipped")
        if pending:
            summary += f", {len(pending)} submitted but unconfirmed (check Seek → Applied jobs)"
        if manual:
            summary += f", {len(manual)} listed for manual apply"
        summary += f" ({filtered} filtered by task_filter)." if filtered else "."
        if stop:
            summary += f" Stopped early: {stop}."
        return {
            "summary": summary,
            "jobs_found": len(candidates),
            "scanned": scanned,
            "applied_count": len(applied),
            "submitted_count": submitted_n,
            "skipped_count": len(skipped),
            "filtered_count": filtered,
            "manual_count": len(manual),
            "unconfirmed_count": len(pending),
            "routed_to_ats": len(routed),
            **base,
            "applied": applied,
            "skipped": skipped,
            "manual": manual[:100],
            "warnings": warnings[:50],
            **({"stopped": stop} if stop else {}),
        }
    except Exception as exc:  # noqa: BLE001 — partial results, never raise
        return {**base, "warnings": warnings[:50], "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if owns_client:
            client.close()
        _close_browser(pw_box)


def _route_offsite(job: dict, target: str, routed: list, manual: list, log: Callable) -> None:
    ref = detect_ats(target) if target else None
    if ref is not None and ref.supported and ref.job_id:
        routed.append({**job, "ats_url": target, "ats": f"{ref.platform}:{ref.company}/{ref.job_id}"})
        log(f"→ {job['job_id']}: applies on {ref.platform} ({target}) — routed to ATS Apply")
        return
    where = f"on {ref.platform}" if ref is not None else "on the employer's site"
    manual.append({**job, "status": "manual", "apply_url": target or job["url"],
                   "reason": f"applies {where} (Seek link-out) — not a supported ATS job link; open apply_url"})
    log(f"→ {job['job_id']}: applies off-site {where} — manual lead")


def _manual_from_routed(routed: list[dict], why: str) -> list[dict]:
    return [{**r, "status": "manual", "apply_url": r["ats_url"],
             "reason": f"applies on {r['ats'].split(':')[0]} — {why}; run ATS Apply with boards={r['ats_url']}"}
            for r in routed]


def _open_browser(box: dict, info: dict, state: str, log: Callable):  # pragma: no cover - real browser
    """Headed real Chrome with the saved session (Seek sits behind Cloudflare;
    headless is challenged), checked for a live login on the market's home."""
    from playwright.sync_api import sync_playwright

    from src.automation.tools.ats_common import launch_browser
    box["pw"] = sync_playwright().start()
    box["browser"] = launch_browser(box["pw"], headless=os.getenv("SEEK_HEADLESS", "0") == "1")
    ctx = box["browser"].new_context(storage_state=state, locale="en-US",
                                     viewport={"width": 1366, "height": 900})
    page = ctx.new_page()
    page.goto(info["base"] + "/", wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2500)
    if looks_logged_out(page):
        raise SessionError(f"Not logged in to {info['label'].split()[0]} — the saved session may have "
                           f"expired; re-run `{LOGIN_SCRIPT} --site {info['site']}`")
    log(f"✓ Authenticated with {info['label']}")
    return PlaywrightWizard(page, info["base"])


def _close_browser(box: dict) -> None:
    for key, closer in (("browser", lambda b: b.close()), ("pw", lambda p: p.stop())):
        if key in box:
            try:
                closer(box[key])
            except Exception:  # noqa: BLE001
                pass


# ── BaseTool wrapper (no LLM gates; catalog/agent parity) ─────────────────────

class SeekApplyInput(BaseModel):
    keywords: str
    site: str = "au"
    location: str = ""
    max_applications: int = 5
    cover_letter: str = ""
    dry_run: bool = True


class SeekApplyTool(BaseTool):
    name: str = "seek_apply"
    description: str = (
        "Quick Apply to Seek (AU/NZ) or JobStreet (SG/MY/PH/ID) jobs with the user's saved "
        "login. Args: keywords (comma-separated), site (au|nz|sg|my|ph|id), location, "
        "max_applications, cover_letter, dry_run. Skips jobs already applied to, link-out jobs "
        "and jobs with an employer question it can't answer; submits only when dry_run is false "
        "and counts a job as applied only when Seek shows its success page."
    )
    args_schema: type[BaseModel] = SeekApplyInput

    def _run(self, keywords: str, site: str = "au", location: str = "", max_applications: int = 5,
             cover_letter: str = "", dry_run: bool = True) -> dict:
        try:
            return run_seek_apply(keywords=keywords, site=site, location=location,
                                  max_applications=max_applications, cover_letter=cover_letter,
                                  dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001
            return {"keywords": keywords, "applied": [], "skipped": [], "manual": [],
                    "warnings": [], "error": f"{type(exc).__name__}: {exc}"}
