"""Multi-term search + LLM keyword expansion for linkedin_apply (no browser)."""
import contextlib

import playwright.sync_api

from src.automation.flows import linkedin_apply_flow as flow_mod
from src.automation.tools import linkedin_apply_tool as t

# ── split_keywords ────────────────────────────────────────────────────────────

def test_split_keywords_mixed_separators_and_dedup():
    assert t.split_keywords("Forward Deployed Engineer, AI Engineer、FDE；fde\nai  engineer") == [
        "Forward Deployed Engineer", "AI Engineer", "FDE"]


def test_split_keywords_keeps_slash_terms_and_accepts_lists():
    assert t.split_keywords("AI/ML Engineer") == ["AI/ML Engineer"]
    assert t.split_keywords(["A, B", "C", ""]) == ["A", "B", "C"]
    assert t.split_keywords("") == []
    assert len(t.split_keywords(",".join(f"t{i}" for i in range(20)))) == t.MAX_SEARCH_TERMS


# ── _parse_keyword_list ───────────────────────────────────────────────────────

def test_parse_keyword_list_variants():
    assert flow_mod._parse_keyword_list('```json\n{"keywords": ["FDE", "AI Engineer"]}\n```') == [
        "FDE", "AI Engineer"]
    assert flow_mod._parse_keyword_list('Sure: ["FDE", " ", 3]') == ["FDE"]
    assert flow_mod._parse_keyword_list("no json here") == []
    assert flow_mod._parse_keyword_list("") == []


# ── LLM expansion step ────────────────────────────────────────────────────────

class _Result:
    def __init__(self, raw): self.raw = raw


def _flow(**state):
    f = flow_mod.LinkedInApplyFlow()
    for k, v in state.items():
        setattr(f.state, k, v)
    return f


def _fake_crew(monkeypatch, raw=None, exc=None, seen_inputs=None):
    class FakeCrew:
        def __init__(self, llm=None): pass
        def crew(self): return self
        def kickoff(self, inputs):
            if seen_inputs is not None:
                seen_inputs.update(inputs)
            if exc:
                raise exc
            return _Result(raw)
    monkeypatch.setattr(flow_mod, "LinkedInKeywordCrew", FakeCrew)
    monkeypatch.setattr(flow_mod, "append_log", lambda *_a, **_k: None)


def test_expand_terms_appends_new_terms_only(monkeypatch):
    inputs: dict = {}
    _fake_crew(monkeypatch, '{"keywords": ["FDE", "forward deployed engineer", "AI Engineer", '
                            '"Solutions Engineer", "Applied AI Engineer"]}', seen_inputs=inputs)
    out = _flow(location="singapore")._expand_terms(
        ["Forward Deployed Engineer"], object(), lambda _r: None)
    # duplicate of the given term dropped; capped at MAX_EXPANDED_TERMS extra
    assert out == ["Forward Deployed Engineer", "FDE", "AI Engineer", "Solutions Engineer"]
    assert inputs["max_extra"] == flow_mod.MAX_EXPANDED_TERMS
    assert inputs["location"] == "singapore"


def test_expand_terms_fails_open(monkeypatch):
    _fake_crew(monkeypatch, exc=RuntimeError("boom"))
    assert _flow()._expand_terms(["X"], object(), lambda _r: None) == ["X"]
    _fake_crew(monkeypatch, "garbage")
    assert _flow()._expand_terms(["X"], object(), lambda _r: None) == ["X"]
    assert _flow()._expand_terms(["X"], None, lambda _r: None) == ["X"]


# ── multi-term scan in run_linkedin_apply ─────────────────────────────────────

class _Page:
    def goto(self, *a, **k): pass
    def wait_for_timeout(self, *_a): pass


class _Browser:
    def new_context(self, **_k):
        return type("Ctx", (), {"new_page": lambda _s: _Page()})()
    def close(self): pass


def _fake_browser(monkeypatch, tmp_path, listings):
    """listings: {term: [[job_id, ...] per page]}"""
    state = tmp_path / "state.json"
    state.write_text("{}")
    monkeypatch.setattr(playwright.sync_api, "sync_playwright",
                        lambda: contextlib.nullcontext(object()))
    monkeypatch.setattr(t, "_launch_browser", lambda _pw: _Browser())
    monkeypatch.setattr(t, "_looks_logged_out", lambda _p: False)
    monkeypatch.setattr(t, "_resolve_geo_id", lambda *_a, **_k: ("1", "X"))
    calls: list = []

    def collect(_page, term, _loc, page_num, *_a, **_k):
        calls.append((term, page_num))
        pages = listings.get(term, [])
        ids = pages[page_num - 1] if page_num <= len(pages) else []
        return [{"job_id": i, "title": f"Job {i}", "company": "", "applied": False} for i in ids]
    monkeypatch.setattr(t, "_collect_page_jobs", collect)
    applied_ids: list = []

    def apply(_page, job, *_a, **_k):
        applied_ids.append(job["job_id"])
        return {**job, "status": "prepared", "submitted": False}
    monkeypatch.setattr(t, "_apply_to_job", apply)
    return str(state), calls, applied_ids


def test_scans_each_term_sharing_dedup_and_budget(monkeypatch, tmp_path):
    state, calls, applied = _fake_browser(monkeypatch, tmp_path, {
        "Forward Deployed Engineer": [["1", "2"]],
        # page 1 fully overlaps term 1 → must keep paging, not abandon the term
        "FDE": [["1", "2"], ["3"]],
        "AI Engineer": [["4", "5"]],
    })
    out = t.run_linkedin_apply(keywords="Forward Deployed Engineer, FDE、AI Engineer",
                               max_applications=4, state_path=state)
    assert applied == ["1", "2", "3", "4"]  # each job once; stops at the budget
    assert out["search_terms"] == ["Forward Deployed Engineer", "FDE", "AI Engineer"]
    assert out["jobs_by_term"] == {"Forward Deployed Engineer": 2, "FDE": 1, "AI Engineer": 1}
    assert ("FDE", 2) in calls


def test_single_term_still_stops_on_repeated_last_page(monkeypatch, tmp_path):
    state, calls, applied = _fake_browser(monkeypatch, tmp_path, {
        "Backend": [["1"], ["1"], ["1"]],
    })
    out = t.run_linkedin_apply(keywords="Backend", max_applications=5, state_path=state)
    assert applied == ["1"]
    assert calls == [("Backend", 1), ("Backend", 2)]
    assert out["keywords"] == "Backend"
