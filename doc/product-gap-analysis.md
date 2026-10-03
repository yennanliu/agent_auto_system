# Product Gap Analysis — Toward a Business AI Agent

> Audit date: 2026-10-03 · Scope: whole repo at `eb56a06` · Views: PM + Tech Lead
> 中文版: [product-gap-analysis.zh-TW.md](product-gap-analysis.zh-TW.md)
>
> Companion docs: [saas-readiness.md](saas-readiness.md) (multi-tenant SaaS), [self-hosted-readiness.md](self-hosted-readiness.md) (on-prem), [improvements.md](improvements.md) (2026-05 code audit, partly stale). This document does **not** repeat them; it focuses on the *product* gap to agent products like Grok Bot and Meta Muse, plus the engineering work that gap implies.

---

## 1. TL;DR

**What we are today:** a solid, self-hosted *automation console*. 13 fixed automations behind a form-and-run UI, a harness that validates, retries, scores and costs every run, cron scheduling, RBAC, multi-LLM, Langfuse/Prometheus. ~780 tests. Strong Taiwan-market coverage (tasker, 104, Shopee, 公會, 經濟部).

**What the market now calls a "business agent"** (Grok Bot, Aug 2026; Meta Muse for Small Business, Sep 2026): you *talk* to a persistent, named agent that knows your business, works unattended 24/7 on your logged-in tools, comes back to you only for approvals, notifies you, and proactively surfaces opportunities.

**The gap in one line:** we have the *engine* (harness, flows, scheduler, browser automation) but not the *agent experience* (conversation, memory, approvals, notifications, connectors, proactivity). Closing it is mostly product and glue work on top of what exists, plus a handful of infrastructure prerequisites (durable queue, timeouts, Postgres).

**Where we already beat them:** user-selectable models with cost per run, an *independent* LLM judge, self-hosting and data residency, Taiwan-local coverage, and a clear audit surface. Reviews of Grok Bot name "no model choice, no scoped permissions, no audit trail" as its enterprise blockers. Those are our strengths to lean into.

**Top 5 moves (in order):**

| # | Move | Why first |
|---|---|---|
| 1 | Fix the security holes (IDOR on jobs, pipeline permission bypass, shared SSRF guard, bounded retries) | Cheap, and no agent product can ship on top of them |
| 2 | Notifications (Email / LINE / Slack / webhook) on finish, fail, needs-approval | Makes the scheduler useful; prerequisite for "unattended" |
| 3 | Business Profile + per-item Approval Queue | The two features that turn "a form" into "an assistant"; reuse every existing flow |
| 4 | Conversational entry point ("Ask") routing natural language onto the existing manifest | The visible leap; low risk because it only drives flows we already have |
| 5 | Durable queue + worker + per-run timeout + real Postgres support | Required for persistent 24/7 agents, multi-replica, and cancel-that-actually-stops |

---

## 2. Snapshot: where the codebase stands

| Area | State | Evidence |
|---|---|---|
| Automations | 13 registered specs; 1 multi-agent crew (profit_health); 12 single-agent or tool-driven | `src/automation/spec.py:247-435` |
| No-code custom automations | Admin-only, single agent, **no tools**, no memory, no edit | `src/custom_automations.py`, `crews/dynamic_crew.py:30` |
| Interaction model | Pick a tile → fill a form → run → read JSON/table/PDF | `ui/index.html:344-358`, `ui/app.js` |
| Chat / NL routing / memory / RAG | None (grep for chat, memory, embedding, knowledge finds nothing) | — |
| Human-in-the-loop | `dry_run` checkbox on 3 apply flows (all-or-nothing); no per-item approval | `tasker_apply_flow.py:50`, `tw104_apply_flow.py:70`, `linkedin_apply_flow.py:48` |
| Notifications | None. Only SSE + history table | `src/routers/runs.py:250` |
| Connectors | Gmail SMTP out (single operator account); Google Sheets read-only; Playwright sessions for 4 sites; no Slack/LINE/Notion/Shopify/MCP | `tools/gmail_send_tool.py:52`, `browser_session.py:56-98` |
| Scheduling | Cron (UTC) in-process, single-instance, no overlap guard, no backfill | `scheduler.py:15-17, 40, 88-106` |
| Execution | `asyncio.create_task` + default thread pool in the web process; no queue, no per-run timeout, cancel does not stop the thread | `launcher.py:49`, `executor.py:114,169` |
| Data model | User / Setting / Job / Run; no org, no audit log, no artifact table; result+log as JSON text | `src/models.py` |
| Observability | Langfuse per run, Prometheus `/metrics`, OTel, eval score per run | `harness/langfuse_tracer.py`, `telemetry.py` |
| Tests / CI | ~780 collected; ruff + pytest + Docker + smoke; **no mypy in CI, no coverage gate** | `.github/workflows/ci.yml` |
| Docs drift | README says 11 automations / 380 tests / "touch 6 files"; reality is 13 / ~780 / spec-driven | `README.md:15, 168, 335` vs `CLAUDE.md` |
| Desktop | Electron shell, Phase 0–2 (backend freeze done; signing pending) | `electron/README.md` |

