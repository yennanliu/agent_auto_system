import json
import urllib.parse
from itertools import zip_longest

from crewai.flow.flow import Flow, listen, start
from pydantic import BaseModel

from src.automation.crews.email_collect_crew.crew import EmailCollectCrew
from src.automation.flows.base import FlowMixin
from src.automation.flows.utils import extract_usage
from src.automation.progress import append_log
from src.automation.tools.contact_harvest import (
    merge_by_keys,
    normalize_company_name,
    social_platform,
    unique_records,
)
from src.automation.tools.email_extract_tool import (
    _is_shared_host,
    _registrable_domain,
    extract_emails,
)
from src.automation.tools.email_verify_tool import verify_email
from src.automation.tools.facebook_contact_tool import fetch_facebook_contact
from src.automation.tools.instagram_contact_tool import fetch_instagram_contact
from src.automation.tools.maps_search_tool import resolve_websites, search_maps
from src.automation.tools.moea_gcis_tool import lookup_company, search_companies
from src.automation.tools.tw_association_tool import search_association
from src.automation.tools.x_profile_contact_tool import fetch_x_profile_contact

# Social "websites" we can mine for a contact instead of scraping HTML.
_SOCIAL_SOURCES = ("facebook", "x", "instagram")

# Discovery sources the funnel can draw businesses from. `maps` is the default
# and the only one that works outside Taiwan; `association` (公會/工會 member
# directories) and `govbiz` (經濟部 商工登記) are TW-specific.
_DISCOVERY_SOURCES = ("maps", "association", "govbiz")
_DEFAULT_SOURCES = ("maps",)

# The LLM qualifier is the expensive stage — cap how many leads we send it.
_MAX_QUALIFY = 30
# Registry enrichment is one API call per lead; same reasoning, tighter cap
# because it buys metadata rather than the copy you actually send.
_MAX_ENRICH = 40
# Rank order for confidence labels when sorting leads.
_CONF_RANK = {"high": 0, "medium": 1, "low": 2, "invalid": 3}

_DEFAULT_OFFER = "an AI agent / automation consulting proposal for small businesses"


class EmailCollectState(BaseModel):
    query: str = ""              # what to search, e.g. "marketing agency"
    region: str = ""             # where, e.g. "Taipei" / "Berlin" / "Austin, TX"
    industry: str = ""           # optional label, folded into the search term
    offer: str = ""              # what you're pitching (drives qualification)
    limit: int = 15              # businesses to discover
    smtp_check: bool = True      # run the SMTP RCPT probe during verification
    include_social: bool = False # also mine social profiles (Facebook, X, Instagram)
    render_js: bool = True       # browser-render sites where static scrape finds nothing
    # ── discovery sources ────────────────────────────────────────────────────
    sources: list[str] = []          # subset of _DISCOVERY_SOURCES; [] = maps only
    associations: list[str] = []     # 公會 slugs ('tca') and/or member-directory URLs
    resolve_missing_websites: bool = False  # look up a website for registry rows
    gcis_enrich: bool = False        # attach 統編/資本額/負責人 from 經濟部 registry
    run_id: int = 0
    usage: dict = {}
    llm_provider: str = ""
    llm_model: str = ""
    previous_error: str = ""


