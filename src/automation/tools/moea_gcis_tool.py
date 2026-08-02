"""
Discover & enrich Taiwanese companies from 經濟部 商工登記公示資料.

Another stage-1 source for the lead-collection funnel (see doc/email_collect),
and the funnel's only *authoritative* one: every row is a legally registered
company, with its 統一編號, registered address, 負責人, and paid-in capital.

**Why the API and not findbiz.** The public UI at
`findbiz.nat.gov.tw/fts/query/QueryBar/queryInit.do` is the human face of this
data, but it is bot-protected (a plain HTTP GET is answered 403) and its markup
is unstable. 經濟部 publishes the same registry through the 商工行政資料開放平臺
open-data API — no key, no scraping, documented parameters — so that is what we
call. Reached from the ministry portal (moea.gov.tw) via 商業司 → 開放資料.

    https://data.gcis.nat.gov.tw/od/data/api/<dataset>?$format=json&$filter=…

Two roles in the funnel:

* **Discovery** — keyword or 營業項目-code search returns companies matching an
  industry. Registry rows carry **no website and no email**, so on their own
  they cannot produce a lead; the flow pairs them with a website-resolution step
  (Google Maps) to make them contactable.
* **Enrichment** — look a company up by name and attach 統編 / 資本額 / 負責人 /
  設立日期 to a lead discovered elsewhere. Capital and setup date are a decent
  free proxy for company size and maturity when scoring ICP fit.

Best-effort like every other tool here: network or format trouble yields partial
results plus a `warnings` list, never an exception.
"""
import json
import re
import urllib.parse
import urllib.request

from crewai.tools import BaseTool
from pydantic import BaseModel

_API_BASE = "https://data.gcis.nat.gov.tw/od/data/api/"
# 公司登記關鍵字查詢 — approximate match on 公司名稱.
_KEYWORD_DATASET = "6BBA2268-1367-4B42-9CCA-BC17499EBE8C"
# 營業項目代碼( F 零售、批發及餐飲業 )查公司 — exact match on one F###### code.
_BUSINESS_ITEM_DATASET = "C8782705-DA48-4897-8537-9F7B0FC463EF"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
}
_TIMEOUT = 45

# 公司狀態代碼 01 = 核准設立. Anything else is dissolved / revoked / suspended —
# never worth cold-emailing.
ACTIVE_STATUS = "01"

# API caps $top at 1000; we page in chunks and stop early. `_MAX_SCAN` bounds the
# rows pulled when a city filter has to be applied client-side (the API has no
# location parameter), so an over-broad keyword can't page forever.
_PAGE_SIZE = 200
_MAX_SCAN = 2000

_BUSINESS_ITEM_RE = re.compile(r"^F\d{6}$", re.I)


class GcisSearchInput(BaseModel):
    keyword: str
    limit: int = 30
    city: str = ""


class MoeaGcisTool(BaseTool):
    name: str = "moea_company_registry"
    description: str = (
        "Search Taiwan's 經濟部 商工登記公示資料 (company registry) for registered "
        "companies by name keyword, returning 統一編號, registered name, address, "
        "負責人 and capital. Args: keyword (str, e.g. '軟體' or an F###### "
        "營業項目 code), limit (int), city (str, optional — filters on the "
        "registered address). Registry rows carry no website or email."
    )
    args_schema: type[BaseModel] = GcisSearchInput

    def _run(self, keyword: str, limit: int = 30, city: str = "") -> dict:
        return search_companies(keyword, limit, city)


# ── discovery ────────────────────────────────────────────────────────────────

