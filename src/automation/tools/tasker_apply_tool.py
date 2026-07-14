"""
Auto-apply (提案) to freelance cases on tasker.com.tw.

The site is a Nuxt SPA backed by a JSON API at api.tasker.com.tw. Clicking
我要提案 in the browser just calls that API and hydrates an in-memory store, so
driving the DOM is brittle (the propose form lives at /cases/propose and only
binds to a case via a store the click populates). Instead this tool talks to the
same API the site uses, which is deterministic and reliable:

  GET  /api/issue/{tk_no}/proposal        -> case title / content / budget /
                                             my existing proposal_content
  GET  /api/issue/{tk_no}/proposal/check  -> {status, data:{can_propose}}
  POST /api/issue/{tk_no}/proposal        -> submit (multipart form-data:
                                             initial_price_min, initial_price_max,
                                             content — those three ONLY)

Auth is a per-session `Authorization: Bearer <token>` the SPA derives from the
logged-in cookies. The token isn't in a readable storage key, so we capture it
at runtime from any api.tasker.com.tw request the page makes after loading with
the persisted session (created once via `scripts/tasker_login.py`).

Flow per run:
  1. Load tasker.com.tw with the saved session; capture the Bearer token.
  2. Fetch my real submitted-proposals list (/api/member/tasker/proposal
     ?status=proposed) — the ONLY reliable "already applied" signal. The
     per-case `proposal_content` field is just a pre-fill template the site
     returns for every proposable case, so it must NOT be used for this.
  3. Page through /cases?selected_categories=<ids>&page=N (DOM), auto-advancing
     until `max_cases` cases have actually been applied (prepared, in dry-run),
     the listing is exhausted, or an account-wide block is hit.
  4. For each case: skip if already proposed (via the list above) or ineligible
     (/proposal/check → can_propose), generate a 提案說明 via `proposal_fn`, and
     POST the proposal — unless dry_run, which prepares but never submits.
  5. After a POST the site reports "status 0", RE-QUERY the case and only count
     it as applied once the site confirms it (can_propose flips to false). A
     "status 0" response alone is not trusted.

The submit POST must send EXACTLY three multipart fields — initial_price_min,
initial_price_max, content — matching the real 送出提案 form. Sending any extra
field (e.g. quota_amount) makes the API reject the request with status 2700271.
Proposals do NOT require points/quota.

`run_tasker_apply(...)` is the primary entry point (the flow calls it directly
with an LLM-backed proposal_fn). `TaskerApplyTool` is a thin BaseTool wrapper
using a static template, for catalog/agent parity.
"""
import os
import re
import time
from collections.abc import Callable

from crewai.tools import BaseTool
from pydantic import BaseModel

_BASE = "https://www.tasker.com.tw"
_API = "https://api.tasker.com.tw"
DEFAULT_STATE_PATH = "data/tasker_state.json"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# Case ids look like TK26062904IFZF37
_CASE_ID_RE = re.compile(r"/cases/(TK[0-9A-Z]+)", re.IGNORECASE)
_MIN_CHARGE_FLOOR = 1000  # site rule: 最低金額不可小於 1000

# proposal/check error statuses → human reason
_CHECK_ERRORS = {
    "1230075": "not eligible to propose (您尚未具備提案資格)",
    "4030075": "case expired (此案件已失效)",
    "4030213": "your own case (您無法對自己的案件提案)",
    "4700246": "cannot propose (案件不可提案：可能已關閉或不符資格)",
}
# postProposal error statuses → human reason
_SUBMIT_ERRORS = {
    "2700071": "proposal text format error (提案說明格式錯誤)",
    "2700247": "min price missing/invalid (初次報價最小值未填或格式錯誤)",
    "2700248": "max price missing/invalid (初次報價最大值/範圍錯誤，最大值須大於最小值)",
    # 2700271 = request rejected. We used to hit this by sending an extra
    # multipart field (quota_amount); the fix is to send only the 3 real fields.
    "2700271": "proposal rejected (status 2700271 — check request fields/price)",
    "1230075": "not eligible to propose (您尚未具備提案資格)",
}
# Submit statuses that mean *every* further submission will also fail (a global
# account state, not a per-case problem) — stop scanning when we hit one.
# 1230075 = the account is not eligible to propose at all.
_STOP_STATUSES = {"1230075"}

