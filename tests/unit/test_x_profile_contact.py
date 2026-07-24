"""Unit tests for the X-profile contact source (no network)."""


# ── contact_harvest: obfuscation + shared validation ────────────────────────

def test_deobfuscate_bracketed_and_word_forms():
    from src.automation.tools.contact_harvest import deobfuscate_emails
    assert "john@gmail.com" in deobfuscate_emails("reach me john [at] gmail [dot] com")
    assert "sales@acme.tw" in deobfuscate_emails("sales (at) acme (dot) tw")
    assert "hi@shop.io" in deobfuscate_emails("hi at shop dot io")


def test_deobfuscate_multi_part_domain():
    from src.automation.tools.contact_harvest import deobfuscate_emails
    got = deobfuscate_emails("contact [at] mail [dot] example [dot] co")
    assert "contact@mail.example.co" in got


def test_harvest_mixes_plain_and_obfuscated_and_filters_junk():
    from src.automation.tools.contact_harvest import harvest_emails_from_text
    text = "plain owner@shop.tw, junk you@example.com, obf info [at] shop [dot] tw"
    got = harvest_emails_from_text(text)
    assert "owner@shop.tw" in got
    assert "info@shop.tw" in got
    assert "you@example.com" not in got          # junk localpart+domain filtered
    assert got[0].startswith("info@")            # role address ranked first


def test_social_platform_detection():
    from src.automation.tools.contact_harvest import social_platform
    assert social_platform("https://x.com/mybiz") == "x"
    assert social_platform("twitter.com/mybiz") == "x"
    assert social_platform("https://www.facebook.com/mybiz") == "facebook"
    assert social_platform("https://acme.com.tw") is None


# ── nitter profile-card parsing ─────────────────────────────────────────────

_NITTER_PROFILE = """
<html><body>
<div class="profile-card">
  <a class="profile-card-fullname">Acme Studio</a>
  <a class="profile-card-username">@acmestudio</a>
  <div class="profile-bio"><p>AI consulting for TW SMEs. 聯絡 info [at] acmestudio [dot] tw</p></div>
  <div class="profile-location"><span class="icon-location"></span> Taipei, Taiwan</div>
  <div class="profile-website"><span class="icon-link"></span> <a href="https://acmestudio.tw">acmestudio.tw</a></div>
</div>
<div class="timeline"><div class="timeline-item">a post owner@should-not-leak.com</div></div>
</body></html>
"""


def test_fetch_x_profile_parses_bio_website_location(mocker):
    from src.automation.tools import x_profile_contact_tool as m
    mocker.patch.object(m, "_get_nitter_instances", return_value=["https://nitter.test"])
    mocker.patch.object(m, "_fetch", return_value=_NITTER_PROFILE)

    r = m.fetch_x_profile_contact("@acmestudio")
    assert r["emails"] == ["info@acmestudio.tw"]     # obfuscated bio email recovered
    assert r["website"] == "acmestudio.tw"
    assert r["location"] == "Taipei, Taiwan"
    # The post feed is outside the profile card — its email must NOT leak in.
    assert "owner@should-not-leak.com" not in r["emails"]


def test_fetch_x_profile_bot_page_then_success(mocker):
    from src.automation.tools import x_profile_contact_tool as m
    mocker.patch.object(m, "_get_nitter_instances",
                        return_value=["https://blocked.test", "https://ok.test"])
    mocker.patch.object(m, "_fetch",
                        side_effect=["<html>captcha required</html>", _NITTER_PROFILE])
    r = m.fetch_x_profile_contact("acmestudio")
    assert r["emails"] == ["info@acmestudio.tw"]
    assert any("bot-detection" in w for w in r["warnings"])


def test_fetch_x_profile_all_fail_returns_empty(mocker):
    from src.automation.tools import x_profile_contact_tool as m
    mocker.patch.object(m, "_get_nitter_instances", return_value=["https://dead.test"])
    mocker.patch.object(m, "_fetch", side_effect=Exception("timeout"))
    r = m.fetch_x_profile_contact("acmestudio")
    assert r["emails"] == [] and r["warnings"]


def test_fetch_x_profile_empty_username():
    from src.automation.tools.x_profile_contact_tool import fetch_x_profile_contact
    r = fetch_x_profile_contact("")
    assert r["emails"] == [] and r["warnings"]


# ── flow routing: social website → X profile extractor + chase-through ──────

def test_flow_routes_x_website_to_profile_tool(mocker):
    from src.automation.flows import email_collect_flow as f

    mocker.patch.object(f, "search_maps", return_value={
        "businesses": [{"name": "Acme", "website": "https://x.com/acmestudio",
                        "category": "AI", "maps_url": "u"}],
        "warnings": [],
    })
    prof = mocker.patch.object(f, "fetch_x_profile_contact", return_value={
        "emails": ["info@acmestudio.tw"], "website": "https://acmestudio.tw",
        "location": "Taipei", "bio": "", "warnings": [],
    })
    ext = mocker.patch.object(f, "extract_emails",
                              return_value={"emails": ["sales@acmestudio.tw"],
                                            "guessed": False})
    mocker.patch.object(f, "verify_email", return_value={
        "confidence": "high", "mx_found": True, "smtp_status": "accepted"})

    flow = f.EmailCollectFlow()
    result = flow.kickoff(inputs={
        "query": "AI agency", "region": "Taipei",
        "include_social": True, "smtp_check": False, "run_id": 0,
    })

    prof.assert_called_once()                    # X profile mined
    ext.assert_called_once_with("https://acmestudio.tw", log=mocker.ANY)  # chase-through
    import json
    data = json.loads(result)
    sources = {lead["source"] for lead in data["leads"]}
    assert sources == {"x", "website"}           # both provenance tags present


def test_flow_skips_x_profile_when_social_disabled(mocker):
    from src.automation.flows import email_collect_flow as f

    mocker.patch.object(f, "search_maps", return_value={
        "businesses": [{"name": "Acme", "website": "https://x.com/acmestudio"}],
        "warnings": [],
    })
    prof = mocker.patch.object(f, "fetch_x_profile_contact")
    mocker.patch.object(f, "extract_emails",
                        return_value={"emails": [], "guessed": False})
    mocker.patch.object(f, "verify_email", return_value={
        "confidence": "low", "mx_found": False, "smtp_status": ""})

    f.EmailCollectFlow().kickoff(inputs={
        "query": "AI agency", "include_social": False, "run_id": 0})
    prof.assert_not_called()                      # default off → no social scraping
