import os
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile

router = APIRouter()

# Where uploaded files live. Honors UPLOAD_DIR (set by the desktop app so data
# lands in the per-user app-data dir, not the repo); defaults to <project>/uploads.
# Project root: src/routers/uploads.py → parents[2]
UPLOAD_ROOT = Path(os.getenv("UPLOAD_DIR") or (Path(__file__).resolve().parents[2] / "uploads"))

_MAX_BYTES = 2 * 1024 * 1024  # 2 MB per file

# Logical role → saved filename. sales + cost are required; ads + returns optional.
_SLOTS = {
    "sales":   "sales.csv",
    "cost":    "cost.csv",
    "ads":     "ads.csv",
    "returns": "returns.csv",
}
_REQUIRED = {"sales", "cost"}

# Filename keyword → role. The uploader drops ALL CSVs in one go and we route each
# by what its name contains, so a seller never has to match a file to a slot. Order
# matters: more specific roles are checked first so e.g. "order_return_refund.csv"
# lands in `returns` (not snagged by a looser keyword). Covers both the Shopee
# English exports and 中文 names.
_ROLE_KEYWORDS = [
    ("returns", ("return", "refund", "退貨", "退款")),
    ("cost",    ("cost", "成本")),
    ("ads",     ("ads", "advert", "廣告", "discount", "折扣")),
    ("sales",   ("sales", "sale", "order", "銷售", "訂單")),
]

# Split an ASCII filename into whole tokens. We match keywords against tokens — not
# raw substrings — so a stray short keyword can't hide inside an unrelated word
# (e.g. "ad" inside "downl**oad**" / "upl**oad**"). CJK keywords have no token
# separators, so those fall back to substring matching.
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _classify(filename: str) -> str | None:
    """Map an uploaded filename to a logical role by keyword, or None if unknown."""
    name = (filename or "").lower()
    tokens = _TOKEN_RE.findall(name)
    for role, keywords in _ROLE_KEYWORDS:
        for kw in keywords:
            if kw.isascii():
                if any(tok == kw or tok.startswith(kw) for tok in tokens):
                    return role
            elif kw in name:  # CJK: no word separators, match as substring
                return role
    return None


async def _read_capped(file: UploadFile, label: str) -> bytes:
    if not (file.filename or "").lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail=f"{label}: must be a .csv file")
    # Read one byte past the cap to detect oversize without loading huge files.
    data = await file.read(_MAX_BYTES + 1)
    if len(data) > _MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"{label}: file exceeds 2 MB limit")
    if not data.strip():
        raise HTTPException(status_code=400, detail=f"{label}: file is empty")
    return data


@router.post("/uploads", status_code=201)
async def create_upload(files: list[UploadFile] = File(...)):
    """Accept all Shopee CSVs at once and auto-route each by filename.

    Drop sales / cost / ads / returns CSVs in a single multipart `files` field;
    each file's role is inferred from keywords in its name (e.g. *cost* → cost,
    *sales* → sales, *ad* → ads, *return*/*refund* → returns). sales + cost are
    required. Each file is validated as a non-empty .csv and capped at 2 MB.

    Returns the upload_id plus how each file was classified, so the UI can show
    the seller what the system decided.
    """
    # Validate + read + classify everything before writing (no partial dirs on error).
    # Close every handle on the way out. Later file wins if two map to the same role.
    contents: dict[str, bytes] = {}
    mapping: dict[str, str] = {}   # role → original filename used
    unmatched: list[str] = []
    try:
        for file in files:
            if file is None or not file.filename:
                continue
            role = _classify(file.filename)
            if role is None:
                unmatched.append(file.filename)
                continue
            contents[role] = await _read_capped(file, file.filename)
            mapping[role] = file.filename
    finally:
        for file in files:
            if file is not None:
                await file.close()

    missing = sorted(_REQUIRED - contents.keys())
    if missing:
        hint = f"; unrecognised files: {unmatched}" if unmatched else ""
        raise HTTPException(
            status_code=400,
            detail=f"could not identify required file(s) by name: {missing} "
                   f"(expected a CSV whose name contains the role, e.g. 'sales'/'cost'){hint}",
        )

    upload_id = uuid.uuid4().hex
    dest = UPLOAD_ROOT / upload_id
    dest.mkdir(parents=True, exist_ok=True)
    for role, data in contents.items():
        (dest / _SLOTS[role]).write_bytes(data)

    return {
        "upload_id": upload_id,
        "files": sorted(contents.keys()),
        "classified": mapping,
        "unmatched": unmatched,
    }
