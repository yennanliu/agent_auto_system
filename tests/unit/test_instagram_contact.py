"""Unit tests for the Instagram profile contact source (no network)."""


# ── handle / URL parsing ────────────────────────────────────────────────────

def test_extract_handle_from_url_or_bare():
    """The funnel passes a full profile URL, not a bare handle."""
    from src.automation.tools.instagram_contact_tool import _extract_handle
    assert _extract_handle("https://www.instagram.com/acmebiz/") == "acmebiz"
    assert _extract_handle("https://instagram.com/acmebiz?hl=en") == "acmebiz"
    assert _extract_handle("instagram.com/acmebiz/") == "acmebiz"
    assert _extract_handle("@acmebiz") == "acmebiz"
    assert _extract_handle("acmebiz") == "acmebiz"
    # Instagram handles may contain dots — a bare dotted handle is NOT a URL.
    assert _extract_handle("acme.studio") == "acme.studio"


def test_social_platform_detects_instagram():
    from src.automation.tools.contact_harvest import social_platform
    assert social_platform("https://www.instagram.com/mybiz") == "instagram"
    assert social_platform("instagr.am/mybiz") == "instagram"
    assert social_platform("https://acme.com.tw") is None


def test_unwrap_ig_link_decodes_wrapper():
    from src.automation.tools.instagram_contact_tool import _unwrap_ig_link
    wrapped = "https://l.instagram.com/?u=http%3A%2F%2Facme.com.tw%2F&e=AT1"
    assert _unwrap_ig_link(wrapped) == "http://acme.com.tw/"
    # A bare, unwrapped URL passes through untouched.
    assert _unwrap_ig_link("https://acme.com.tw") == "https://acme.com.tw"
    assert _unwrap_ig_link("") == ""


# ── inline-JSON profile parsing ─────────────────────────────────────────────

# Mirrors the logged-out profile HTML: bio + external link embedded as inline
# JSON, plus a post caption (with its own email) that must NOT be harvested.
_PROFILE_HTML = """
<html><head>
<meta property="og:description" content="1,234 Followers, 56 Following">
</head><body>
<script type="text/javascript">
{"data":{"user":{"biography":"AI studio for TW SMEs \\u2014 \\u806f\\u7d61 info [at] acmestudio [dot] tw",
"external_url":"https://l.instagram.com/?u=https%3A%2F%2Facmestudio.tw%2F&e=AT",
"business_email":"sales@acmestudio.tw","full_name":"Acme Studio"}}}
</script>
</body></html>
"""


def test_parse_profile_html_bio_email_website_and_explicit_email():
    from src.automation.tools.instagram_contact_tool import _parse_profile_html
    r = _parse_profile_html(_PROFILE_HTML)
    # Obfuscated bio email recovered AND the explicit business_email field.
    assert "info@acmestudio.tw" in r["emails"]
    assert "sales@acmestudio.tw" in r["emails"]
    # external_url unwrapped from the l.instagram.com redirect → chase-through.
    assert r["website"] == "https://acmestudio.tw/"
    assert "AI studio" in r["bio"]


def test_parse_profile_html_drops_social_external_url():
    from src.automation.tools.instagram_contact_tool import _parse_profile_html
    html = ('{"biography":"hi","external_url":'
            '"https://l.instagram.com/?u=https%3A%2F%2Flinktr.ee%2Facme&e=1"}')
    r = _parse_profile_html(html)
    assert r["website"] == ""     # a social/linktree link is not a real site


def test_meta_description_bio_fallback_when_no_biography_json():
    """With no `biography` JSON, the bio (and its email) come from the meta tag."""
    from src.automation.tools.instagram_contact_tool import _parse_profile_html
    html = ('<meta property="og:description" '
            'content="Acme Studio — bookings hello@acme.tw">')
    r = _parse_profile_html(html)
    assert r["bio"].startswith("Acme Studio")
    assert "hello@acme.tw" in r["emails"]   # harvested from the meta fallback


def test_meta_description_direct():
    from src.automation.tools.instagram_contact_tool import _meta_description
    assert _meta_description('<meta name="description" content="hi there">') == "hi there"
    assert _meta_description(
        '<meta property="og:description" content="og bio">') == "og bio"
    assert _meta_description("<html>no meta tag here</html>") == ""


def test_website_from_hrefs_decodes_and_skips_social():
    """The Playwright header links the external site via an l.instagram wrapper."""
    from src.automation.tools.instagram_contact_tool import _website_from_hrefs
    hrefs = [
        "https://l.instagram.com/?u=https%3A%2F%2Finstagram.com%2Fother&e=1",  # social → skip
        "https://l.instagram.com/?u=https%3A%2F%2Facme.com.tw%2F&e=2",
    ]
    assert _website_from_hrefs(hrefs) == "https://acme.com.tw/"
    assert _website_from_hrefs([]) == ""
    assert _website_from_hrefs(["https://example.com"]) == ""  # not a wrapper href


