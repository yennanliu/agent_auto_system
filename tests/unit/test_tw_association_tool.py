"""Unit tests for the 公會/工會 member-directory discovery tool.

All network access is monkeypatched — the fixtures below are trimmed copies of
the real markup (TCA's Big5 ASP tables, a generic association member list), so
the parsers are exercised against the shapes they actually meet.
"""
import pytest

from src.automation.tools import tw_association_tool as T

# ── fixtures ─────────────────────────────────────────────────────────────────

TCA_LIST = """
<table>
  <tr><td>會員編號</td><td>公司名稱</td><td>聯絡電話</td></tr>
  <tr>
    <td>00278</td>
    <td align="left"><a href="#" onClick="GoList('00278')">飛捷科技股份有限公司</a></td>
    <td>02-87914988</td>
  </tr>
  <tr>
    <td>02334</td>
    <td align="left"><a href="#" onClick="GoList('02334')">趨勢科技股份有限公司</a></td>
    <td>02-23789666</td>
  </tr>
</table>
"""

TCA_DETAIL = """
<table>
  <tr><td>公司名稱</td><td>飛捷科技股份有限公司（Flytech Technology Co., Ltd.）</td></tr>
  <tr><td>公司地址</td><td>台北市內湖區行愛路168號1樓</td></tr>
  <tr><td>公司電話</td><td>02-87914988</td></tr>
  <tr><td>公司傳真</td><td>02-87914966</td></tr>
  <tr><td>公司網址</td><td><a href="http://www.flytech.com.tw">http://www.flytech.com.tw</a></td></tr>
  <tr><td>業務類型</td><td>硬體製造(含零組件)，系統整合</td></tr>
</table>
<div class="footer">聯絡我們 service@tca.org.tw</div>
"""

GENERIC_LIST = """
<html><head><meta charset="utf-8"></head><body>
<nav><a href="/about.html">關於我們</a><a href="/news.html">最新消息</a></nav>
<table>
  <tr><td>會員名稱</td><td>甲級營造股份有限公司</td></tr>
  <tr><td>電話</td><td>04-22001234</td></tr>
  <tr><td>網址</td><td>www.jiajia-build.com.tw</td></tr>
</table>
<ul>
  <li><a href="https://www.member-one.com.tw">一號實業有限公司</a></li>
  <li><a href="https://www.facebook.com/guild">我們的粉絲團</a></li>
  <li><a href="/member_detail.php?id=4417">二號工程有限公司</a></li>
</ul>
<a href="/members.php?page=2">下一頁</a>
</body></html>
"""

GENERIC_DETAIL = """
<html><head><meta charset="utf-8"></head><body>
<div>公司名稱：二號工程有限公司</div>
<div>地址：高雄市前鎮區成功二路25號</div>
<div>電話：07-3345678</div>
<div>網址：http://no2-eng.com.tw</div>
<div>E-mail：sales@no2-eng.com.tw</div>
<footer>本會信箱 info@guild.org.tw</footer>
</body></html>
"""

GENERIC_PAGE2 = """
<html><head><meta charset="utf-8"></head><body>
<ul><li><a href="https://www.member-three.com.tw">三號科技有限公司</a></li></ul>
</body></html>
"""


@pytest.fixture
def fake_fetch(monkeypatch):
    """Route _fetch through an in-memory {url: html} map.

    Matches the *longest* needle contained in the URL, so routing can't depend
    on dict insertion order (`members.php` would otherwise shadow
    `members.php?page=2` whenever it happened to be declared first).

    Also neutralizes the SSRF host guard: it does a real DNS lookup, and the
    fixture hosts are fictional. The guard has its own tests below.
    """
    calls = []

    def install(pages: dict, default=None):
        def _fetch(url, encoding=None, data=None):
            calls.append((url, data))
            matches = [(n, h) for n, h in pages.items() if n in url]
            if not matches:
                return default
            return max(matches, key=lambda item: len(item[0]))[1]
        monkeypatch.setattr(T, "_fetch", _fetch)
        monkeypatch.setattr(T, "_reject_reason", lambda _url: "")
        return calls
    return install


# ── TCA adapter ──────────────────────────────────────────────────────────────