class EmailCollectFlow(FlowMixin, Flow[EmailCollectState]):

    @start()
    def validate_payload(self):
        self._check_required("query")
        term = " ".join(x for x in (self.state.industry, self.state.query) if x)
        append_log(self.state.run_id,
                   f"Payload validated — searching '{term}' in "
                   f"'{self.state.region or 'anywhere'}', limit {self.state.limit}, "
                   f"sources: {', '.join(self._sources())}")
        return self.state.model_dump()

    @listen(validate_payload)
    def run_funnel(self, _):
        rid = self.state.region
        offer = self.state.offer or _DEFAULT_OFFER
        search_query = " ".join(
            x for x in (self.state.industry, self.state.query) if x
        )

        # ── Stage 1: DISCOVER (Maps and/or 公會名錄 and/or 經濟部登記) ────────
        businesses, warnings = self._discover(search_query, rid)

        # A registry row has no website, so nothing downstream can reach it.
        # Resolving one turns it from a name on a list into a contactable lead.
        if self.state.resolve_missing_websites:
            self._resolve_websites(businesses)

        # ── Stage 2 + 3: EXTRACT EMAIL → VERIFY, deduped across businesses ────
        leads: list[dict] = []
        seen_emails: set[str] = set()
        with_website = 0
        for i, biz in enumerate(businesses, 1):
            website = biz.get("website", "")
            # Directory rows sometimes publish the contact address outright —
            # take it before spending a fetch on the company's site.
            if biz.get("emails"):
                self._append_leads(leads, seen_emails, biz, rid, website,
                                   biz["emails"], source="directory")
            if not website:
                continue
            with_website += 1
            prefix = f"[{i}/{len(businesses)}]"

            # A social profile as the "website" is a dead end for the plain
            # scraper (login-walled, JS-rendered) — route it to a dedicated
            # extractor and chase through to any real site it links out to.
            platform = social_platform(website) if self.state.include_social else None
            if platform in _SOCIAL_SOURCES:
                self._mine_social(platform, prefix, biz, rid, website,
                                  leads, seen_emails)
                continue

            append_log(self.state.run_id,
                       f"{prefix} Extracting email from {website}")
            ext = extract_emails(website, log=lambda m: append_log(self.state.run_id, m),
                                 render=self.state.render_js)
            self._append_leads(
                leads, seen_emails, biz, rid, website, ext.get("emails", []),
                source="guessed" if ext.get("guessed") else "website")

        leads.sort(key=lambda x: _CONF_RANK.get(x["confidence"], 9))
        append_log(self.state.run_id,
                   f"Collected {len(leads)} verified lead(s) from "
                   f"{with_website} site(s)")

        # ── Stage 4: ENRICH (經濟部 registry) — 統編 / 資本額 / 負責人 ────────
        if leads and self.state.gcis_enrich:
            self._enrich_from_registry(leads)

        # ── Stage 5: QUALIFY (LLM) — ICP fit + personalization hook ──────────
        if leads:
            self._qualify(leads, offer)

        result = {
            "query": self.state.query,
            "region": rid,
            "industry": self.state.industry,
            "offer": offer,
            "sources": self._sources(),
            "discovered_count": len(businesses),
            "with_website": with_website,
            "lead_count": len(leads),
            "leads": leads,
            "businesses": [
                {"company": b.get("name", ""), "website": b.get("website", ""),
                 "category": b.get("category", ""), "phone": b.get("phone", ""),
                 "address": b.get("address", ""),
                 "discovery": b.get("discovery", "maps"),
                 **({"tax_id": b["tax_id"]} if b.get("tax_id") else {})}
                for b in businesses
            ],
        }
        if warnings:
            result["warnings"] = warnings
        append_log(self.state.run_id, "Lead collection complete, formatting result...")
        return json.dumps(result, ensure_ascii=False)

    # ── discovery ────────────────────────────────────────────────────────────

    def _sources(self) -> list[str]:
        """Enabled discovery sources, order-preserved and validated."""
        chosen = [s.strip().lower() for s in (self.state.sources or []) if s.strip()]
        valid = [s for s in dict.fromkeys(chosen) if s in _DISCOVERY_SOURCES]
        return valid or list(_DEFAULT_SOURCES)

    def _discover(self, search_query: str, region: str) -> tuple[list[dict], list[str]]:
        """Run every enabled source and merge the results into one business list.

        Each source contributes up to the full `limit` and the merge dedupes
        across them, so a company listed both on Maps and in a 公會 directory
        becomes one business carrying whichever fields each source knew.
        """
        sources = self._sources()
        merged: dict[str, dict] = {}
        warnings: list[str] = []
        append_log(self.state.run_id,
                   f"Discovering businesses from: {', '.join(sources)}...")

        for source in sources:
            found, warns = self._run_source(source, search_query, region)
            warnings.extend(warns)
            for biz in found:
                biz.setdefault("discovery", source)
                _merge_business(merged, biz)
            append_log(self.state.run_id,
                       f"Source '{source}' contributed {len(found)} business(es); "
                       f"{len(unique_records(merged))} unique so far")

        businesses = _interleave_by_source(unique_records(merged))[: self.state.limit]
        append_log(self.state.run_id, f"Discovered {len(businesses)} business(es)")
        return businesses, warnings

    def _run_source(self, source: str, search_query: str,
                    region: str) -> tuple[list[dict], list[str]]:
        """Dispatch one discovery source. Never raises — a broken source only
        costs its own results, not the run."""
        log = lambda m: append_log(self.state.run_id, m)  # noqa: E731
        try:
            if source == "maps":
                res = search_maps(search_query, region, self.state.limit, log=log)
                return res.get("businesses", []), list(res.get("warnings", []))

            if source == "association":
                targets = [a.strip() for a in (self.state.associations or [])
                           if a.strip()]
                if not targets:
                    return [], ["source 'association' selected but no 公會 "
                                "directory chosen"]
                found: list[dict] = []
                warns: list[str] = []
                for target in targets:
                    # Isolate each directory too, not just the source as a
                    # whole: one guild's site being down must not discard the
                    # members already collected from the others.
                    try:
                        res = search_association(target, search_query,
                                                 self.state.limit, log=log)
                    except Exception as exc:  # noqa: BLE001
                        append_log(self.state.run_id,
                                   f"Directory '{target}' failed: {exc}")
                        warns.append(f"directory '{target}' failed: "
                                     f"{type(exc).__name__}: {exc}")
                        continue
                    found.extend(res.get("businesses", []))
                    warns.extend(res.get("warnings", []))
                return found, warns

            if source == "govbiz":
                res = search_companies(search_query, self.state.limit,
                                       city=region, log=log)
                return res.get("businesses", []), list(res.get("warnings", []))
        except Exception as exc:  # noqa: BLE001 — one source failing ≠ run failing
            append_log(self.state.run_id, f"Source '{source}' failed: {exc}")
            return [], [f"source '{source}' failed: {type(exc).__name__}: {exc}"]
        return [], [f"unknown discovery source '{source}'"]

    def _resolve_websites(self, businesses: list[dict]) -> None:
        """Fill in a website for businesses discovered without one, via Maps."""
        missing = [b for b in businesses if not b.get("website") and b.get("name")]
        if not missing:
            return
        append_log(self.state.run_id,
                   f"Resolving websites for {len(missing)} business(es) with none...")
        try:
            found = resolve_websites(
                [b["name"].strip() for b in missing], self.state.region,
                log=lambda m: append_log(self.state.run_id, m),
            )
        except Exception as exc:  # noqa: BLE001 — best-effort; keep the leads we have
            append_log(self.state.run_id, f"Website resolution failed ({exc})")
            return
        resolved = 0
        for biz in missing:
            site = found.get(biz["name"].strip(), "")
            if site:
                biz["website"] = site
                biz["website_source"] = "maps_lookup"
                resolved += 1
        append_log(self.state.run_id,
                   f"Resolved {resolved}/{len(missing)} website(s)")

    def _enrich_from_registry(self, leads: list[dict]) -> None:
        """Attach 經濟部 registry facts (統編/資本額/負責人/設立日期) to each lead.

        One lookup per distinct company, capped and best-effort: the registry is
        a nice-to-have signal for ICP scoring, never a reason to fail a run.
        """
        companies = list(dict.fromkeys(
            lead["company"] for lead in leads if lead.get("company")))[:_MAX_ENRICH]
        if not companies:
            return
        append_log(self.state.run_id,
                   f"Enriching {len(companies)} company(ies) with 經濟部 registry data...")
        facts: dict[str, dict] = {}
        for name in companies:
            try:
                record = lookup_company(
                    name, log=lambda m: append_log(self.state.run_id, m))
            except Exception as exc:  # noqa: BLE001
                append_log(self.state.run_id, f"Registry lookup failed for {name}: {exc}")
                continue
            if record:
                facts[name] = record
        for lead in leads:
            record = facts.get(lead.get("company", ""))
            if record:
                lead.update({k: v for k, v in record.items() if v})
        append_log(self.state.run_id,
                   f"Registry matched {len(facts)}/{len(companies)} company(ies)")

    def _mine_social(self, platform, prefix, biz, region, website,
                     leads, seen_emails) -> None:
        """Mine a social 'website' for contacts, then chase through to any real
        site it links. Shared by the X and Facebook sources."""
        label = {"x": "X profile", "facebook": "Facebook Page",
                 "instagram": "Instagram profile"}[platform]
        fetch = {"x": fetch_x_profile_contact,
                 "facebook": fetch_facebook_contact,
                 "instagram": fetch_instagram_contact}[platform]
        append_log(self.state.run_id, f"{prefix} Mining {label} {website}")
        prof = fetch(website, log=lambda m: append_log(self.state.run_id, m))
        self._append_leads(leads, seen_emails, biz, region, website,
                           prof.get("emails", []), source=platform)

        linked = prof.get("website", "")
        if linked and not social_platform(linked):
            append_log(self.state.run_id,
                       f"{prefix} Following {label}-linked site {linked}")
            ext = extract_emails(linked,
                                 log=lambda m: append_log(self.state.run_id, m),
                                 render=self.state.render_js)
            self._append_leads(
                leads, seen_emails, biz, region, linked, ext.get("emails", []),
                source="guessed" if ext.get("guessed") else "website")

    def _append_leads(self, leads, seen_emails, biz, region,
                      website, emails, source) -> None:
        """Verify each email and append a lead row, deduped across businesses."""
        for email in emails:
            # Normalize the dedupe key to match verify_email (strip + lowercase),
            # so the same address in different casing from two sources — e.g. a
            # guessed info@Acme.com vs a published info@acme.com — dedupes to one lead.
            key = email.strip().lower()
            if key in seen_emails:
                continue
            seen_emails.add(key)
            v = verify_email(email, smtp_check=self.state.smtp_check)
            if v["confidence"] == "invalid":
                continue
            # A guessed info@<domain> is unproven: MX-present just means the
            # domain accepts mail, not that this mailbox exists. Don't let it
            # earn "medium" (the send-worthy tier) on the role-address bonus —
            # only a real SMTP accept ("high") can lift a guess above "low".
            if source == "guessed" and v["confidence"] == "medium":
                v = {**v, "confidence": "low"}
            leads.append({
                "company":  biz.get("name", ""),
                "email":    email,
                "website":  website,
                "category": biz.get("category", ""),
                "phone":    biz.get("phone", ""),
                "address":  biz.get("address", ""),
                "region":   region,
                "maps_url": biz.get("maps_url", ""),
                "source":   source,
                # Which discovery source found the company (maps /
                # association:<slug> / govbiz) — distinct from `source`, which
                # says where the *email* came from.
                "discovery": biz.get("discovery", "maps"),
                "confidence":  v["confidence"],
                "mx_found":    v["mx_found"],
                "smtp_status": v["smtp_status"],
                # Provenance the discovery source knew: the registry's 統編 and
                # the directory page the row was read from (a member with no
                # website of its own still has a traceable origin).
                **{k: biz[k] for k in
                   ("tax_id", "responsible", "capital", "setup_date", "source_url")
                   if biz.get(k)},
            })

    def _qualify(self, leads: list[dict], offer: str) -> None:
        """Merge LLM-generated icp_fit / reason / hook into `leads` in place.

        Best-effort: any failure leaves the leads intact (just without hooks) so
        a flaky/absent LLM never sinks an otherwise good collection run.
        """
        try:
            from src.automation.harness.provider import resolve as resolve_llm
            llm, _p, _m = resolve_llm(
                self.state.llm_provider or None,
                self.state.llm_model or None,
                temperature=0.4,
            )
        except Exception as exc:  # noqa: BLE001
            append_log(self.state.run_id,
                       f"No LLM for qualification ({exc}); returning leads unqualified.")
            return

        subset = leads[:_MAX_QUALIFY]
        leads_json = json.dumps(
            [{"i": i, "company": lead["company"], "website": lead["website"],
              "category": lead["category"]} for i, lead in enumerate(subset)],
            ensure_ascii=False,
        )
        append_log(self.state.run_id,
                   f"Qualifying {len(subset)} lead(s) with LLM (ICP fit + hook)...")
        try:
            result = EmailCollectCrew(llm=llm).crew().kickoff(inputs={
                "offer": offer,
                "region": self.state.region or "anywhere",
                "leads_json": leads_json,
                "previous_error": self.state.previous_error,
            })
            self.state.usage = extract_usage(result)
            text = result.raw if hasattr(result, "raw") else str(result)
            for item in _parse_qualifications(text):
                idx = item.get("i")
                if isinstance(idx, int) and 0 <= idx < len(subset):
                    subset[idx]["icp_fit"] = item.get("icp_fit")
                    subset[idx]["reason"] = item.get("reason", "")
                    subset[idx]["hook"] = item.get("hook", "")
        except Exception as exc:  # noqa: BLE001 — never fail the run on qualification
            append_log(self.state.run_id,
                       f"Qualification failed ({exc}); returning leads unqualified.")


