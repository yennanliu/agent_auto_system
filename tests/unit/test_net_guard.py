"""SSRF guard (src/automation/net_guard.py).

No real network: DNS is mocked, and the redirect tests talk to a throwaway
HTTP server on loopback that is reached through the allowlist.
"""
import http.server
import ipaddress
import socket
import threading
import urllib.error

import pytest

from src.automation import net_guard as G

PUBLIC = "93.184.216.34"
_REAL_GETADDRINFO = socket.getaddrinfo


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("SSRF_ALLOW_PRIVATE", raising=False)
    monkeypatch.delenv("SSRF_ALLOW_HOSTS", raising=False)
    monkeypatch.delenv("DESKTOP_MODE", raising=False)
    monkeypatch.setenv("no_proxy", "*")   # never route test traffic via a proxy


@pytest.fixture
def dns(monkeypatch):
    """Mock DNS: `dns({"host": "ip" | ["ip", ...]})`. Unknown names fail.

    IP literals fall through to the real getaddrinfo (no network involved) —
    `socket.create_connection` needs it to open a loopback socket.
    """
    lookups: list[str] = []

    def install(table: dict):
        def _getaddrinfo(host, port, *a, **k):
            try:
                ipaddress.ip_address(str(host))
                return _REAL_GETADDRINFO(host, port, *a, **k)
            except ValueError:
                pass
            lookups.append(host)
            if host not in table:
                raise socket.gaierror("mock: unknown host")
            answer = table[host]
            if callable(answer):
                answer = answer()
            addrs = [answer] if isinstance(answer, str) else answer
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port or 0))
                    for a in addrs]
        monkeypatch.setattr(G.socket, "getaddrinfo", _getaddrinfo)
        return lookups
    return install


# ── address classes ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("addr", [
    "127.0.0.1", "127.255.255.254",          # loopback
    "10.0.0.5", "172.16.0.1", "172.31.255.255", "192.168.1.1",   # RFC 1918
    "169.254.169.254",                       # link-local / AWS-GCP-Azure metadata
    "169.254.170.2",                         # ECS task metadata
    "100.64.0.1", "100.100.100.200",         # CGNAT / Alibaba metadata
    "192.0.0.192",                           # Oracle metadata
    "0.0.0.0", "0.1.2.3",                    # unspecified / "this network"
    "224.0.0.1", "239.255.255.250",          # multicast
    "240.0.0.1", "255.255.255.255",          # reserved / broadcast
    "198.18.0.1", "192.0.2.10", "203.0.113.7",   # benchmarking / documentation
    "::", "::1",                             # unspecified / loopback
    "fe80::1", "fe80::1%eth0",               # link-local (with zone id)
    "fc00::1", "fd00:ec2::254",              # unique local / AWS IPv6 metadata
    "fec0::1",                               # deprecated site-local
    "ff02::1",                               # multicast
    "::ffff:127.0.0.1", "::ffff:10.0.0.1", "::ffff:169.254.169.254",   # v4-mapped
    "::127.0.0.1", "::a00:1",                # v4-compatible
    "2002:7f00:1::", "2002:a9fe:a9fe::",     # 6to4 of 127.0.0.1 / 169.254.169.254
    "64:ff9b::7f00:1", "64:ff9b::a9fe:a9fe",  # NAT64 of the same
    "64:ff9b:1::1",                          # local-use NAT64
    "2001:0:4136:e378:8000:63bf:f5ff:fffe",  # Teredo wrapping 10.0.0.1
    "not-an-ip",
])
def test_non_public_addresses_are_blocked(addr):
    assert G.address_block_reason(addr)


@pytest.mark.parametrize("addr", [
    PUBLIC, "8.8.8.8", "1.1.1.1", "140.112.8.116",
    "2606:4700:4700::1111", "2404:6800:4008:c07::65",
    "::ffff:8.8.8.8", "64:ff9b::808:808",   # wrappers of 8.8.8.8
])
def test_public_addresses_are_allowed(addr):
    assert G.address_block_reason(addr) == ""


# ── literal spellings & URL tricks (decided by what the host resolves to) ────

