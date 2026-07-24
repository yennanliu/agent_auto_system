"""Unit tests for the Facebook Page contact source (no network)."""


# ── page/URL → slug ─────────────────────────────────────────────────────────

def test_extract_page_slug_from_url_or_bare():
    from src.automation.tools.facebook_contact_tool import _extract_page_slug
    assert _extract_page_slug("https://www.facebook.com/acmebiz") == "acmebiz"
    assert _extract_page_slug("https://facebook.com/acmebiz/about") == "acmebiz"
    assert _extract_page_slug("facebook.com/acmebiz?ref=page_internal") == "acmebiz"
    assert _extract_page_slug("@acmebiz") == "acmebiz"
    assert _extract_page_slug("acmebiz") == "acmebiz"
    assert _extract_page_slug(
        "https://www.facebook.com/profile.php?id=123") == "profile.php?id=123"
    assert _extract_page_slug("") == ""


def test_about_url_vanity_vs_numeric():
    from src.automation.tools.facebook_contact_tool import _about_url
    assert _about_url("acmebiz").endswith(
        "/acmebiz/about_contact_and_basic_info")
    # Numeric Pages take the tab as a query param, not a path segment.
    assert "profile.php?id=123&sk=about_contact_and_basic_info" in \
        _about_url("profile.php?id=123")


# ── link / body field extraction ───────────────────────────────────────────

def test_website_decoded_from_lfacebook_wrapper():
    from src.automation.tools.facebook_contact_tool import _website_from_links
    hrefs = [
        "https://www.facebook.com/acmebiz",
        "https://l.facebook.com/l.php?u=http%3A%2F%2Facme.com.tw%2F&h=AT1",
    ]
    assert _website_from_links(hrefs) == "http://acme.com.tw/"


def test_website_from_links_skips_social_redirect():
    from src.automation.tools.facebook_contact_tool import _website_from_links
    hrefs = ["https://l.facebook.com/l.php?u=https%3A%2F%2Finstagram.com%2Facmebiz"]
    assert _website_from_links(hrefs) == ""      # a social link is not the website


def test_website_from_body_fallback_skips_facebook():
    from src.automation.tools.facebook_contact_tool import _website_from_body
    body = "Websites and social links\nhttp://www.acme.com.tw/\nWebsite"
    assert _website_from_body(body) == "http://www.acme.com.tw/"


def test_phone_and_category_from_body():
    from src.automation.tools.facebook_contact_tool import _labelled, _phone_from_body
    body = "Categories\nSoftware\nContact info\n+886 2 1234 5678\nMobile"
    assert _phone_from_body(body).startswith("+886")
    assert _labelled(body, "Categories") == "Software"


# ── fetch_facebook_contact orchestration (browser mocked) ───────────────────

def test_fetch_facebook_contact_harvests_from_about(mocker):
    from src.automation.tools import facebook_contact_tool as m
    mocker.patch.object(m, "_scrape_about", return_value={
        "emails": ["info@acme.com.tw"], "website": "http://acme.com.tw/",
        "phone": "+886 2 1234 5678", "category": "Software", "warnings": []})
    r = m.fetch_facebook_contact("https://www.facebook.com/acmebiz")
    assert r["page"] == "acmebiz"
    assert r["source"] == "facebook (playwright)"
    assert r["emails"] == ["info@acme.com.tw"]
    assert r["website"] == "http://acme.com.tw/"


def test_fetch_facebook_contact_empty_page():
    from src.automation.tools.facebook_contact_tool import fetch_facebook_contact
    r = fetch_facebook_contact("")
    assert r["emails"] == [] and r["warnings"]


def test_fetch_facebook_contact_swallows_browser_error(mocker):
    from src.automation.tools import facebook_contact_tool as m
    mocker.patch.object(m, "_scrape_about", side_effect=RuntimeError("no browser"))
    r = m.fetch_facebook_contact("acmebiz")
    assert r["emails"] == [] and any("playwright" in w for w in r["warnings"])


# ── flow routing: facebook website → FB extractor + chase-through ───────────

def test_flow_routes_facebook_website(mocker):
    from src.automation.flows import email_collect_flow as f

    mocker.patch.object(f, "search_maps", return_value={
        "businesses": [{"name": "Acme", "website": "https://www.facebook.com/acmebiz",
                        "category": "AI", "maps_url": "u"}],
        "warnings": [],
    })
    fb = mocker.patch.object(f, "fetch_facebook_contact", return_value={
        "emails": ["info@acme.com.tw"], "website": "https://acme.com.tw",
        "phone": "", "category": "AI", "warnings": []})
    ext = mocker.patch.object(f, "extract_emails",
                              return_value={"emails": ["sales@acme.com.tw"],
                                            "guessed": False})
    mocker.patch.object(f, "verify_email", return_value={
        "confidence": "high", "mx_found": True, "smtp_status": "accepted"})
    # X source must NOT be touched for a facebook website.
    xp = mocker.patch.object(f, "fetch_x_profile_contact")

    result = f.EmailCollectFlow().kickoff(inputs={
        "query": "AI agency", "region": "Taipei",
        "include_social": True, "smtp_check": False, "run_id": 0})

    fb.assert_called_once()
    xp.assert_not_called()
    ext.assert_called_once_with("https://acme.com.tw", log=mocker.ANY)  # chase-through
    import json
    data = json.loads(result)
    sources = {lead["source"] for lead in data["leads"]}
    assert sources == {"facebook", "website"}
