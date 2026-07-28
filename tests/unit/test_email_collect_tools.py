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


# ── off-domain filter + per-site cap (data-quality gates) ────────────────────

def test_registrable_domain_handles_multilevel_tlds():
    from src.automation.tools.email_extract_tool import _registrable_domain
    assert _registrable_domain("www.shop.acme.com.tw") == "acme.com.tw"
    assert _registrable_domain("mail.acme.com") == "acme.com"
    assert _registrable_domain("acme.com") == "acme.com"
    assert _registrable_domain("acme.co.uk") == "acme.co.uk"
    # PSL-backed, so suffixes beyond any hand-curated list resolve correctly and
    # two unrelated businesses under the same public suffix are NOT same-site.
    assert _registrable_domain("shop.acme.co.za") == "acme.co.za"


def test_same_site_domain_psl_backed_for_uncurated_suffix():
    from src.automation.tools.email_extract_tool import _same_site_domain
    assert not _same_site_domain("vendor.co.za", "acme.co.za")   # co.za is a suffix
    assert _same_site_domain("mail.acme.co.za", "acme.co.za")    # real subdomain


def test_same_site_domain_separates_private_psl_tenants():
    """Hosting platforms (github.io, wixsite.com, …) are PSL *private* suffixes:
    two unrelated tenants must not be treated as the same site."""
    from src.automation.tools.email_extract_tool import _registrable_domain, _same_site_domain
    assert _registrable_domain("acme.github.io") == "acme.github.io"
    assert not _same_site_domain("vendor.github.io", "acme.github.io")  # diff tenants
    assert _same_site_domain("shop.acme.github.io", "acme.github.io")   # same tenant


def test_same_site_domain_matches_sub_and_parent():
    from src.automation.tools.email_extract_tool import _same_site_domain
    assert _same_site_domain("acme.com.tw", "acme.com.tw")
    assert _same_site_domain("mail.acme.com.tw", "acme.com.tw")      # subdomain
    assert _same_site_domain("acme.com.tw", "shop.acme.com.tw")      # parent
    assert not _same_site_domain("latofonts.com", "acme.com.tw")     # third party
    assert not _same_site_domain("vendor.io", "acme.com")
    assert not _same_site_domain("", "acme.com")


def test_extract_drops_offdomain_third_party_emails(mocker):
    """A vendor / CDN / distributor email scraped off the page is dropped when
    the site publishes its own on-domain address."""
    from src.automation.tools import email_extract_tool as m
    html = ('<a href="mailto:info@acme.com.tw">us</a> '
            'font by team@latofonts.com and partner info@vendor.io')
    mocker.patch.object(m, "_fetch", return_value=html)
    r = m.extract_emails("https://www.acme.com.tw")
    assert "info@acme.com.tw" in r["emails"]
    assert "team@latofonts.com" not in r["emails"]
    assert "info@vendor.io" not in r["emails"]


def test_extract_keeps_offdomain_when_no_ondomain(mocker):
    """If the only address is off-domain (e.g. a gmail), keep it — better than
    nothing — rather than falling through to a guess."""
    from src.automation.tools import email_extract_tool as m
    mocker.patch.object(m, "_fetch",
                        return_value='<a href="mailto:acme.tw@gmail.com">x</a>')
    r = m.extract_emails("https://acme.com")
    assert r["emails"] == ["acme.tw@gmail.com"]
    assert r["guessed"] is False


def test_extract_caps_per_site_and_prefers_generic_role(mocker):
    """One company's long branch list is capped; a generic role beats a
    regionalized variant."""
    from src.automation.tools import email_extract_tool as m
    html = " ".join(f'<a href="mailto:{e}">x</a>' for e in [
        "info.taiwan@acme.com", "info.brazil@acme.com", "info.asia@acme.com",
        "info@acme.com", "sales@acme.com"])
    mocker.patch.object(m, "_fetch", return_value=html)
    r = m.extract_emails("https://acme.com")
    assert len(r["emails"]) == 3                 # capped
    assert "info@acme.com" in r["emails"]        # generic role kept
    assert "info.taiwan@acme.com" not in r["emails"]  # regional variant dropped


# ── JSON-LD / schema.org structured-data source ─────────────────────────────

def test_jsonld_harvests_organization_email():
    from src.automation.tools.email_extract_tool import _harvest_jsonld
    html = ('<script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"Organization",'
            '"name":"Acme","email":"info@acme.com.tw"}</script>')
    assert _harvest_jsonld(html) == {"info@acme.com.tw"}


def test_jsonld_harvests_nested_contactpoint_and_strips_mailto():
    from src.automation.tools.email_extract_tool import _harvest_jsonld
    html = ('<script type="application/ld+json">'
            '{"@type":"LocalBusiness","contactPoint":['
            '{"@type":"ContactPoint","email":"mailto:sales@acme.com.tw?subject=hi"},'
            '{"@type":"ContactPoint","email":"support@acme.com.tw"}]}</script>')
    assert _harvest_jsonld(html) == {"sales@acme.com.tw", "support@acme.com.tw"}


def test_jsonld_walks_graph_and_email_list():
    from src.automation.tools.email_extract_tool import _harvest_jsonld
    html = ('<script type="application/ld+json">'
            '{"@graph":[{"@type":"WebSite"},'
            '{"@type":"Organization","email":["a@acme.com","b@acme.com"]}]}</script>')
    assert _harvest_jsonld(html) == {"a@acme.com", "b@acme.com"}


def test_jsonld_malformed_block_is_skipped():
    from src.automation.tools.email_extract_tool import _harvest_jsonld
    html = ('<script type="application/ld+json">{oops not json,,,</script>'
            '<script type="application/ld+json">{"email":"ok@acme.com"}</script>')
    assert _harvest_jsonld(html) == {"ok@acme.com"}   # bad block skipped, good kept


def test_extract_recovers_jsonld_only_email(mocker):
    """A site that exposes its email ONLY in JSON-LD (no mailto:/visible text)
    now yields a real lead instead of a guess."""
    from src.automation.tools import email_extract_tool as m
    html = ('<html><body>No visible contact here.'
            '<script type="application/ld+json">'
            '{"@type":"Organization","email":"hello@acme.com.tw"}</script>'
            '</body></html>')
    mocker.patch.object(m, "_fetch", return_value=html)
    r = m.extract_emails("https://acme.com.tw")
    assert "hello@acme.com.tw" in r["emails"]
    assert r["guessed"] is False        # a real address was found, not guessed