_DEFAULT_TEMPLATE = (
    "您好，我對這個案件很感興趣，具備完成此需求的相關經驗，"
    "能依您的規格與時程交付，過程中保持清楚溝通。\n\n"
    "請參考我的專案:\n"
    "- https://brothersupport.github.io/ai_consultant/index.html\n"
    "- https://yennj12.js.org/ai_builder.html\n"
    "- https://yennj12.js.org/yennj12_blog_V4/\n\n"
    "前FAANG工程師, 9年開發經驗 精通backend, AI, full stack, cloud infra. "
    "python/javascript/java/typescript\n\n"
    "上方為初步估價，實際費用可依細節討論調整。期待與您合作，謝謝！"
)


def _noop(_msg: str) -> None:
    pass


# ── session / auth ───────────────────────────────────────────────────────────

def _looks_logged_out(page) -> bool:
    """Reliable positive signals (登出 / 會員代碼 / avatar) beat the ever-present
    /auth/login link the site keeps in the header even when authenticated."""
    if "/auth/login" in page.url or "/auth/legacy" in page.url:
        return True
    try:
        for sel in (':text("登出")', ':text("會員代碼")', 'img[src*="avatar"]'):
            if page.locator(sel).count() > 0:
                return False
    except Exception:  # noqa: BLE001
        pass
    try:
        loc = page.locator('a[href*="/auth/login"], a:has-text("立即登入")')
        return loc.count() > 0 and loc.first.is_visible()
    except Exception:  # noqa: BLE001
        return False


def _capture_session(page, category_ids: str, log: Callable) -> str | None:
    """Load the category listing with the saved session and capture the Bearer
    token from any api.tasker.com.tw request. Returns the token or None."""
    holder = {"bearer": None}

    def _on_request(req):
        if holder["bearer"]:
            return
        if "api.tasker.com.tw" in req.url:
            auth = req.headers.get("authorization")
            if auth and auth.lower().startswith("bearer "):
                holder["bearer"] = auth

    page.on("request", _on_request)
    url = f"{_BASE}/cases?selected_categories={category_ids}"
    log(f"Opening {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    # Poll: the SPA fires member/user API calls (carrying the token) after load.
    for _ in range(16):
        if holder["bearer"]:
            break
        page.wait_for_timeout(500)

    if _looks_logged_out(page) and not holder["bearer"]:
        return None
    if not holder["bearer"]:
        log("Logged in but no API token captured yet; retrying via a case page...")
        # Any authenticated page reliably fires a token-bearing request.
        page.goto(f"{_BASE}/users/tasker/proposal-management",
                  wait_until="domcontentloaded", timeout=30000)
        for _ in range(12):
            if holder["bearer"]:
                break
            page.wait_for_timeout(500)
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)
    return holder["bearer"]


def _headers(bearer: str) -> dict:
    return {
        "authorization": bearer,
        "accept": "application/json",
        "origin": _BASE,
        "referer": _BASE + "/",
        "user-agent": _UA,
    }


# ── case discovery (DOM, paginated) ──────────────────────────────────────────

