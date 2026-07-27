"""Unit tests for EmailCollectFlow — funnel wiring, dedupe, qualifier merge."""
import json
from unittest.mock import MagicMock

import pytest

# ── Validation ────────────────────────────────────────────────────────────────

def test_raises_on_missing_query():
    from src.automation.flows.email_collect_flow import EmailCollectFlow
    with pytest.raises(Exception):
        EmailCollectFlow().kickoff(inputs={"query": ""})


def test_state_defaults():
    from src.automation.flows.email_collect_flow import EmailCollectState
    s = EmailCollectState(query="x")
    assert s.limit == 15 and s.smtp_check is True and s.region == ""


# ── Funnel orchestration ────────────────────────────────────────────────────────

def _patch_funnel(mocker, businesses, emails_by_site, verify_conf="medium"):
    lc = "src.automation.flows.email_collect_flow"
    mocker.patch(f"{lc}.search_maps", return_value={"businesses": businesses, "warnings": []})
    mocker.patch(
        f"{lc}.extract_emails",
        side_effect=lambda site, log=None, render=False: {"emails": emails_by_site.get(site, []), "guessed": False},
    )
    mocker.patch(
        f"{lc}.verify_email",
        side_effect=lambda email, smtp_check=True: {
            "email": email, "confidence": verify_conf,
            "mx_found": True, "smtp_status": "unknown",
        },
    )


def test_funnel_collects_and_dedupes(mocker):
    from src.automation.flows.email_collect_flow import EmailCollectFlow
    businesses = [
        {"name": "A", "website": "https://a.com", "category": "cafe", "phone": "", "address": "", "maps_url": ""},
        {"name": "B", "website": "https://b.com", "category": "bar",  "phone": "", "address": "", "maps_url": ""},
        {"name": "C", "website": "", "category": "shop", "phone": "", "address": "", "maps_url": ""},  # no site
    ]
    emails = {
        "https://a.com": ["info@a.com", "info@a.com"],   # dup within site
        "https://b.com": ["info@a.com", "hi@b.com"],     # info@a.com dup across sites
    }
    _patch_funnel(mocker, businesses, emails)
    # No LLM — qualifier is best-effort and should be skipped gracefully.
    mocker.patch("src.automation.harness.provider.resolve", side_effect=RuntimeError("no key"))

    raw = EmailCollectFlow().kickoff(inputs={"query": "cafe", "region": "TW", "run_id": 0})
    d = json.loads(raw.raw if hasattr(raw, "raw") else str(raw))

    assert d["discovered_count"] == 3
    assert d["with_website"] == 2
    # info@a.com, hi@b.com — the cross-site + in-site dups collapse to 2 leads.
    assert d["lead_count"] == 2
    assert {x["email"] for x in d["leads"]} == {"info@a.com", "hi@b.com"}


def test_invalid_emails_dropped(mocker):
    from src.automation.flows.email_collect_flow import EmailCollectFlow
    businesses = [{"name": "A", "website": "https://a.com", "category": "", "phone": "", "address": "", "maps_url": ""}]
    _patch_funnel(mocker, businesses, {"https://a.com": ["bad@a.com"]}, verify_conf="invalid")
    mocker.patch("src.automation.harness.provider.resolve", side_effect=RuntimeError("no key"))

    raw = EmailCollectFlow().kickoff(inputs={"query": "x", "run_id": 0})
    d = json.loads(raw.raw if hasattr(raw, "raw") else str(raw))
    assert d["lead_count"] == 0


def test_qualifier_merges_hooks(mocker):
    from src.automation.flows.email_collect_flow import EmailCollectFlow
    businesses = [{"name": "A", "website": "https://a.com", "category": "cafe", "phone": "", "address": "", "maps_url": ""}]
    _patch_funnel(mocker, businesses, {"https://a.com": ["info@a.com"]})
    mocker.patch("src.automation.harness.provider.resolve", return_value=(None, "openai", "gpt-4o-mini"))

    mock_result = MagicMock()
    mock_result.raw = '[{"i": 0, "icp_fit": 4, "reason": "fits", "hook": "great hook"}]'
    mock_crew = MagicMock()
    mock_crew.crew.return_value.kickoff.return_value = mock_result
    mocker.patch("src.automation.flows.email_collect_flow.EmailCollectCrew", return_value=mock_crew)

    raw = EmailCollectFlow().kickoff(inputs={"query": "cafe", "run_id": 0})
    d = json.loads(raw.raw if hasattr(raw, "raw") else str(raw))
    assert d["leads"][0]["icp_fit"] == 4
    assert d["leads"][0]["hook"] == "great hook"


def test_qualifier_failure_is_nonfatal(mocker):
    from src.automation.flows.email_collect_flow import EmailCollectFlow
    businesses = [{"name": "A", "website": "https://a.com", "category": "", "phone": "", "address": "", "maps_url": ""}]
    _patch_funnel(mocker, businesses, {"https://a.com": ["info@a.com"]})
    mocker.patch("src.automation.harness.provider.resolve", return_value=(None, "openai", "gpt-4o-mini"))
    mock_crew = MagicMock()
    mock_crew.crew.return_value.kickoff.side_effect = RuntimeError("LLM down")
    mocker.patch("src.automation.flows.email_collect_flow.EmailCollectCrew", return_value=mock_crew)

    raw = EmailCollectFlow().kickoff(inputs={"query": "x", "run_id": 0})
    d = json.loads(raw.raw if hasattr(raw, "raw") else str(raw))
    assert d["lead_count"] == 1  # lead survives; just no hook
    assert "hook" not in d["leads"][0]


# ── Qualifier parsing helper ────────────────────────────────────────────────────

def test_parse_qualifications_strips_fences():
    from src.automation.flows.email_collect_flow import _parse_qualifications
    fenced = '```json\n[{"i":0,"icp_fit":5,"hook":"h"}]\n```'
    out = _parse_qualifications(fenced)
    assert out == [{"i": 0, "icp_fit": 5, "hook": "h"}]


def test_parse_qualifications_handles_prose_wrap():
    from src.automation.flows.email_collect_flow import _parse_qualifications
    out = _parse_qualifications('Here you go: [{"i":1,"icp_fit":3}] hope that helps')
    assert out == [{"i": 1, "icp_fit": 3}]


def test_parse_qualifications_bad_input():
    from src.automation.flows.email_collect_flow import _parse_qualifications
    assert _parse_qualifications("not json at all") == []
    assert _parse_qualifications(None) == []