def test_tca_reads_list_then_detail(fake_fetch):
    fake_fetch({"tcaprdqc.asp": TCA_LIST, "members_list.asp": TCA_DETAIL})
    res = T.search_association("tca", "科技", limit=2)

    assert res["association"].startswith("台北市電腦商業同業公會")
    first = res["businesses"][0]
    # The detail page's richer name/website wins over the list row.
    assert first["name"].startswith("飛捷科技股份有限公司")
    assert first["website"] == "http://www.flytech.com.tw"
    assert first["address"] == "台北市內湖區行愛路168號1樓"
    assert first["phone"] == "02-87914988"
    assert first["category"] == "硬體製造(含零組件)，系統整合"
    assert first["member_no"] == "00278"
    assert first["discovery"] == "association:tca"
    # Falls back to the list row's phone for the second member.
    assert res["businesses"][1]["member_no"] == "02334"


def test_tca_drops_the_associations_own_email(fake_fetch):
    """service@tca.org.tw is the guild's inbox, not the member's."""
    fake_fetch({"tcaprdqc.asp": TCA_LIST, "members_list.asp": TCA_DETAIL})
    res = T.search_association("tca", "科技", limit=1)
    assert res["businesses"][0]["emails"] == []


def test_tca_caps_detail_fetches_and_says_so(fake_fetch, monkeypatch):
    """One sequential 25 s POST per member — an unbounded limit can't run for hours."""
    monkeypatch.setattr(T, "_MAX_DETAIL_PAGES", 1)
    fake_fetch({"tcaprdqc.asp": TCA_LIST, "members_list.asp": TCA_DETAIL})
    res = T.search_association("tca", "科技", limit=50)
    assert len(res["businesses"]) == 1
    assert any("read 1 of 2 matched member" in w for w in res["warnings"])


def test_tca_does_not_warn_when_the_callers_limit_is_the_binding_one(fake_fetch):
    """Stopping at the requested limit is the limit working, not a truncation."""
    fake_fetch({"tcaprdqc.asp": TCA_LIST, "members_list.asp": TCA_DETAIL})
    res = T.search_association("tca", "科技", limit=1)
    assert len(res["businesses"]) == 1
    assert res["warnings"] == []


def test_tca_requires_a_two_char_keyword(fake_fetch):
    fake_fetch({})
    res = T.search_association("tca", "x", limit=5)
    assert res["businesses"] == []
    assert any("2+ characters" in w for w in res["warnings"])


def test_tca_encodes_the_keyword_as_big5(fake_fetch):
    calls = fake_fetch({"tcaprdqc.asp": TCA_LIST, "members_list.asp": TCA_DETAIL})
    T.search_association("tca", "科技", limit=1)
    # 科技 percent-encoded in cp950/Big5, not UTF-8 (%E7%A7%91 would be UTF-8).
    assert "BNA_C=%AC%EC%A7%DE" in calls[0][0]


def test_tca_survives_an_unreachable_list_page(fake_fetch):
    fake_fetch({}, default=None)
    res = T.search_association("tca", "科技", limit=5)
    assert res["businesses"] == []
    assert any("unavailable" in w for w in res["warnings"])


# ── generic crawler ──────────────────────────────────────────────────────────

def test_generic_directory_harvests_links_labels_and_details(fake_fetch):
    fake_fetch({
        "members.php?page=2": GENERIC_PAGE2,
        "member_detail.php": GENERIC_DETAIL,
        "members.php": GENERIC_LIST,
    })
    res = T.search_association("https://www.guild.org.tw/members.php", limit=10)
    by_name = {b["name"]: b for b in res["businesses"]}

    # 1. outbound member link on the list page
    assert by_name["一號實業有限公司"]["website"] == "https://www.member-one.com.tw"
    # 2. labelled table on the list page (scheme-less URL gets one)
    assert by_name["甲級營造股份有限公司"]["website"] == "https://www.jiajia-build.com.tw"
    assert by_name["甲級營造股份有限公司"]["phone"] == "04-22001234"
    # 3. same-site detail page, "label：value" form
    two = by_name["二號工程有限公司"]
    assert two["website"] == "http://no2-eng.com.tw"
    assert two["address"] == "高雄市前鎮區成功二路25號"
    assert two["emails"] == ["sales@no2-eng.com.tw"]   # guild footer address dropped
    # 4. 下一頁 pagination
    assert "三號科技有限公司" in by_name


def test_generic_directory_skips_social_and_nav_links(fake_fetch):
    fake_fetch({"members.php": GENERIC_LIST, "member_detail.php": GENERIC_DETAIL,
                "page=2": GENERIC_PAGE2})
    res = T.search_association("https://www.guild.org.tw/members.php", limit=10)
    sites = [b["website"] for b in res["businesses"]]
    assert not any("facebook.com" in s for s in sites)
    assert "我們的粉絲團" not in [b["name"] for b in res["businesses"]]


