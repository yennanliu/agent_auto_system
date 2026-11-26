"""Unit tests for the 104 auto-apply automation: area resolution, cover-letter
patterns, validator, and flow helpers."""
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine

from src import settings_store

# ── area resolution (tw104_area) ──────────────────────────────────────────────

def test_area_static_chinese_and_english():
    from src.automation.tools.tw104_area import resolve_area

    assert resolve_area("台北")[0] == "6001001000"
    assert resolve_area("台北市")[0] == "6001001000"
    assert resolve_area("臺北")[0] == "6001001000"          # 臺→台 normalisation
    assert resolve_area("taipei")[0] == "6001001000"
    assert resolve_area("Taipei City")[0] == "6001001000"


def test_area_multiword_english_not_split():
    """Regression: "New Taipei" must not split on the space into "taipei"."""
    from src.automation.tools.tw104_area import resolve_area

    assert resolve_area("new taipei")[0] == "6001002000"
    assert resolve_area("New Taipei City")[0] == "6001002000"


def test_area_suffix_fallback_and_raw_code():
    from src.automation.tools.tw104_area import resolve_area

    assert resolve_area("桃園縣")[0] == "6001005000"        # old 縣 name → 桃園市 code
    assert resolve_area("6001001000")[0] == "6001001000"    # raw code passes through


def test_area_multiple_comma_separated():
    from src.automation.tools.tw104_area import resolve_area

    assert resolve_area("高雄, 台中")[0] == "6001016000,6001008000"


def test_area_unresolved_is_nationwide_not_error():
    from src.automation.tools.tw104_area import resolve_area

    codes, note = resolve_area("nowhere-land")
    assert codes == ""
    assert "nationwide" in note


def test_area_llm_fallback_only_for_misses():
    from src.automation.tools.tw104_area import resolve_area

    calls = []

    def fake_llm(unresolved):
        # resolve_area's llm_fn contract: return comma-separated canonical NAMES
        # (the flow's adapter is what parses the crew's JSON into this string).
        calls.append(unresolved)
        return "台北市, 新北市"

    # A statically-resolvable input must NOT invoke the LLM.
    resolve_area("台北", llm_fn=fake_llm)
    assert calls == []

    # An unresolved input is handed to the LLM, whose names map back to codes.
    codes, _ = resolve_area("雙北", llm_fn=fake_llm)
    assert calls == ["雙北"]
    assert codes == "6001001000,6001002000"


def test_area_llm_failure_degrades_gracefully():
    from src.automation.tools.tw104_area import resolve_area

    def boom(_):
        raise RuntimeError("llm down")

    codes, _ = resolve_area("weirdplace", llm_fn=boom)  # must not raise
    assert codes == ""


# ── validator ─────────────────────────────────────────────────────────────────

def test_validator_accepts_processed_run():
    from src.automation.harness.validator import validate

    result = {"applied": [{"job_id": "abc"}], "skipped": [], "jobs_found": 1,
              "summary": "Keyword 'x': 1 job scanned, 1 prepared, 0 skipped."}
    assert validate("tw104_apply", result).valid


def test_validator_accepts_zero_jobs():
    from src.automation.harness.validator import validate

    result = {"applied": [], "skipped": [], "jobs_found": 0,
              "summary": "No open jobs found for keyword 'x'."}
    assert validate("tw104_apply", result).valid


def test_validator_rejects_error():
    from src.automation.harness.validator import validate

    assert not validate("tw104_apply", {"applied": [], "error": "not logged in"}).valid


# ── flow helpers ──────────────────────────────────────────────────────────────

def test_parse_areas_tolerates_prose():
    from src.automation.flows.tw104_apply_flow import _parse_areas

    assert _parse_areas('noise {"areas":["台北市","新北市"]} tail') == ["台北市", "新北市"]
    assert _parse_areas("not json") == []


def test_flow_raises_on_missing_keyword():
    from src.automation.flows.tw104_apply_flow import TW104ApplyFlow

    flow = TW104ApplyFlow()
    with pytest.raises(Exception):
        flow.kickoff(inputs={"keyword": ""})


# ── cover-letter patterns (settings_store) ────────────────────────────────────

@pytest.fixture
def store_engine(monkeypatch):
    import src.database as _db

    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(eng)
    monkeypatch.setattr(_db, "engine", eng)
    return eng


