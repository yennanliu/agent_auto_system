import pytest

# ── validator ─────────────────────────────────────────────────────────────────

def test_validate_google_form_success():
    from src.automation.harness.validator import validate
    vr = validate("google_form_fill", {"submitted": True, "message": "Form submitted successfully today"})
    assert vr.valid


def test_validate_google_form_not_submitted():
    from src.automation.harness.validator import validate
    vr = validate("google_form_fill", {"submitted": False, "message": "Could not submit the form here"})
    assert not vr.valid
    assert "form not submitted" in vr.reason


def test_validate_result_with_error_key():
    from src.automation.harness.validator import validate
    vr = validate("hacker_news_digest", {"error": "something went wrong unexpectedly"})
    assert not vr.valid
    assert "error" in vr.reason


def test_validate_content_too_short():
    from src.automation.harness.validator import validate
    # "True" is 4 chars — below the 20-char threshold
    vr = validate("google_form_fill", {"submitted": True})
    assert not vr.valid
    assert "too short" in vr.reason


def test_validate_unknown_job_type_passes_if_content_ok():
    from src.automation.harness.validator import validate
    vr = validate("unknown_type", {"data": "This is a long enough result content for the check"})
    assert vr.valid


def test_validate_non_dict_result():
    from src.automation.harness.validator import validate
    vr = validate("google_form_fill", "not a dict")
    assert not vr.valid
    assert "not a dict" in vr.reason


def test_validate_hn_digest_success():
    from src.automation.harness.validator import validate
    vr = validate("hacker_news_digest", {
        "stories": ["Top AI news today", "Rust release"],
        "digest": "Here are the highlights from today",
    })
    assert vr.valid


def test_validate_hn_digest_no_stories():
    from src.automation.harness.validator import validate
    vr = validate("hacker_news_digest", {"message": "No results available at this time"})
    assert not vr.valid
    assert "no stories" in vr.reason


def test_validate_x_scraper_success():
    from src.automation.harness.validator import validate
    vr = validate("x_scraper", {"posts": [{"text": "Hello from test account today!"}]})
    assert vr.valid


def test_validate_email_sender_success():
    from src.automation.harness.validator import validate
    vr = validate("email_sender", {"sent": True, "message": "Email delivered to recipient successfully"})
    assert vr.valid


def test_validate_email_sender_not_sent():
    from src.automation.harness.validator import validate
    vr = validate("email_sender", {"sent": False, "reason": "SMTP authentication error occurred"})
    assert not vr.valid
    assert "email not sent" in vr.reason


# ── costs ─────────────────────────────────────────────────────────────────────

def test_estimate_cost_input_tokens():
    from src.automation.harness.costs import estimate_cost
    cost = estimate_cost("gpt-4o-mini", 1_000_000, 0)
    assert abs(cost - 0.15) < 1e-6


def test_estimate_cost_output_tokens():
    from src.automation.harness.costs import estimate_cost
    cost = estimate_cost("gpt-4o-mini", 0, 1_000_000)
    assert abs(cost - 0.60) < 1e-6


def test_estimate_cost_strips_gemini_prefix():
    from src.automation.harness.costs import estimate_cost
    cost_prefixed = estimate_cost("gemini/gemini-2.0-flash", 1_000_000, 0)
    cost_bare = estimate_cost("gemini-2.0-flash", 1_000_000, 0)
    assert cost_prefixed == cost_bare
    assert abs(cost_prefixed - 0.10) < 1e-6


def test_estimate_cost_unknown_model_uses_fallback():
    from src.automation.harness.costs import estimate_cost
    cost = estimate_cost("totally-unknown-model-xyz", 1_000_000, 0)
    assert cost == 1.0  # fallback input rate is 1.0 per 1M tokens


def test_estimate_cost_anthropic_model():
    from src.automation.harness.costs import estimate_cost
    cost = estimate_cost("claude-sonnet-4-6", 1_000_000, 0)
    assert abs(cost - 3.0) < 1e-6