def test_generic_keyword_filters_but_never_empties_the_result(fake_fetch):
    fake_fetch({"members.php": GENERIC_LIST, "member_detail.php": GENERIC_DETAIL,
                "page=2": GENERIC_PAGE2})
    hit = T.search_association("https://www.guild.org.tw/members.php", "營造", limit=10)
    assert [b["name"] for b in hit["businesses"]] == ["甲級營造股份有限公司"]

    miss = T.search_association("https://www.guild.org.tw/members.php", "zzz", limit=10)
    assert miss["businesses"], "a non-matching keyword should not discard the page"
    assert any("matched no member names" in w for w in miss["warnings"])


MULTI_MEMBER_LIST = """
<html><head><meta charset="utf-8"></head><body>
<table>
  <tr><td>公司名稱</td><td>頭一家有限公司</td><td>地址</td><td>台北市中山區1號</td></tr>
  <tr><td>公司名稱</td><td>第二家有限公司</td><td>地址</td><td>高雄市前鎮區2號</td>
      <td>電話</td><td>07-2222222</td><td>網址</td><td>http://second.com.tw</td></tr>
</table>
</body></html>
"""


def test_a_multi_member_table_does_not_mix_fields_between_rows(fake_fetch):
    """The first row has no phone; it must not inherit the second row's."""
    fake_fetch({"members": MULTI_MEMBER_LIST})
    res = T.search_association("https://www.guild.org.tw/members.php", limit=10)
    by_name = {b["name"]: b for b in res["businesses"]}

    assert set(by_name) == {"頭一家有限公司", "第二家有限公司"}
    first = by_name["頭一家有限公司"]
    assert first["address"] == "台北市中山區1號"
    assert first["phone"] == "" and first["website"] == ""
    second = by_name["第二家有限公司"]
    assert second["phone"] == "07-2222222"
    assert second["website"] == "http://second.com.tw"


def test_a_single_member_field_table_is_read_as_one_record(fake_fetch):
    """Fields down the page belong to one member — don't split them per row."""
    fake_fetch({"members": GENERIC_LIST, "member_detail.php": GENERIC_DETAIL,
                "page=2": GENERIC_PAGE2})
    res = T.search_association("https://www.guild.org.tw/members.php", limit=10)
    jia = {b["name"]: b for b in res["businesses"]}["甲級營造股份有限公司"]
    assert jia["phone"] == "04-22001234"
    assert jia["website"] == "https://www.jiajia-build.com.tw"


def test_a_member_seen_with_and_without_a_website_becomes_one_row():
    """List page knows the site, detail page doesn't — must not yield two rows."""
    found = {}
    T._add_business(found, {"name": "一號實業有限公司",
                            "website": "https://www.member-one.com.tw"})
    T._add_business(found, {"name": "一號實業有限公司", "phone": "02-1111111"})
    rows = T._members(found)
    assert len(rows) == 1
    assert rows[0]["website"] == "https://www.member-one.com.tw"
    assert rows[0]["phone"] == "02-1111111"
    # Both identities resolve to that one record.
    assert found["member-one.com.tw"] is found["一號實業有限公司"]


def test_a_row_gains_its_website_key_when_the_website_arrives_second():
    found = {}
    T._add_business(found, {"name": "二號工程有限公司", "phone": "07-3345678"})
    T._add_business(found, {"name": "二號工程有限公司",
                            "website": "http://no2-eng.com.tw"})
    rows = T._members(found)
    assert len(rows) == 1 and rows[0]["phone"] == "07-3345678"
    # A later sighting of the domain alone must land on the same record.
    T._add_business(found, {"website": "https://www.no2-eng.com.tw",
                            "category": "土木"})
    assert len(T._members(found)) == 1
    assert T._members(found)[0]["category"] == "土木"


# ── SSRF guard ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url,resolved", [
    ("http://169.254.169.254/latest/meta-data/", "169.254.169.254"),  # cloud metadata
    ("http://localhost:8000/admin", "127.0.0.1"),
    ("https://internal.corp/members", "10.0.0.5"),
    ("http://[::1]/members", "::1"),
])
def test_non_public_directory_urls_are_refused(monkeypatch, url, resolved):
    monkeypatch.setattr(T.socket, "getaddrinfo",
                        lambda *_a, **_k: [(None, None, None, "", (resolved, 80))])
    assert T._reject_reason(url)

    fetched = []
    monkeypatch.setattr(T, "_fetch", lambda *a, **k: fetched.append(a) or None)
    res = T.search_association(url, limit=5)
    assert res["businesses"] == []
    assert "refusing to crawl" in res["warnings"][0]
    assert fetched == [], "a blocked URL must never be requested"