def _collect_page_case_ids(page, category_ids: str, page_num: int,
                           log: Callable) -> list[str]:
    """Case ids on a single listing page (?page=N). The listing lazy-loads on
    scroll, so we scroll until the count stops growing for that page."""
    url = f"{_BASE}/cases?selected_categories={category_ids}&page={page_num}"
    # goto is the one unguarded external call here; a transient failure would
    # otherwise crash the whole run. Retry a few times before giving up on the page.
    for attempt in range(3):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1500)
            break
        except Exception as exc:  # noqa: BLE001
            if attempt == 2:
                log(f"⚠ Page {page_num}: failed to load after 3 attempts ({exc})")
                return []
            log(f"⚠ Page {page_num}: load attempt {attempt + 1} failed, retrying ({exc})")
            page.wait_for_timeout(2000)
    seen: list[str] = []
    seen_set: set[str] = set()
    prev = -1
    for _ in range(8):
        try:
            hrefs = page.evaluate(
                "() => Array.from(document.querySelectorAll('a[href*=\"/cases/\"]'))"
                ".map(a => a.getAttribute('href') || '')"
            )
        except Exception:  # noqa: BLE001
            hrefs = []
        for h in hrefs:
            m = _CASE_ID_RE.search(h or "")
            if not m:
                continue
            cid = m.group(1).upper()
            if cid not in seen_set:
                seen_set.add(cid)
                seen.append(cid)
        if len(seen) == prev:
            break  # this page fully loaded
        prev = len(seen)
        # window.scrollBy is more deterministic than mouse.wheel (no dependency
        # on cursor position over the scroll container) for triggering lazy load.
        try:
            page.evaluate("() => window.scrollBy(0, 3000)")
        except Exception:  # noqa: BLE001
            page.mouse.wheel(0, 3000)
        page.wait_for_timeout(1000)
    log(f"Page {page_num}: found {len(seen)} case link(s)")
    return seen


# ── API calls ────────────────────────────────────────────────────────────────

def _get_json(ctx_request, bearer: str, path: str) -> dict | None:
    try:
        resp = ctx_request.get(f"{_API}{path}", headers=_headers(bearer), timeout=20000)
        return resp.json()
    except Exception:  # noqa: BLE001
        return None


def _my_proposed_ids(ctx_request, bearer: str, log: Callable,
                     max_pages: int = 10) -> set[str]:
    """The set of tk_no I have *actually* submitted a proposal to. This is the
    authoritative 'already applied' signal — the per-case `proposal_content`
    field is only a pre-fill template and must NOT be used for this."""
    ids: set[str] = set()
    for pg in range(1, max_pages + 1):
        d = _get_json(ctx_request, bearer,
                      f"/api/member/tasker/proposal?status=proposed&page={pg}")
        if not isinstance(d, dict) or str(d.get("status")) != "0":
            break
        data = d.get("data") if isinstance(d.get("data"), dict) else {}
        rows = data.get("list") or []
        if not rows:
            break
        for row in rows:
            tk = row.get("tk_no")
            if tk:
                ids.add(str(tk).upper())
        if pg >= int(data.get("total_pages") or 1):
            break
    log(f"You have {len(ids)} existing proposal(s) on record.")
    return ids


def _confirm_recorded(ctx_request, bearer: str, cid: str, retries: int = 3) -> bool:
    """Re-query the site after a submit to confirm it was truly recorded: an
    applied case flips to can_propose=false (and its proposal endpoint returns
    the 4700246 'already handled' status). Only trust success when the site
    reflects it — a 'status 0' POST response alone is not enough.

    Retries a few times: the read-back can briefly lag the write (eventual
    consistency) or hit a transient network blip, and a false negative here
    would both mislabel a real success and trigger an extra submission."""
    for attempt in range(retries):
        c = _get_json(ctx_request, bearer, f"/api/issue/{cid}/proposal/check")
        if isinstance(c, dict) and str(c.get("status")) == "0":
            data = c.get("data") if isinstance(c.get("data"), dict) else {}
            if data.get("can_propose") is False:
                return True
        d = _get_json(ctx_request, bearer, f"/api/issue/{cid}/proposal")
        if isinstance(d, dict) and str(d.get("status")) == "4700246":
            return True
        if attempt < retries - 1:
            time.sleep(1)
    return False


def _case_info(ctx_request, bearer: str, cid: str) -> dict:
    """Title / description / budget / my existing proposal for a case."""
    d = _get_json(ctx_request, bearer, f"/api/issue/{cid}/proposal")
    if not isinstance(d, dict):
        d = {}
    data = d.get("data") if isinstance(d.get("data"), dict) else d
    if not isinstance(data, dict):
        data = {}
    return {
        "title": data.get("title", "") or "",
        "content": data.get("content", "") or "",
        "budget_text": data.get("budget_text", "") or "",
        "proposal_content": data.get("proposal_content", "") or "",
    }


