"""Unit tests for the 經濟部 商工登記 (GCIS open-data) discovery/enrichment tool.

The API is stubbed: it answers HTTP 200 for everything, so most of the logic
under test is about classifying the *body* — rows, an empty string, or a
plain-text Chinese error — and mapping registry fields onto the funnel's shape.
"""
import json

import pytest

from src.automation.tools import moea_gcis_tool as G

ROW = {
    "Business_Accounting_NO": "22099131",
    "Company_Status_Desc": "核准設立",
    "Company_Name": "台灣積體電路製造股份有限公司",
    "Capital_Stock_Amount": "280500000000",
    "Paid_In_Capital_Amount": "259323700670",
    "Responsible_Name": "魏哲家",
    "Company_Location": "新竹市力行六路8號",
    "Register_Organization_Desc": "新竹科學園區管理局",
    "Company_Setup_Date": "0760221",
}


@pytest.fixture
def api(monkeypatch):
    """Stub urlopen with a queue of response bodies; records requested URLs."""
    urls = []

    def install(bodies):
        queue = list(bodies)

        class _Resp:
            def __init__(self, body): self._body = body.encode("utf-8")
            def read(self): return self._body
            def __enter__(self): return self
            def __exit__(self, *_a): return False

        def _urlopen(req, timeout=None):
            urls.append(req.full_url)
            return _Resp(queue.pop(0) if queue else "")
        monkeypatch.setattr(G.urllib.request, "urlopen", _urlopen)
        return urls
    return install


def _rows(n, **overrides):
    out = []
    for i in range(n):
        row = dict(ROW)
        row["Business_Accounting_NO"] = f"{i:08d}"
        row["Company_Name"] = f"第{i}號科技股份有限公司"
        row.update(overrides)
        out.append(row)
    return json.dumps(out, ensure_ascii=False)


# ── discovery ────────────────────────────────────────────────────────────────

def test_search_maps_registry_rows_onto_the_business_shape(api):
    api([json.dumps([ROW], ensure_ascii=False)])
    res = G.search_companies("積體電路", limit=1)
    biz = res["businesses"][0]

    assert biz["name"] == "台灣積體電路製造股份有限公司"
    assert biz["address"] == "新竹市力行六路8號"
    # The registry publishes no URL and no phone — the funnel must see them empty.
    assert biz["website"] == "" and biz["phone"] == ""
    assert biz["tax_id"] == "22099131"
    assert biz["responsible"] == "魏哲家"
    assert biz["capital"] == 280_500_000_000
    assert biz["setup_date"] == "1987-02-21"      # 民國 076-02-21
    assert biz["discovery"] == "govbiz"


def test_search_filters_to_active_companies_by_default(api):
    urls = api([json.dumps([ROW], ensure_ascii=False)])
    G.search_companies("科技", limit=1)
    assert "Company_Status+eq+01" in urls[0]
    assert "6BBA2268" in urls[0]                  # 公司登記關鍵字查詢 dataset


def test_an_f_code_routes_to_the_business_item_dataset(api):
    urls = api([json.dumps([ROW], ensure_ascii=False)])
    G.search_companies("f501030", limit=1)
    assert "C8782705" in urls[0]
    assert "Business_Item+eq+F501030" in urls[0]


def test_whitespace_is_stripped_from_the_keyword(api):
    """The $filter grammar is `Company_Name like [^\\s]+` — a space is a 400."""
    urls = api([json.dumps([ROW], ensure_ascii=False)])
    G.search_companies(" 數位 行銷 ", limit=1)
    assert "%E6%95%B8%E4%BD%8D%E8%A1%8C%E9%8A%B7" in urls[0]  # 數位行銷, no space


def test_a_bracketed_latin_alias_is_stripped_before_querying(api):
    """公會 rows read "慧與科技股份有限公司（Hewlett Packard…）"; the registry doesn't."""
    urls = api([json.dumps(
        [{**ROW, "Company_Name": "慧與科技股份有限公司",
          "Business_Accounting_NO": "20946791"}], ensure_ascii=False)])
    rec = G.lookup_company("慧與科技股份有限公司（Hewlett Packard Enterprise Taiwan）")
    assert rec["tax_id"] == "20946791"
    assert "Hewlett" not in urls[0]


def test_empty_keyword_warns_without_calling_the_api(api):
    urls = api([])
    res = G.search_companies("   ", limit=5)
    assert res["businesses"] == [] and urls == []
    assert "empty keyword" in res["warnings"][0]


def test_city_filter_matches_the_tai_spelling_split(api):
    rows = json.dumps([
        {**ROW, "Company_Name": "北一有限公司", "Company_Location": "臺北市中山區1號"},
        {**ROW, "Company_Name": "南一有限公司", "Company_Location": "台南市東區2號"},
    ], ensure_ascii=False)
    api([rows])
    res = G.search_companies("有限公司", limit=10, city="台北")
    assert [b["name"] for b in res["businesses"]] == ["北一有限公司"]


def test_paging_stops_on_a_short_page(api):
    urls = api([_rows(G._PAGE_SIZE), _rows(3)])
    res = G.search_companies("科技", limit=1000)
    assert len(res["businesses"]) == G._PAGE_SIZE + 3
    assert len(urls) == 2
    assert f"%24skip={G._PAGE_SIZE}" in urls[1] or f"$skip={G._PAGE_SIZE}" in urls[1]


def test_a_plain_text_error_body_becomes_a_warning(api):
    api(["$filter參數有誤，請查明後繼續。"])
    res = G.search_companies("科技", limit=5)
    assert res["businesses"] == []
    assert "rejected the query" in res["warnings"][0]


def test_a_transport_failure_degrades_to_a_warning(monkeypatch):
    def _boom(req, timeout=None):
        raise TimeoutError("nope")
    monkeypatch.setattr(G.urllib.request, "urlopen", _boom)
    res = G.search_companies("科技", limit=5)
    assert res["businesses"] == []
    assert "unreachable" in res["warnings"][0]


# ── enrichment ───────────────────────────────────────────────────────────────

def test_lookup_prefers_an_exact_name_match(api):
    api([json.dumps([
        {**ROW, "Company_Name": "台灣積體電路製造股份有限公司分公司",
         "Business_Accounting_NO": "99999999"},
        ROW,
    ], ensure_ascii=False)])
    rec = G.lookup_company("臺灣積體電路製造股份有限公司")   # 臺 vs 台
    assert rec["tax_id"] == "22099131"
    assert rec["capital"] == 280_500_000_000
    assert rec["setup_date"] == "1987-02-21"


def test_lookup_accepts_a_lone_candidate(api):
    api([json.dumps([ROW], ensure_ascii=False)])
    assert G.lookup_company("積體電路")["tax_id"] == "22099131"


def test_lookup_refuses_to_guess_between_candidates(api):
    """A wrong 統一編號 on a lead is worse than no 統一編號."""
    api([_rows(3)])
    assert G.lookup_company("科技公司") is None


def test_lookup_returns_none_when_nothing_matches(api):
    api([""])
    assert G.lookup_company("不存在的公司") is None


# ── helpers ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("roc,iso", [
    ("1130920", "2024-09-20"),
    ("0760221", "1987-02-21"),
    ("990101", "2010-01-01"),
    ("", ""),
    ("not-a-date", "not-a-date"),
])
def test_roc_date_conversion(roc, iso):
    assert G._roc_to_iso(roc) == iso


@pytest.mark.parametrize("value,expected", [("123", 123), (456, 456), (None, 0), ("", 0)])
def test_capital_coercion(value, expected):
    assert G._as_int(value) == expected