# ── fetch_instagram_contact orchestration (network mocked) ──────────────────

def test_fetch_instagram_static_success(mocker):
    from src.automation.tools import instagram_contact_tool as m
    mocker.patch.object(m, "_fetch", return_value=_PROFILE_HTML)
    r = m.fetch_instagram_contact("https://www.instagram.com/acmestudio/")
    assert r["username"] == "acmestudio"
    assert r["source"] == "instagram.com"
    assert "info@acmestudio.tw" in r["emails"]
    assert r["website"] == "https://acmestudio.tw/"


def test_fetch_instagram_falls_back_to_playwright(mocker):
    from src.automation.tools import instagram_contact_tool as m
    # Static fetch is login-walled / thin → render fallback carries the goods.
    mocker.patch.object(m, "_fetch", side_effect=Exception("timeout"))
    mocker.patch.object(m, "_scrape_profile_with_playwright", return_value={
        "emails": ["hi@acme.io"], "website": "https://acme.io",
        "bio": "reach me at hi@acme.io"})
    r = m.fetch_instagram_contact("acme")
    assert r["emails"] == ["hi@acme.io"]
    assert r["source"] == "instagram.com (playwright)"


def test_fetch_instagram_all_fail_returns_empty(mocker):
    from src.automation.tools import instagram_contact_tool as m
    mocker.patch.object(m, "_fetch", side_effect=Exception("timeout"))
    mocker.patch.object(m, "_scrape_profile_with_playwright",
                        return_value={"emails": [], "website": "", "bio": ""})
    r = m.fetch_instagram_contact("acme")
    assert r["emails"] == [] and r["warnings"]


def test_fetch_instagram_empty_username():
    from src.automation.tools.instagram_contact_tool import fetch_instagram_contact
    r = fetch_instagram_contact("")
    assert r["emails"] == [] and r["warnings"]


# ── flow routing: instagram website → IG extractor + chase-through ──────────

def test_flow_routes_instagram_website_to_profile_tool(mocker):
    from src.automation.flows import email_collect_flow as f

    mocker.patch.object(f, "search_maps", return_value={
        "businesses": [{"name": "Acme", "website": "https://www.instagram.com/acmestudio",
                        "category": "AI", "maps_url": "u"}],
        "warnings": [],
    })
    prof = mocker.patch.object(f, "fetch_instagram_contact", return_value={
        "emails": ["info@acmestudio.tw"], "website": "https://acmestudio.tw",
        "bio": "", "warnings": [],
    })
    ext = mocker.patch.object(f, "extract_emails",
                              return_value={"emails": ["sales@acmestudio.tw"],
                                            "guessed": False})
    mocker.patch.object(f, "verify_email", return_value={
        "confidence": "high", "mx_found": True, "smtp_status": "accepted"})
    # The other social sources must NOT be touched for an instagram website.
    fb = mocker.patch.object(f, "fetch_facebook_contact")
    xp = mocker.patch.object(f, "fetch_x_profile_contact")

    result = f.EmailCollectFlow().kickoff(inputs={
        "query": "AI agency", "region": "Taipei",
        "include_social": True, "smtp_check": False, "run_id": 0})

    prof.assert_called_once()                    # IG profile mined
    fb.assert_not_called()
    xp.assert_not_called()
    ext.assert_called_once_with("https://acmestudio.tw", log=mocker.ANY, render=mocker.ANY)  # chase-through
    import json
    data = json.loads(result)
    sources = {lead["source"] for lead in data["leads"]}
    assert sources == {"instagram", "website"}   # both provenance tags present


def test_flow_skips_instagram_profile_when_social_disabled(mocker):
    from src.automation.flows import email_collect_flow as f

    mocker.patch.object(f, "search_maps", return_value={
        "businesses": [{"name": "Acme", "website": "https://www.instagram.com/acmestudio"}],
        "warnings": [],
    })
    prof = mocker.patch.object(f, "fetch_instagram_contact")
    mocker.patch.object(f, "extract_emails",
                        return_value={"emails": [], "guessed": False})
    mocker.patch.object(f, "verify_email", return_value={
        "confidence": "low", "mx_found": False, "smtp_status": ""})

    f.EmailCollectFlow().kickoff(inputs={
        "query": "AI agency", "include_social": False, "run_id": 0})
    prof.assert_not_called()                      # default off → no social scraping