@pytest.mark.parametrize("url", [
    "http://127.0.0.1/",
    "http://2130706433/",            # decimal 127.0.0.1
    "http://0x7f000001/",            # hex
    "http://0x7f.0.0.1/",            # hex octet
    "http://0177.0.0.1/",            # octal
    "http://017700000001/",          # octal, single part
    "http://127.1/",                 # short form
    "http://0/",                     # 0.0.0.0
    "http://2852039166/latest/meta-data/",   # decimal 169.254.169.254
    "http://0xa9fea9fe/",            # hex 169.254.169.254
    "http://[::1]:8000/",
    "http://[::ffff:127.0.0.1]/",
    "http://[::ffff:7f00:1]/",
    "http://[0:0:0:0:0:ffff:a9fe:a9fe]/",
    "http://public.example@127.0.0.1/",          # userinfo trick
    "http://public.example:80@10.0.0.1:8080/",
    "https://user:pw@[fd00:ec2::254]/",
])
def test_ip_literal_spellings_are_blocked(url, dns):
    lookups = dns({})
    assert "non-public address" in G.reject_reason(url)
    assert lookups == [], "an IP literal must be decided without a DNS lookup"


@pytest.mark.parametrize("url,why", [
    ("file:///etc/passwd", "scheme"),
    ("ftp://example.com/x", "scheme"),
    ("gopher://example.com/", "scheme"),
    ("data:text/plain,hi", "scheme"),
    ("javascript:alert(1)", "scheme"),
    ("//example.com/x", "scheme"),
    ("example.com", "scheme"),
    ("http:///nohost", "no host"),
    ("", "empty"),
    ("http://127.0.0.1\\@public.example/", "backslash"),   # parser-differential
    ("http://public.example/\nHost: evil", "backslash"),
    ("http://example.com:99999/", "malformed"),
])
def test_malformed_and_non_http_urls_are_blocked(url, why, dns):
    dns({"example.com": PUBLIC, "public.example": PUBLIC})
    assert why in G.reject_reason(url)


@pytest.mark.parametrize("answer", [
    "127.0.0.1", "10.1.2.3", "169.254.169.254", "100.64.0.9", "::1",
    "::ffff:192.168.0.1", "fe80::1%en0",
    [PUBLIC, "10.0.0.1"],            # one bad record poisons the host
    ["2606:4700:4700::1111", "::1"],
])
def test_hostname_resolving_to_non_public_is_blocked(answer, dns):
    dns({"sneaky.example": answer})
    with pytest.raises(G.BlockedURLError, match="non-public address"):
        G.check_url("https://sneaky.example/path")


def test_public_hostname_is_allowed(dns):
    dns({"www.example.com": [PUBLIC, "2606:4700:4700::1111"]})
    assert G.reject_reason("https://www.example.com/a?b=c") == ""
    assert G.check_url("http://www.example.com:8080/") == [PUBLIC, "2606:4700:4700::1111"]


def test_unresolvable_host_is_blocked(dns):
    dns({})
    assert "does not resolve" in G.reject_reason("https://nope.invalid/")


# ── escape hatches ───────────────────────────────────────────────────────────

def test_allow_private_env_disables_the_address_check_only(monkeypatch, dns):
    dns({"wiki.corp": "10.0.0.5"})
    assert G.reject_reason("http://wiki.corp/")
    monkeypatch.setenv("SSRF_ALLOW_PRIVATE", "1")
    assert G.reject_reason("http://wiki.corp/") == ""
    assert G.reject_reason("http://127.0.0.1:9000/") == ""
    assert "scheme" in G.reject_reason("file:///etc/passwd")