def test_a_public_directory_url_is_allowed(monkeypatch):
    monkeypatch.setattr(T.socket, "getaddrinfo",
                        lambda *_a, **_k: [(None, None, None, "", ("93.184.216.34", 443))])
    assert T._reject_reason("https://www.guild.org.tw/members.php") == ""


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://host/x", "gopher://h/"])
def test_non_http_schemes_are_refused(url):
    assert T._reject_reason(url)


def test_an_unresolvable_host_is_refused(monkeypatch):
    def _boom(*_a, **_k):
        raise OSError("nodename nor servname provided")
    monkeypatch.setattr(T.socket, "getaddrinfo", _boom)
    assert "does not resolve" in T._reject_reason("https://nope.invalid/x")


def test_a_redirect_to_a_private_address_is_blocked(monkeypatch):
    """A public host that 302s inward must not slip past the initial check."""
    monkeypatch.setattr(T.socket, "getaddrinfo",
                        lambda host, *_a, **_k: [(None, None, None, "", (
                            "127.0.0.1" if "internal" in str(host) else "93.184.216.34",
                            80))])
    handler = T._PublicOnlyRedirectHandler()
    with pytest.raises(T.urllib.error.HTTPError):
        handler.redirect_request(None, None, 302, "Found", {},
                                 "http://internal.example/secret")


def test_a_same_site_link_to_a_private_host_is_not_crawled(monkeypatch):
    """`_same_site` accepts sub-domains, so the seed check alone is not enough.

    A public `guild.org.tw` linking to `internal.guild.org.tw` (→ 127.0.0.1)
    would otherwise get that page's text and emails harvested into leads.
    """
    monkeypatch.setattr(T.socket, "getaddrinfo",
                        lambda host, *_a, **_k: [(None, None, None, "", (
                            "127.0.0.1" if "internal" in str(host) else "93.184.216.34",
                            80))])
    seed = "https://guild.org.tw/members.php"
    listing = ('<html><body><a href="https://internal.guild.org.tw/member12345">'
               '會員資料</a></body></html>')
    fetched = []

    def _fetch(url, encoding=None, data=None):
        fetched.append(url)
        return listing if url == seed else "<html>secret@internal</html>"
    monkeypatch.setattr(T, "_fetch", _fetch)

    res = T.search_association(seed, limit=5)
    assert fetched == [seed], "the private sub-domain must never be requested"
    assert any("internal.guild.org.tw" in w for w in res["warnings"])


def test_unknown_source_is_reported_not_raised(fake_fetch):
    fake_fetch({})
    res = T.search_association("not-a-guild", "科技", limit=5)
    assert res["businesses"] == []
    assert "unknown association source" in res["warnings"][0]


def test_list_associations_exposes_the_builtins():
    slugs = {a["slug"] for a in T.list_associations()}
    assert "tca" in slugs
    assert all(a["name"] and a["url"] for a in T.list_associations())


# ── parsing helpers ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("html,expected", [
    ("<td>網址</td><td>http://a.com.tw</td>", "http://a.com.tw"),
    ("<div>網址：www.b.com</div>", "https://www.b.com"),
    ("<td>網址</td><td>電話</td>", None),          # next cell is another label
    ("<td>網址</td><td>無</td>", None),            # not a URL
])
def test_labelled_website_extraction(html, expected):
    assert T._labelled_fields(html).get("website") == expected


def test_cells_keeps_intra_cell_spacing():
    cells = T._cells("<td>公司名稱</td><td>Acme  Co., Ltd.</td>")
    assert cells == ["公司名稱", "Acme  Co., Ltd."]


@pytest.mark.parametrize("declared,raw_encoding,expected", [
    (b'<meta charset="big5">', "cp950", "cp950"),
    (b'<meta charset="UTF-8">', "utf-8", "utf-8"),
])
def test_charset_sniffing(declared, raw_encoding, expected):
    assert T._sniff_charset(declared, "") == expected
    assert T._canonical_charset(raw_encoding) == expected


def test_charset_falls_back_to_cp950_for_undeclared_non_utf8():
    """Not the literal 'big5': HKSCS characters in company names must survive."""
    assert T._sniff_charset("公司".encode("cp950"), "") == "cp950"
