from src.automation.harness.evaluator import EvalResult, _heuristic, _parse_json, evaluate


class _FakeLLM:
    def __init__(self, response):
        self._response = response

    def call(self, prompt):
        return self._response


# ── heuristic ────────────────────────────────────────────────────────────────

def test_heuristic_scores_populated_dict():
    ev = _heuristic("web_scraper", {"title": "T", "summary": "S" * 200, "links": ["a", "b"]})
    assert ev.method == "heuristic"
    assert 0 < ev.score <= 100
    assert 0 <= ev.confidence <= 1


def test_heuristic_error_result_is_zero():
    ev = _heuristic("web_scraper", {"error": "boom"})
    assert ev.score == 0.0
    assert ev.confidence == 1.0


def test_heuristic_non_dict_is_zero():
    ev = _heuristic("web_scraper", "not a dict")
    assert ev.score == 0.0


# ── _parse_json ──────────────────────────────────────────────────────────────

def test_parse_json_plain():
    assert _parse_json('{"score": 80}') == {"score": 80}


def test_parse_json_strips_fence():
    assert _parse_json('```json\n{"score": 80}\n```') == {"score": 80}


def test_parse_json_garbage_returns_none():
    assert _parse_json("not json at all") is None


def test_parse_json_extracts_object_from_prose():
    assert _parse_json('Sure! Here it is: {"score": 75} — hope that helps') == {"score": 75}


# ── evaluate ───────────────────────────────────────────────────────────────--

def test_evaluate_error_result_short_circuits_without_llm(mocker):
    resolve = mocker.patch("src.automation.harness.provider.resolve")
    ev = evaluate("web_scraper", {"error": "fetch failed"})
    assert ev.method == "heuristic"
    assert ev.score == 0.0
    resolve.assert_not_called()


def test_evaluate_llm_path_parses_score(mocker):
    fake = _FakeLLM('{"score": 87, "confidence": 0.92, "notes": "complete"}')
    # Judge resolves to an independent model (different from the run's default).
    mocker.patch("src.automation.harness.provider.has_api_key", return_value=True)
    mocker.patch("src.automation.harness.provider.resolve",
                 return_value=(fake, "anthropic", "claude-sonnet-4-6"))
    ev = evaluate("web_scraper", {"title": "T", "summary": "S"}, provider="openai", model="gpt-4o-mini")
    assert ev.method == "llm"
    assert ev.score == 87.0
    assert ev.confidence == 0.92
    assert ev.notes == "complete"
    assert ev.judge_model == "anthropic/claude-sonnet-4-6"


def test_evaluate_uses_independent_judge_not_run_model(mocker):
    """The judge candidate list must lead with a model != the run's model."""
    fake = _FakeLLM('{"score": 80, "confidence": 0.9, "notes": "ok"}')
    calls: list[tuple] = []

    def _resolve(p, m, temperature=0.0):
        calls.append((p, m))
        return fake, p, m

    mocker.patch("src.automation.harness.provider.has_api_key", return_value=True)
    mocker.patch("src.automation.harness.provider.resolve", side_effect=_resolve)
    evaluate("web_scraper", {"title": "T", "summary": "S"}, provider="openai", model="gpt-4o-mini")
    assert calls[0] != ("openai", "gpt-4o-mini")  # never self-grades first


def test_admin_setting_overrides_judge(mocker):
    """The admin-configured judge (settings_store) takes precedence over the default."""
    mocker.patch("src.settings_store.get_eval_judge", return_value=("gemini", "gemini/gemini-2.5-pro"))
    mocker.patch("src.automation.harness.provider.has_api_key", return_value=True)
    calls: list[tuple] = []

    def _resolve(p, m, temperature=0.0):
        calls.append((p, m))
        return _FakeLLM('{"score": 80, "confidence": 0.9, "notes": "ok"}'), p, m

    mocker.patch("src.automation.harness.provider.resolve", side_effect=_resolve)
    ev = evaluate("web_scraper", {"title": "T"}, provider="openai", model="gpt-4o-mini")
    assert calls[0] == ("gemini", "gemini/gemini-2.5-pro")
    assert ev.judge_model == "gemini/gemini-2.5-pro"


def test_judge_candidates_skip_self_when_preferred_equals_run(mocker):
    """If the preferred judge IS the run model, an independent sibling leads and the
    run model drops to last resort (avoids needless self-grading)."""
    from src.automation.harness.evaluator import _judge_candidates
    mocker.patch("src.automation.harness.evaluator._preferred_judge",
                 return_value=("gemini", "gemini/gemini-2.5-flash"))
    pm = {"gemini": ["gemini/gemini-2.5-flash", "gemini/gemini-2.5-pro"]}
    cands = _judge_candidates("gemini", "gemini/gemini-2.5-flash", pm)
    assert cands[0] == ("gemini", "gemini/gemini-2.5-pro")       # independent sibling first
    assert cands[-1] == ("gemini", "gemini/gemini-2.5-flash")    # run model only as last resort


def test_evaluate_self_grade_discounts_confidence(mocker):
    """When only the run's own model is available, confidence is halved + flagged."""
    fake = _FakeLLM('{"score": 90, "confidence": 0.8, "notes": "great"}')

    def _resolve(p, m, temperature=0.0):
        # Every independent candidate is unavailable; only the run model resolves.
        if (p, m) == ("openai", "gpt-4o-mini"):
            return fake, p, m
        raise OSError("no api key")

    mocker.patch("src.automation.harness.provider.has_api_key", return_value=True)
    mocker.patch("src.automation.harness.provider.resolve", side_effect=_resolve)
    ev = evaluate("web_scraper", {"title": "T"}, provider="openai", model="gpt-4o-mini")
    assert ev.method == "llm"
    assert ev.confidence == 0.4  # 0.8 * 0.5
    assert "self-graded" in ev.notes


def test_evaluate_llm_path_clamps_out_of_range(mocker):
    fake = _FakeLLM('{"score": 150, "confidence": 3}')
    mocker.patch("src.automation.harness.provider.has_api_key", return_value=True)
    mocker.patch("src.automation.harness.provider.resolve",
                 return_value=(fake, "anthropic", "claude-sonnet-4-6"))
    ev = evaluate("web_scraper", {"title": "T"}, provider="openai", model="gpt-4o-mini")
    assert ev.score == 100.0
    assert ev.confidence == 1.0


def test_evaluate_falls_back_to_heuristic_on_resolve_error(mocker):
    mocker.patch("src.automation.harness.provider.has_api_key", return_value=True)
    mocker.patch("src.automation.harness.provider.resolve", side_effect=OSError("no api key"))
    ev = evaluate("web_scraper", {"title": "T", "summary": "S" * 100})
    assert ev.method == "heuristic"
    assert isinstance(ev, EvalResult)


def test_evaluate_falls_back_when_judge_output_unparseable(mocker):
    fake = _FakeLLM("I think it's pretty good honestly")
    mocker.patch("src.automation.harness.provider.has_api_key", return_value=True)
    mocker.patch("src.automation.harness.provider.resolve", return_value=(fake, "openai", "gpt-4o-mini"))
    ev = evaluate("web_scraper", {"title": "T", "summary": "S"})
    assert ev.method == "heuristic"


def test_result_for_judge_labels_harness_truncation():
    from src.automation.harness.evaluator import _MAX_RESULT_CHARS, _result_for_judge

    assert _result_for_judge({"a": 1}) == '{"a": 1}'
    long = _result_for_judge({"x": "y" * (_MAX_RESULT_CHARS * 2)})
    assert "truncated by the evaluation harness" in long
    assert len(long) < _MAX_RESULT_CHARS + 300
