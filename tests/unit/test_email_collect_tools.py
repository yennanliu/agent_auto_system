"""Unit tests for the lead-collection funnel tools (no network)."""


# ── web_email_extract: filtering / ranking / guessing ───────────────────────────

def test_junk_emails_filtered():
    from src.automation.tools.email_extract_tool import _is_valid
    assert not _is_valid("a@sentry.io", "acme.com")
    assert not _is_valid("logo@2x.png", "acme.com")
    assert not _is_valid("you@example.com", "acme.com")
    assert not _is_valid("x@sub.wixpress.com", "acme.com")
    assert _is_valid("info@acme.com", "acme.com")


def test_role_addresses_ranked_first():
    from src.automation.tools.email_extract_tool import _rank
    ranked = _rank({"ceo@a.com", "info@a.com", "hello@a.com"})
    assert ranked[0].split("@")[0] in ("info", "hello")
    assert ranked[-1] == "ceo@a.com"


def test_harvest_pulls_mailto_and_text():
    from src.automation.tools.email_extract_tool import _harvest
    html = '<a href="mailto:info@shop.com">mail</a> or reach owner@shop.com today'
    got = _harvest(html)
    assert "info@shop.com" in got
    assert "owner@shop.com" in got


def test_harvest_decodes_obfuscated_and_cloudflare():
    from src.automation.tools.contact_harvest import decode_cfemail
    from src.automation.tools.email_extract_tool import _harvest
    # `info [at] shop [dot] com` textual obfuscation is now recovered.
    assert "info@shop.com" in _harvest("mail us at info [at] shop [dot] com please")
    # Cloudflare hex blob for "hi@acme.com" (XOR each byte by the first).
    plain = "hi@acme.com"
    key = 0x7a
    blob = bytes([key] + [ord(ch) ^ key for ch in plain]).hex()
    assert "hi@acme.com" in decode_cfemail(f'<a data-cfemail="{blob}">[email protected]</a>')


def test_widget_and_placeholder_domains_are_junk():
    from src.automation.tools.email_extract_tool import _is_valid
    for bad in ("info@website.com", "info@mysite.com", "support@inline.app",
                "hi@surveycake.com", "x@bit.ly"):
        assert not _is_valid(bad, "acme.com"), bad


def test_chinese_contact_link_discovered():
    from src.automation.tools.email_extract_tool import _discover_contact_links
    html = '<a href="/p/8821">聯絡我們</a><a href="/x">首頁</a>'
    links = _discover_contact_links("https://clinic.tw", html)
    assert "https://clinic.tw/p/8821" in links


def test_no_guess_on_social_hosts(mocker):
    from src.automation.tools import email_extract_tool as m
    mocker.patch.object(m, "_fetch", return_value="<html>no emails here</html>")
    fb = m.extract_emails("https://www.facebook.com/somebiz/")
    assert fb["emails"] == [] and fb["guessed"] is False


def test_single_role_guess_on_real_domain(mocker):
    from src.automation.tools import email_extract_tool as m
    mocker.patch.object(m, "_fetch", return_value="<html>no emails here</html>")
    r = m.extract_emails("https://acme.com")
    assert r["guessed"] is True
    assert r["emails"] == ["info@acme.com"]


def test_render_fallback_recovers_when_static_empty(mocker):
    """render=True: a rendered hit prevents the guess fallback."""
    from src.automation.tools import email_extract_tool as m
    mocker.patch.object(m, "_fetch", return_value="<html>no emails here</html>")
    mocker.patch.object(m, "_render_and_harvest", return_value=({"hello@acme.com"}, 1))
    r = m.extract_emails("https://acme.com", render=True)
    assert r["guessed"] is False
    assert r["emails"] == ["hello@acme.com"]


def test_render_not_invoked_without_flag(mocker):
    """render defaults off — the browser path must never fire for the tool/tests."""
    from src.automation.tools import email_extract_tool as m
    mocker.patch.object(m, "_fetch", return_value="<html>no emails here</html>")
    spy = mocker.patch.object(m, "_render_and_harvest")
    r = m.extract_emails("https://acme.com")
    spy.assert_not_called()
    assert r["guessed"] is True


# ── email_verify: layered confidence ────────────────────────────────────────────

def test_verify_rejects_bad_syntax():
    from src.automation.tools.email_verify_tool import verify_email
    r = verify_email("not-an-email", smtp_check=False)
    assert r["syntax_valid"] is False
    assert r["confidence"] == "invalid"


def test_verify_mx_only_role_is_medium(mocker):
    from src.automation.tools import email_verify_tool as m
    mocker.patch.object(m, "_lookup_mx", return_value="mx.acme.com")
    r = m.verify_email("info@acme.com", smtp_check=False)
    assert r["mx_found"] and r["is_role"]
    assert r["confidence"] == "medium"


def test_verify_smtp_accept_is_high(mocker):
    from src.automation.tools import email_verify_tool as m
    mocker.patch.object(m, "_lookup_mx", return_value="mx.acme.com")
    mocker.patch.object(m, "_smtp_probe", return_value="accepted")
    r = m.verify_email("hello@acme.com", smtp_check=True)
    assert r["confidence"] == "high"


def test_verify_no_mx_is_low(mocker):
    from src.automation.tools import email_verify_tool as m
    mocker.patch.object(m, "_lookup_mx", return_value=None)
    r = m.verify_email("info@acme.com", smtp_check=False)
    assert r["mx_found"] is False
    assert r["confidence"] == "low"