---

## 3. The benchmark: what Grok Bot and Muse actually do

Facts below are from public coverage as of 2026-10 (sources in §10). Take them as the *shape* of the category, not a spec to clone.

| Capability | Grok Bot (xAI / SpaceXAI, Aug 2026) | Meta Muse / Muse for Small Business (Sep 2026) |
|---|---|---|
| Interaction | Chat threads with **named, persistent bots**; a coordinating bot delegates to specialist bots in shared threads | Chat-first personal agent with name, avatar and style; goal-driven ("find new customers", "boost revenue") |
| Business context | Each bot keeps its configuration and context across tasks | Learns what the business sells, brand voice, common customer inquiries |
| Execution | Each bot has its **own persistent cloud computer** (browser, filesystem, terminal, MCP, logged-in web apps); runs 24/7 after you close your laptop | Long-running tasks on the user's behalf in a **separate VM per user**; browser visible to the user |
| Tool reach | "Signs into apps the way a person does" — no API needed; MCP connectors | Shopify, Stripe, QuickBooks, Slack, Notion, Asana, Canva, Klaviyo, HighLevel, Zoom, Dropbox, Box, Figma, IG/FB business accounts |
| Human-in-the-loop | Bots **return to you when a decision or approval is needed** | "Sentinel" permission system separating read from write |
| Proactivity | Repeats workflows across sales, marketing, finance, ops, support, recruiting | Alerts owners to customer-connection opportunities; cash-flow assessment; campaign creation; recommendations |
| Channels | macOS, Windows, Linux, iOS | iOS, Android, web, glasses, WhatsApp-style messaging |
| Governance | **Weak**: no user model choice, no scoped permissions, no audit trail (per reviewers) | Permission tiers; privacy mode planned end-2026 |
| Pricing | ~$120/seat/month bundled | Free tier; $20 / $100 per month |

**The common pattern:** *goal in → agent with memory + tools + schedule → approval when needed → notification out*. Everything is organised around a persistent agent, not around a one-shot form.

---

## 4. Gap matrix

Legend: ✅ have · 🟡 partial · ❌ missing · ⭐ our advantage

