# Auto-Collect SMB / Business Contact Emails

Goal: automatically collect SMB / business-owner / company contact emails
(TW, US, EU, Asia…) for cold outreach of an AI-agent proposal — **without buying
from existing databases or paid contact services.**

## The core insight

Don't try to "find emails" directly. Run a **funnel**: discover businesses →
find their website → extract email → verify → dedupe. Each stage is cheap, and
the highest-ROI free source for SMBs is **Google Maps + the business's own
website**, not any contact database.

```
DISCOVER          →  ENRICH           →  EXTRACT         →  VERIFY        →  STORE
"AI agency in TW"    business website    scrape mailto:,    MX + SMTP       dedupe,
Maps/directory       from listing        /contact, footer   check (free)    rank quality
listings
```

## Free sources, ranked by ROI

| Source | Yield | Notes |
|---|---|---|
| **Google Maps / Places** | ★★★★★ | Best for SMBs. Listing → website → scrape email. Free tier is generous; or scrape Maps directly with Playwright. |
| **Company website crawl** | ★★★★★ | 80% of the value. Grab `mailto:`, `/contact`, `/about`, footer. Regex + DOM. |
| **Directories** | ★★★★ | Yelp, Yellow Pages, **local chambers of commerce**, industry association member lists. Chamber sites often list email openly. |
| **Facebook / IG business pages** | ★★★ | SMBs frequently expose email/phone in the "About" contact block. |
| **Public registries** | ★★ | TW 經濟部商業司, US state SoS, UK Companies House. Great for *company identity*, rarely have email. |
| **Email pattern + verify** | ★★★ | Guess `info@`, `contact@`, `hello@`, `sales@domain` then verify. Works surprisingly well for SMBs. |
| **WHOIS** | ★ | Mostly GDPR-redacted now. Skip. |

**Avoid**: LinkedIn scraping (ToS + aggressive anti-bot, legal risk) and anything
that guesses *personal* named inboxes at scale — stick to role/company addresses.

## Email verification without a paid service

Validate ~90% for free before ever sending:

1. **Syntax** regex
2. **MX record** lookup (`dnspython`) — does the domain accept mail?
3. **SMTP handshake** — connect, `RCPT TO`, read the response *without* sending.
   Catches dead mailboxes. (Rate-limit it; many servers greylist.)
4. Score/rank so you send to `info@` (verified) before a guessed address.

## How it drops into the architecture

Textbook new job type — the 5-file touch (see `CLAUDE.md`):

1. `executor.py` → `_FLOW_MAP["email_collect"]`
2. `flows/email_collect_flow.py` → `Flow[EmailCollectState]` (state holds `query`,
   `region`, `industry`, `max_results`, `llm_provider/model`)
3. `crews/email_collect_crew/` → a small agent crew:
   - **Discovery agent** (calls a Maps/search tool)
   - **Extractor agent** (Playwright tool — already used for `form_fill`)
   - **Verifier** — plain Python tool, no LLM needed (MX/SMTP)
   - **Qualifier agent** — LLM decides "is this an SMB that fits my ICP?" and
     drafts a one-line personalization hook
4. `routers/system.py` → `_CATALOG`
5. `ui/app.js` → form (query, region, industry, count)

New tools under the tools layer: `maps_search`, `web_email_extract` (Playwright),
`email_verify` (dnspython + smtplib). Output → a `leads` table (or the run
payload), with columns: company, website, email, source, confidence, region,
personalization_hook.

The **qualifier + personalization** stage is where the LLM harness earns its
keep — turning a raw email into "worth contacting + here's the angle."

## ⚠️ Compliance — build in from day one, not bolted on

Cold *B2B* outreach is legal in most places, but the rules differ and matter for
deliverability too:

- **EU (GDPR / ePrivacy)**: role addresses (`info@`) safer than personal ones;
  need a legitimate-interest basis, must identify yourself, honor opt-out.
- **US (CAN-SPAM)**: opt-out link, real physical address, no deceptive subject.
  Most permissive.
- **Canada (CASL)**: strictest — arguably needs consent. Consider excluding.
- **TW / Asia (PDPA)**: business contact for B2B generally OK with opt-out.

Guardrails to bake into the crew: **prefer role addresses**, store the `source`
for every email (provenance), always include unsubscribe + identity in the send
step, keep a **suppression list**, throttle volume, respect `robots.txt`.

## Recommended MVP

`maps_search` (one region/industry) → Playwright email extract → MX/SMTP verify →
LLM qualifier with personalization hook → store. Ship that one vertical slice,
then add directory / Facebook sources.

---

## Implementation (shipped — `email_collect` job type)

The Google Maps funnel is built. The funnel's discover → extract → verify → dedupe
stages run **deterministically in the flow** (fast, cheap, reliable); only the
final ICP-fit + personalization-hook stage uses the LLM — mirroring the
`tasker_apply` architecture.

**Files** (the 5-file job-type touch + 3 tools + 1 crew):