def search_companies(keyword: str, limit: int = 30, city: str = "",
                     status: str = ACTIVE_STATUS, log=None) -> dict:
    """Find up to `limit` registered companies whose name contains `keyword`.

    An F###### 營業項目 code is routed to the business-item dataset instead, so
    the caller can search either "軟體" or "F401010" through the same entry point.
    Returns ``{"keyword", "businesses": [...], "warnings": [...]}``; businesses
    match the Maps shape (with an empty ``website``) plus registry extras.
    """
    _log = log or (lambda _m: None)
    limit = max(1, min(int(limit or 1), 500))
    keyword = _api_keyword(keyword)
    warnings: list[str] = []
    if not keyword:
        return {"keyword": "", "businesses": [],
                "warnings": ["empty keyword — 經濟部 registry search needs a "
                             "company-name fragment (Chinese) or an F###### code"]}

    if _BUSINESS_ITEM_RE.match(keyword):
        _log(f"Querying 經濟部 registry for 營業項目 {keyword.upper()}...")
        rows = _fetch_all(_BUSINESS_ITEM_DATASET,
                          f"Business_Item eq {keyword.upper()}",
                          limit, city, warnings, _log)
    else:
        _log(f"Querying 經濟部 商工登記 for companies named like '{keyword}'...")
        rows = _fetch_all(_KEYWORD_DATASET,
                          f"Company_Name like {keyword} and Company_Status eq {status}",
                          limit, city, warnings, _log)

    businesses = [_to_business(r) for r in rows[:limit]]
    _log(f"經濟部 registry: {len(businesses)} company record(s)")
    return {"keyword": keyword, "businesses": businesses, "warnings": warnings[:8]}


def _fetch_all(dataset: str, filt: str, limit: int, city: str,
               warnings: list[str], log) -> list[dict]:
    """Page the API until `limit` city-matching rows are collected (or we stop).

    The API has no location parameter, so a city filter is applied here — which
    means over-fetching. `_MAX_SCAN` is the ceiling on that.
    """
    out: list[dict] = []
    scanned = 0
    while len(out) < limit and scanned < _MAX_SCAN:
        page = _get(dataset, filt, skip=scanned, top=_PAGE_SIZE, warnings=warnings)
        if page is None:
            break
        if not page:
            break
        scanned += len(page)
        out.extend(r for r in page if _matches_city(r, city))
        if len(page) < _PAGE_SIZE:
            break
        if city and len(out) < limit:
            log(f"Scanned {scanned} registry rows, {len(out)} in '{city}'...")
    if city and len(out) < limit and scanned >= _MAX_SCAN:
        warnings.append(f"stopped after scanning {scanned} registry rows for "
                        f"'{city}' — narrow the keyword for more matches")
    return out


def _matches_city(row: dict, city: str) -> bool:
    if not city:
        return True
    city = city.strip()
    if not city:
        return True
    blob = f"{row.get('Company_Location', '')} {row.get('Register_Organization_Desc', '')}"
    # '台'/'臺' are interchangeable in TW addresses and the registry uses both.
    for variant in {city, city.replace("台", "臺"), city.replace("臺", "台")}:
        if variant and variant in blob:
            return True
    return False


# ── enrichment ───────────────────────────────────────────────────────────────

def lookup_company(name: str, log=None) -> dict | None:
    """Return registry facts for `name`, or None if no confident match.

    The keyword endpoint is an approximate match, so several companies can share
    a name fragment. We accept only an exact name hit (or, failing that, a single
    candidate) — a wrong 統一編號 on a lead is worse than no 統一編號.
    """
    _log = log or (lambda _m: None)
    keyword = _api_keyword(name)
    if not keyword:
        return None
    rows = _get(_KEYWORD_DATASET,
                f"Company_Name like {keyword} and Company_Status eq {ACTIVE_STATUS}",
                skip=0, top=50, warnings=[])
    if not rows:
        return None
    target = _normalize_name(name)
    exact = [r for r in rows if _normalize_name(r.get("Company_Name", "")) == target]
    match = exact[0] if exact else (rows[0] if len(rows) == 1 else None)
    if match is None:
        _log(f"經濟部 registry: '{name}' matched {len(rows)} companies, none exactly")
        return None
    return {
        "tax_id": match.get("Business_Accounting_NO", ""),
        "registered_name": match.get("Company_Name", ""),
        "responsible": match.get("Responsible_Name", ""),
        "capital": _as_int(match.get("Capital_Stock_Amount")),
        "paid_in_capital": _as_int(match.get("Paid_In_Capital_Amount")),
        "registered_address": match.get("Company_Location", ""),
        "registrar": match.get("Register_Organization_Desc", ""),
        "setup_date": _roc_to_iso(match.get("Company_Setup_Date", "")),
        "registry_status": match.get("Company_Status_Desc", ""),
    }


