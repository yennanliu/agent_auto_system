"""
Resolve a free-form area input into 104.com.tw area code(s).

104's job search filters by ``area`` codes like ``6001001000`` (台北市). Users,
though, want to type a name — "台北", "taipei", "高雄市", or even a typo — so this
module maps names → codes. The city table is baked from 104's own public
category JSON (static.104.com.tw/category-tool/json/Area.json) so it needs no
network access at run time and stays authoritative.

Resolution order:
  1. Already a raw code (10 digits) → passed through untouched.
  2. Static alias lookup (Chinese full/short, 臺/台 variants, English) — covers
     the common cases deterministically and for free.
  3. Anything still unresolved is handed to an optional ``llm_fn`` (the caller
     wires this to the run's LLM), which normalises fuzzy / mixed / misspelled
     input to canonical city names we then map to codes. This is the "LLM
     auto-fix / enrich" step; it only runs for inputs the static table missed.

Unresolved input degrades to "" (no area filter → nationwide) with a note,
never an error — mirrors the scraper-tools degrade-gracefully policy.
"""
import re
from collections.abc import Callable

# (code, canonical 中文, English, [extra aliases]). 104 merges 新竹縣市 and
# 嘉義縣市 under one code each, matching the site's own filter.
_CITIES: list[tuple[str, str, str, list[str]]] = [
    ("6001001000", "台北市", "Taipei",      ["北市", "臺北", "台北", "tpe"]),
    ("6001002000", "新北市", "New Taipei",  ["新北", "北縣", "ntpc"]),
    ("6001003000", "宜蘭縣", "Yilan",       ["宜蘭"]),
    ("6001004000", "基隆市", "Keelung",     ["基隆"]),
    ("6001005000", "桃園市", "Taoyuan",     ["桃園"]),
    ("6001006000", "新竹縣市", "Hsinchu",   ["新竹", "新竹市", "新竹縣", "竹科", "竹北"]),
    ("6001007000", "苗栗縣", "Miaoli",      ["苗栗"]),
    ("6001008000", "台中市", "Taichung",    ["台中", "臺中", "中市"]),
    ("6001010000", "彰化縣", "Changhua",    ["彰化"]),
    ("6001011000", "南投縣", "Nantou",      ["南投"]),
    ("6001012000", "雲林縣", "Yunlin",      ["雲林"]),
    ("6001013000", "嘉義縣市", "Chiayi",    ["嘉義", "嘉義市", "嘉義縣"]),
    ("6001014000", "台南市", "Tainan",      ["台南", "臺南", "南市"]),
    ("6001016000", "高雄市", "Kaohsiung",   ["高雄", "高市", "khh"]),
    ("6001018000", "屏東縣", "Pingtung",    ["屏東"]),
    ("6001019000", "台東縣", "Taitung",     ["台東", "臺東"]),
    ("6001020000", "花蓮縣", "Hualien",     ["花蓮"]),
    ("6001021000", "澎湖縣", "Penghu",      ["澎湖"]),
    ("6001022000", "金門縣", "Kinmen",      ["金門"]),
    ("6001023000", "連江縣", "Lienchiang",  ["連江", "馬祖", "matsu"]),
]

# code → canonical Chinese name (for logging / LLM prompt)
CODE_TO_NAME: dict[str, str] = {c[0]: c[1] for c in _CITIES}
# canonical Chinese names, for the LLM prompt's allowed-values list
CANONICAL_NAMES: list[str] = [c[1] for c in _CITIES]

_CODE_RE = re.compile(r"^\d{10}$")
# Split on comma-family separators ONLY — never plain spaces, or multi-word
# names like "New Taipei" would be torn apart. Multiple areas are comma-separated.
_SPLIT_RE = re.compile(r"[,、，;|/]+")


def _norm(s: str) -> str:
    """Normalise for alias matching: trim, lowercase, unify 臺→台."""
    return (s or "").strip().lower().replace("臺", "台")


def _lookup(token: str) -> str | None:
    """Alias lookup with a 市/縣-suffix fallback so both "桃園" and the old-style
    "桃園縣" (or "台北市") resolve to the same code."""
    key = _norm(token)
    return AREA_CODES.get(key) or AREA_CODES.get(key.rstrip("市縣"))


def _build_aliases() -> dict[str, str]:
    m: dict[str, str] = {}
    for code, zh, en, extras in _CITIES:
        keys = [zh, zh.replace("市", "").replace("縣", ""), en, f"{en} city",
                f"{en} county", *extras]
        for k in keys:
            nk = _norm(k)
            if nk and nk not in m:
                m[nk] = code
    return m


AREA_CODES: dict[str, str] = _build_aliases()


def _noop(_msg: str) -> None:
    pass


def resolve_area(
    raw: str,
    llm_fn: Callable[[str], str] | None = None,
    log: Callable[[str], None] | None = None,
) -> tuple[str, str]:
    """Map a free-form area string to a comma-joined 104 area-code string.

    Returns ``(codes_csv, note)``. ``codes_csv`` is "" when nothing resolved
    (caller then searches nationwide). ``note`` is a short human summary of what
    happened, suitable for a run log."""
    log = log or _noop
    raw = (raw or "").strip()
    if not raw:
        return "", ""

    tokens = [t for t in _SPLIT_RE.split(raw) if t]
    codes: list[str] = []
    unresolved: list[str] = []

    def _add(code: str) -> None:
        if code and code not in codes:
            codes.append(code)

    for tok in tokens:
        tok = tok.strip()
        if not tok:
            continue
        if _CODE_RE.match(tok):
            _add(tok)
            continue
        hit = _lookup(tok)
        if hit:
            _add(hit)
        else:
            unresolved.append(tok)

    llm_note = ""
    if unresolved and llm_fn is not None:
        try:
            names = llm_fn(", ".join(unresolved)) or ""
            matched = []
            for name in _SPLIT_RE.split(names):
                hit = _lookup(name)
                if hit:
                    _add(hit)
                    matched.append(CODE_TO_NAME.get(hit, hit))
            if matched:
                llm_note = f"; LLM resolved {unresolved} → {matched}"
                unresolved = []
        except Exception as exc:  # noqa: BLE001 — enrichment must never fail a run
            log(f"⚠ area LLM enrichment failed ({exc}); ignoring unresolved input")

    resolved_names = [CODE_TO_NAME.get(c, c) for c in codes]
    if codes:
        note = f"area '{raw}' → {resolved_names} ({','.join(codes)}){llm_note}"
    elif unresolved:
        note = (f"area '{raw}' could not be resolved to a 104 area code; "
                "searching nationwide")
    else:
        note = ""
    if note:
        log(note)
    if unresolved and codes:
        log(f"⚠ ignored unrecognised area token(s): {unresolved}")
    return ",".join(codes), note
