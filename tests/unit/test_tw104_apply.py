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