| File | Role |
|---|---|
| `src/automation/tools/maps_search_tool.py` | Stage 1 — Playwright scrape of Google Maps: name, website, phone, address, category. Also `resolve_websites()`, the name→website lookup that makes website-less sources usable |
| `src/automation/tools/tw_association_tool.py` | Stage 1 (TW) — 公會/工會 member directories: a built-in TCA adapter plus a generic crawler for any member-list URL |
| `src/automation/tools/moea_gcis_tool.py` | Stage 1 (TW) + enrichment — 經濟部 商工登記公示資料 via the 商工行政資料開放平臺 API: keyword / 營業項目 search, and per-company 統編 / 資本額 / 負責人 lookup |
| `src/automation/tools/email_extract_tool.py` | Stage 2 — urllib fetch of homepage + contact/about/impressum pages; mailto+text emails; junk filter; role-address ranking; single `info@` guess (never on social hosts) |
| `src/automation/tools/email_verify_tool.py` | Stage 3 — syntax → MX (dnspython, A-record fallback) → best-effort SMTP RCPT probe (no send); high/medium/low confidence |
| `src/automation/flows/email_collect_flow.py` | `Flow[EmailCollectState]` — drives the funnel + dedupe, then the qualifier crew |
| `src/automation/crews/email_collect_crew/` | LLM qualifier (ICP fit 1-5 + hook), no tools |
| `executor.py` · `validator.py` · `evaluator.py` · `routers/system.py` · `ui/*` | wiring |

**Dependency added:** `dnspython` (MX lookups).

**Inputs:** `query` (required), `region`, `industry`, `offer`, `limit` (1–500),
`smtp_check`, `include_social`, plus the multi-source controls: `sources`
(`maps` · `association` · `govbiz`, default `["maps"]`), `associations`
(公會 slugs / member-list URLs), `resolve_missing_websites`, `gcis_enrich`.
**Result JSON:** `sources`, `discovered_count`, `with_website`, `lead_count`,
`leads[]` (company, email, website, category, phone, address, source,
`discovery`, confidence, mx_found, smtp_status, icp_fit, reason, hook, and —
with `gcis_enrich` — tax_id, responsible, capital, setup_date), plus
`businesses[]` (all discovered) and non-fatal `warnings[]`.

### Multi-source discovery (shipped)

Stage 1 is now pluggable. Every enabled source is asked for the full `limit`,
results are **merged and deduped** on registrable domain (falling back to a
normalized company name), then **interleaved round-robin** so one source can't
consume the whole budget. A company found twice becomes one business carrying
whatever each source knew — Maps supplies the `maps_url`, the 公會 row the
業務類型, the registry the 統一編號.

| Source | What it is | Gives a website? |
|---|---|---|
| `maps` | Google Maps search (default; the only worldwide source) | usually |
| `association` | 公會/工會 member directories. Built-in slug `tca` (台北市電腦商業同業公會 會員e名錄, Big5 ASP: keyword list → per-member detail). Any other member-list URL is crawled generically — outbound member links, 公司名稱/網址/電話/地址 labelled cells, same-site detail pages, 下一頁 paging | usually |
| `govbiz` | 經濟部 商工登記公示資料, via the 商工行政資料開放平臺 open-data API (`data.gcis.nat.gov.tw`). Keyword search on 公司名稱, or an `F######` 營業項目 code; `region` filters the registered address | **no** |

Two notes on `govbiz`. First, we call the **API, not findbiz** — the public UI at
`findbiz.nat.gov.tw` answers a plain HTTP GET with 403 and its markup is
unstable, while the open-data platform serves the same registry with documented
parameters and no key. Second, registry rows have no URL and therefore cannot
produce an email on their own: `resolve_missing_websites` bridges that by
looking each company up on Maps by its exact registered name.

That lookup is deliberately strict. Maps never answers "not found" — search a
company it doesn't list and it returns whichever business was nearest — so the
place's own name has to corroborate the hit before its website is accepted
(`_name_matches`, comparing name *cores* with the corporate suffix, bracketed
Latin alias, and 台/臺 split removed). It runs in `hl=zh-TW`, since an
English-locale Maps answers "Trend Micro" for 趨勢科技股份有限公司 and the check
could never match. Real companies that Maps only lists in English are still
missed — a missed website costs one lead, a wrong one files a stranger's email
under a real company.

Independently of discovery, `gcis_enrich` attaches 統一編號 / 資本額 / 負責人 /
設立日期 to leads found by *any* source (exact-name match only — a wrong 統編 is
worse than none). Capital and setup date are a free company-size signal for
ICP scoring.

**Output UX:** the run detail renders a leads table (company · email · confidence
· ICP · hook · site) and a **Download CSV** button →
`GET /api/runs/{id}/leads.csv` (UTF-8 BOM so Excel reads 中文 correctly).

**Run it:**

```bash
uv run playwright install chromium          # one-time
uv run pytest tests/unit/test_email_collect_tools.py tests/unit/test_email_collect_flow.py -v
# then use the "Email Collector" card in the UI, or POST /api/jobs with
# job_type="email_collect".
```

**Known limits / next steps:** Google Maps DOM class names (`hfpxzc`, `DUwDvf`)
are Google's own and drift over time — every field read is guarded, so a shift
degrades one field rather than failing the run, but the selectors will need
periodic refresh. SMTP port 25 is blocked on many networks/clouds → probes
report `unknown` (verification falls back to MX-only). The TCA adapter searches
Chinese company names, so an English `query` returns nothing there. The generic
公會 crawler is structural and best-effort: these sites are hand-rolled, and a
directory that renders its member list from JavaScript (the crawler is
`urllib`-only) or hides it behind a login will come back empty with a warning
rather than a partial list. Next sources worth adding: B2B directories
(台灣經貿網 / 中華黃頁), 政府採購網 tender participants, and inbound
contact-form capture.