def test_cover_letter_crud(store_engine):
    assert settings_store.get_cover_letters() == []
    settings_store.save_cover_letter("standard", "hello 你好")
    got = settings_store.get_cover_letters()
    assert got == [{"name": "standard", "text": "hello 你好"}]

    # upsert by name (newest-first), not duplicate
    settings_store.save_cover_letter("standard", "updated")
    got = settings_store.get_cover_letters()
    assert len(got) == 1 and got[0]["text"] == "updated"

    settings_store.save_cover_letter("second", "another")
    assert [c["name"] for c in settings_store.get_cover_letters()] == ["second", "standard"]

    settings_store.delete_cover_letter("standard")
    assert [c["name"] for c in settings_store.get_cover_letters()] == ["second"]


def test_cover_letter_requires_name_and_caps_length(store_engine):
    with pytest.raises(ValueError):
        settings_store.save_cover_letter("  ", "text")
    settings_store.save_cover_letter("long", "x" * 5000)
    assert len(settings_store.get_cover_letters()[0]["text"]) == 2000


# ── tool: pure helpers & error paths (no browser) ─────────────────────────────

def test_search_url_builds_area_keyword_page():
    from src.automation.tools.tw104_apply_tool import _search_url

    url = _search_url("python", "6001001000", "1", 2)
    assert "keyword=python" in url and "area=6001001000" in url and "page=2" in url
    # blank area is omitted entirely (→ nationwide search)
    assert "area=" not in _search_url("python", "", "1", 1)


def test_search_url_remote_and_part_time_filters():
    from src.automation.tools.tw104_apply_tool import _search_url

    # off by default — no filter params emitted
    plain = _search_url("python", "", "1", 1)
    assert "remoteWork=" not in plain and "ro=" not in plain
    # remote → remoteWork=1 (完全遠端); part-time → ro=2 (工作性質: 兼職)
    assert "remoteWork=1" in _search_url("python", "", "1", 1, remote=True)
    assert "ro=2" in _search_url("python", "", "1", 1, part_time=True)
    both = _search_url("python", "", "1", 1, remote=True, part_time=True)
    assert "remoteWork=1" in both and "ro=2" in both


def test_run_no_session_returns_error_not_raise():
    from src.automation.tools.tw104_apply_tool import run_tw104_apply

    res = run_tw104_apply(keyword="x", state_path="/nope/does-not-exist.json")
    assert res["applied"] == [] and "error" in res
    assert "104_login.py" in res["error"]


def test_basetool_wraps_exceptions(mocker):
    import src.automation.tools.tw104_apply_tool as T

    mocker.patch.object(T, "run_tw104_apply", side_effect=RuntimeError("boom"))
    out = T.TW104ApplyTool()._run(keyword="x")
    assert out["applied"] == [] and "boom" in out["error"]


# ── tool: _looks_logged_out (fake page) ───────────────────────────────────────

class _FakeLoc:
    def __init__(self, n, visible=False):
        self._n = n
        self._visible = visible

    @property
    def first(self):
        return self

    def count(self):
        return self._n

    def is_visible(self):
        return self._visible

    def fill(self, text, **_kw):
        self.filled = text


class _FakePage:
    def __init__(self, url, locators=None):
        self.url = url
        self._locators = locators or {}

    def locator(self, sel):
        return self._locators.get(sel, _FakeLoc(0))


def test_looks_logged_out_true_on_login_url():
    from src.automation.tools.tw104_apply_tool import _looks_logged_out

    assert _looks_logged_out(_FakePage("https://www.104.com.tw/login")) is True


def test_looks_logged_in_when_positive_signal_present():
    from src.automation.tools.tw104_apply_tool import _looks_logged_out

    page = _FakePage("https://www.104.com.tw/jobs/search/",
                     {':text("我的104")': _FakeLoc(1)})
    assert _looks_logged_out(page) is False


def test_looks_logged_out_when_only_login_link_visible():
    from src.automation.tools.tw104_apply_tool import _looks_logged_out

    page = _FakePage("https://www.104.com.tw/jobs/search/",
                     {'a:has-text("會員登入"), a:has-text("登入")': _FakeLoc(1, visible=True)})
    assert _looks_logged_out(page) is True


