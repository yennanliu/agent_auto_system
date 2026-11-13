"""Unit tests for WebScraperTool — verifies structured extraction without network."""
from unittest.mock import MagicMock, patch

import pytest

from src.automation.tools.web_scraper_tool import WebScraperTool

# Minimal HTML page used across tests
SAMPLE_HTML = """<!DOCTYPE html>
<html>
<head>
  <title>Test Page Title</title>
  <meta name="description" content="A sample page for unit testing." />
</head>
<body>
  <h1>Main Heading</h1>
  <h2>Sub Heading One</h2>
  <h3>Sub Heading Two</h3>
  <p>This is the main content of the page. It has several words.</p>
  <script>var x = 1;</script>
  <style>.foo { color: red; }</style>
  <a href="https://external.com/page1">Link One</a>
  <a href="/relative">Relative link (should be skipped)</a>
  <a href="https://external.com/page2">Link Two</a>
</body>
</html>"""


def _mock_urlopen(html: str):
    mock_resp = MagicMock()
    mock_resp.read.return_value = html.encode("utf-8")
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


@pytest.fixture
def scraper():
    return WebScraperTool()


@pytest.fixture
def result(scraper):
    with patch("src.automation.tools.web_scraper_tool.net_guard.urlopen",
               return_value=_mock_urlopen(SAMPLE_HTML)):
        return scraper._run(url="https://test.example.com")


# ── Return shape ──────────────────────────────────────────────────────────────

def test_result_has_all_required_keys(result):
    assert set(result.keys()) >= {"url", "title", "meta_description", "headings",
                                   "text", "word_count", "links"}


def test_url_is_echoed_back(result):
    assert result["url"] == "https://test.example.com"


# ── Title ─────────────────────────────────────────────────────────────────────

def test_title_extracted(result):
    assert result["title"] == "Test Page Title"


def test_title_fallback_on_missing(scraper):
    html = "<html><body><p>No title here</p></body></html>"
    with patch("src.automation.tools.web_scraper_tool.net_guard.urlopen",
               return_value=_mock_urlopen(html)):
        r = scraper._run(url="https://example.com")
    assert r["title"] == "Untitled"


# ── Meta description ──────────────────────────────────────────────────────────

def test_meta_description_extracted(result):
    assert result["meta_description"] == "A sample page for unit testing."


def test_meta_description_empty_when_absent(scraper):
    html = "<html><head><title>X</title></head><body>hi</body></html>"
    with patch("src.automation.tools.web_scraper_tool.net_guard.urlopen",
               return_value=_mock_urlopen(html)):
        r = scraper._run(url="https://example.com")
    assert r["meta_description"] == ""


# ── Headings ──────────────────────────────────────────────────────────────────

def test_headings_list_not_empty(result):
    assert len(result["headings"]) > 0


def test_headings_contain_h1(result):
    assert "Main Heading" in result["headings"]


def test_headings_contain_h2_and_h3(result):
    assert any("Sub Heading One" in h for h in result["headings"])
    assert any("Sub Heading Two" in h for h in result["headings"])


# ── Text extraction ───────────────────────────────────────────────────────────

def test_text_is_not_empty(result):
    assert len(result["text"]) > 0


def test_scripts_stripped_from_text(result):
    assert "var x = 1" not in result["text"]


def test_style_stripped_from_text(result):
    assert ".foo" not in result["text"]


def test_text_truncated_to_8000_chars(scraper):
    long_body = "word " * 2000  # ~10 000 chars
    html = f"<html><head><title>T</title></head><body><p>{long_body}</p></body></html>"
    with patch("src.automation.tools.web_scraper_tool.net_guard.urlopen",
               return_value=_mock_urlopen(html)):
        r = scraper._run(url="https://example.com")
    assert len(r["text"]) <= 8000


# ── Word count ────────────────────────────────────────────────────────────────

def test_word_count_is_positive_int(result):
    assert isinstance(result["word_count"], int)
    assert result["word_count"] > 0


# ── Links ─────────────────────────────────────────────────────────────────────

def test_only_absolute_links_returned(result):
    for link in result["links"]:
        assert link.startswith("http"), f"Relative link leaked: {link}"


def test_known_links_present(result):
    assert "https://external.com/page1" in result["links"]
    assert "https://external.com/page2" in result["links"]


def test_relative_link_excluded(result):
    assert "/relative" not in result["links"]