# ── provider ─────────────────────────────────────────────────────────────────

def test_normalize_defaults_to_openai():
    from src.automation.harness.provider import normalize
    provider, model = normalize(None, None)
    assert provider == "openai"
    assert model == "gpt-6-luna"


def test_normalize_empty_string_defaults_to_openai():
    from src.automation.harness.provider import normalize
    provider, model = normalize("", "")
    assert provider == "openai"
    assert model == "gpt-6-luna"


def test_normalize_explicit_model():
    from src.automation.harness.provider import normalize
    provider, model = normalize("anthropic", "claude-sonnet-4-6")
    assert provider == "anthropic"
    assert model == "claude-sonnet-4-6"


def test_normalize_default_sentinel_picks_catalog_default():
    from src.automation.harness.provider import normalize
    provider, model = normalize("anthropic", "default")
    assert provider == "anthropic"
    assert model == "claude-haiku-5-5"


def test_normalize_unknown_provider_falls_back_to_openai_default_model():
    from src.automation.harness.provider import normalize
    provider, model = normalize("totally-unknown", None)
    assert provider == "totally-unknown"
    assert model == "gpt-6-luna"


def test_resolve_raises_when_api_key_missing(monkeypatch):
    # resolve() reads the key solely through settings_store.get_llm_key (DB →
    # env). Stub it to None to simulate "no key anywhere" deterministically —
    # deleting the env var is unreliable since crewai/database re-run load_dotenv.
    from src import settings_store
    from src.automation.harness.provider import resolve

    monkeypatch.setattr(settings_store, "get_llm_key", lambda provider, env_name: None)
    with pytest.raises(EnvironmentError, match="OPENAI_API_KEY"):
        resolve("openai", "gpt-4o-mini")


# ── pipeline validator ─────────────────────────────────────────────────────────

def test_validate_pipeline_success():
    from src.automation.harness.validator import validate
    r = {"steps": [{"step": 1, "job_type": "x_scraper", "result": {"summary": "good content here"}}], "final_result": {"summary": "good content here"}}
    vr = validate("pipeline", r)
    assert vr.valid

def test_validate_pipeline_no_steps():
    from src.automation.harness.validator import validate
    vr = validate("pipeline", {"final_result": {}})
    assert not vr.valid
    assert "pipeline" in vr.reason

def test_validate_pipeline_empty_steps():
    from src.automation.harness.validator import validate
    vr = validate("pipeline", {"steps": [], "final_result": {}})
    assert not vr.valid

# ── google_sheet_reader validator ─────────────────────────────────────────────

def test_validate_google_sheet_success():
    from src.automation.harness.validator import validate
    r = {"columns": ["id", "name"], "row_count": 3, "summary": "A product price list", "data": []}
    vr = validate("google_sheet_reader", r)
    assert vr.valid

def test_validate_google_sheet_no_data():
    from src.automation.harness.validator import validate
    vr = validate("google_sheet_reader", {"error": "could not read sheet"})
    assert not vr.valid


# ── newer models: temperature, Anthropic routing, forced tool choice ──────────

def _fake_key(monkeypatch):
    from src import settings_store
    monkeypatch.setattr(settings_store, "get_llm_key", lambda provider, env_name: "test-key")


@pytest.mark.parametrize("model,ok", [
    ("gpt-4o-mini", True),
    ("gpt-4o", True),
    ("gpt-6-sol", False),
    ("gpt-6-luna", False),
    ("claude-haiku-4-5-20251001", True),
    ("claude-sonnet-4-6", True),
    ("claude-haiku-5-5", False),
    ("claude-sonnet-5-5", False),
    ("claude-opus-5-5", False),
    ("gemini/gemini-4-argon", True),
    ("gemini/gemini-3.8-flash", True),
])
def test_accepts_temperature(model, ok):
    from src.automation.harness.provider import accepts_temperature
    assert accepts_temperature(model) is ok