def test_allowlist_by_hostname_wildcard_ip_and_cidr(monkeypatch, dns):
    dns({"wiki.corp": "10.0.0.5", "wiki.corp.": "10.0.0.5", "a.internal.example": "172.16.3.4",
         "internal.example": "172.16.3.5", "other.corp": "10.9.9.9",
         "nas.lan": "192.168.1.5", "in-range.lan": "10.20.7.7",
         "evilinternal.example": "172.16.3.6"})
    monkeypatch.setenv(
        "SSRF_ALLOW_HOSTS", " wiki.corp, *.internal.example ,192.168.1.5,10.20.0.0/16,")
    assert G.reject_reason("http://wiki.corp/") == ""             # exact host
    assert G.reject_reason("http://WIKI.corp./") == ""            # case / trailing dot
    assert G.reject_reason("http://a.internal.example/") == ""    # wildcard
    assert G.reject_reason("http://nas.lan/") == ""               # resolved IP listed
    assert G.reject_reason("http://192.168.1.5/") == ""
    assert G.reject_reason("http://in-range.lan/") == ""          # resolved IP in CIDR
    assert G.reject_reason("http://10.20.255.1/") == ""
    # ...and nothing else leaks through.
    assert G.reject_reason("http://other.corp/")
    assert G.reject_reason("http://internal.example/")            # "*." needs a label
    assert G.reject_reason("http://evilinternal.example/")
    assert G.reject_reason("http://192.168.1.6/")
    assert G.reject_reason("http://127.0.0.1/")
    assert G.reject_reason("http://169.254.169.254/")


def test_desktop_mode_gets_no_exemption(monkeypatch):
    """The desktop backend auto-authenticates loopback callers as admin, so the
    guard must stay on there by default."""
    monkeypatch.setenv("DESKTOP_MODE", "1")
    assert G.reject_reason("http://127.0.0.1:8000/api/jobs")
    assert G.reject_reason("http://192.168.0.1/")


# ── urllib transport: redirects + pinning ────────────────────────────────────

@pytest.fixture
def server():
    """Loopback HTTP server. `/redir?<url>` 302s to <url>; anything else is 200."""
    hits: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(f"{self.headers.get('Host')}{self.path}")
            if self.path.startswith("/redir?"):
                self.send_response(302)
                self.send_header("Location", self.path.split("?", 1)[1])
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = b"hello from " + self.path.encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    srv.hits = hits
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


def test_guarded_urlopen_fetches_an_allowed_host(monkeypatch, dns, server):
    dns({"allowed.test": "127.0.0.1"})
    monkeypatch.setenv("SSRF_ALLOW_HOSTS", "allowed.test")
    port = server.server_port
    with G.urlopen(f"http://allowed.test:{port}/page", timeout=5) as resp:
        assert resp.read() == b"hello from /page"
    assert server.hits == [f"allowed.test:{port}/page"]   # Host header keeps the name


def test_redirect_to_a_private_host_is_blocked(monkeypatch, dns, server):
    """An allowed host that 302s inward must not get the second request made."""
    dns({"allowed.test": "127.0.0.1", "internal.test": "127.0.0.1"})
    monkeypatch.setenv("SSRF_ALLOW_HOSTS", "allowed.test")
    port = server.server_port
    with pytest.raises(G.BlockedURLError, match="internal.test"):
        G.urlopen(f"http://allowed.test:{port}/redir?http://internal.test:{port}/secret",
                  timeout=5)
    assert not any(h.split("/", 1)[1] == "secret" for h in server.hits)


@pytest.mark.parametrize("target", [
    "http://169.254.169.254/latest/meta-data/",
    "http://2130706433:{port}/secret",
    "http://[::ffff:127.0.0.1]:{port}/secret",
])
def test_redirect_to_a_private_literal_is_blocked(monkeypatch, dns, server, target):
    dns({"allowed.test": "127.0.0.1"})
    monkeypatch.setenv("SSRF_ALLOW_HOSTS", "allowed.test")
    port = server.server_port
    with pytest.raises(G.BlockedURLError, match="non-public address"):
        G.urlopen(f"http://allowed.test:{port}/redir?{target.format(port=port)}", timeout=5)
    assert not any(h.split("/", 1)[1] == "secret" for h in server.hits)


