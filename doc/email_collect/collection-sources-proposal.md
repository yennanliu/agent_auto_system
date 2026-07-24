# Proposal: From One Source to a Multi-Source Lead Engine

Status: **proposal / not implemented.** This document surveys how email collection
works today and proposes new ways to collect emails. Scoped for a side-project
pitch (target: Taiwan SME AI-consulting outreach — see
[brothersupport.github.io/ai_consultant](https://brothersupport.github.io/ai_consultant/)).
It changes no code.

See [README.md](README.md) for the original funnel rationale.

---

## Where we are today

The system has **two separate email automations** — they are not chained:

- **`email_collect`** — the collection funnel (the subject of this proposal).
- **`email_sender`** — sends one composed email via Gmail SMTP. No collection.
  (Its crew is dead code; the flow calls the tool directly.)

### The `email_collect` funnel

| Stage | What it does | Implementation |
|---|---|---|
| 1. **Discover** | Search `industry + query` in a `region` on Google Maps; scrape each listing's name, **website**, phone, address, category | Headless Playwright — `maps_search_tool.py` |
| 2. **Extract** | Fetch each business's own website (homepage + contact/about/impressum, max 8 pages); harvest `mailto:` + regex emails; fall back to guessing `info@domain` | `urllib` + regex — `email_extract_tool.py` |
| 3. **Verify** | Syntax → MX lookup → SMTP RCPT probe (port 25, no send); label confidence high/medium/low/invalid | `dnspython` + `smtplib` — `email_verify_tool.py` |
| 4. **Qualify** | Score `icp_fit` 1–5, write a `reason` + a cold-email `hook` per lead | LLM crew — `email_collect_crew/` |

Output → leads table + `GET /api/runs/{id}/leads.csv` (BOM-encoded for Excel/Chinese).

### The one real limitation

Today there is exactly **one source: Google Maps → the business's own website.**
It's a strong ICP-fit source, but it structurally misses:

- *(Historical baseline — see the status update below; Facebook and X are now mined.)* Every business whose Maps "website" is Facebook / IG / Linktree — the page is still fetched and any *published* email harvested, but on these shared hosts `_is_shared_host` (backed by `_NO_GUESS_DOMAINS`) suppresses the `info@` fallback guess, and a JS/login-walled social page rarely exposes an email to the plain `urllib` fetch — so with the Maps-only funnel these yielded **nothing**.
- Every business not on Maps, or not near the searched region.
- It over-produces low-confidence guessed `info@` addresses.

Compliance (GDPR / PDPA / CAN-SPAM) is **documentation-only** — no suppression
list, rate limiting, or robots.txt check exists in code.

---

## Proposed new collection sources

Ranked by ROI-for-effort for the Taiwan-SME context. Free / public sources first,
matching the project's stated philosophy of not buying from paid databases.

### Tier 1 — Recover leads we already drop (cheapest wins)

| Idea | Why | Effort |
|---|---|---|
| **Facebook / IG "About" block extractor** | Biggest gap. Many TW SMEs use a FB page *as* their website; the About / 聯絡資訊 block often lists email + phone openly. **Facebook is now implemented** (`facebook_contact_tool.py`); IG is next. | Medium |
| **Google Maps profile contact field** | Scrape any contact info Maps surfaces on the listing itself before falling through to website extraction. | Low |

### Tier 2 — New public directories (high fit for TW SMEs)

| Idea | Why | Effort |
|---|---|---|
| **政府 / 法人登記資料** (經濟部商業司, 台灣公司網) | Verified company identity (統一編號, registered name, address); pair with domain-guessing to produce a low-confidence `contact@` *candidate* that still needs independent MX/SMTP verification before it earns higher confidence. | Medium |
| **Chambers of commerce & industry associations** (工商協進會, 中小企業總會, sector guilds) | Frequently publish member emails openly — *exactly* the ICP. | Medium |
| **B2B directories** (台灣經貿網 / Taiwantrade, 中華黃頁, 104 / 1111 company pages) | Structured, email-rich, SME-heavy. | Medium |
| **Government procurement lists** (政府採購網) | Companies bidding on tenders are actively spending and usually list a contact. | Medium |

> **Status update.** The **Facebook Page** and **X (Twitter) profile-bio**
> sources from Tier 1 are now implemented (`facebook_contact_tool.py`,
> `x_profile_contact_tool.py`, opt-in via the `include_social` flag). When a
> business's Maps "website" is a Facebook Page or X profile — previously dropped
> by `_NO_GUESS_DOMAINS` — the funnel now mines it for a contact email
> (de-obfuscating `name [at] domain [dot] com` forms), tags the lead
> `source=facebook` / `source=x`, and *chases through* to any real website it
> links, running that back through the normal website extractor. Both reuse the
> shared `contact_harvest.py` harvest/validation helper. Instagram About
> extraction is next.

### Tier 3 — Smarter extraction on sources we already hit

| Idea | Why | Effort |
|---|---|---|
| **Search-engine seeding** | Use a search API (Bing / Brave / Google) with dorks like `"marketing agency" 台北 "@" 聯絡` to find contact pages directly, then run the existing extractor. Breaks the Maps geographic bias. | Medium |
| **WHOIS registrant email** | Many `.tw` domains still expose a registrant contact for sites with no on-page email. | Low |
| **Email-pattern inference** | When one email is found on a domain (`abc@x.tw`), infer the org's pattern instead of only guessing `info@`. | Low |
| **PDF / brochure parsing** | SME sites often bury contacts in a downloadable PDF the current text-regex misses. | Medium |

### Tier 4 — Inbound (flip the model)

The highest-quality emails are *given*, not scraped. The linked site already has a
Google Form diagnostic + a contact form.

| Idea | Why | Effort |
|---|---|---|
| **Ingest contact-form / Google Form submissions** | Pipe them straight into the leads table as `source=inbound`, `confidence=high` — no verification or qualification guesswork. | Low |
| **Lead-magnet gate** (免費 AI 診斷報告) | Capture email before delivering a report. | Medium |

### Cross-cutting — quality & safety (needed as sources multiply)

- **Suppression / do-not-contact list** + per-domain rate limiting — currently absent.
- **Source provenance + unified confidence** — tag each lead's origin; normalize confidence so outreach prioritizes *given / verified* over *guessed*.
- **Cross-source / cross-run dedupe** — the same company will appear in Maps + FB + a directory; today dedupe is per-run, per-email only.

---

## Recommended pitch framing

**"One source today → a multi-source lead engine."**

The two highest-value, best-fit additions:

1. **Facebook / IG About extractor** — recovers leads we actively discard; easy to justify.
2. **Taiwan chamber / association + registry directories** — verified, on-ICP,
   email-rich, and legally cleaner than scraping.

Frame **inbound-form capture** as the highest-quality tier to show that
scraped ≠ best.