def _interleave_by_source(businesses) -> list[dict]:
    """Round-robin the merged businesses across their discovery sources.

    Each source is asked for the full `limit` so any one of them can fill the
    quota alone, which means a straight concatenation would let whichever source
    ran first consume the entire budget. Interleaving keeps every enabled source
    represented in the truncated list while still filling it up when the others
    come back short.
    """
    buckets: dict[str, list[dict]] = {}
    for biz in businesses:
        buckets.setdefault(biz.get("discovery", "maps"), []).append(biz)
    out: list[dict] = []
    for row in zip_longest(*buckets.values()):
        out.extend(b for b in row if b is not None)
    return out


def _merge_business(merged: dict[str, dict], biz: dict) -> None:
    """Insert `biz` into `merged`, or fold it into the matching entry.

    Indexed under *both* the website's registrable domain and the company name,
    because the sources disagree about which they know: Maps has a website,
    the 經濟部 registry never does, and a 公會 row may have either. Keying on one
    alone would list the same firm twice — the funnel would scrape its site
    twice and the lead count would double-count it. Existing values win; a later
    source only fills blanks (Maps knows the maps_url, the directory knows the
    業務類型, the registry knows the 統編 — together they make one complete row).
    """
    name = (biz.get("name") or "").strip()
    website = (biz.get("website") or "").strip()
    if not name and not website:
        return
    entry = merge_by_keys(merged, biz, (_business_key(website), _name_key(name)))
    # Register a website contributed by the merge, so a later row carrying only
    # that domain finds this entry.
    site_key = _business_key(entry.get("website") or "")
    if site_key:
        merged.setdefault(site_key, entry)