| # | Capability | Us | Them | Gap / note |
|---|---|---|---|---|
| 1 | Conversational, goal-driven entry point | ❌ | ✅ | Users must know which of 13 tiles to pick and fill 5–15 fields |
| 2 | Persistent business profile / memory | ❌ | ✅ | Phone, nationality, offer, brand voice re-entered on every form; prompts never see "who the user is" |
| 3 | Named, persistent agents (goal + schedule + tools) | 🟡 | ✅ | We have Job + cron; missing identity, memory, connectors, notifications bundled as one object |
| 4 | Unattended 24/7 execution | 🟡 | ✅ | Cron exists but runs in the web process; no durable queue, no timeout, no overlap guard, restart fails all in-flight runs |
| 5 | Browser / logged-in-app reach | 🟡 | ✅ | Playwright for tasker/104/LinkedIn/Shopee; sessions stored local-only, headed login needs a desktop, Docker image has no Chromium |
| 6 | Connectors / integrations | ❌ | ✅ | Gmail SMTP (one operator account) and Sheets read only. No Slack, LINE, Notion, Shopify, QuickBooks, Stripe, Drive write, MCP |
| 7 | Per-item approval (human-in-the-loop) | 🟡 | ✅ | `dry_run` is all-or-nothing per run; no "review these 8 proposals, approve 5" |
| 8 | Notifications (done / failed / needs you) | ❌ | ✅ | User must have the tab open |
| 9 | Proactive insights / alerts | ❌ | ✅ | Every automation is pull; nothing watches trends (profit, leads, applications) and speaks first |
| 10 | Multi-agent delegation | 🟡 | ✅ (Grok) | One 4-agent crew; no orchestrator, no `allow_delegation`, no agent-to-agent threads |
| 11 | Scoped permissions (read vs write) | 🟡 | 🟡 | Per-user automation allowlist ✅; but jobs are not owner-scoped (IDOR) and pipelines bypass the allowlist |
| 12 | Audit trail | 🟡 | ❌ | Run rows carry user_id, tokens, cost, eval — better than Grok Bot — but no log of logins, key changes, approvals, admin actions |
| 13 | Model choice + cost per run | ⭐ | ❌ (Grok) | Per-run provider/model, cost, tokens, 7-day stats. Reviewers flag Grok's opaque routing |
| 14 | Independent quality judge | ⭐ | ❌ | LLM-as-judge never grades its own output; heuristic fallback |
| 15 | Self-hosted / data residency | ⭐ | ❌ | Docker, Electron, Postgres path. Their agents run on vendor VMs |
| 16 | Taiwan-local coverage | ⭐ | ❌ | tasker, 104, Shopee, 公會 directories, 經濟部 registry, 統編 enrichment |
| 17 | Mobile / messaging channels | ❌ | ✅ | Web only (responsive); no LINE / Telegram / WhatsApp bot |
| 18 | UI language | 🟡 | ✅ | `lang="en"` chrome with Chinese forms mixed in; no zh-TW/en toggle in the app (only on the marketing site) |
| 19 | Shareable results | ❌ | 🟡 | PDF/CSV download only; no share link, no CRM-lite over leads |
| 20 | Public API / webhooks / SDK | ❌ | 🟡 (MCP) | Cookie auth only; no API keys, no inbound or outbound webhooks |
| 21 | Teams / workspaces / billing | ❌ | ✅ | Covered in saas-readiness.md; not repeated here |

---

## 5. PM view — what to add

Priorities assume the product direction "**the auditable, self-hosted business agent for Taiwan SMBs and solo operators**". Each item says what it unlocks and what it reuses.

### P0 — turn the console into an assistant

**A1. Business Profile (memory v1).**
A per-user (later per-workspace) profile: company name, what you sell, target customers (ICP), region, brand voice, default language, default LLM, contact details (phone, nationality, years of experience for the apply flows), saved cover letters and proposal templates (already exist, but global). Injected into every prompt via a `{{profile.*}}` template and pre-filling forms.
*Unlocks:* "it knows my business"; removes re-typing; makes `email_collect` offer text, tasker proposals and 104 cover letters consistent. *Reuses:* `settings_store` (make it per-user), manifest `fields` (add `default_from_profile`).

**A2. Approval Queue (human-in-the-loop v2).**
A `PendingAction` table + "Inbox" page. Side-effecting flows (tasker/104/LinkedIn apply, email_sender, future connectors) produce drafts; the user approves / edits / rejects per item; approved items execute. Replace the all-or-nothing `dry_run`. Add an `auto_approve` policy per agent later.
*Unlocks:* safe unattended runs, the "comes back to you for decisions" behaviour, and an audit trail of who approved what. *Reuses:* the apply tools already separate "fill" from "submit".

**A3. Notifications.**
Events: run finished, run failed, approval needed, schedule skipped. Channels: Email (existing Gmail tool → per-user), **LINE Messaging API** (Taiwan default), Slack webhook, generic webhook. Per-user channel settings in the profile. A digest option ("one message per day").
*Unlocks:* the scheduler becomes a real product; prerequisite for A5/A6.

**A4. "Ask" — conversational entry point (copilot over existing flows).**
A chat panel that: (1) classifies intent against the automation manifest, (2) extracts field values from the message + profile, (3) shows the pre-filled form for confirmation, (4) runs it, (5) answers follow-up questions about the result (JSON in context). Start with *routing only*; do not build an open-ended tool-calling agent yet.
*Unlocks:* the headline experience with low risk — it can only trigger flows that already exist and pass validation. *Reuses:* `GET /api/automations/manifest` already describes every field.