# ── tool: _fill_cover_letter (fake popup) ─────────────────────────────────────

class _FakePopup:
    def __init__(self, textarea=None):
        self._textarea = textarea

    def locator(self, sel):
        if self._textarea is not None and "textarea" in sel:
            return self._textarea
        return _FakeLoc(0)


def test_fill_cover_letter_empty_leaves_default():
    from src.automation.tools.tw104_apply_tool import _fill_cover_letter

    ta = _FakeLoc(1, visible=True)
    _fill_cover_letter(_FakePopup(ta), "", log=lambda m: None, warnings=[], job_id="j")
    assert not hasattr(ta, "filled")  # untouched → site default kept


def test_fill_cover_letter_types_custom_text():
    from src.automation.tools.tw104_apply_tool import _fill_cover_letter

    ta = _FakeLoc(1, visible=True)
    _fill_cover_letter(_FakePopup(ta), "你好 hello", log=lambda m: None, warnings=[], job_id="j")
    assert ta.filled == "你好 hello"


def test_fill_cover_letter_truncates_to_limit():
    from src.automation.tools.tw104_apply_tool import _COVER_LETTER_MAX, _fill_cover_letter

    ta = _FakeLoc(1, visible=True)
    warnings = []
    _fill_cover_letter(_FakePopup(ta), "x" * 5000, log=lambda m: None, warnings=warnings, job_id="j")
    assert len(ta.filled) == _COVER_LETTER_MAX and any("truncated" in w for w in warnings)


def test_fill_cover_letter_missing_textarea_warns():
    from src.automation.tools.tw104_apply_tool import _fill_cover_letter

    warnings = []
    _fill_cover_letter(_FakePopup(None), "text", log=lambda m: None, warnings=warnings, job_id="j")
    assert any("自我推薦信" in w for w in warnings)


# ── flow orchestration (mocked tool + LLM; no live calls) ─────────────────────

def test_flow_resolves_area_name_and_passes_args(mocker):
    captured = {}
    mocker.patch("src.automation.flows.tw104_apply_flow.run_tw104_apply",
                 side_effect=lambda **kw: captured.update(kw) or {
                     "applied": [], "skipped": [], "jobs_found": 0, "summary": "none"})
    mocker.patch("src.automation.harness.provider.resolve",
                 return_value=(None, "gemini", "gemini/gemini-2.5-flash"))

    from src.automation.flows.tw104_apply_flow import TW104ApplyFlow

    TW104ApplyFlow().kickoff(inputs={
        "keyword": "軟體工程師", "area": "台北", "cover_letter": "我的推薦信",
        "max_applications": 4, "dry_run": False,
    })
    assert captured["keyword"] == "軟體工程師"
    assert captured["area"] == "6001001000"        # name resolved to code
    assert captured["cover_letter"] == "我的推薦信"
    assert captured["dry_run"] is False
    assert captured["relevance_fn"] is None         # no task_filter → no gate


def test_flow_wires_relevance_gate_when_task_filter_set(mocker):
    captured = {}
    mocker.patch("src.automation.flows.tw104_apply_flow.run_tw104_apply",
                 side_effect=lambda **kw: captured.update(kw) or {
                     "applied": [], "skipped": [], "jobs_found": 0, "summary": "none"})
    # A truthy dummy LLM object is enough — the gate fn is built but not invoked
    # here (run_tw104_apply is mocked).
    mocker.patch("src.automation.harness.provider.resolve",
                 return_value=(object(), "gemini", "gemini/gemini-2.5-flash"))

    from src.automation.flows.tw104_apply_flow import TW104ApplyFlow

    TW104ApplyFlow().kickoff(inputs={
        "keyword": "python", "task_filter": "只要後端職缺",
    })
    assert callable(captured["relevance_fn"])


# ── concurrent runs share the apply ledger (per_type.tw104_apply > 1) ─────────

def _run_with_jobs(mocker, tmp_path, jobs, apply, ledger, dry_run=False):
    """run_tw104_apply with Playwright and the listing scraper stubbed out."""
    import src.automation.tools.tw104_apply_tool as T

    state = tmp_path / "state.json"
    state.write_text("{}")
    mocker.patch("playwright.sync_api.sync_playwright")
    mocker.patch.object(T, "_looks_logged_out", return_value=False)
    pages = iter([jobs, []])
    mocker.patch.object(T, "_collect_page_jobs", side_effect=lambda *a, **k: next(pages))
    mocker.patch.object(T, "_apply_to_job", side_effect=apply)
    return T.run_tw104_apply(keyword="後端", max_applications=10, dry_run=dry_run,
                             state_path=str(state), ledger_path=ledger)


