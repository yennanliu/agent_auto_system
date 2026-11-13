"""SSRF guard — the one place that decides whether the server may fetch a URL.

Several tools fetch a URL that the server did not choose: a job payload field, an
argument an LLM agent made up, or a link scraped off a third-party page. Fetched
unchecked, each is a read primitive against whatever the server can reach — cloud
metadata (169.254.169.254), a localhost admin port, RFC 1918 hosts — because the
page text flows back into the run result.

Rules (default-secure):

* only ``http`` / ``https``;
* the hostname is **resolved**, and the fetch is refused if *any* resolved
  address is not a public unicast address (loopback, private, link-local,
  CGNAT, multicast, reserved, unspecified, and the IPv4-in-IPv6 embeddings —
  mapped, compatible, 6to4, NAT64, Teredo — of those). Deciding on the resolved
  address rather than on the URL string is what makes ``http://2130706433/``,
  ``http://0x7f.1/``, ``http://0177.0.0.1/`` and ``http://ok.com@127.0.0.1/``
  non-issues: they all come out as 127.0.0.1;
* urllib fetches go through :func:`build_opener` / :func:`urlopen`, whose
  connections connect to *the address that was just validated* (no second DNS
  lookup, so no DNS-rebinding window) and therefore re-validate every redirect
  hop for free — a redirect is just another connection;
* Playwright navigation cannot be pinned (Chromium resolves for itself), so
  :func:`safe_goto` validates before navigating, :func:`guard_page` aborts
  disallowed requests, and the final URL / server address is checked before the
  page content is read. See "Residual gaps" in ``doc/ssrf-guard.md``.

Escape hatches for self-hosters who really do scrape their intranet (read from
the environment on every call):

* ``SSRF_ALLOW_PRIVATE=1`` — turn the address check off (scheme check stays);
* ``SSRF_ALLOW_HOSTS=wiki.corp,*.internal.example,10.20.0.0/16,192.168.1.5`` —
  hostnames (exact or ``*.suffix``), IPs or CIDRs that are allowed even though
  they are not public.

Desktop mode (``DESKTOP_MODE=1``) deliberately gets **no** exemption: the
packaged backend auto-authenticates every loopback request as the admin, so an
agent tricked into fetching ``http://127.0.0.1:<port>/api/...`` would be acting
with full admin rights. A desktop user who wants intranet scraping sets the same
env vars as anyone else.
"""

from __future__ import annotations

import http.client
import ipaddress
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

__all__ = [
    "BlockedURLError", "address_block_reason", "reject_reason", "check_url",
    "resolve_public", "build_opener", "urlopen", "guard_page", "safe_goto",
]

MAX_REDIRECTS = 5
_ALLOWED_SCHEMES = ("http", "https")
# Schemes a browser handles without touching the network — never blocked by the
# Playwright request interceptor.
_BROWSER_LOCAL_SCHEMES = ("data", "blob", "about", "chrome-error", "chrome-extension")


class BlockedURLError(ValueError):
    """A fetch was refused by the SSRF guard.

    A ``ValueError`` (not an ``OSError``) on purpose: urllib wraps every
    ``OSError`` raised while connecting into a generic ``URLError``, which would
    hide the reason from the caller.
    """


# ── configuration ────────────────────────────────────────────────────────────

def _allow_private() -> bool:
    return os.getenv("SSRF_ALLOW_PRIVATE", "").strip().lower() in ("1", "true", "yes", "on")


def _allowlist() -> tuple[list[str], list]:
    """Parse ``SSRF_ALLOW_HOSTS`` into (hostname patterns, ip networks)."""
    names: list[str] = []
    nets: list = []
    for raw in os.getenv("SSRF_ALLOW_HOSTS", "").split(","):
        item = raw.strip().lower().strip("[]")
        if not item:
            continue
        try:
            nets.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            names.append(item.rstrip("."))
    return names, nets


def _host_allowlisted(host: str, names: list[str]) -> bool:
    host = host.lower().rstrip(".")
    for pat in names:
        if pat.startswith("*."):
            if host.endswith(pat[1:]):
                return True
        elif host == pat:
            return True
    return False


