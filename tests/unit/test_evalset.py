"""Eval regression set — offline tier + the live tier's plumbing (no LLM calls).

The first tests ARE the offline tier (what `scripts/run_evals.py` runs): a new
automation without an eval case or a justified skip turns this file red, next
to test_spec_registry.py. The rest pin the machinery so a broken gate can't
pass silently. See doc/evals.md.
"""
import json
from types import SimpleNamespace

import pytest
import yaml

from src.automation import spec
from src.automation.harness import evalset
from src.automation.harness.evaluator import EvalResult

CASES, _LOAD_PROBLEMS = evalset.load_cases()


# ── The offline tier itself ──────────────────────────────────────────────────

def test_offline_tier_is_green():
    assert evalset.offline_problems() == []


def test_every_registered_automation_is_covered_or_skipped():
    skips, problems = evalset.load_skips()
    assert problems == []
    covered = {c.job_type for c in CASES}
    for jt in spec.REGISTRY:
        assert (jt in covered) != (jt in skips), (
            f"{jt}: needs an eval case under evals/{jt}/ OR a reason in evals/skip.yaml (not both)")


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.key)
def test_reference_output_meets_its_own_expectations(case):
    assert evalset.check_expectations(case, case.reference_output) == []


def test_committed_baseline_is_well_formed():
    baseline = json.loads((evalset.EVALS_ROOT / evalset.BASELINE_FILE).read_text())
    assert isinstance(baseline["scores"], dict)
    keys = {c.key for c in CASES}
    assert set(baseline["scores"]) <= keys, "baseline has scores for cases that no longer exist"


# ── Schema / coverage guards actually bite ───────────────────────────────────

def _write_case(root, job_type="web_scraper", case_id="c1", directory=None, **overrides):
    raw = {
        "id": case_id, "job_type": job_type, "payload": {"url": "https://example.com"},
        "fixtures": {"tools": {"web_scraper": {"title": "T", "text": "x"}}},
        "expect": {"contains": ["Example"]},
        "reference_output": {"title": "Example Domain", "summary": "An example page for docs."},
        **overrides,
    }
    d = root / (directory or evalset._dir_for(job_type))
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{case_id}.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")


def test_well_formed_case_loads(tmp_path):
    _write_case(tmp_path)
    cases, problems = evalset.load_cases(tmp_path)
    assert problems == [] and [c.key for c in cases] == ["web_scraper/c1"]


@pytest.mark.parametrize("overrides, needle", [
    ({"id": "other"}, "must equal the file name"),
    ({"job_type": "hacker_news_digest"}, "belongs in evals/hacker_news_digest/"),
    ({"reference_output": "text"}, "'reference_output' is required"),
    ({"extra": 1}, "unknown key(s)"),
    ({"expect": {}}, "needs at least one of"),
    ({"expect": {"assertions": [{"path": "a"}]}}, "needs at least one check"),
    ({"expect": {"assertions": [{"path": "a", "regex": "x"}]}}, "unknown check(s)"),
    ({"expect": {"contains": ["x"], "min_judge_score": 140}}, "min_judge_score"),
])
def test_malformed_case_is_rejected(tmp_path, overrides, needle):
    _write_case(tmp_path, directory="web_scraper", **overrides)
    cases, problems = evalset.load_cases(tmp_path)
    assert cases == []
    assert any(needle in p for p in problems), problems


def test_coverage_flags_uncovered_doubly_listed_and_stale(tmp_path):
    _write_case(tmp_path)
    cases, _ = evalset.load_cases(tmp_path)
    problems = evalset.coverage_problems(
        cases, {"web_scraper": "x" * 30, "gone_type": "y" * 30},
        job_types=["web_scraper", "brand_new_automation"])
    text = "\n".join(problems)
    assert "brand_new_automation: no eval case" in text
    assert "web_scraper: has eval cases but is also in skip.yaml" in text
    assert "'gone_type' is not a registered automation" in text


def test_skip_needs_a_real_reason(tmp_path):
    (tmp_path / evalset.SKIP_FILE).write_text("pipeline: n/a\n")
    _, problems = evalset.load_skips(tmp_path)
    assert problems and "needs a real reason" in problems[0]


def test_bad_fixture_and_bad_reference_fail_offline(tmp_path):
    _write_case(tmp_path, case_id="nofixture", fixtures={})
    _write_case(tmp_path, case_id="badref", expect={"contains": ["not in the output"]})
    text = "\n".join(evalset.offline_problems(tmp_path))
    assert "web_scraper/nofixture: fixtures.tools must define exactly 'web_scraper'" in text
    assert "web_scraper/badref: reference_output: missing expected fact" in text