def _job104(jid, applied=False):
    return {"job_id": jid, "url": f"https://www.104.com.tw/job/{jid}",
            "title": f"Backend {jid}", "company": "Acme", "applied": applied}


def test_tw104_skips_a_job_claimed_or_ledgered_by_another_run(mocker, tmp_path):
    from src.automation.tools import ats_common as ac

    ledger = str(tmp_path / "l.json")
    ac.record_applied(ledger, "j2", {})                       # applied by an earlier run
    calls = []

    def apply(_ctx, _page, job, *_a):
        calls.append(job["job_id"])
        return {**job, "status": "submitted", "submitted": True}

    with ac.ledger_claim(ledger, "j3"):                        # another run is applying now
        out = _run_with_jobs(mocker, tmp_path, [_job104("j1"), _job104("j2"), _job104("j3")],
                             apply, ledger)
    assert calls == ["j1"]
    assert [e["job_id"] for e in out["applied"]] == ["j1"]
    reasons = {s["job_id"]: s["reason"] for s in out["skipped"]}
    assert reasons == {"j2": "already applied (ledger)",
                       "j3": "already applied (ledger, another run)"}
    assert ac.load_ledger(ledger)["j1"]["status"] == "applied"


def test_tw104_exception_after_submit_click_is_pending_and_never_resent(mocker, tmp_path):
    from src.automation.tools import ats_common as ac

    ledger = str(tmp_path / "l.json")

    def apply(_ctx, _page, job, _cl, _dry, _log, _warn, on_submit=None):
        if job["job_id"] == "j1":
            on_submit()
            raise RuntimeError("Target closed")                 # after 確認送出
        if job["job_id"] == "j2":
            on_submit()
            return {**job, "status": "unconfirmed", "submitted": False}
        raise RuntimeError("page.goto timeout")                 # j3: before 確認送出

    _run_with_jobs(mocker, tmp_path, [_job104("j1"), _job104("j2"), _job104("j3")], apply, ledger)
    assert {k: v["status"] for k, v in ac.load_ledger(ledger).items()} == {"j1": "pending", "j2": "pending"}

    retried = []
    out = _run_with_jobs(mocker, tmp_path, [_job104("j1"), _job104("j2"), _job104("j3")],
                         lambda _c, _p, job, *_a: retried.append(job["job_id"]) or
                         {**job, "status": "failed"}, ledger)
    assert retried == ["j3"]                                    # only the pre-Submit failure
    reasons = {s["job_id"]: s.get("reason", "") for s in out["skipped"]}
    assert reasons["j1"].startswith("submitted on an earlier run but never confirmed")


def test_tw104_dry_run_leaves_no_ledger_entry(mocker, tmp_path):
    from src.automation.tools import ats_common as ac

    ledger = str(tmp_path / "l.json")
    _run_with_jobs(mocker, tmp_path, [_job104("j1")],
                   lambda _c, _p, job, *_a: {**job, "status": "prepared", "submitted": False},
                   ledger, dry_run=True)
    assert ac.load_ledger(ledger) == {}


def test_tw104_job_page_already_applied_button_is_skipped(mocker):
    import src.automation.tools.tw104_apply_tool as T

    btn = mocker.Mock()
    btn.inner_text.return_value = "已應徵"
    mocker.patch.object(T, "_first_visible", return_value=btn)
    page = mocker.Mock()
    on_submit = mocker.Mock()
    entry = T._apply_to_job(mocker.Mock(), page, _job104("j1"), "", False, lambda _m: None, [],
                            on_submit)
    assert entry["status"] == "skipped" and entry["reason"] == "already applied (已應徵)"
    btn.click.assert_not_called()
    on_submit.assert_not_called()