# ── mapping helpers ──────────────────────────────────────────────────────────

def _to_business(row: dict) -> dict:
    """Map a registry row onto the funnel's business shape.

    ``website`` and ``phone`` are deliberately empty: the registry publishes
    neither. The flow's website-resolution step fills ``website`` in when asked.
    """
    return {
        "name": row.get("Company_Name", ""),
        "website": "",
        "phone": "",
        "address": row.get("Company_Location", ""),
        "category": row.get("Company_Status_Desc", ""),
        "maps_url": "",
        "discovery": "govbiz",
        "tax_id": row.get("Business_Accounting_NO", ""),
        "responsible": row.get("Responsible_Name", ""),
        "capital": _as_int(row.get("Capital_Stock_Amount")),
        "registrar": row.get("Register_Organization_Desc", ""),
        "setup_date": _roc_to_iso(row.get("Company_Setup_Date", "")),
    }


# 公會 directories and company sites routinely append a Latin alias the registry
# has never heard of — "慧與科技股份有限公司（Hewlett Packard Enterprise Taiwan）".
# Querying with it attached matches nothing, so it comes off first.
_ALIAS_RE = re.compile(r"[（(\[【][^）)\]】]*[）)\]】]")


def _api_keyword(value: str) -> str:
    """The `$filter` grammar is ``Company_Name like [^\\s]+`` — no whitespace.

    TW company names contain none, so collapsing spaces is lossless here and
    stops a stray space from turning into a 400 from the API.
    """
    return re.sub(r"\s+", "", _ALIAS_RE.sub("", value or ""))


def _normalize_name(name: str) -> str:
    """Compare names ignoring the Latin alias, spacing, and the 台/臺 split."""
    return re.sub(r"\s+", "", _ALIAS_RE.sub("", name or "")).replace("台", "臺")


def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _roc_to_iso(roc: str) -> str:
    """`1130920` (民國 113-09-20) → `2024-09-20`; anything odd passes through."""
    roc = (roc or "").strip()
    if not re.fullmatch(r"\d{6,7}", roc):
        return roc
    year, month, day = int(roc[:-4]) + 1911, roc[-4:-2], roc[-2:]
    return f"{year}-{month}-{day}"


# ── HTTP ─────────────────────────────────────────────────────────────────────

def _get(dataset: str, filt: str, skip: int, top: int,
         warnings: list[str]) -> list[dict] | None:
    """One API call. Returns rows, [] for an empty result, None on failure.

    The platform answers HTTP 200 for *everything* — an empty body means "no
    rows", and a plain-text Chinese sentence means "bad request" — so the status
    code tells us nothing and the body has to be classified here.
    """
    query = urllib.parse.urlencode(
        {"$format": "json", "$filter": filt, "$skip": str(skip), "$top": str(top)})
    url = f"{_API_BASE}{dataset}?{query}"
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", errors="replace").strip()
    except Exception as exc:  # noqa: BLE001 — timeout / DNS / TLS → partial result
        warnings.append(f"經濟部 registry unreachable: {type(exc).__name__}")
        return None
    if not body:
        return []
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        warnings.append(f"經濟部 registry rejected the query: {body[:120]}")
        return None
    return data if isinstance(data, list) else []
