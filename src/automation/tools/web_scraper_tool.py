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

_MAX_RESPONSE_BYTES = 10 * 1024 * 1024  # 10 MB


class ScrapeInput(BaseModel):
    url: str


class WebScraperTool(BaseTool):
    name: str = "web_scraper"
    description: str = (
        "Fetch a web page and return its full structured content: title, meta description, "
        "headings, main text, outbound links, and word count. "
        "Use this to extract everything from a page before summarising it."
    )
    args_schema: type[BaseModel] = ScrapeInput

    def _run(self, url: str) -> dict:
        req = urllib.request.Request(url, headers=_HEADERS)
        # The URL comes from the job payload / the agent, so it goes through the
        # SSRF guard (public hosts only, re-checked on every redirect hop).
        try:
            resp = net_guard.urlopen(req, timeout=30)
        except net_guard.BlockedURLError as exc:
            return {"url": url, "error": f"Blocked by SSRF guard: {exc}"}

        content_length = resp.headers.get("Content-Length")
        if content_length and int(content_length) > _MAX_RESPONSE_BYTES:
            return {"url": url, "error": f"Page too large ({content_length} bytes), skipping"}

        html = resp.read(_MAX_RESPONSE_BYTES).decode("utf-8", errors="replace")

        # Title
        title_m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.I)
        title = title_m.group(1).strip() if title_m else "Untitled"

        # Meta description
        meta_m = re.search(
            r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']{1,300})["\']',
            html, re.I,
        ) or re.search(
            r'<meta[^>]+content=["\']([^"\']{1,300})["\'][^>]+name=["\']description["\']',
            html, re.I,
        )
        meta_desc = meta_m.group(1).strip() if meta_m else ""

        # Headings h1–h3
        headings = [
            re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h)).strip()
            for h in re.findall(r"<h[1-3][^>]*>(.*?)</h[1-3]>", html, re.I | re.DOTALL)
            if re.sub(r"<[^>]+>", "", h).strip()
        ][:12]

        # Outbound links
        all_links = re.findall(r'<a[^>]+href=["\']([^"\']+)["\']', html, re.I)
        links = [link for link in all_links if link.startswith("http")][:12]

        # Clean main text
        clean = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.I)
        clean = re.sub(r"<style[^>]*>.*?</style>", " ", clean, flags=re.DOTALL | re.I)
        text = re.sub(r"<[^>]+>", " ", clean)
        text = re.sub(r"\s+", " ", text).strip()
        word_count = len(text.split())

        return {
            "url": url,
            "title": title,
            "meta_description": meta_desc,
            "headings": headings,
            "text": text[:8000],
            "word_count": word_count,
            "links": links,
        }