def _check(ctx_request, bearer: str, cid: str) -> tuple[bool, str]:
    """Can we propose to this case? Returns (ok, reason_if_not)."""
    d = _get_json(ctx_request, bearer, f"/api/issue/{cid}/proposal/check")
    if not isinstance(d, dict):
        return False, "eligibility check failed (no response)"
    status = str(d.get("status"))
    if status == "0":
        data = d.get("data") or {}
        if data.get("can_propose"):
            return True, ""
        return False, "cannot propose (can_propose=false)"
    return False, _CHECK_ERRORS.get(status, f"eligibility check failed (status {status})")


def _submit(ctx_request, bearer: str, cid: str, min_charge: int, max_charge: int,
            content: str) -> tuple[bool, str, str | None]:
    """POST the proposal as multipart form-data. Returns (ok, message, status).

    STRICT success: only ``resp.status == 200`` AND site ``status == "0"``. A
    non-JSON body or any other status is a failure — we never infer success from
    the HTTP code alone (a plain 200 page is not a recorded proposal)."""
    try:
        # EXACTLY the three fields the real 送出提案 form sends. Sending an extra
        # field (we previously sent quota_amount) makes the API reject the whole
        # request with status 2700271 — proposals do NOT require points/quota.
        resp = ctx_request.post(
            f"{_API}/api/issue/{cid}/proposal",
            headers=_headers(bearer),
            multipart={
                "initial_price_min": str(min_charge),
                "initial_price_max": str(max_charge),
                "content": content,
            },
            timeout=30000,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}", None
    status: str | None = None
    try:
        d = resp.json()
        if isinstance(d, dict):
            status = str(d.get("status"))
    except Exception:  # noqa: BLE001
        d = None
    if resp.status == 200 and status == "0":
        return True, "site accepted (status 0)", status
    reason = _SUBMIT_ERRORS.get(
        status, f"submit failed (HTTP {resp.status}, status {status})")
    return False, reason, status


# ── orchestration ────────────────────────────────────────────────────────────