@pytest.mark.parametrize("provider,model,temp", [
    ("openai", "gpt-4o-mini", 0.3),
    ("openai", "gpt-6-sol", None),
    ("gemini", "gemini/gemini-4-argon", 0.3),
])
def test_resolve_drops_temperature_only_where_rejected(monkeypatch, provider, model, temp):
    from src.automation.harness.provider import resolve
    _fake_key(monkeypatch)
    llm, _, eff = resolve(provider, model, temperature=0.3)
    assert eff == model
    assert llm.temperature == temp


@pytest.mark.parametrize("model,temp", [
    ("claude-sonnet-4-6", 0.3),
    ("claude-haiku-5-5", None),
    ("claude-sonnet-5-5", None),
    ("claude-opus-5-5", None),
])
def test_resolve_routes_claude_to_native_anthropic_client(monkeypatch, model, temp):
    # Bare claude-* ids crewai doesn't know would otherwise fall through to the
    # OpenAI client and send the Anthropic key to api.openai.com.
    from crewai.llms.providers.anthropic.completion import AnthropicCompletion

    from src.automation.harness.provider import resolve
    _fake_key(monkeypatch)
    llm, provider, eff = resolve("anthropic", model, temperature=0.3)
    assert isinstance(llm, AnthropicCompletion)
    assert (provider, eff, llm.model) == ("anthropic", model, model)
    assert llm.temperature == temp


_ONE_TOOL = [{"name": "fetch", "description": "Fetch stories", "input_schema": {"type": "object", "properties": {}}}]


@pytest.mark.parametrize("model,choice", [
    ("claude-sonnet-4-6", "tool"),   # older models keep crewai's forced choice
    ("claude-haiku-5-5", "tool"),
    ("claude-sonnet-5-5", "auto"),   # 400 on forced tool_choice
    ("claude-opus-5-5", "auto"),
])
def test_single_tool_forced_choice_relaxed_only_where_rejected(monkeypatch, model, choice):
    from src.automation.harness.provider import resolve
    _fake_key(monkeypatch)
    llm, _, _ = resolve("anthropic", model)
    params = llm._prepare_completion_params(
        [{"role": "user", "content": "hi"}], tools=_ONE_TOOL, available_functions={"fetch": lambda: None},
    )
    assert params["tool_choice"]["type"] == choice
    assert "temperature" not in params or model == "claude-sonnet-4-6"


def test_every_catalog_model_has_pricing():
    from src.automation.harness.costs import _PRICING
    from src.automation.harness.provider import PROVIDER_MODELS
    missing = [m for ms in PROVIDER_MODELS.values() for m in ms if m.split("/")[-1] not in _PRICING]
    assert not missing, f"add pricing for {missing}"


def test_ui_model_dropdown_matches_backend_catalog():
    import re
    from pathlib import Path

    from src.automation.harness.provider import PROVIDER_MODELS
    js = (Path(__file__).resolve().parents[2] / "ui" / "app.js").read_text()
    block = js[js.index("const LLM_MODELS = {"):]
    block = block[:block.index("};")]
    for provider, models in PROVIDER_MODELS.items():
        section = re.search(rf"{provider}:\s*\[(.*?)\n  \]", block, re.S)
        assert section, provider
        ui_models = re.findall(r"\['([^']+)'", section.group(1))
        assert ui_models == models, provider


def test_provider_defaults_are_registered_and_priced():
    from src.automation.harness.costs import _PRICING
    from src.automation.harness.provider import _CATALOG
    for provider, cfg in _CATALOG.items():
        assert cfg["default"] in cfg["models"], provider
        assert cfg["default"].split("/")[-1] in _PRICING, provider