@pytest.mark.parametrize("target", ["file:///etc/passwd", "ftp://allowed.test/x"])
def test_redirect_to_a_non_http_scheme_is_blocked(monkeypatch, dns, server, target):
    dns({"allowed.test": "127.0.0.1"})
    monkeypatch.setenv("SSRF_ALLOW_HOSTS", "allowed.test")
    # file: is refused by urllib itself (HTTPError), ftp: by the guard.
    with pytest.raises((G.BlockedURLError, urllib.error.HTTPError)):
        G.urlopen(f"http://allowed.test:{server.server_port}/redir?{target}", timeout=5)
    assert len(server.hits) == 1


def test_redirect_chain_is_capped(monkeypatch, dns, server):
    dns({"allowed.test": "127.0.0.1"})
    monkeypatch.setenv("SSRF_ALLOW_HOSTS", "allowed.test")
    base = f"http://allowed.test:{server.server_port}"
    url = f"{base}/end"
    for _ in range(G.MAX_REDIRECTS + 3):
        url = f"{base}/redir?{url}"
    with pytest.raises(Exception) as exc:   # urllib's HTTPError for a redirect loop
        G.urlopen(url, timeout=5)
    assert not isinstance(exc.value, G.BlockedURLError)
    assert not any(h.split("/", 1)[1] == "end" for h in server.hits)
    assert len(server.hits) <= G.MAX_REDIRECTS + 1


def test_opener_refuses_non_http_schemes_outright():
    for url in ("file:///etc/passwd", "ftp://example.com/x", "data:text/plain,hi"):
        with pytest.raises(G.BlockedURLError):
            G.urlopen(url, timeout=1)


def test_connection_is_pinned_to_the_validated_address(monkeypatch, dns):
    """DNS rebinding: the name answers public once, then 127.0.0.1.

    The guard must resolve once and connect to the address it validated — never
    hand the *name* back to the socket layer for a second, different answer.
    """
    answers = iter([PUBLIC, "127.0.0.1", "127.0.0.1"])
    lookups = dns({"rebind.example": lambda: next(answers)})
    connects: list[tuple] = []

    def _create_connection(address, *a, **k):
        connects.append(address)
        raise ConnectionRefusedError("mock: no network in tests")

    monkeypatch.setattr(G.socket, "create_connection", _create_connection)
    with pytest.raises(OSError):
        G.urlopen("http://rebind.example/", timeout=1)
    assert lookups == ["rebind.example"]
    assert connects == [(PUBLIC, 80)]


def test_connect_time_check_blocks_even_without_a_precheck(monkeypatch, dns):
    dns({"sneaky.example": "10.0.0.1"})
    monkeypatch.setattr(G.socket, "create_connection",
                        lambda *a, **k: pytest.fail("must not connect"))
    with pytest.raises(G.BlockedURLError):
        G.build_opener().open("https://sneaky.example/", timeout=1)


# ── tools surface a block as a result, not an exception ──────────────────────

def test_web_scraper_tool_reports_a_blocked_url():
    from src.automation.tools.web_scraper_tool import WebScraperTool
    out = WebScraperTool()._run(url="http://169.254.169.254/latest/meta-data/")
    assert out["error"].startswith("Blocked by SSRF guard")
    assert "text" not in out


def test_google_sheet_tool_reports_a_blocked_url():
    from src.automation.tools.google_sheet_tool import GoogleSheetTool
    out = GoogleSheetTool()._run(url="http://127.0.0.1:8000/api/jobs?format=csv")
    assert out["error"].startswith("Blocked by SSRF guard")
    assert out["data"] == [] and out["row_count"] == 0


def test_form_inspector_reports_a_blocked_url():
    from src.automation.tools.google_form_tools import GoogleFormInspectorTool
    out = GoogleFormInspectorTool()._run(url="http://10.0.0.1/forms/d/e/abc/viewform")
    assert out["error"].startswith("Blocked by SSRF guard")
    assert out["questions"] == []


def test_playwright_form_tool_blocks_before_launching_a_browser(mocker):
    from src.automation.tools import playwright_form_tool as m
    launch = mocker.patch.object(m, "sync_playwright")
    out = m.PlaywrightFormTool()._run(url="http://192.168.0.10/form")
    assert out["submitted"] is False and "SSRF guard" in out["error"]
    launch.assert_not_called()