def run_tasker_apply(
    *,
    category_ids: str,
    min_charge: int,
    max_charge: int,
    max_cases: int = 5,
    dry_run: bool = True,
    proposal_fn: Callable[[str, str], str] | None = None,
    relevance_fn: Callable[[str, str], tuple[bool, str]] | None = None,
    log: Callable[[str], None] | None = None,
    state_path: str | None = None,
    max_pages: int = 60,
) -> dict:
    """Log in and apply to open cases in the given category. See module docstring.

    ``max_cases`` is the number of cases to actually apply to (prepare, in
    dry-run); the scanner auto-advances through listing pages — skipping cases
    already proposed / not eligible — until it reaches that target, runs out of
    new cases (up to ``max_pages``), or hits an account-wide block."""
    from playwright.sync_api import sync_playwright

    log = log or _noop
    proposal_fn = proposal_fn or (lambda _t, _d: _DEFAULT_TEMPLATE)
    # Optional "second gate": a case that survives the category URL filter and the
    # eligibility check still must match relevance_fn (title, content) -> (keep, reason)
    # before we spend a proposal-writing call. Default keeps every case.
    relevance_fn = relevance_fn or (lambda _t, _d: (True, ""))
    state_path = state_path or os.getenv("TASKER_STORAGE_STATE", DEFAULT_STATE_PATH)
    username = os.getenv("TASKER_USERNAME", "")  # noqa: F841 (kept for parity/logs)
    max_cases = max(1, min(int(max_cases), 500))
    max_pages = max(1, min(int(max_pages), 100))

    base = {"category_ids": category_ids, "dry_run": dry_run,
            "applied": [], "skipped": []}

    if not (state_path and os.path.exists(state_path)):
        return {**base, "error": (
            f"No saved tasker.com.tw session at '{state_path}'. Refresh it from "
            "Admin → Sessions in the UI (local server only), or run "
            "`uv run python scripts/tasker_login.py` once to log in and save it."
        )}

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
            bearer = _capture_session(page, category_ids, log)
            if not bearer:
                return {**base, "error": (
                    "Not logged in / could not obtain an API token. The saved "
                    "session may have expired — refresh it from Admin → Sessions "
                    "in the UI (local server only), or re-run "
                    "`uv run python scripts/tasker_login.py`."
                )}
            log("✓ Authenticated with tasker.com.tw API")

            if min_charge < _MIN_CHARGE_FLOOR:
                log(f"⚠ min_charge {min_charge} is below the site minimum "
                    f"{_MIN_CHARGE_FLOOR}; submissions may be rejected.")

            # Authoritative 'already applied' set — never infer this from the
            # per-case proposal_content field (that's just a pre-fill template).
            my_proposed = _my_proposed_ids(ctx.request, bearer, log)

            # `max_cases` is the number of cases to ACTUALLY apply to (prepare in
            # dry-run). We page through the listing until we hit that target, run
            # out of new cases, or hit an account-wide block.
            applied: list[dict] = []
            skipped: list[dict] = []
            seen: set[str] = set()
            scanned = 0
            filtered = 0
            pages_scanned = 0
            block_msg: str | None = None
            page_num = 1

            while len(applied) < max_cases and page_num <= max_pages:
                page_ids = _collect_page_case_ids(page, category_ids, page_num, log)
                new_ids = [c for c in page_ids if c not in seen]
                if not new_ids:
                    log(f"No new cases on page {page_num}; reached the end.")
                    break
                seen.update(new_ids)
                pages_scanned += 1

                for cid in new_ids:
                    if len(applied) >= max_cases:
                        break
                    scanned += 1
                    url = f"{_BASE}/cases/{cid}"
                    log(f"[{len(applied)}/{max_cases} applied | scan #{scanned}] {cid}")
                    entry = {"case_id": cid, "url": url}
                    try:
                        if cid in my_proposed:
                            entry.update(status="skipped", reason="already proposed")
                            log(f"↷ {cid}: already proposed — next")
                            skipped.append(entry)
                            continue

                        ok, reason = _check(ctx.request, bearer, cid)
                        if not ok:
                            entry.update(status="skipped", reason=reason)
                            log(f"↷ {cid}: {reason} — next")
                            skipped.append(entry)
                            continue

                        info = _case_info(ctx.request, bearer, cid)
                        entry["title"] = (info.get("title") or cid)[:200]

                        # Second gate: relevance filter on the case content. Fail
                        # open — a raising relevance_fn must never drop a good case
                        # (the surrounding except would otherwise mark it failed).
                        try:
                            keep, why = relevance_fn(info.get("title", ""),
                                                     info.get("content", ""))
                        except Exception as exc:  # noqa: BLE001
                            keep, why = True, ""
                            log(f"⚠ {cid}: relevance filter errored ({exc}); "
                                "keeping case (fail-open)")
                        if not keep:
                            reason = f"filtered out: {why}" if why else "filtered out by task_filter"
                            entry.update(status="skipped", reason=reason, filtered=True)
                            log(f"↷ {cid}: {reason} — next")
                            skipped.append(entry)
                            filtered += 1
                            continue

                        proposal = (proposal_fn(info.get("title", ""),
                                                info.get("content", "")) or "").strip() \
                            or _DEFAULT_TEMPLATE
                        entry.update(min_charge=min_charge, max_charge=max_charge,
                                     proposal=proposal[:500])

                        if dry_run:
                            entry.update(status="prepared", submitted=False,
                                         reason="dry_run")
                            log(f"✓ {cid}: proposal prepared (dry-run, NOT submitted)")
                            applied.append(entry)
                            continue

                        sok, smsg, sstatus = _submit(ctx.request, bearer, cid,
                                                     min_charge, max_charge, proposal)
                        if not sok:
                            entry.update(status="failed", submitted=False, reason=smsg)
                            log(f"✗ {cid}: submit failed — {smsg}")
                            skipped.append(entry)
                            if sstatus in _STOP_STATUSES:
                                block_msg = smsg
                                log("■ Account-wide block hit — every further "
                                    "submission would also fail; stopping.")
                                break
                            continue

                        # Site said 'accepted' — now CONFIRM it was truly recorded
                        # before claiming success (per user's requirement).
                        if _confirm_recorded(ctx.request, bearer, cid):
                            entry.update(status="submitted", submitted=True,
                                         confirmation=smsg)
                            log(f"✓ {cid}: 提案 submitted AND confirmed by site")
                            applied.append(entry)
                        else:
                            entry.update(
                                status="unconfirmed", submitted=False,
                                reason=("site returned success but the proposal was "
                                        "NOT confirmed on re-check — treating as failed"))
                            log(f"⚠ {cid}: submit unconfirmed — NOT counting as applied")
                            skipped.append(entry)
                    except Exception as exc:  # noqa: BLE001 — one bad case shouldn't stop the run
                        entry.update(status="failed", reason=f"{type(exc).__name__}: {exc}")
                        log(f"✗ {cid}: {type(exc).__name__}: {exc}")
                        skipped.append(entry)

                if block_msg:
                    break
                page_num += 1

            if scanned == 0:
                return {**base, "cases_found": 0,
                        "summary": f"No open cases found for category {category_ids}."}

            submitted_n = sum(1 for e in applied if e.get("submitted"))
            verb = "prepared (dry-run)" if dry_run else "submitted & confirmed"
            summary = (
                f"Category {category_ids}: {scanned} case(s) scanned across "
                f"{pages_scanned} page(s), {len(applied)} {verb}, "
                f"{len(skipped)} skipped"
            )
            summary += f" ({filtered} filtered by task_filter)." if filtered else "."
            if block_msg:
                summary += f" Stopped early: {block_msg}."
            return {
                **base,
                "cases_found": scanned,
                "pages_scanned": pages_scanned,
                "applied": applied,
                "skipped": skipped,
                "applied_count": len(applied),
                "submitted_count": submitted_n,
                "skipped_count": len(skipped),
                "filtered_count": filtered,
                "blocked": block_msg,
                "summary": summary,
            }
        finally:
            browser.close()