# ── address classification ───────────────────────────────────────────────────

# Belt and braces on top of `ipaddress.is_global`, whose table has shifted
# between Python releases. Everything here is non-public by definition.
_BLOCKED_NETS = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8",        # "this network" / unspecified
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",   # RFC 1918
    "100.64.0.0/10",    # CGNAT (also Alibaba metadata 100.100.100.200)
    "127.0.0.0/8",      # loopback
    "169.254.0.0/16",   # link-local — AWS/GCP/Azure metadata 169.254.169.254
    "192.0.0.0/24",     # IETF protocol assignments (Oracle metadata 192.0.0.192)
    "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24",  # documentation
    "198.18.0.0/15",    # benchmarking
    "224.0.0.0/4",      # multicast
    "240.0.0.0/4",      # reserved + broadcast
    "::/128", "::1/128",
    "64:ff9b:1::/48",   # local-use NAT64
    "fc00::/7",         # unique local (AWS IPv6 metadata fd00:ec2::254)
    "fe80::/10",        # link-local
    "fec0::/10",        # deprecated site-local
    "ff00::/8",         # multicast
)]
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_V4_COMPAT = ipaddress.ip_network("::/96")


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    """IPv4 addresses an IPv6 address is a transition-mechanism wrapper for."""
    out: list[ipaddress.IPv4Address] = []
    if ip.ipv4_mapped is not None:                 # ::ffff:a.b.c.d
        out.append(ip.ipv4_mapped)
    if ip.sixtofour is not None:                   # 2002:AABB:CCDD::/48
        out.append(ip.sixtofour)
    if ip.teredo is not None:                      # 2001::/32 (server, client)
        out.extend(ip.teredo)
    if ip in _NAT64 or (ip in _V4_COMPAT and int(ip) > 1):   # 64:ff9b::a.b.c.d, ::a.b.c.d
        out.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return out


def address_block_reason(addr: str) -> str:
    """Why the literal address `addr` is not a public target, or "" if it is.

    Pure classification — ignores the env escape hatches.
    """
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])   # drop an IPv6 zone id
    except ValueError:
        return f"unparseable address {addr!r}"
    if isinstance(ip, ipaddress.IPv6Address):
        for inner in _embedded_ipv4(ip):
            if address_block_reason(str(inner)):
                return f"non-public address {ip} (embeds {inner})"
    if (not ip.is_global or ip.is_multicast or ip.is_unspecified
            or any(ip.version == n.version and ip in n for n in _BLOCKED_NETS)):
        return f"non-public address {ip}"
    return ""


# ── resolution ───────────────────────────────────────────────────────────────

_LEGACY_V4_RE = re.compile(r"^[0-9a-fx.]+$", re.I)


def _resolve(host: str, port: int) -> list[str]:
    """Every address `host` stands for, as strings. Raises OSError if none.

    IP literals — including the legacy inet_aton spellings (decimal
    ``2130706433``, hex ``0x7f.1``, octal ``0177.0.0.1``, short ``127.1``) that
    the C resolver would accept — are decoded here, deterministically, instead
    of being left to a platform-dependent getaddrinfo.
    """
    host = host.strip("[]")
    try:
        return [str(ipaddress.ip_address(host.split("%", 1)[0]))]
    except ValueError:
        pass
    if _LEGACY_V4_RE.match(host):
        try:
            return [socket.inet_ntoa(socket.inet_aton(host))]
        except OSError:
            pass
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addrs: list[str] = []
    for info in infos:
        addr = str(info[4][0])
        if addr not in addrs:
            addrs.append(addr)
    if not addrs:
        raise OSError(f"no addresses for {host}")
    return addrs


