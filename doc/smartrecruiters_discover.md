# SmartRecruiters Discover (`smartrecruiters_discover`)

Finds jobs that companies post on SmartRecruiters (`jobs.smartrecruiters.com/<Company>`) and returns them with apply links. It's a sibling of `greenhouse_apply`, and its keyword, role-shorthand and location matching are imported from that tool.

**It doesn't apply for you.** Each result has an `apply_url` to open in your own browser.

## Why discovery only

Checked against live SmartRecruiters pages in November 2026:

| Part | Status |
|---|---|
| Posting list and detail | Public: `GET api.smartrecruiters.com/v1/companies/<id>/postings`, no key, case-insensitive company id |
| Application questions | Not public: the Application API configuration endpoint returns 401 without a partner key |
| Application form (`/oneclick-ui/company/<id>/publication/<uuid>`) | Behind DataDome bot protection. A Playwright-driven browser, headless or visible real Chrome, gets a "Verification Required" slider. After the slider, the network was shown "Access is temporarily restricted … automated (bot) activity" |

Getting an automated browser through that check would need bot-detection evasion: stealth patches, hiding automation, or rotating proxies. This project doesn't do that. If SmartRecruiters offers a sanctioned route later, such as a partner Application API key, auto-apply can be revisited along the lines of `greenhouse_apply`.

## Funnel

| Step | How | Where |
|---|---|---|
| Discover | Posting API, 100 per page, up to `SMARTRECRUITERS_MAX_LISTED` (default 1000) per company. Filtered by title keywords (shorthand like SWE/SRE expanded) and location substrings; "Remote" matches postings the API flags as remote. Newest first within a company, interleaved across companies | `discover_jobs` |
| Pick one | A pasted posting URL (`…/<Company>/<id>-slug` or the `oneclick-ui` publication URL) returns that posting and skips the filters | `parse_company_ref` |
| Only new | `only_new` (default on) skips postings an earlier run returned, using a local ledger (`SMARTRECRUITERS_LEDGER`, default `data/smartrecruiters_seen.json`). Postings removed by the filter are not recorded, so they come back if you change the filter | `load_ledger` / `record_seen` |
| Gate (optional) | `task_filter` uses the shared `GreenhouseRelevanceCrew` and fails open | flow `relevance_fn` |

An unknown company returns `totalFound: 0` with HTTP 200, so it shows up as a warning ("no open postings, or no such company"), not an error. API failures also become warnings.

## Result

```json
{
  "summary": "12 new matching posting(s) from 2 companies (5813 listed, 40 matched, 28 already seen). Open apply_url ...",
  "jobs_found": 12,
  "jobs": [{"title": "...", "company": "ServiceNow", "location": "Atlanta, Georgia, United States (Remote)",
            "posted": "2026-11-07", "department": "Product", "employment_type": "Full-time",
            "url": "https://jobs.smartrecruiters.com/ServiceNow/744000160326680",
            "apply_url": "https://jobs.smartrecruiters.com/oneclick-ui/company/ServiceNow/publication/<uuid>?dcr_ci=ServiceNow"}],
  "skipped": [{"...": "...", "reason": "filtered out: management role"}],
  "warnings": ["BoschGroup: scanned the first 1000 of 5068 postings"]
}
```

## Env

```
SMARTRECRUITERS_LEDGER=data/smartrecruiters_seen.json
SMARTRECRUITERS_MAX_LISTED=1000
```