# ── BaseTool wrapper (static-template proposal; catalog/agent parity) ─────────

class TaskerApplyInput(BaseModel):
    category_ids: str
    min_charge: int
    max_charge: int
    proposal_template: str = _DEFAULT_TEMPLATE
    max_cases: int = 5
    dry_run: bool = True


class TaskerApplyTool(BaseTool):
    name: str = "tasker_apply"
    description: str = (
        "Log in to tasker.com.tw and auto-apply (提案) to open cases in a category "
        "via its JSON API. Args: category_ids (e.g. '110' or '110,101001'), "
        "min_charge, max_charge, proposal_template, max_cases, dry_run. Checks "
        "eligibility, skips already-applied/own/expired cases, and submits the "
        "proposal only when dry_run is false."
    )
    args_schema: type[BaseModel] = TaskerApplyInput

    def _run(self, category_ids: str, min_charge: int, max_charge: int,
             proposal_template: str = _DEFAULT_TEMPLATE, max_cases: int = 5,
             dry_run: bool = True) -> dict:
        template = proposal_template or _DEFAULT_TEMPLATE

        def _proposal(title: str, _desc: str) -> str:
            try:
                return template.format(title=title)
            except Exception:  # noqa: BLE001 — template may lack {title}
                return template

        try:
            return run_tasker_apply(
                category_ids=category_ids, min_charge=min_charge, max_charge=max_charge,
                max_cases=max_cases, dry_run=dry_run, proposal_fn=_proposal,
            )
        except Exception as exc:  # noqa: BLE001
            return {"category_ids": category_ids, "applied": [], "skipped": [],
                    "error": f"{type(exc).__name__}: {exc}"}