def resolve_public(host: str, port: int = 0) -> list[str]:
    """Resolve `host` and return its addresses — only if all of them are allowed.

    Raises :class:`BlockedURLError` when the host does not resolve or when *any*
    address is non-public (a host with one public and one private A record is
    refused outright: which one gets used is the attacker's choice, not ours).
    Callers that connect should connect to one of the returned addresses rather
    than to `host`, so the check and the connection share one DNS answer.
    """
    if not host:
        raise BlockedURLError("no host")
    try:
        addrs = _resolve(host, port)
    except (OSError, UnicodeError) as exc:
        raise BlockedURLError(f"host {host!r} does not resolve ({type(exc).__name__})") from exc
    if _allow_private():
        return addrs
    names, nets = _allowlist()
    if _host_allowlisted(host.strip("[]"), names):
        return addrs
    for addr in addrs:
        reason = address_block_reason(addr)
        if not reason:
            continue
        try:
            ip = ipaddress.ip_address(addr.split("%", 1)[0])
        except ValueError:
            ip = None
        if ip is not None and any(ip.version == n.version and ip in n for n in nets):
            continue
        raise BlockedURLError(f"host {host!r} resolves to the {reason}")
    return addrs


def _split(url: str) -> tuple[str, str, int]:
    """(scheme, host, port) of `url`, or raise BlockedURLError."""
    if not isinstance(url, str) or not url.strip():
        raise BlockedURLError("empty URL")
    # A backslash / whitespace / control char is where urllib's and a browser's
    # URL parsers disagree about which part is the host. Refuse rather than guess.
    if re.search(r"[\\\s\x00-\x1f\x7f]", url.strip()):
        raise BlockedURLError("URL contains a backslash, whitespace or control character")
    try:
        parts = urllib.parse.urlsplit(url.strip())
        port = parts.port
    except ValueError as exc:
        raise BlockedURLError(f"malformed URL ({exc})") from exc
    scheme = (parts.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise BlockedURLError(f"unsupported scheme {scheme!r} (only http/https)")
    if not parts.hostname:
        raise BlockedURLError("no host in URL")
    return scheme, parts.hostname, port or (443 if scheme == "https" else 80)


def check_url(url: str) -> list[str]:
    """Validate `url`; return the resolved addresses or raise BlockedURLError."""
    _scheme, host, port = _split(url)
    return resolve_public(host, port)


def reject_reason(url: str) -> str:
    """Why `url` must not be fetched, or "" if it is a fine public target."""
    try:
        check_url(url)
    except BlockedURLError as exc:
        return str(exc)
    return ""


# ── urllib transport (pinned) ────────────────────────────────────────────────

def _pinned_create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
                              source_address=None, **_kw):
    """`socket.create_connection`, but validate-then-connect on one DNS answer.

    http.client calls this with the host name; we resolve it once, refuse it if
    any address is non-public, and connect to the validated address itself. TLS
    (SNI + certificate check) still uses the host name — that happens in
    HTTPSConnection after this returns.
    """
    host, port = address[0], address[1]
    last_exc: OSError | None = None
    for addr in resolve_public(host, port):
        try:
            return socket.create_connection((addr, port), timeout, source_address)
        except OSError as exc:
            last_exc = exc
    raise last_exc or OSError(f"could not connect to {host}")


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _pinned_create_connection


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _pinned_create_connection


def _via_proxy(req: urllib.request.Request) -> bool:
    return bool(req.has_proxy() or getattr(req, "_tunnel_host", None))


class _GuardedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        if _via_proxy(req):
            # The proxy does the DNS lookup, so there is nothing to pin: check
            # the target by resolving it ourselves and let the proxy connect.
            check_url(req.full_url)
            return self.do_open(http.client.HTTPConnection, req)
        return self.do_open(_PinnedHTTPConnection, req)


class _GuardedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        kwargs = {}
        if getattr(self, "_context", None) is not None:
            kwargs["context"] = self._context
        if _via_proxy(req):
            check_url(req.full_url)
            return self.do_open(http.client.HTTPSConnection, req, **kwargs)
        return self.do_open(_PinnedHTTPSConnection, req, **kwargs)


class _SchemeGuard(urllib.request.BaseHandler):
    """Refuse anything that is not http/https before any handler sees it."""

    handler_order = 0

    def default_open(self, req):
        _split(req.full_url)
        return None