# ── Expectations ─────────────────────────────────────────────────────────────

def test_resolve_path_indexes_and_fans_out():
    doc = {"a": {"b": [{"c": 1}, {"c": 2}]}}
    assert evalset.resolve_path(doc, "a.b[*].c") == [1, 2]
    assert evalset.resolve_path(doc, "a.b[1].c") == [2]
    assert evalset.resolve_path(doc, "a.b[5].c") == [evalset._MISSING]
    assert evalset.resolve_path(doc, "a.zzz") == [evalset._MISSING]
    assert evalset.resolve_path({"a": []}, "a[*].c") == []


def _case(expect, job_type="hacker_news_digest"):
    return evalset.Case(id="t", job_type=job_type, path=evalset.EVALS_ROOT, payload={},
                        expect=expect, reference_output={})


def test_assertions_report_each_violation():
    out = {"stories": [{"title": "Alpha story", "score": 5}, {"title": "", "score": True}], "n": 2}
    case = _case({"assertions": [
        {"path": "stories", "type": "list", "min_len": 3},
        {"path": "stories[*].title", "non_empty": True},
        {"path": "stories[*].score", "type": "number", "max": 4},
        {"path": "n", "equals": 3},
        {"path": "n", "one_of": [7, 8]},
        {"path": "missing.key", "type": "str"},
        {"path": "empty[*].x", "type": "str"},
    ]})
    failures = evalset.check_expectations(case, {**out, "empty": []})
    text = "\n".join(failures)
    assert "stories: length 2 violates min_len=3" in text
    assert "stories[*].title: is empty" in text
    assert "stories[*].score: 5 violates max=4" in text
    assert "stories[*].score: expected number, got true" in text  # bool is not a number
    assert "n: expected 3, got 2" in text and "n: 2 not in [7, 8]" in text
    assert "missing.key: not found" in text
    assert "empty[*].x: matched nothing" in text


def test_contains_validator_and_non_dict_output():
    case = _case({"contains": ["Alpha"], "not_contains": ["cannot"]})
    assert evalset.check_expectations(case, {"stories": [{"title": "ALPHA story is long enough"}]}) == []
    failures = evalset.check_expectations(case, {"message": "I cannot fetch any stories right now"})
    text = "\n".join(failures)
    assert "validator: expected valid=True, got valid=False (no stories in result)" in text
    assert "missing expected fact: 'Alpha'" in text and "contains forbidden text: 'cannot'" in text
    assert evalset.check_expectations(case, "raw text") == ["output is not a JSON object"]


def test_profit_consistency_catches_a_changed_number():
    case = next(c for c in CASES if c.key == "profit_health_check/four-signals")
    tampered = json.loads(json.dumps(case.reference_output))
    tampered["skus"][0]["net_profit"] += 100
    tampered["flags"]["fake_hits"] = []
    text = "\n".join(evalset.check_expectations(case, tampered))
    assert f"profit_calc: {tampered['skus'][0]['sku']}.net_profit" in text
    assert "profit_calc: flags.fake_hits" in text


# ── Live tier plumbing (LLM + judge stubbed) ─────────────────────────────────

def _result(score=80.0, judge="gemini/gemini-2.5-flash"):
    return evalset.CaseResult(key="jt/c", score=score, judge=judge)


def _baseline(score=85.0, judge="gemini/gemini-2.5-flash", run_model="openai/gpt-4o-mini"):
    return {"run_model": run_model, "scores": {"jt/c": {"score": score, "judge": judge}}}


def test_baseline_within_tolerance_is_ok():
    r = _result(80.0)
    evalset.compare_to_baseline(r, _baseline(85.0), "openai/gpt-4o-mini", 10)
    assert (r.status, r.comparison, r.delta) == ("pass", "ok", -5.0)


def test_baseline_regression_fails_the_case():
    r = _result(70.0)
    evalset.compare_to_baseline(r, _baseline(85.0), "openai/gpt-4o-mini", 10)
    assert (r.status, r.comparison) == ("fail", "regression")
    assert "below baseline 85.0" in r.failures[0]


def test_unset_or_incomparable_baseline_never_fails():
    r = _result(10.0)
    evalset.compare_to_baseline(r, {"scores": {}}, "openai/gpt-4o-mini", 10)
    assert (r.status, r.comparison) == ("pass", "new")

    r = _result(10.0)
    evalset.compare_to_baseline(r, _baseline(), "gemini/gemini-2.5-pro", 10)
    assert r.status == "pass" and r.comparison.startswith("skipped: baseline recorded with")

    r = _result(10.0, judge="openai/gpt-4o")
    evalset.compare_to_baseline(r, _baseline(), "openai/gpt-4o-mini", 10)
    assert r.status == "pass" and r.comparison.startswith("skipped: baseline judged by")


