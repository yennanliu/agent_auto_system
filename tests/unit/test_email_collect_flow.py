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


# ── Multi-source discovery (Maps + 公會名錄 + 經濟部商工登記) ────────────────────

def _biz(name, website="", **extra):
    return {"name": name, "website": website, "category": "", "phone": "",
            "address": "", "maps_url": "", **extra}


def _patch_sources(mocker, *, maps=(), association=(), govbiz=()):
    """Stub every discovery source; returns the association mock for assertions."""
    lc = "src.automation.flows.email_collect_flow"
    mocker.patch(f"{lc}.search_maps",
                 return_value={"businesses": list(maps), "warnings": []})
    assoc = mocker.patch(
        f"{lc}.search_association",
        return_value={"businesses": list(association), "warnings": []})
    mocker.patch(f"{lc}.search_companies",
                 return_value={"businesses": list(govbiz), "warnings": []})
    mocker.patch(
        f"{lc}.extract_emails",
        side_effect=lambda site, log=None, render=False: {
            "emails": [f"info@{site.split('//')[-1]}"], "guessed": False},
    )
    mocker.patch(
        f"{lc}.verify_email",
        side_effect=lambda email, smtp_check=True: {
            "email": email, "confidence": "medium",
            "mx_found": True, "smtp_status": "unknown"},
    )
    mocker.patch("src.automation.harness.provider.resolve",
                 side_effect=RuntimeError("no key"))
    return assoc


def _run(**inputs):
    from src.automation.flows.email_collect_flow import EmailCollectFlow
    raw = EmailCollectFlow().kickoff(inputs={"query": "科技", "run_id": 0, **inputs})
    return json.loads(raw.raw if hasattr(raw, "raw") else str(raw))


def test_defaults_to_maps_only(mocker):
    assoc = _patch_sources(mocker, maps=[_biz("A", "https://a.com")],
                           association=[_biz("Z", "https://z.com")])
    d = _run()
    assert d["sources"] == ["maps"]
    assert d["discovered_count"] == 1
    assoc.assert_not_called()


def test_association_source_needs_a_directory(mocker):
    _patch_sources(mocker, association=[_biz("Z", "https://z.com")])
    d = _run(sources=["association"])
    assert d["discovered_count"] == 0
    assert any("no 公會 directory chosen" in w for w in d["warnings"])


def test_association_source_searches_every_named_directory(mocker):
    assoc = _patch_sources(mocker, association=[_biz("Z", "https://z.com")])
    d = _run(sources=["association"], limit=10,
             associations=["tca", "https://guild.org.tw/members.php"])
    assert assoc.call_count == 2
    assert [c.args[0] for c in assoc.call_args_list] == [
        "tca", "https://guild.org.tw/members.php"]
    # Both directories returned the same company — it must collapse to one.
    assert d["discovered_count"] == 1


def test_same_company_from_two_sources_merges_into_one_business(mocker):
    """Maps knows the maps_url, the 公會 row knows the 業務類型 — one lead, both."""
    _patch_sources(
        mocker,
        maps=[_biz("賽博整合行銷有限公司", "https://www.cybermaster.com.tw/",
                   maps_url="https://maps.google/x")],
        association=[{**_biz("賽博整合行銷有限公司（CyberMaster Co., Ltd.）",
                             "https://cybermaster.com.tw"),
                      "category": "資訊服務", "discovery": "association:tca"}],
    )
    d = _run(sources=["maps", "association"], associations=["tca"])
    assert d["discovered_count"] == 1
    assert d["lead_count"] == 1
    assert d["businesses"][0]["category"] == "資訊服務"        # from the directory
    assert d["leads"][0]["maps_url"] == "https://maps.google/x"  # from Maps


def test_registry_rows_carry_tax_id_through_to_the_lead(mocker):
    _patch_sources(mocker, govbiz=[
        {**_biz("未來人工智慧股份有限公司", "https://future-ai.com.tw"),
         "discovery": "govbiz", "tax_id": "00172766", "responsible": "塗陳心瑀",
         "capital": 100000000}])
    d = _run(sources=["govbiz"])
    lead = d["leads"][0]
    assert lead["discovery"] == "govbiz"
    assert lead["tax_id"] == "00172766"
    assert lead["capital"] == 100000000
    assert d["businesses"][0]["tax_id"] == "00172766"


def test_a_failing_source_does_not_sink_the_run(mocker):
    lc = "src.automation.flows.email_collect_flow"
    _patch_sources(mocker, maps=[_biz("A", "https://a.com")])
    mocker.patch(f"{lc}.search_companies", side_effect=RuntimeError("API down"))
    d = _run(sources=["maps", "govbiz"])
    assert d["lead_count"] == 1
    assert any("govbiz" in w and "API down" in w for w in d["warnings"])


def test_limit_is_shared_fairly_across_sources(mocker):
    """Maps alone could fill the quota; the directory must still get a slot."""
    _patch_sources(
        mocker,
        maps=[_biz(f"M{i}", f"https://m{i}.com") for i in range(4)],
        association=[{**_biz(f"G{i}", f"https://g{i}.com"),
                      "discovery": "association:tca"} for i in range(4)],
    )
    d = _run(sources=["maps", "association"], associations=["tca"], limit=4)
    origins = [b["discovery"] for b in d["businesses"]]
    assert origins.count("maps") == 2
    assert origins.count("association:tca") == 2