def _business_key(website: str) -> str:
    """The website's registrable domain, or "" when it can't identify a company.

    Shared hosts are excluded on purpose: `facebook.com/shopA` and
    `facebook.com/shopB` both reduce to `facebook.com`, so keying on the domain
    would merge two unrelated businesses into one and drop the second. Those
    fall back to the name key. (Platform tenancies that the Public Suffix List
    tracks — `a.wixsite.com` vs `b.wixsite.com` — already resolve to distinct
    registrable domains, which is why `_registrable_domain` runs first.)
    """
    host = urllib.parse.urlparse(
        website if website.startswith(("http://", "https://"))
        else f"https://{website}" if website else ""
    ).netloc.lower()
    if not host:
        return ""
    domain = _registrable_domain(host)
    return "" if _is_shared_host(domain) else domain


def _name_key(name: str) -> str:
    """Normalize a company name for cross-source matching (alias, spacing, 台/臺)."""
    return normalize_company_name(name)


def _parse_qualifications(text: str) -> list[dict]:
    """Parse the qualifier's JSON array, tolerating markdown fences / stray prose."""
    if not isinstance(text, str):
        return []
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        candidate = candidate[candidate.find("[") : candidate.rfind("]") + 1]
    else:
        start, end = candidate.find("["), candidate.rfind("]")
        if start != -1 and end != -1:
            candidate = candidate[start : end + 1]
    try:
        data = json.loads(candidate)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, TypeError):
        return []
