import json

from crewai.flow.flow import Flow, listen, start
from pydantic import BaseModel

from src.automation.crews.email_collect_crew.crew import EmailCollectCrew
from src.automation.flows.base import FlowMixin
from src.automation.flows.utils import extract_usage
from src.automation.progress import append_log
from src.automation.tools.contact_harvest import social_platform
from src.automation.tools.email_extract_tool import extract_emails
from src.automation.tools.email_verify_tool import verify_email
from src.automation.tools.facebook_contact_tool import fetch_facebook_contact
from src.automation.tools.maps_search_tool import search_maps
from src.automation.tools.x_profile_contact_tool import fetch_x_profile_contact

# Social "websites" we can mine for a contact instead of scraping HTML.
_SOCIAL_SOURCES = ("facebook", "x")

# The LLM qualifier is the expensive stage — cap how many leads we send it.
_MAX_QUALIFY = 30
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
    include_social: bool = False # also mine social profiles (X) for contacts
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
                   f"'{self.state.region or 'anywhere'}', limit {self.state.limit}")
        return self.state.model_dump()

    @listen(validate_payload)
    def run_funnel(self, _):
        rid = self.state.region
        offer = self.state.offer or _DEFAULT_OFFER
        search_query = " ".join(
            x for x in (self.state.industry, self.state.query) if x
        )

        # ── Stage 1: DISCOVER ────────────────────────────────────────────────
        append_log(self.state.run_id, "Discovering businesses on Google Maps...")
        disc = search_maps(
            search_query, rid, self.state.limit,
            log=lambda m: append_log(self.state.run_id, m),
        )
        businesses = disc.get("businesses", [])
        warnings = list(disc.get("warnings", []))
        append_log(self.state.run_id, f"Discovered {len(businesses)} business(es)")

        # ── Stage 2 + 3: EXTRACT EMAIL → VERIFY, deduped across businesses ────
        leads: list[dict] = []
        seen_emails: set[str] = set()
        with_website = 0
        for i, biz in enumerate(businesses, 1):
            website = biz.get("website", "")
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
            ext = extract_emails(website, log=lambda m: append_log(self.state.run_id, m))
            self._append_leads(
                leads, seen_emails, biz, rid, website, ext.get("emails", []),
                source="guessed" if ext.get("guessed") else "website")

        leads.sort(key=lambda x: _CONF_RANK.get(x["confidence"], 9))
        append_log(self.state.run_id,
                   f"Collected {len(leads)} verified lead(s) from "
                   f"{with_website} site(s)")

        # ── Stage 4: QUALIFY (LLM) — ICP fit + personalization hook ──────────
        if leads:
            self._qualify(leads, offer)

        result = {
            "query": self.state.query,
            "region": rid,
            "industry": self.state.industry,
            "offer": offer,
            "discovered_count": len(businesses),
            "with_website": with_website,
            "lead_count": len(leads),
            "leads": leads,
            "businesses": [
                {"company": b.get("name", ""), "website": b.get("website", ""),
                 "category": b.get("category", ""), "phone": b.get("phone", ""),
                 "address": b.get("address", "")}
                for b in businesses
            ],
        }
        if warnings:
            result["warnings"] = warnings
        append_log(self.state.run_id, "Lead collection complete, formatting result...")
        return json.dumps(result, ensure_ascii=False)

    def _mine_social(self, platform, prefix, biz, region, website,
                     leads, seen_emails) -> None:
        """Mine a social 'website' for contacts, then chase through to any real
        site it links. Shared by the X and Facebook sources."""
        label = {"x": "X profile", "facebook": "Facebook Page"}[platform]
        fetch = {"x": fetch_x_profile_contact,
                 "facebook": fetch_facebook_contact}[platform]
        append_log(self.state.run_id, f"{prefix} Mining {label} {website}")
        prof = fetch(website, log=lambda m: append_log(self.state.run_id, m))
        self._append_leads(leads, seen_emails, biz, region, website,
                           prof.get("emails", []), source=platform)

        linked = prof.get("website", "")
        if linked and not social_platform(linked):
            append_log(self.state.run_id,
                       f"{prefix} Following {label}-linked site {linked}")
            ext = extract_emails(linked,
                                 log=lambda m: append_log(self.state.run_id, m))
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
                "confidence":  v["confidence"],
                "mx_found":    v["mx_found"],
                "smtp_status": v["smtp_status"],
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