def test_email_extract_skips_a_private_website(mocker):
    from src.automation.tools import email_extract_tool as m
    mocker.patch.object(G.socket, "create_connection",
                        side_effect=AssertionError("must not connect"))
    assert m._fetch("http://127.0.0.1:8000/admin") is None
    r = m.extract_emails("http://10.0.0.7")
    assert r["pages_scanned"] == 0


def test_smtp_probe_never_dials_a_private_mx(mocker, dns):
    from src.automation.tools import email_verify_tool as m
    dns({"mx.evil.example": "127.0.0.1", "mx.ok.example": PUBLIC})
    smtp = mocker.patch.object(m.smtplib, "SMTP")
    assert m._smtp_probe("mx.evil.example", "a@evil.example") == "unknown"
    smtp.assert_not_called()
    smtp.return_value.__enter__.return_value.rcpt.return_value = (250, b"ok")
    assert m._smtp_probe("mx.ok.example", "a@ok.example") == "accepted"
    assert smtp.call_args.args[:2] == (PUBLIC, 25)   # the checked address, not the name


# ── Playwright helpers ───────────────────────────────────────────────────────

class _Route:
    def __init__(self, url):
        self.request = type("Req", (), {"url": url})()
        self.outcome = None

    def abort(self, code=None):
        self.outcome = f"abort:{code}"

    def continue_(self):
        self.outcome = "continue"


class _Page:
    def __init__(self, final_url=None, server_ip=None):
        self.handler = None
        self.url = "about:blank"
        self._final, self._ip = final_url, server_ip
        self.visited: list[str] = []

    def route(self, _pattern, handler):
        self.handler = handler

    def goto(self, url, **_kw):
        self.visited.append(url)
        self.url = url if url == "about:blank" else (self._final or url)
        ip = self._ip
        return type("Resp", (), {"server_addr": lambda _s: {"ipAddress": ip, "port": 80}})()


def test_guard_page_aborts_requests_to_non_public_hosts(dns):
    dns({"cdn.example": PUBLIC, "intranet.example": "10.0.0.8"})
    page, blocked = _Page(), []
    G.guard_page(page, blocked)

    def outcome(url):
        route = _Route(url)
        page.handler(route)
        return route.outcome

    assert outcome("https://cdn.example/app.js") == "continue"
    assert outcome("data:image/png;base64,AAAA") == "continue"
    assert outcome("about:blank") == "continue"
    assert outcome("http://intranet.example/x") == "abort:blockedbyclient"
    assert outcome("http://169.254.169.254/latest/") == "abort:blockedbyclient"
    assert outcome("http://[::1]:9222/json") == "abort:blockedbyclient"
    assert outcome("file:///etc/passwd") == "abort:blockedbyclient"
    assert len(blocked) == 4 and "intranet.example" in blocked[0]


def test_safe_goto_refuses_a_private_url_without_navigating():
    page = _Page()
    with pytest.raises(G.BlockedURLError):
        G.safe_goto(page, "http://127.0.0.1:8000/")
    assert page.visited == []


def test_safe_goto_rejects_a_navigation_that_ended_somewhere_private(dns):
    """Chromium followed a redirect the route handler never saw."""
    dns({"shop.example": PUBLIC})
    page = _Page(final_url="http://192.168.1.1/admin", server_ip="192.168.1.1")
    with pytest.raises(G.BlockedURLError, match="192.168.1.1"):
        G.safe_goto(page, "https://shop.example/")
    assert page.visited == ["https://shop.example/", "about:blank"]   # page blanked


def test_safe_goto_rejects_a_document_served_from_a_private_address(dns):
    """DNS rebinding in the browser: the name checked out, the peer did not."""
    dns({"rebind.example": PUBLIC})
    page = _Page(server_ip="127.0.0.1")
    with pytest.raises(G.BlockedURLError, match="served from"):
        G.safe_goto(page, "http://rebind.example/")


def test_safe_goto_allows_a_public_page(dns):
    dns({"shop.example": PUBLIC})
    page = _Page(server_ip=PUBLIC)
    assert G.safe_goto(page, "https://shop.example/", timeout=1) is not None
    assert page.visited == ["https://shop.example/"]
