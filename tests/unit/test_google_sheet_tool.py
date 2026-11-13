from unittest.mock import MagicMock, patch

import pytest

from src.automation.tools.google_sheet_tool import GoogleSheetTool, _to_export_url

SAMPLE_CSV = "id,name,value\n1,alpha,100\n2,beta,200\n"

SHEET_ID = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"
EDIT_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid=0"
EXPORT_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid=0"


def _mock_urlopen(csv_text: str):
    mock_resp = MagicMock()
    mock_resp.read.return_value = csv_text.encode("utf-8")
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


@pytest.fixture
def tool():
    return GoogleSheetTool()


@pytest.fixture
def result(tool):
    with patch("src.automation.tools.google_sheet_tool.net_guard.urlopen",
               return_value=_mock_urlopen(SAMPLE_CSV)):
        return tool._run(url=EDIT_URL)


# ── _to_export_url ────────────────────────────────────────────────────────────

def test_to_export_url_passthrough_when_already_export():
    already = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid=0"
    assert _to_export_url(already) == already


def test_to_export_url_converts_edit_url():
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"
    result = _to_export_url(url)
    assert "export" in result
    assert "format=csv" in result
    assert SHEET_ID in result


def test_to_export_url_plain_spreadsheet_id_url():
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"
    result = _to_export_url(url)
    assert f"/spreadsheets/d/{SHEET_ID}/export" in result
    assert "format=csv" in result


def test_to_export_url_preserves_gid():
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid=1"
    result = _to_export_url(url)
    assert "gid=1" in result


# ── Return shape ──────────────────────────────────────────────────────────────

def test_result_has_all_required_keys(result):
    assert set(result.keys()) >= {"url", "columns", "row_count", "data", "preview"}


# ── Columns ───────────────────────────────────────────────────────────────────

def test_columns_correctly_parsed(result):
    assert result["columns"] == ["id", "name", "value"]


# ── Row count ─────────────────────────────────────────────────────────────────

def test_row_count_equals_data_rows(result):
    assert result["row_count"] == len(result["data"])


def test_row_count_is_two_for_sample(result):
    assert result["row_count"] == 2


# ── Preview ───────────────────────────────────────────────────────────────────

def test_preview_is_at_most_five_rows(result):
    assert len(result["preview"]) <= 5


def test_preview_matches_first_rows(result):
    assert result["preview"] == result["data"][:5]


# ── Limit ─────────────────────────────────────────────────────────────────────

def test_limit_one_returns_one_row(tool):
    with patch("src.automation.tools.google_sheet_tool.net_guard.urlopen",
               return_value=_mock_urlopen(SAMPLE_CSV)):
        r = tool._run(url=EDIT_URL, limit=1)
    assert r["row_count"] == 1
    assert len(r["data"]) == 1


# ── Data rows ─────────────────────────────────────────────────────────────────

def test_data_rows_are_dicts_keyed_by_columns(result):
    for row in result["data"]:
        assert isinstance(row, dict)
        assert set(row.keys()) == {"id", "name", "value"}


def test_data_row_values(result):
    assert result["data"][0]["id"] == "1"
    assert result["data"][0]["name"] == "alpha"
    assert result["data"][1]["name"] == "beta"