def _hn_case():
    return next(c for c in CASES if c.key == "hacker_news_digest/top3-infra-mix")


class _CrewCallingTool:
    """Stands in for HNDigestCrew: 'the LLM' calls the tool and echoes its rows."""

    def __init__(self, llm=None):
        pass

    def crew(self):
        from src.automation.tools.hn_tool import HNTopStoriesTool

        def kickoff(inputs):
            stories = HNTopStoriesTool()._run(limit=inputs["limit"])  # must be the fixture, not HN
            return SimpleNamespace(raw=json.dumps({
                "story_of_the_day": {"title": stories[0]["title"], "reason": "Highest score of the day."},
                "stories": [{**s, "summary": f"About {s['title']}"} for s in stories],
                "themes": ["Databases", "Reliability"],
            }))
        return SimpleNamespace(kickoff=kickoff)


@pytest.fixture
def stub_llm(mocker):
    mocker.patch("src.automation.harness.provider.resolve",
                 side_effect=lambda p, m, temperature=0.7: (object(), p, m))


def _judge(mocker, **kwargs):
    defaults = dict(score=88.0, confidence=0.9, notes="ok", method="llm",
                    judge_model="gemini/gemini-2.5-flash")
    return mocker.patch("src.automation.harness.evaluator.evaluate",
                        return_value=EvalResult(**{**defaults, **kwargs}))


def test_run_case_runs_the_real_flow_on_fixture_tools(mocker, stub_llm):
    mocker.patch("src.automation.flows.hn_digest_flow.HNDigestCrew", _CrewCallingTool)
    judge = _judge(mocker)
    case = _hn_case()
    res = evalset.run_case(case, "openai", "gpt-4o-mini", {"scores": {}})
    assert res.failures == [] and (res.status, res.score, res.comparison) == ("pass", 88.0, "new")
    assert [s["title"] for s in res.output["stories"]] == [
        s["title"] for s in case.fixtures["tools"]["hn_top_stories"]]
    # The judge is asked about the run's own provider/model so it can stay independent of it.
    assert judge.call_args.args[0] == "hacker_news_digest"
    assert judge.call_args.args[2:] == ("openai", "gpt-4o-mini")


def test_run_case_fails_below_min_judge_score(mocker, stub_llm):
    mocker.patch("src.automation.flows.hn_digest_flow.HNDigestCrew", _CrewCallingTool)
    _judge(mocker, score=40.0)
    res = evalset.run_case(_hn_case(), "openai", "gpt-4o-mini", {"scores": {}})
    assert res.status == "fail" and "below min_judge_score 70" in res.failures[0]


@pytest.mark.parametrize("judge_kwargs, why", [
    (dict(method="heuristic", judge_model=""), "heuristic fallback"),
    (dict(judge_model="openai/gpt-4o-mini"), "self-graded by the run model"),
])
def test_run_case_without_independent_judge_is_unscored(mocker, stub_llm, judge_kwargs, why):
    """Eval-judge independence: a heuristic or self-graded number is not a
    quality measurement — never a pass, never compared, never baselined."""
    mocker.patch("src.automation.flows.hn_digest_flow.HNDigestCrew", _CrewCallingTool)
    _judge(mocker, **judge_kwargs)
    baseline = {"scores": {"hacker_news_digest/top3-infra-mix": {"score": 99.0}}}
    res = evalset.run_case(_hn_case(), "openai", "gpt-4o-mini", baseline)
    assert res.status == "unscored" and res.score is None and res.comparison == ""
    assert why in res.failures[0]
    report = {"generated_at": "t", "run_model": "openai/gpt-4o-mini",
              "results": [evalset.asdict(res)]}
    assert evalset.baseline_from(report)["scores"] == {}


def test_run_case_reports_a_crash_as_error(mocker):
    mocker.patch("src.automation.harness.provider.resolve", side_effect=OSError("API key not set"))
    res = evalset.run_case(_hn_case(), "openai", "gpt-4o-mini", {"scores": {}})
    assert res.status == "error" and "API key not set" in res.failures[0]


