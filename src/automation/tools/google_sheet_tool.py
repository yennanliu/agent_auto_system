import csv
import io
import re
import urllib.request

from crewai.tools import BaseTool
from pydantic import BaseModel

from src.automation import net_guard

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
_MAX_ROWS  = 500


class SheetReadInput(BaseModel):
    url: str
    limit: int = 200


def _to_export_url(url: str) -> str:
    """Convert any Google Sheets URL to a CSV export URL."""
    if "format=csv" in url:
        return url
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    if not m:
        return url  # assume it is already a direct CSV URL
    sheet_id = m.group(1)
    gid_m = re.search(r"[#&?]gid=(\d+)", url)
    gid = gid_m.group(1) if gid_m else "0"
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"


class GoogleSheetTool(BaseTool):
    name: str = "google_sheet_reader"
    description: str = (
        "Fetch a Google Sheet as CSV and return its structured data. "
        "Accepts a standard Google Sheets URL or a direct CSV export URL. "
        "Returns column names, row count, all data rows (up to the limit), "
        "and a 5-row preview."
    )
    args_schema: type[BaseModel] = SheetReadInput

    def _run(self, url: str, limit: int = 200) -> dict:
        export_url = _to_export_url(url)
        limit = min(max(1, limit), _MAX_ROWS)

        req  = urllib.request.Request(export_url, headers=_HEADERS)
        # "A direct CSV export URL" is any URL at all — guard it (SSRF).
        try:
            resp = net_guard.urlopen(req, timeout=30)
        except net_guard.BlockedURLError as exc:
            return {"url": export_url, "error": f"Blocked by SSRF guard: {exc}",
                    "columns": [], "row_count": 0, "data": [], "preview": []}
        raw  = resp.read(_MAX_BYTES).decode("utf-8", errors="replace")

        reader  = csv.DictReader(io.StringIO(raw))
        columns = list(reader.fieldnames or [])
        rows: list[dict] = []
        for row in reader:
            rows.append(dict(row))
            if len(rows) >= limit:
                break

        return {
            "url":       export_url,
            "columns":   columns,
            "row_count": len(rows),
            "data":      rows,
            "preview":   rows[:5],
        }
