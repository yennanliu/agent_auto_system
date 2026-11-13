# SSRF guard

Several automations fetch a URL the server did not choose — a job-payload field, an
argument an LLM agent invented, or a link scraped off a third-party page. Unchecked, each
is a read primitive against whatever the server can reach (cloud metadata at
`169.254.169.254`, localhost admin ports, RFC 1918 hosts), because the fetched text flows
back into the run result. `src/automation/net_guard.py` is the single place that decides
whether such a fetch may happen.

## Rules (default-secure)

- Only `http` / `https`.
- The hostname is **resolved**; the fetch is refused if *any* resolved address is not a
  public unicast address: loopback, RFC 1918, link-local (incl. metadata endpoints), CGNAT,
  multicast, reserved, unspecified, unique-local, and IPv4-mapped / IPv4-compatible / 6to4 /
  NAT64 / Teredo wrappers of those. The decision is made on the address, never on the URL
  string — so `http://2130706433/`, `http://0x7f.1/`, `http://0177.0.0.1/` and
  `http://ok.com@127.0.0.1/` are all just 127.0.0.1.
- urllib fetches use `net_guard.urlopen()` / `net_guard.build_opener()`. Their connections
  resolve once and connect to **the address that was validated** (TLS still verifies the
  host name), so there is no DNS-rebinding window, and every redirect hop is re-validated
  because every hop is a new connection. Redirect chains are capped at 5; `file:`/`ftp:`/
  `data:` handlers are not installed at all.
- A blocked fetch is reported in the tool result (`"error": "Blocked by SSRF guard: …"`, a
  crawler warning, or a skipped page) — it never raises out of a run.

## Configuration

| Env var | Effect |
|---|---|
| `SSRF_ALLOW_PRIVATE=1` | Turn the address check off entirely (scheme check stays). For a trusted single-tenant install that scrapes its own intranet. |
| `SSRF_ALLOW_HOSTS=wiki.corp,*.internal.example,10.20.0.0/16,192.168.1.5` | Allow specific targets only: hostnames (exact or `*.suffix`), IPs or CIDRs. Prefer this over `SSRF_ALLOW_PRIVATE`. |

Both are read on every call (no restart-order pitfalls) and default to off/empty.

**Desktop mode gets no exemption.** With `DESKTOP_MODE=1` the backend auto-authenticates
every loopback request as the admin, so a page that talks an agent into fetching
`http://127.0.0.1:<port>/api/...` would act with full admin rights; the user's router and
LAN devices are equally in reach. A desktop user who wants intranet scraping sets the same
env vars as anyone else.

## Fetch-site audit

| Site | URL comes from | Status |
|---|---|---|
| `web_scraper_tool.py` | payload / agent | **guarded** (pinned urllib) |
| `google_sheet_tool.py` | payload ("or a direct CSV URL") | **guarded** (pinned urllib) |
| `google_form_tools.py` — inspector `_fetch_html` | payload / agent | **guarded** (pinned urllib) |
| `google_form_tools.py` — submit | fixed `docs.google.com` + form id | fixed host |
| `playwright_form_tool.py` | payload / agent | **guarded** (pre-check + request interception + post-navigation check) |
| `email_extract_tool.py` — `_fetch` (homepage + discovered contact links) | Maps / registry / payload / scraped links | **guarded** (pinned urllib) |
| `email_extract_tool.py` — `_render_and_harvest` | same | **guarded** (Playwright, as above) |
| `tw_association_tool.py` — crawler + hop following | payload + scraped links | **guarded** (already had a per-hop check; now shares the module and is pinned) |
| `email_verify_tool.py` — SMTP probe to the MX | DNS of a scraped e-mail domain | **guarded** (public MX only, dials the checked address) |
| `email_verify_tool.py` — MX / A lookup | scraped domain | DNS query only, no connection |
| `maps_search_tool.py` | fixed `google.com/maps` search; place links read from Google's own results DOM | fixed origin (see gaps) |
| `tw104_apply_tool.py` — job pages | fixed `104.com.tw` search; job links read from 104's own results DOM | fixed origin (see gaps) |
| `x_scraper_tool.py`, `x_profile_contact_tool.py` — nitter | operator's `NITTER_INSTANCES` + handle as path | operator-configured host (see gaps) |
| `x_*`, `instagram_contact_tool.py`, `facebook_contact_tool.py` — profile pages | fixed host + handle as path | fixed host |
| `hn_tool.py`, `moea_gcis_tool.py`, `linkedin_apply_tool.py` (typeahead) | constants | fixed host |
| `greenhouse_apply_tool.py` | fixed Greenhouse API / board hosts + token/id in path | fixed host |
| `shopee_scraper_tool.py`, `tw104_apply_tool.py` (search), `linkedin_apply_tool.py`, `tasker_apply_tool.py`, `browser_session.py` | fixed site base + ids/keywords | fixed host |

There is no webhook-style outbound fetch in `src/` today. LLM-provider, Langfuse and OAuth
endpoints are operator configuration, not request data.

## Residual gaps

- **Playwright cannot be pinned.** Chromium does its own DNS, and Playwright does not call
  route handlers for the later hops of an HTTP redirect. So a page on a public host can
  still make the browser *send* a GET to an internal address (via a 30x, or by rebinding
  DNS between our check and Chromium's lookup). What the guard does guarantee: the URL is
  validated before navigation; first-hop requests of every kind (documents, iframes,
  XHR/fetch, sub-resources) to non-public hosts are aborted; and after navigation the
  final URL and the address the document was served from are checked — if either is
  non-public the page is blanked and nothing is read. Net: blind request possible, content
  read-back blocked for the main document. Run browser automations in a network-isolated
  container if blind requests matter to you.
- **Behind an HTTP(S) proxy** (`HTTP_PROXY`/`HTTPS_PROXY`), the proxy resolves names, so
  the connection cannot be pinned; the guard still resolves and checks every hop itself,
  but a rebinding window exists between that check and the proxy's lookup.
- **Nitter instances** are operator-chosen hosts and are fetched with plain urllib: a
  hostile third-party instance could redirect the request inward. Only nitter-shaped HTML
  is parsed from the response.
- **Google Maps place links and 104 job links** are navigated as read from that site's own
  results page, on the assumption that Google's / 104's listing DOM only links to itself.
  They are not passed through the guard.
- **Hosts with mixed public/private records** are refused outright (stricter than strictly
  necessary); 6to4 addresses are always refused.
- No port restriction: any port on a public host is allowed.