def test_tw104_dry_run_does_not_block_a_concurrent_real_run(mocker, tmp_path):
    from src.automation.tools import ats_common as ac

    ledger = str(tmp_path / "l.json")
    seen = []

    def apply(_c, _p, job, *_a):
        with ac.ledger_claim(ledger, job["job_id"]) as real:     # a real run, mid dry-run
            seen.append(bool(real))
        return {**job, "status": "prepared", "submitted": False}

    _run_with_jobs(mocker, tmp_path, [_job104("j1")], apply, ledger, dry_run=True)
    assert seen == [True]


def test_tw104_lost_claim_never_clicks_confirm_send(mocker):
    import src.automation.tools.tw104_apply_tool as T

    btn, send, popup = mocker.Mock(), mocker.Mock(), mocker.Mock()
    btn.inner_text.return_value = "應徵"
    mocker.patch.object(T, "_first_visible", side_effect=[btn, send])
    mocker.patch.object(T, "_fill_cover_letter")
    ctx = mocker.Mock()
    ctx.expect_page.return_value.__enter__ = lambda s: mocker.Mock(value=popup)
    ctx.expect_page.return_value.__exit__ = lambda s, *a: False
    entry = T._apply_to_job(ctx, mocker.Mock(), _job104("j1"), "", False, lambda _m: None, [],
                            lambda: False)
    assert entry["status"] == "skipped" and "claim lost" in entry["reason"]
    send.click.assert_not_called()


def test_tw104_reads_the_ledger_once_per_run(mocker, tmp_path):
    import src.automation.tools.tw104_apply_tool as T

    spy = mocker.spy(T, "load_ledger")
    _run_with_jobs(mocker, tmp_path, [_job104(f"j{i}") for i in range(5)],
                   lambda _c, _p, job, *_a: {**job, "status": "failed"},
                   str(tmp_path / "l.json"))
    assert spy.call_count == 1


# ── an empty result page is not always the end of the listing ────────────────

class _BodyPage:
    def __init__(self, text):
        self._text = text

    def evaluate(self, _js):
        if isinstance(self._text, Exception):
            raise self._text
        return self._text


def test_bot_challenged_reads_the_interstitial():
    import src.automation.tools.tw104_apply_tool as T

    assert T._bot_challenged(_BodyPage("此網站使用安全服務抵禦惡意機器人。當網站驗證並確認您不是機器人時，便會顯示此頁面。"))
    assert not T._bot_challenged(_BodyPage("共 1000+ 筆 第 1 頁"))
    assert not T._bot_challenged(_BodyPage(""))
    assert not T._bot_challenged(_BodyPage(RuntimeError("page closed")))


def _run_with_pages(mocker, tmp_path, pages, challenged):
    """run_tw104_apply over a scripted sequence of result pages (None = failed to
    load); `challenged` scripts _bot_challenged for each empty read. Returns
    (result, log lines, the waits asked of the page in ms)."""
    import src.automation.tools.tw104_apply_tool as T

    state = tmp_path / "state.json"
    state.write_text("{}")
    pw = mocker.patch("playwright.sync_api.sync_playwright")
    page = (pw.return_value.__enter__.return_value.chromium.launch.return_value
            .new_context.return_value.new_page.return_value)
    mocker.patch.object(T, "_looks_logged_out", return_value=False)
    feed, flags = iter(pages), iter(challenged)
    mocker.patch.object(T, "_collect_page_jobs", side_effect=lambda *a, **k: next(feed))
    mocker.patch.object(T, "_bot_challenged", side_effect=lambda _p: next(flags))
    mocker.patch.object(T, "_apply_to_job", side_effect=lambda _c, _p, job, *_a: {
        **job, "status": "prepared", "submitted": False})
    logs = []
    out = T.run_tw104_apply(keyword="後端", max_applications=500, dry_run=True, log=logs.append,
                            state_path=str(state), ledger_path=str(tmp_path / "l.json"))
    waits = [c.args[0] for c in page.wait_for_timeout.call_args_list if c.args[0] >= 20_000]
    assert next(flags, None) is None, "unused _bot_challenged answers"
    return out, logs, waits


def _full_page(prefix):
    return [_job104(f"{prefix}{i}") for i in range(22)]


def test_tw104_challenged_first_page_is_retried_then_read(mocker, tmp_path):
    out, logs, waits = _run_with_pages(mocker, tmp_path, [[], [], [_job104("a1")], []],
                                       [True, True, False])
    assert waits == [20_000, 60_000]
    assert out["jobs_found"] == 1 and out["incomplete"] is False and out["warnings"] == []
    assert sum("bot check, not the listing; retrying" in m for m in logs) == 2