class RedirectGuard(urllib.request.HTTPRedirectHandler):
    """Cap the redirect chain and refuse a hop to a non-http(s) scheme.

    The *address* of each hop is checked where it cannot be bypassed — when the
    connection for that hop is opened (`_pinned_create_connection`).
    """

    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _split(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def build_opener(*handlers, context=None) -> urllib.request.OpenerDirector:
    """An opener that can only reach allowed hosts — on every hop.

    Unlike `urllib.request.build_opener` it carries no file:/ftp:/data: handlers.
    Extra `handlers` (a cookie processor, a custom `RedirectGuard` subclass) are
    added on top.
    """
    opener = urllib.request.OpenerDirector()
    defaults: list = [
        _SchemeGuard(),
        urllib.request.ProxyHandler(),
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.HTTPErrorProcessor(),
        _GuardedHTTPHandler(),
        _GuardedHTTPSHandler(context=context),
    ]
    if not any(isinstance(h, urllib.request.HTTPRedirectHandler) for h in handlers):
        defaults.append(RedirectGuard())
    for handler in (*defaults, *handlers):
        opener.add_handler(handler)
    return opener


def urlopen(url, data=None, timeout: float = 30, *, context=None):
    """Guarded drop-in for `urllib.request.urlopen` (a URL or a Request).

    Raises :class:`BlockedURLError` if the URL — or any redirect hop — is refused.
    """
    return build_opener(context=context).open(url, data, timeout)


# ── Playwright ───────────────────────────────────────────────────────────────

def _request_block_reason(url: str, cache: dict[str, str]) -> str:
    scheme = url.split(":", 1)[0].lower()
    if scheme in _BROWSER_LOCAL_SCHEMES:
        return ""
    try:
        scheme, host, port = _split(url)
    except BlockedURLError as exc:
        return str(exc)
    key = f"{host}:{port}"
    if key not in cache:
        try:
            resolve_public(host, port)
            cache[key] = ""
        except BlockedURLError as exc:
            cache[key] = str(exc)
    return cache[key]


def guard_page(target, blocked: list[str] | None = None) -> None:
    """Abort every request a Playwright page/context makes to a disallowed host.

    Covers the document, sub-resources, iframes and script-initiated navigation.
    Blocked URLs are appended to `blocked` when given. Best-effort by nature:
    Playwright does not call route handlers for the later hops of an HTTP
    redirect, and Chromium resolves names itself — `safe_goto` checks where the
    navigation actually ended up before the content is used.
    """
    cache: dict[str, str] = {}

    def _handler(route, request=None):
        url = (request or route.request).url
        reason = _request_block_reason(url, cache)
        if reason:
            if blocked is not None:
                blocked.append(f"{url}: {reason}")
            route.abort("blockedbyclient")
        else:
            route.continue_()

    target.route("**/*", _handler)


def safe_goto(page, url: str, **kwargs):
    """`page.goto(url)` for a URL the server did not choose.

    Validates before navigating, then validates where the navigation ended up —
    the final URL and, when Chromium reports it, the address the document was
    actually served from. If either is disallowed the page is blanked and
    :class:`BlockedURLError` is raised, so the caller never reads the content.
    """
    check_url(url)
    response = page.goto(url, **kwargs)
    reason = ""
    final = getattr(page, "url", "") or ""
    if isinstance(final, str) and final.split(":", 1)[0].lower() in _ALLOWED_SCHEMES:
        reason = reject_reason(final)
    if not reason and response is not None and not _allow_private():
        try:
            addr = (response.server_addr() or {}).get("ipAddress", "")
        except Exception:  # noqa: BLE001 — not every response knows its peer
            addr = ""
        if isinstance(addr, str) and addr:
            _names, nets = _allowlist()
            why = address_block_reason(addr)
            if why:
                try:
                    ip = ipaddress.ip_address(addr.split("%", 1)[0])
                    listed = any(ip.version == n.version and ip in n for n in nets)
                except ValueError:
                    listed = False
                host = urllib.parse.urlsplit(final).hostname or ""
                if not listed and not _host_allowlisted(host, _names):
                    reason = f"page was served from the {why}"
    if reason:
        try:
            page.goto("about:blank")
        except Exception:  # noqa: BLE001
            pass
        raise BlockedURLError(f"{final or url}: {reason}")
    return response