def test_directory_published_emails_are_used_directly(mocker):
    """A 公會 row that prints the member's address needs no site scrape."""
    lc = "src.automation.flows.email_collect_flow"
    _patch_sources(mocker, association=[
        {**_biz("二號工程有限公司"), "emails": ["sales@no2-eng.com.tw"],
         "source_url": "https://guild.org.tw/member_detail.php?id=4417",
         "discovery": "association:tca"}])
    extract = mocker.patch(f"{lc}.extract_emails")

    d = _run(sources=["association"], associations=["tca"])
    lead = d["leads"][0]
    assert lead["email"] == "sales@no2-eng.com.tw"
    assert lead["source"] == "directory"
    # No company site of its own, but the guild page it came from is traceable —
    # and must not masquerade as the company's website.
    assert lead["website"] == ""
    assert lead["source_url"] == "https://guild.org.tw/member_detail.php?id=4417"
    extract.assert_not_called()  # no website → nothing to scrape


# ── Website resolution + registry enrichment ─────────────────────────────────

def test_missing_websites_are_resolved_via_maps(mocker):
    lc = "src.automation.flows.email_collect_flow"
    _patch_sources(mocker, govbiz=[
        {**_biz("未來人工智慧股份有限公司"), "discovery": "govbiz"}])
    resolve = mocker.patch(f"{lc}.resolve_websites",
                           return_value={"未來人工智慧股份有限公司": "https://future-ai.tw"})
    d = _run(sources=["govbiz"], resolve_missing_websites=True)
    resolve.assert_called_once()
    assert d["with_website"] == 1
    assert d["leads"][0]["website"] == "https://future-ai.tw"


def test_website_resolution_is_off_by_default(mocker):
    lc = "src.automation.flows.email_collect_flow"
    _patch_sources(mocker, govbiz=[{**_biz("X"), "discovery": "govbiz"}])
    resolve = mocker.patch(f"{lc}.resolve_websites", return_value={})
    d = _run(sources=["govbiz"])
    resolve.assert_not_called()
    assert d["with_website"] == 0 and d["lead_count"] == 0


def test_website_resolution_failure_is_nonfatal(mocker):
    lc = "src.automation.flows.email_collect_flow"
    _patch_sources(mocker, maps=[_biz("A", "https://a.com"), _biz("B")])
    mocker.patch(f"{lc}.resolve_websites", side_effect=RuntimeError("no browser"))
    d = _run(resolve_missing_websites=True)
    assert d["lead_count"] == 1


def test_gcis_enrichment_attaches_registry_facts(mocker):
    lc = "src.automation.flows.email_collect_flow"
    _patch_sources(mocker, maps=[_biz("賽博整合行銷有限公司", "https://cyber.com.tw")])
    mocker.patch(f"{lc}.lookup_company", return_value={
        "tax_id": "54301566", "responsible": "張家芬", "capital": 2000000,
        "setup_date": "2013-10-08", "registered_name": "賽博整合行銷有限公司",
        "paid_in_capital": 0,
    })
    d = _run(gcis_enrich=True)
    lead = d["leads"][0]
    assert lead["tax_id"] == "54301566"
    assert lead["setup_date"] == "2013-10-08"
    assert "paid_in_capital" not in lead  # falsy registry fields aren't copied


def test_gcis_enrichment_is_off_by_default(mocker):
    lc = "src.automation.flows.email_collect_flow"
    _patch_sources(mocker, maps=[_biz("A", "https://a.com")])
    lookup = mocker.patch(f"{lc}.lookup_company", return_value={"tax_id": "1"})
    _run()
    lookup.assert_not_called()


def test_gcis_enrichment_failure_is_nonfatal(mocker):
    lc = "src.automation.flows.email_collect_flow"
    _patch_sources(mocker, maps=[_biz("A", "https://a.com")])
    mocker.patch(f"{lc}.lookup_company", side_effect=RuntimeError("API down"))
    d = _run(gcis_enrich=True)
    assert d["lead_count"] == 1
    assert "tax_id" not in d["leads"][0]


# ── Source selection / merge helpers ─────────────────────────────────────────

@pytest.mark.parametrize("given,expected", [
    ([], ["maps"]),
    (["govbiz"], ["govbiz"]),
    (["MAPS", " association "], ["maps", "association"]),
    (["maps", "maps"], ["maps"]),
    (["nonsense"], ["maps"]),
])
def test_source_selection_normalizes_and_falls_back(given, expected):
    from src.automation.flows.email_collect_flow import EmailCollectFlow
    flow = EmailCollectFlow()
    flow.state.sources = given
    assert flow._sources() == expected


def test_business_merge_keys_on_registrable_domain():
    from src.automation.flows.email_collect_flow import _merge_business
    merged = {}
    _merge_business(merged, {"name": "A", "website": "https://www.acme.com.tw/x"})
    _merge_business(merged, {"name": "A Co", "website": "http://shop.acme.com.tw",
                             "phone": "02-1"})
    assert len(merged) == 1
    assert merged["acme.com.tw"]["phone"] == "02-1"      # blank filled
    assert merged["acme.com.tw"]["name"] == "A"          # existing kept


def test_business_merge_falls_back_to_a_normalized_name():
    from src.automation.flows.email_collect_flow import _merge_business
    merged = {}
    _merge_business(merged, {"name": "台灣 大 有限公司"})
    _merge_business(merged, {"name": "臺灣大有限公司（Taiwan Da Co., Ltd.）",
                             "address": "台北市"})
    assert len(merged) == 1
    assert next(iter(merged.values()))["address"] == "台北市"


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