def test_tw104_first_page_still_challenged_is_flagged_not_a_clean_zero(mocker, tmp_path):
    out, _logs, waits = _run_with_pages(mocker, tmp_path, [[], [], []], [True, True, True])
    assert waits == [20_000, 60_000]
    assert out["jobs_found"] == 0 and out["incomplete"] is True
    assert "bot check" in out["summary"] and "No open jobs found" not in out["summary"]
    assert "page 1: 104 answered with its bot check 3 times" in out["warnings"][0]


def test_tw104_keyword_with_no_results_is_a_clean_zero_with_no_wait(mocker, tmp_path):
    out, _logs, waits = _run_with_pages(mocker, tmp_path, [[]], [False])
    assert waits == [] and out["jobs_found"] == 0 and out["incomplete"] is False
    assert out["summary"] == "No open jobs found for keyword '後端'." and out["warnings"] == []


def test_tw104_challenge_mid_listing_is_retried_and_the_run_carries_on(mocker, tmp_path):
    out, _logs, waits = _run_with_pages(
        mocker, tmp_path, [_full_page("a"), [], _full_page("b"), [_job104("c1")], []],
        [True, False])
    assert waits == [20_000]                       # recovered on the first re-read
    assert out["jobs_found"] == 45 and out["pages_scanned"] == 3 and out["incomplete"] is False


def test_tw104_truncated_listing_is_reported(mocker, tmp_path):
    out, logs, _waits = _run_with_pages(mocker, tmp_path, [_full_page("a"), [], [], []],
                                        [True, True, True])
    assert out["jobs_found"] == 22 and out["incomplete"] is True
    assert "Listing incomplete: stopped at page 2" in out["summary"]
    assert "page 2: 104 answered with its bot check" in out["warnings"][-1]
    assert not any("reached the end" in m for m in logs)


def test_tw104_listing_that_ends_on_a_full_page_is_the_real_end(mocker, tmp_path):
    """An empty page after a full one, with no bot check on it, is how a listing
    whose last page happens to be full ends: no wait, not incomplete."""
    out, logs, waits = _run_with_pages(mocker, tmp_path, [_full_page("a"), []], [False])
    assert waits == [] and out["incomplete"] is False and out["warnings"] == []
    assert out["jobs_found"] == 22 and any("reached the end" in m for m in logs)


def test_tw104_challenge_that_clears_onto_an_empty_listing_ends_normally(mocker, tmp_path):
    out, logs, waits = _run_with_pages(mocker, tmp_path, [_full_page("a"), [], []],
                                       [True, False])
    assert waits == [20_000] and out["incomplete"] is False
    assert out["jobs_found"] == 22 and any("reached the end" in m for m in logs)


def test_tw104_reread_that_fails_to_load_moves_on_to_the_next_page(mocker, tmp_path):
    """A re-read returning None (page failed to load) is not "still challenged":
    the run skips that page, as it does for any failed load, and carries on."""
    out, _logs, waits = _run_with_pages(
        mocker, tmp_path, [_full_page("a"), [], None, None, [_job104("c1")], []],
        [True, False])
    assert waits == [20_000, 60_000]
    assert out["jobs_found"] == 23 and out["incomplete"] is False


# ── relevance judge prompt (task_filter second gate) ─────────────────────────

def test_relevance_prompt_interpolates_and_judges_the_core_work():
    """The judge must look at what the job does, not at words in its title — an
    "AI" filter used to pass every title containing "AI" (video editors, sales)."""
    from src.automation.crews.tw104_relevance_crew.crew import _AGENTS, _TASKS

    task = _TASKS["judge_relevance_task"]
    text = task["description"].format(task_filter="AI", job_title="AI 影音剪輯師", job_meta="公司：X")
    assert "AI 影音剪輯師" in text and "公司：X" in text
    assert task["expected_output"].format().startswith("一個嚴格的 JSON 物件")
    for rule in ("核心工作", "嚴格條件", "符合任一項即可", "永遠優先"):
        assert rule in text, rule
    assert "核心工作" in _AGENTS["relevance_judge"]["backstory"]