@pytest.mark.parametrize("provider,old_default", [
    ("openai", "gpt-4o-mini"),
    ("anthropic", "claude-haiku-4-5-20251001"),
    ("gemini", "gemini/gemini-2.5-flash"),
])
def test_provider_default_never_costs_more_than_the_previous_default(provider, old_default):
    # A default applies to every job that names no model, so bumping it must not
    # raise anyone's bill; the previous default stays selectable and priced.
    from src.automation.harness.costs import _PRICING
    from src.automation.harness.provider import _CATALOG
    cfg = _CATALOG[provider]
    assert old_default in cfg["models"]
    new_in, new_out = _PRICING[cfg["default"].split("/")[-1]]
    old_in, old_out = _PRICING[old_default.split("/")[-1]]
    assert new_in <= old_in and new_out <= old_out


@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini"])
def test_default_model_run_is_judged_by_another_model_first(provider, mocker, monkeypatch):
    # Eval-judge independence must survive a default change: for a run on the
    # provider default, the run's own model is only ever the last-resort judge.
    from src.automation.harness.evaluator import _judge_candidates
    from src.automation.harness.provider import PROVIDER_MODELS, fallback_sequence, normalize
    mocker.patch("src.settings_store.get_eval_judge", return_value=(None, None))
    monkeypatch.delenv("EVAL_JUDGE_MODEL", raising=False)
    run = normalize(provider, None)
    cands = _judge_candidates(*run, PROVIDER_MODELS)
    assert cands[0] != run
    assert cands.index(run) == len(cands) - 1
    assert fallback_sequence(provider, None)[0] == run[1]


@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini"])
def test_judge_set_to_provider_default_does_not_self_grade_a_default_run(provider, mocker):
    # An admin judge of (provider, None) means "that provider's default model". When
    # the run used that same model it must not lead the candidates under its alias.
    from src.automation.harness.evaluator import _judge_candidates
    from src.automation.harness.provider import PROVIDER_MODELS, normalize
    mocker.patch("src.settings_store.get_eval_judge", return_value=(provider, None))
    run = normalize(provider, None)
    cands = _judge_candidates(*run, PROVIDER_MODELS)
    assert normalize(*cands[0]) != run
    assert [normalize(*c) for c in cands].count(run) == 1
    assert normalize(*cands[-1]) == run


def test_ui_preselected_models_match_backend_defaults():
    import re
    from pathlib import Path

    from src.automation.harness.provider import _CATALOG
    js = (Path(__file__).resolve().parents[2] / "ui" / "app.js").read_text()
    block = js[js.index("const LLM_DEFAULTS = {"):]
    block = block[:block.index("};")]
    ui_defaults = dict(re.findall(r"(\w+):\s*'([^']+)'", block))
    assert ui_defaults == {p: cfg["default"] for p, cfg in _CATALOG.items()}
    # The static first-paint <select> (OpenAI) preselects the same model.
    html = (Path(__file__).resolve().parents[2] / "ui" / "index.html").read_text()
    assert f'<option value="{_CATALOG["openai"]["default"]}" selected>' in html


@pytest.mark.parametrize("model,effort", [("gpt-6-luna", "none"), ("gpt-6-sol", "none"), ("gpt-4o-mini", None)])
def test_gpt6_chat_request_turns_reasoning_off_and_omits_temperature(monkeypatch, model, effort):
    # GPT-6 400s on function tools over chat completions unless reasoning_effort="none".
    from src.automation.harness.provider import resolve
    _fake_key(monkeypatch)
    llm, _, _ = resolve("openai", model, temperature=0.3)
    params = llm._prepare_completion_params([{"role": "user", "content": "hi"}])
    assert params.get("reasoning_effort") == effort
    assert ("temperature" in params) is (effort is None)


def test_lite_models_priced_below_their_full_sibling():
    from src.automation.harness.costs import _PRICING
    for lite, rates in _PRICING.items():
        full = lite.removesuffix("-lite")
        if lite.endswith("-lite") and full in _PRICING:
            assert rates < _PRICING[full], f"{lite} {rates} should be cheaper than {full} {_PRICING[full]}"