def test_run_case_retries_a_transient_provider_error(mocker, stub_llm):
    mocker.patch.object(evalset, "_RETRY_BACKOFF_SECS", 0)
    calls = {"n": 0}

    class _FlakyCrew(_CrewCallingTool):
        def crew(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("503 UNAVAILABLE: the model is overloaded")
            return super().crew()

    mocker.patch("src.automation.flows.hn_digest_flow.HNDigestCrew", _FlakyCrew)
    _judge(mocker)
    res = evalset.run_case(_hn_case(), "openai", "gpt-4o-mini", {"scores": {}})
    assert calls["n"] == 2 and res.status == "pass"


def test_email_collect_case_runs_the_funnel_from_fixtures(mocker, stub_llm):
    """Discovery/extraction/verification come from the fixture; only qualify is 'LLM'."""
    case = next(c for c in CASES if c.key == "email_collect/dental-clinics-qualify")
    quals = [{"i": i, "icp_fit": lead["icp_fit"], "reason": lead["reason"], "hook": lead["hook"]}
             for i, lead in enumerate(case.reference_output["leads"])]
    fake_crew = SimpleNamespace(crew=lambda: SimpleNamespace(
        kickoff=lambda inputs: SimpleNamespace(raw=json.dumps(quals))))
    mocker.patch("src.automation.flows.email_collect_flow.EmailCollectCrew", return_value=fake_crew)
    _judge(mocker)
    res = evalset.run_case(case, "openai", "gpt-4o-mini", {"scores": {}})
    assert res.failures == [] and res.status == "pass"
    # The stubbed funnel reproduces the committed reference exactly.
    assert res.output == case.reference_output


def test_profit_fixture_upload_feeds_the_real_profit_calc():
    from src.automation.tools.profit_calc_tool import ProfitCalcTool

    case = next(c for c in CASES if c.key == "profit_health_check/four-signals")
    with evalset._fixture_upload(case):
        computed = ProfitCalcTool()._run(upload_id=case.payload["upload_id"])
    assert computed["skus"] == case.reference_output["skus"]
    assert computed["flags"] == case.reference_output["flags"]


def test_custom_case_runs_the_no_tools_dynamic_crew(mocker, stub_llm):
    case = next(c for c in CASES if c.key == "custom:review-triage/review-triage")
    built = {}

    def fake_dynamic_crew(definition, inputs, previous_error="", llm=None):
        built.update(name=definition.name, inputs=inputs)
        return SimpleNamespace(crew=lambda: SimpleNamespace(
            kickoff=lambda: SimpleNamespace(raw=json.dumps(case.reference_output))))

    mocker.patch("src.automation.crews.dynamic_crew.DynamicCrew", side_effect=fake_dynamic_crew)
    _judge(mocker)
    res = evalset.run_case(case, "openai", "gpt-4o-mini", {"scores": {}})
    assert res.status == "pass" and built == {"name": "Review Triage", "inputs": case.payload}


def test_run_live_report_baseline_and_markdown(mocker):
    cases = [c for c in CASES if c.job_type == "hacker_news_digest"]
    scores = iter([90.0, 60.0])

    def fake_run_case(case, provider, model, baseline, tolerance):
        r = evalset.CaseResult(key=case.key, score=next(scores), judge="gemini/gemini-2.5-flash")
        evalset.compare_to_baseline(r, baseline, "openai/gpt-4o-mini", tolerance)
        return r

    mocker.patch.object(evalset, "run_case", side_effect=fake_run_case)
    mocker.patch.object(evalset, "load_baseline", return_value={
        "run_model": "openai/gpt-4o-mini", "recorded_at": "2026-01-01T00:00:00+00:00",
        "scores": {cases[1].key: {"score": 85.0, "judge": "gemini/gemini-2.5-flash"},
                   "web_scraper/kept": {"score": 70.0, "judge": "gemini/gemini-2.5-flash"}},
    })
    report = evalset.run_live("openai", "gpt-4o-mini", only=["hacker_news_digest"])
    assert report["counts"] == {"pass": 1, "fail": 1, "error": 0, "unscored": 0}
    assert report["ok"] is False

    md = evalset.render_markdown(report)
    assert "**FAIL**" in md and "regression" in md and cases[1].key in md

    new = evalset.baseline_from(report, evalset.load_baseline())
    assert new["run_model"] == "openai/gpt-4o-mini"
    assert new["scores"][cases[0].key] == {"score": 90.0, "judge": "gemini/gemini-2.5-flash"}
    assert "web_scraper/kept" in new["scores"]  # untouched cases survive a partial (--only) run


def test_run_live_with_no_matching_case_is_not_ok(mocker):
    mocker.patch.object(evalset, "load_baseline", return_value={"scores": {}})
    assert evalset.run_live("openai", "gpt-4o-mini", only=["nope"])["ok"] is False