**A5. Connectors v1.**
Per-user OAuth / token storage (encrypted, like LLM keys) for: Google (Gmail send as the user, Sheets write, Drive), Slack, LINE, Notion. Add write-side tools: "append rows to Sheet", "post to Slack/LINE", "create Notion page". Expose connectors to pipelines and to custom automations via a tool allowlist.
*Unlocks:* results land where the business already works, not in our JSON viewer. Replaces the single operator Gmail account (also a privacy fix).

### P1 — become an agent product

**A6. Agents (named, persistent).**
`Agent = profile slice + goal + one or more automations + schedule + connectors + notification policy + approval policy`. The Schedules page becomes "My Agents": "Lead Hunter — every Monday 09:00, Maps + 公會, notify LINE, approve emails before send". Each agent has a thread (its runs, approvals, messages) — this is the Grok Bot / Muse object model, built from what we have.

**A7. Proactive insights.**
Scheduled "watcher" automations over existing data: profit-health trend across uploads (margin dropped 8% this month), lead pipeline (12 new leads, 3 replied), application stats (apply rate, responses). Weekly digest to the user's channel. Reuses `profit_health_check`, `email_collect`, apply flows as data sources.

**A8. Tool-enabled custom automations.**
Allow admins to attach allow-listed tools (web fetch with SSRF guard, Sheets read/write, connectors, MCP servers) to `custom:<slug>` automations, with per-automation cost cap and run quota. This is the gated item in extensibility-design §Phase 3; A5's connector layer and the shared SSRF guard (F3) are its prerequisites.

**A9. Results workspace.**
Leads become records (status new / contacted / replied / won; notes; owner), exportable to Sheets; read-only share links for PDF/HTML reports (signed, expiring); run results searchable.

**A10. Product polish that blocks adoption.**
In-app **zh-TW / en toggle** (the marketing site has one; the app does not); fix catalog drift (README "11", landing "Eight", code 13); first-run wizard (also in self-hosted-readiness P0-2); timezone-aware schedule editor; "Why did this fail?" explanation from the validator reason + eval notes.

**A11. Public API + webhooks.**
Personal API keys (hashed), `POST /api/jobs/{id}/run` by key, inbound webhook triggers per job, outbound webhooks on run events (shares A3's event bus). Enables Zapier/Make/n8n users and a future SDK/MCP server.

**A12. Messaging channel bot.**
A LINE (and later Telegram/Slack) bot that fronts A4: "幫我找台中的室內設計公司 email" → routed → approval → results back in chat. Mobile without a mobile app.

### P2 — scale the model

**A13. Multi-agent orchestration.** A coordinator that decomposes a goal into existing automations (pipeline v2 with branching/conditions), `allow_delegation` where useful, agent-to-agent threads.
**A14. Templates / marketplace.** Share agent definitions (A6) and custom automations as installable templates; plugin registry already exists behind `AUTOMATION_PLUGINS_ENABLED`.
**A15. Teams, workspaces, billing or licensing.** Follow saas-readiness.md or self-hosted-readiness.md depending on the chosen business model.

### What *not* to copy

- Open-ended "the agent decides which tools to call" execution. Our deterministic-funnel principle (CLAUDE.md) is a feature: cheaper, testable, auditable. Add autonomy at the *routing* layer (A4/A13), keep flows deterministic.
- Vendor-hosted VMs per user. Self-hosting and model choice are our differentiation; keep them.

---

## 6. Tech Lead view — what to improve and fix

Grouped by urgency. File references point at the current tree.

### 6.1 Fix now (security, all small)

| # | Issue | Where | Fix |
|---|---|---|---|
| F1 | **IDOR on jobs**: list/get/update/delete/trigger never check `created_by`; `/overview*`, `/runs/{id}/steps`, `/jobs/{id}/overview`, `/runs/{id}/report.pdf`, `/stats` leak other users' data | `routers/jobs.py:46-110, 202-290`, `routers/runs.py:30-45, 194-200, 345` | Reuse the owner-scoping pattern from `runs.py:24-27` (already tested in `test_run_scoping.py`); add IDOR tests for jobs |
| F2 | **Pipeline bypasses the allowlist**: only the top-level `"pipeline"` type is checked; step types are not | `routers/jobs.py:57`, `pipeline.py:75` | `assert_can_run` for every step type at create *and* run time |
| F3 | **SSRF**: `web_scraper_tool.py:31` passes any URL to `urlopen` (`file://`, metadata IP); same for `email_extract_tool.py:293`, `google_sheet_tool.py:24-52`, `google_form_tools.py:129`, `playwright_form_tool.py:43` | tools/ | Extract the redirect-aware guard from `tw_association_tool.py:603-660` into `tools/_net.py`; use everywhere |
| F4 | **Unbounded cost**: `max_retries` from payload has no cap; each retry can make 5 model attempts; `hn_tool.py:43` spawns `limit` threads | `executor.py:185`, `provider.py:8` | Clamp `max_retries ≤ 3`, cap threads, add per-user concurrent-run quota |
| F5 | **Insecure defaults only warn**: admin/admin, `APP_SECRET=dev-insecure-change-me`, `https_only=False`, no login rate limit, no CSRF token | `main.py:32-47, 103`, `routers/auth.py:49` | Fail-fast when not `DESKTOP_MODE` and not `DEBUG`; `https_only` behind env; slowapi on login; SameSite=strict or CSRF token |
| F6 | **SSO auto-link**: any verified-email match links to an existing account, including admins | `oauth.py:130-145` | Require explicit link from a logged-in session, or exclude admins from auto-link |
| F7 | **Wrong auth level**: `POST /sessions/{name}/login` opens a headed browser on the server for any user; cover letters are global and writable by all; `/metrics`, `/docs`, `/openapi.json` are public | `routers/sessions.py:30`, `routers/cover_letters.py`, `telemetry.py:46`, `main.py:96` | Admin-gate sessions; make cover letters per-user (folds into A1); auth or network-gate metrics/docs |
| F8 | **Uploads**: `upload_id` not tied to a user; `profit_calc_tool.py:193` joins it into a path without validation; no retention | `routers/uploads.py`, `profit_calc_tool.py:193` | Validate UUID format, store `user_id`, TTL cleanup |
| F9 | **Key derivation**: Fernet key is `sha256(APP_SECRET)`; rotation silently blanks all stored keys | `settings_store.py:83-97` | HKDF with salt + key-version column; explicit re-encrypt command |

### 6.2 Reliability (needed before "unattended")

| # | Issue | Where | Fix |
|---|---|---|---|
| R1 | **No per-run timeout** — a hung browser or LLM call runs forever | `executor.py` (no `wait_for`) | `asyncio.wait_for` per attempt + a cooperative `cancel_event` in flow state checked between steps |
| R2 | **Cancel does not stop the work** — only the awaiting coroutine is cancelled; the thread (and its browser, emails, applications) continues | `executor.py:114`, `registry.py` | Same `cancel_event`; close Playwright context on cancel; mark `cancelled` not `failed` |
| R3 | **Retries repeat side effects** — a "timeout" substring in any error re-runs the whole flow, including `email_sender` and the apply flows | `executor.py:215-279`, `provider.py:12-24` | Add `idempotent: bool` to `AutomationSpec`; never auto-retry non-idempotent flows past the LLM step; idempotency key on `POST /run`; scheduler overlap guard |
| R4 | **DB failure after success re-runs the flow** — `_update_run` is inside the retry `try` | `executor.py:263-279` | Separate persistence from execution; persist in its own try |
| R5 | **Cost booked against the requested, not served, model** after fallback | `executor.py:247` | Return the served model from `_run_flow` |
| R6 | **No concurrency control** — only the default thread pool (~CPU+4) bounds parallel runs; queued runs already show "running" | `executor.py:179` | Semaphore + `queued` status; per-user quota |
| R7 | `append_log` swallows all errors; one DB write per log line | `progress.py:20-27` | Log at WARNING; batch or move to a log table |

### 6.3 Architecture (prerequisites for Agents / multi-replica)

| # | Issue | Fix |
|---|---|---|
| S1 | **In-process execution** (`launcher.py:49`): no durable queue, cancel only on the originating replica, restart marks *every* running run failed (`database.py:75-88`), scheduler fires N× with N replicas | Durable queue + worker: Postgres `SELECT … FOR UPDATE SKIP LOCKED` (no new infra) or Redis/arq. Scheduler leader lock. Heartbeat column instead of reconcile-on-boot |
| S2 | **Postgres does not actually work** despite docs: `check_same_thread` passed unconditionally (`database.py:10`), `ALTER TABLE user` (reserved word, `:46-49`), `json_insert` (`progress.py:20`) and `julianday()` (`runs.py:368,442,507`) are SQLite-only, failures swallowed; psycopg not in deps | Dialect-aware SQL or SQLAlchemy JSON functions; Alembic; a Postgres job in CI |
| S3 | **SSE = DB polling** every 0.5 s per client, re-parsing the whole log JSON; no heartbeat, no max duration | Publish run events to an in-process bus (or Redis pub/sub with S1); SSE heartbeat; cap stream lifetime |
| S4 | **Run row is thin and heavy at once**: no `job_type`, payload snapshot or trigger column, but result + unbounded log as JSON text | Add `job_type`, `payload_snapshot`, `trigger`, `cancel_requested`; `RunLog` and `Artifact` tables; `PendingAction` (A2); `AuditEvent` (gap #12) |
| S5 | **Local-disk state**: uploads, PDFs, browser `*_state.json` not shared across replicas; cookies in plaintext | Storage abstraction (local / S3); encrypt session files with the same key service as F9 |
| S6 | **Browser strategy**: runtime image has no Chromium; `linkedin_apply_tool.py:67,71` and `tw104_apply_tool.py:415` hard-code `headless=False`; 17 separate `sync_playwright` launch sites, 13 copies of the UA string | `tools/_browser.py` shared launcher (stealth args, UA, storage state, SSRF-guarded routes); `browser` image variant with Chromium + xvfb or remote browser (CDP) |
| S7 | `routers/system.py` `_AGENT_DEFS` is hand-maintained (889 lines) against the SSOT principle | Derive from `spec.py` + `agents.yaml` |

### 6.4 Code quality & drift

| # | Issue | Where | Fix |
|---|---|---|---|
| Q1 | **Pricing table is wrong/stale**: Haiku 4.5 at $0.25/$1.25 (actual $1/$5, 4× under); unknown models silently priced at $1/$3; Gemini rows unverified | `harness/costs.py:7, 26` | Refresh; flag `cost_estimated=false` for unknown models instead of guessing |
| Q2 | **Model catalog is dated** and duplicated in UI: only gpt-4o(-mini), Haiku 4.5, Sonnet 4.6; no `claude-sonnet-5-5` ($2/$10) or `claude-opus-5-5` ($4/$20) | `harness/provider.py:26-48`, `ui/app.js` `LLM_MODELS` | Serve catalog from `GET /api/system` (or manifest); UI reads it |
| Q3 | mypy not in CI, lax config; ruff without `B`/`S` rules; no coverage threshold | `pyproject.toml`, `ci.yml` | Add mypy + `--cov-fail-under`, ruff `B`, `S` (bandit) |
| Q4 | **README drift**: "11 automation types" (13), "380 tests" (~780), "touch 6 files to add a job type" (now spec-driven); landing page says "Eight ways" | `README.md:15,168,335`, `ui/index.html:142` | Generate counts in CI or remove hard numbers |
| Q5 | **Untested**: linkedin flow/tool, shopee flow/tool, `x_scraper_tool`, `hn_tool`, `telemetry.py`, cover-letters router, `registry.py` cancel semantics, OAuth callback route, SSRF cases, IDOR on jobs | `tests/` | Add targeted tests; the IDOR + SSRF ones double as regression tests for F1/F3 |
| Q6 | Large files: `linkedin_apply_tool.py` 898, `system.py` 889, `tw_association_tool.py` 711, `runs.py` 648 lines | — | Split by concern once S6/S7 land |
| Q7 | `improvements.md` (2026-05) has no status markers; many items shipped | `doc/improvements.md` | Add a status column or archive it |

### 6.5 What is done well (keep)

Single-source `AutomationSpec` registry with a loud conformance test · one `launch_run` path for HTTP and cron · pure, unit-tested scheduler core · run-level owner scoping (tested) · bcrypt with 72-byte handling and last-admin guards · Fernet-encrypted keys shown masked · the redirect-aware SSRF guard in `tw_association_tool` · size-capped, validated uploads · independent eval judge and never-raise tracing · single-pass stats · Docker test stage + in-container tests · opt-in plugins · the deterministic-funnel principle.

---

## 7. Roadmap

| Phase | Weeks | Deliverables | Gap rows closed |
|---|---|---|---|
| **0 — Harden** | 1–2 | F1–F9 · R3 (idempotent flag) · Q1, Q2, Q4 · A3 notifications MVP (email + webhook + LINE) | 8, 11, partial 12 |
| **1 — Assistant** | 3–6 | A1 Business Profile · A2 Approval Queue + Inbox · A4 "Ask" routing · A10 zh-TW/en toggle · R1, R2 timeouts & real cancel | 1, 2, 7, 18 |
| **2 — Agent** | 6–10 | S1 queue/worker · S2 Postgres + Alembic · S3 event bus · S4 tables · A5 Connectors v1 · A6 Agents · A7 proactive digests | 3, 4, 6, 9 |
| **3 — Platform** | 10+ | A8 tool-enabled custom automations · A9 results workspace · A11 API + webhooks · A12 LINE bot · S5, S6 · A13–A15 | 5, 10, 17, 19, 20, 21 |

Phases 0–1 need no new infrastructure. Phase 2 is where the durable queue and Postgres become mandatory; everything in Phase 1 should be designed so that Agents (A6) is composition, not rewrite.

---

## 8. Positioning

> **"The auditable, self-hosted business agent for Taiwan SMBs."**
> Grok Bot and Muse run on their vendor's computers with their vendor's model. We run on yours, with the model you choose, show you the cost and a second-opinion quality score for every action, ask before anything is sent, and already speak tasker, 104, Shopee, 公會 and 經濟部.

Keep the differentiators sharp: model choice + cost (gap 13), independent judge (14), self-hosting (15), local coverage (16), audit (12). Close the experience gaps (1, 2, 7, 8, 9) before chasing breadth of connectors.

---

## 9. Open questions for the team

1. **Business model first**: self-hosted licence (self-hosted-readiness.md) or hosted SaaS (saas-readiness.md)? It decides whether Phase 2 needs tenancy or packaging.
2. **Primary channel**: web app, LINE bot, or Electron desktop? LINE fits the Taiwan SMB persona; desktop fits the headed-browser apply flows.
3. **Autonomy ceiling**: do we ever let an agent *send* without approval (A2 `auto_approve`)? Suggest: never for applications/emails in v1; allow for read-only and write-to-our-own-Sheet actions.
4. **Which connector first**: LINE (notifications + channel) or Google (Sheets write + Gmail as user)? Both are Phase 0–2; order by the first design partner's workflow.

---

## 10. Sources

- TechCrunch, 2026-09-29 — [Meta is expanding its AI agent Muse to small businesses](https://techcrunch.com/2026/09/29/meta-is-expanding-its-ai-agent-muse-to-small-businesses/)
- CNBC, 2026-09-29 — [Meta launches Muse for Small Business](https://www.cnbc.com/2026/09/29/meta-launches-muse-for-small-business-zuckerberg-pushes-enterprise-ai.html)
- Wikipedia — [Muse (AI agent)](https://en.wikipedia.org/wiki/Muse_(AI_agent))
- MediaPost, 2026-10-01 — [Meta Launches More Business-Facing Muse AI Offerings](https://www.mediapost.com/publications/article/418408/meta-launches-more-business-facing-muse-ai-offerin.html)
- Beam AI — [Grok Bot for Enterprise AI Agents: The 2026 Reality](https://beam.ai/agentic-insights/grok-bot-enterprise-ai-agents)
- Netalith — [What Is Grok Bot? xAI's New AI Agent Explained](https://netalith.com/blogs/ai-tools/what-is-grok-bot)
- Anthropic model pricing (Haiku 4.5 $1/$5, Sonnet 5.5 $2/$10, Opus 5.5 $4/$20 per 1M tokens) — [platform.claude.com/docs](https://platform.claude.com/docs/en/about-claude/models)
