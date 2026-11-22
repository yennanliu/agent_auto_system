# Peer Comparison — OpenWorker & QM vs. Agent Auto System

> Date: 2026-08-05
> Peers reviewed:
> - [`andrewyng/openworker`](https://github.com/andrewyng/openworker) — "an open-source AI coworker that lives on your desktop and delivers finished work, not just chat" (MIT, open beta)
> - [`yc-software/qm`](https://github.com/yc-software/qm) — "a multiplayer agent harness for work"
>
> **Method / caveat.** This review is based on both projects' public READMEs, docs, and
> repository layout — not a line-by-line source read. Claims about *their* internals are
> stated at the level their own docs state them. Claims about *our* system are verified
> against this repo at `eb56a06`.

---

## 1. TL;DR

The three projects are not competitors; they occupy three different corners of the same
space. Understanding which corner we're in is the point of this document.

| | **Agent Auto System** (us) | **OpenWorker** | **QM** |
|---|---|---|---|
| One-liner | Operator-run platform for *pre-built, verticalised* business automations | Local-first desktop AI coworker for *ad-hoc knowledge work* | Org-scale *multiplayer* agent harness with per-person isolation |
| Unit of work | `AutomationSpec` — a typed, code-declared job type | A natural-language outcome; the agent plans the steps | A `skill` owned by a scope, grantable and promotable |
| Who authors work | Engineers (PR) + admins (no-code custom automations) | The end user, in prose | Anyone in the org; admins promote org-wide |
| Agent shape | Deterministic funnel; LLM only for the judgement step | General agent loop over 25+ connectors + shell + files | Delegates the loop to a pluggable coding agent (Pi / OpenCode / Codex / Claude Code) |
| Primary axis of value | **Reliability + verticalisation + cost control** | **Breadth + privacy** | **Isolation + governance** |
| Deployment | Server (FastAPI, Docker/ECS), multi-user behind login | Desktop app (Tauri), single user, local | Org deployment repo (Fly/AWS), Postgres, Slack + web |
| Tech | Python, FastAPI, CrewAI, SQLModel, Playwright, vanilla-JS UI | Python (aisuite) + React/Tauri + Rust STT | TypeScript, Fastify, Postgres, Vite+Lit |

**Our position in one sentence:** we are the only one of the three that treats an
automation as a *product with a quality gate* — a declared spec, a validator, an
independent LLM judge, a rubric, a cost ledger, and a step graph. Neither peer has any of
that. That is the moat to widen.

**Our biggest structural gaps, in order:** (1) no platform-level human-in-the-loop for
irreversible outward-facing actions, (2) no per-user credential/session isolation, (3) no
cross-run memory, (4) no escape hatch for work that isn't already a coded job type.

---

## 2. Positioning map

```
                       BROAD / general-purpose
                                 ▲
                                 │
              OpenWorker ●       │
        (agent plans; 25+        │
         connectors; shell)      │
                                 │        ● QM
                                 │   (pluggable coding-agent
                                 │    harness; per-scope sandbox)
    single user ◄────────────────┼────────────────► many users / org
                                 │
                                 │
                                 │
              ● Agent Auto System│
        (11 coded verticals;     │
         deterministic funnels;  │
         validator + judge)      │
                                 ▼
                       NARROW / verticalised
```

We are deep-and-narrow and multi-user. OpenWorker is broad-and-shallow and single-user.
QM is broad and multi-user but deliberately *owns no verticals at all* — it is
infrastructure under someone else's agent.

---

## 3. Dimension-by-dimension

### 3.1 Extensibility — how a new capability gets added

**Us.** One `register(AutomationSpec(...))` in `src/automation/spec.py` derives dispatch
(`executor._FLOW_MAP`), the allowlist (`settings_store.ALL_AUTOMATIONS`), the validator
check, the judge rubric, and the step graph — with `tests/unit/test_spec_registry.py`
failing loud on inconsistency. Phase 2 added a manifest (`GET /api/automations/manifest`)
so `custom_ui=False` specs render their run form generically. Phase 3 added DB-backed
no-code `custom:<slug>` automations and entry-point plugins.

**OpenWorker.** No authoring step at all — you describe the outcome and the agent
decomposes it. Extensibility is *tool* extensibility: 25+ connectors plus MCP, with
per-tool permissions.

**QM.** `skills` are the unit: scope-owned, shareable by grant, importable from a git
repo, promotable org-wide by an admin. Plus per-scope sandbox tools contributed in
`deploy/layers/<org>/`.

**Verdict.** Our SSOT registry is the best *typed* extensibility story of the three — it
is genuinely hard to add an inconsistent automation here. But it is also the most
expensive: a real new capability is a PR. Our no-code escape hatch (`DynamicCrew`, a
single **no-tools** LLM agent) is far weaker than QM's skills, which can carry files and
run in a sandbox. QM's three-tier distribution model — *own it, grant it, promote it* — is
something we have no analogue for: our automations are global-per-deployment, and a user
cannot build one and share it with a colleague.

### 3.2 Trust and safety — the biggest gap

**Us.** Authorisation is *coarse and up-front*: RBAC per user (`allowed_automations`), a
global enabled set, Fernet-encrypted API keys, and env gates (`BROWSER_LOGIN_ENABLED=0`
on remote hosts). Once a run starts, nothing stops it. There is no approval step, no audit
table (`src/models.py` has `User`, `CustomAutomation`, `Setting`, `Job`, `Run` — no
`AuditEvent`), and no screening of scraped content before it reaches an LLM that then
acts.

This matters because our automations are unusually consequential: `email_sender` sends
real mail via Gmail, `form_fill` submits real Google Forms, `tasker_apply` /
`tw104_apply` / `linkedin_apply` send real applications under the user's identity. A
scheduled cron run does all of that with nobody watching.

We do already have the right *pattern* in one place: `linkedin_apply_tool.py` takes
`dry_run: bool = True` and does everything except the final Submit. That is a per-tool
convention, not a platform primitive.

**OpenWorker.** Approval-gated by construction: "writes, sends, and shell commands are
approval-gated." Unattended scheduled runs do **not** act — they park the approval request
in an **inbox** with the full transcript. Per-tool permissions on top.

**QM.** Three org-wide security postures, which narrower scopes may only *tighten*:
- **Strict** — every harness tool call pauses for a human except turn-enders.
- **Auto** (default) — a classifier screens external data and tool results before the
  model sees them (i.e. explicit prompt-injection defence).
- **Dangerous** — no screening, no pauses.

Plus predeclared command policies (approval rules and hard denials for destructive ops)
that apply under *all* postures, and a full audit trail on the premise that "agents act as
the person using them with their credentials."

**Verdict.** Both peers treat "the agent is about to do something irreversible" as a
first-class platform concern. We treat it as a per-tool implementation detail. This is our
single largest gap and the one with the worst failure mode — a bad scrape that silently
emails 200 strangers is not recoverable. See recommendation **R1**.

### 3.3 Agent architecture

**Us.** Documented invariant: *deterministic-funnel flows drive the tools directly and use
the crew only for the LLM-judgement step*. Fast, cheap, debuggable, and the reason
`email_collect` can merge three discovery sources and dedupe on registrable domain
reliably. The ceiling is equally clear: anything not pre-coded is impossible.

**OpenWorker.** A general loop over a fixed tool catalogue, built on `aisuite`. Maximum
flexibility, minimum determinism, highest token cost per outcome.

**QM.** Owns no loop. It wraps an existing coding agent and hands it a sandbox. The
implicit bet is sharp: *an agent that can write code and run a shell is a superset of any
fixed tool catalogue.* Its job is identity, policy, persistence, and scheduling — the
things a coding agent doesn't have.

**Verdict.** Our choice is right for repeatable business processes and we should not
abandon it. But QM's bet is worth partially copying: a single **general-purpose job type**
with a real toolbelt (fetch, browser, sandboxed Python) would absorb all the one-off
requests that currently need a PR — and, crucially, would run inside our existing harness,
so it still gets validated, judged, priced, and traced. That is something neither peer's
general agent gets. See **R4**.

### 3.4 Isolation and multi-tenancy

**Us.** Single process. Runs are `asyncio.create_task` in the web process
(`launcher.launch_run`), executing with the *server's* credentials against the server's
filesystem. Two concrete consequences:

- **Browser sessions are deployment-global.** `browser_session.py` resolves one
  storage-state file per site (`data/tasker_state.json`, `data/shopee_state.json`,
  `data/tw104_state.json`, `data/linkedin_state.json`). In a multi-user deployment, user
  A's `tasker_apply` run submits proposals as whoever last logged in. For an
  applications-and-outreach product that is not a rough edge, it is a correctness bug.
- **No execution sandbox.** A flow can read `uploads/` for every user and the SQLite file.

**QM.** Per-scope sandbox with its own files, tools, and authenticated services; per-scope
keychain; consistent identity across Slack and web.

**OpenWorker.** Single-user by construction — isolation is the machine boundary. Only the
OAuth broker is cloud-side, and manual credentials bypass even that.

**Verdict.** Our RBAC answers "may you run this?" but not "*as whom* does it run?" QM
answers both. Per-user credential and session scoping is the prerequisite for us being
genuinely multi-tenant. See **R2**.

### 3.5 Scheduling and background work

**Us.** `CronScheduler` ticks every `SCHEDULER_INTERVAL`s; `_sync_and_collect()` is a
pure, unit-tested due-detection core; `cron_utils.py` wraps croniter with macros and
next-fire; `GET /api/schedules` surfaces next-run and last-run summary. Solid — arguably
the most rigorous of the three, and the only one with a pure-function core under test.

**OpenWorker.** Recurring automations (morning brief, weekly report, channel monitoring),
each run producing a transcript and an approval queue.

**QM.** **Crons *and watches*** — time-triggered and event-triggered.

**Verdict.** Our cron machinery is good and we should keep it. Two ideas to take: (a)
**watches** — our lead-gen and job-application verticals are naturally event-shaped ("new
104 posting matching X", "new Shopee competitor price"), and today the only expressible
trigger is "every hour, re-scrape everything"; (b) coupling scheduling to approval, per
**R1** — an unattended run is exactly when a gate matters most.

### 3.6 Surfaces

**Us.** One browser UI (`ui/app.js`, ~3.5k lines vanilla JS) + an Electron shell. Results
leave as downloads (`/leads.csv`, `/report.pdf`).

**OpenWorker.** Desktop app *and* Slack mentions.

**QM.** Slack (Bolt), web (Vite+Lit), admin, and a public portal — all **plugin surfaces**
over a headless core, with one identity across them.

**Verdict.** We are single-surface, and it costs us. A lead list that a salesperson has to
log in and download is worth less than one delivered into a channel. QM's headless-core +
plugin-surface split is the right architecture for this, and our FastAPI core is already
close to headless — the coupling is that the UI is hand-written per automation, which
Phase 2's manifest is already unwinding. Landing a Slack (or LINE, given the TW SMB focus)
surface on top of the manifest is a well-shaped next step. See **R6**.

### 3.7 Memory and state

**Us.** None. Every run is stateless; dedupe in `email_collect` is within-run only. Run a
lead-collection job twice and you re-collect and can re-email the same businesses.

**OpenWorker.** A `memory/` module plus `conversations.py` / `sessions.py`.

**QM.** Per-scope memory in Postgres, isolated per person and room.

**Verdict.** For a chat agent, memory is a nice-to-have. For a *lead-generation funnel*,
cross-run memory is functional correctness — "have we already contacted this domain?" is
the question the product exists to answer. Cheapest high-value item on this list. See
**R3**.

### 3.8 Observability, evaluation, cost

**Us.** Langfuse trace per run (no-op unless keyed), 0.5s SSE progress stream, token and
cost accounting (`harness/costs.py`), an independent LLM judge scoring 0–100 with a
documented independence invariant (never the model that produced the output) and a
heuristic fallback, a per-automation rubric, a validator quality gate with
error-injecting retries, cross-model fallback within a provider, and an Airflow-style
runs × steps grid (`/api/jobs/{id}/overview`).

**OpenWorker.** Full transcripts per run, in an inbox.

**QM.** Full audit trail of agent actions.

**Verdict.** **We win this dimension outright, and it is not close.** Transcripts and
audit logs answer *what happened*; only we answer *was it any good, and what did it cost*.
The gap in our own story is that this is all **per-run and never aggregated**: we compute
an eval score on every run and then never ask "is `email_collect` getting better or worse
over time?" or "would this prompt change regress the last 50 runs?" We have the judge but
no golden set and no offline eval. See **R5**.

### 3.9 Model strategy

All three are BYO-key and multi-provider — table stakes. Differences:

- **Us:** provider+model chosen per run from the UI (`LLM_MODELS` in `ui/app.js`),
  per-flow temperature, `fallback_sequence()` retrying within a provider on transient
  failure, cost priced per model.
- **OpenWorker:** widest provider list (incl. DeepSeek, Mistral, Ollama-local) via
  `aisuite`; switchable any time.
- **QM:** swaps the *whole harness*, not just the model — Pi / OpenCode / Codex / Claude
  Code — and admins choose which harnesses and models the org may use.

**Verdict.** Near-parity. Two small ideas: local models via Ollama (OpenWorker) would let
the cheap classification steps in `email_collect` run at zero marginal cost; and
admin-level *model policy* (QM) is a natural extension of our existing allowlist — today
an admin controls which automations a user may run but not which models they may spend on.

### 3.10 Deployment and org customisation

**Us.** Docker (~450MB slim image), ECS Fargate guide, env-var configuration, SQLite→
Postgres path.

**QM.** The interesting one: `qm init` scaffolds an **org deployment repo** that merely
*depends on* `@yc-software/qm`, with all org customisation confined to
`deploy/layers/<org>/`, so core stays byte-identical to upstream and private forks merge
upstream cleanly. Deployment itself is executed by an agent skill
(`.codex/skills/deploy-qm/`) that confirms billing, wires auth, and runs health checks.

**OpenWorker.** Signed DMG / Windows installer with auto-update.

**Verdict.** QM's layering discipline is the answer to a problem we will have the moment a
second party runs this: today customisation means editing `spec.py` and `ui/app.js`, which
forks the code. A `config/overlays/<org>/` directory read at startup (extra specs, branding,
enabled set, prompt overrides) would give us the same clean-upstream property cheaply.
Deployment-as-a-skill is also a cheap, high-charm copy given we already run Claude Code
here.

---

## 4. What we should be careful not to lose

Enumerated explicitly, because most of the recommendations below pull toward the peers'
generality and that generality has real costs.

1. **Deterministic funnels.** An `email_collect` run that drives three source tools in
   Python and calls the LLM once is cheaper, faster, and far more reproducible than an
   agent deciding to make forty tool calls. Our CLAUDE.md invariant is correct.
2. **The quality gate.** Validator → independent judge → rubric, plus error-injecting
   retries. Unique among the three.
3. **Cost accountability.** Per-run token and USD attribution.
4. **Vertical depth.** MOEA GCIS open data, TW 公會/工會 directories, 104, tasker, Shopee —
   and the hard-won correctness details, like `maps_search_tool.resolve_websites()`'s
   name-corroboration check that stops a stranger's email being filed under a real
   company. Horizontal harnesses have nothing like this and can't cheaply acquire it.
5. **SSOT + a test that fails loud.** `test_spec_registry.py` is why the derived tables
   never drift.

---

## 5. Recommendations, ranked

Impact × effort, most valuable first. Each names the concrete surface it touches.

### R1 — Platform-level action gate + approval inbox  ·  impact: **critical**  ·  effort: M

The gap from §3.2. Generalise `linkedin_apply_tool`'s `dry_run` into a platform primitive.

- Add `consequential: bool` (or a finer `effects: ("send"|"submit"|"write")` tuple) to
  `AutomationSpec`. It is declarative, so it lands in the SSOT and the manifest for free.
- Add a run-level `posture` — `preview` | `approve` | `auto` — defaulting to `approve` for
  any consequential spec. `preview` runs the full funnel and returns what *would* have been
  sent.
- Add `RunApproval` (run_id, payload preview, requested_at, decided_at, decided_by,
  decision). Executor pauses the run at the gate, appends to the SSE log, and waits.
- New surface: an **approval inbox** page listing pending gates — this is exactly
  OpenWorker's answer for unattended runs, and it is what makes our cron scheduler safe to
  point at `email_sender`.
- Add an `AuditEvent` table while you're here: run_id, actor, tool, target, outcome. QM's
  premise — the agent acts as the person, so everything is audited — should be ours too,
  since our agents act *outward*, under a user's identity.

### R2 — Per-user credential and session scoping  ·  impact: **high**  ·  effort: M–L

The multi-tenancy bug from §3.4.

- Key browser storage-state by user: `data/sessions/{user_id}/{site}.json`, resolved
  through `browser_session.py` rather than a single env path. Keep the env path as the
  single-user/local default so nothing breaks.
- Extend the existing Fernet key store into a general per-user secret store (Gmail creds,
  site logins), so `POST /api/sessions/{name}/login` refreshes *your* session.
- Surface whose identity a run will act as, in the run form. Silent ambiguity here is the
  actual danger.

### R3 — Cross-run entity memory  ·  impact: **high**  ·  effort: **S**

Best value-per-hour item on the list (§3.7).

- One table: `Contact` (registrable domain, normalised name, source, first_seen,
  last_contacted_run_id, status), written by `email_collect` and read by both
  `email_collect` (skip known) and `email_sender` (never re-mail inside N days).
- Reuse the normalisation that already exists in the funnel — registrable-domain plus
  alias/spacing/台-臺 folding — so the key is consistent with in-run dedupe.
- Immediate wins: no duplicate outreach, "new leads only" as a real mode, and a
  suppression list for opt-outs (which is also a compliance need for outbound mail).

### R4 — A general-purpose job type ("ad-hoc agent")  ·  impact: medium-high  ·  effort: M

The escape hatch from §3.3. Our `custom:<slug>` automations are a single **no-tools** LLM
agent, which is why they can't do the things people actually want.

- Give `DynamicCrew` an opt-in toolbelt: HTTP fetch, the existing web scraper, and a
  sandboxed Python step. Admin-gated per custom automation, defaulting to none.
- Because it runs through the harness, it still gets validated, judged, priced, traced, and
  step-graphed — an agent loop with a quality gate, which neither peer offers.
- Pairs with R1: a general agent is exactly what you want gated at `approve` by default.

### R5 — Offline eval: golden sets and quality trend  ·  impact: medium-high  ·  effort: M

Compound the advantage from §3.8 instead of leaving it per-run.

- Fixture-backed golden runs per automation (recorded scrape payloads → expected shape),
  runnable in CI without spending on live scrapes. `scripts/smoke_test.py --no-run`
  already establishes the offline-CI pattern.
- Aggregate `eval_score` per automation over time and chart it on the stats page — a
  regression signal for prompt and model changes.
- Gate prompt/model changes in CI on "no regression against the golden set." This is the
  natural end state of already having a judge, and it is the strongest possible answer to
  "how do you know a model swap didn't break `email_collect`?"

### R6 — A second surface (Slack or LINE)  ·  impact: medium  ·  effort: M

From §3.6. Trigger a run and receive the result where the work happens. The Phase-2
manifest already describes every generic automation's form, so a chat surface can render
its own prompts from the same data rather than hand-coding per automation. Deliver
`leads.csv` / `report.pdf` into the thread instead of behind a login. Given the TW SMB
focus, LINE may beat Slack on reach.

### R7 — Config overlays for org customisation  ·  impact: medium  ·  effort: **S**

From §3.10. A `config/overlays/<org>/` directory read at startup — extra specs, branding,
enabled automations, prompt overrides — so a deploying org never edits `spec.py` or
`ui/app.js` and can merge upstream cleanly. Cheap now; expensive to retrofit after someone
has forked.

### R8 — Event-driven triggers ("watches")  ·  impact: medium  ·  effort: M

From §3.5. A watch = a cheap poller + a change predicate + a job to fire. Most of the
machinery exists (`CronScheduler`, `launch_run`, the scraper tools); what's missing is a
persisted last-seen digest per watch and a predicate. Unlocks "alert me on new 104
postings matching X" and "tell me when a competitor's Shopee price moves" — which is what
these scrapers are for, expressed correctly.

### R9 — Local models for cheap steps  ·  impact: low-medium  ·  effort: S

From §3.9. Ollama in `provider.py`'s provider list. The high-volume classification steps
in `email_collect` (relevance, is-this-the-right-company) are exactly the workload where a
local 8B model at zero marginal cost is good enough, and cost is per-lead here.

### R10 — Skill sharing / grants  ·  impact: low now, high later  ·  effort: L

From §3.1. QM's own-it → grant-it → promote-it model for user-authored automations. Not
urgent at current user count, but worth keeping in mind as the shape `custom_automations`
should grow into rather than something bolted on later.

---

## 6. Deliberately *not* copying

- **A general agent loop as the primary execution model.** Our determinism is a feature.
  R4 adds an escape hatch beside the funnels, not a replacement for them.
- **Desktop-first / local-only.** OpenWorker's privacy story depends on being single-user.
  Ours is a shared operator platform; the server model is correct for it.
- **Delegating the loop to an external coding agent (QM's core bet).** It buys generality
  at the price of every one of our per-step guarantees — the validator, the rubric, the
  step graph, the cost ledger. Wrong trade for us.
- **A "Dangerous" posture with no screening.** QM can offer it because its agents mostly
  act on the org's own repos. Ours send mail to third parties.
- **Speech-to-text.** Real work in OpenWorker (a Rust sidecar), no value in a
  configure-and-schedule product.

---

## 7. Suggested sequencing

| Phase | Items | Theme |
|---|---|---|
| 1 | **R1**, **R3**, R7 | Make consequential actions safe, and stop re-contacting people. The two things that are currently *wrong*, plus the cheap overlay foundation. |
| 2 | **R2**, R5 | Make multi-tenancy real; turn per-run eval into a regression signal. |
| 3 | R4, R6 | Widen what users can do without a PR; meet them on a second surface. |
| 4 | R8, R9, R10 | Event triggers, cost floor, sharing model. |

R1 and R3 are the two items where the current behaviour is not merely missing a feature
but capable of doing something the user would not have sanctioned — sending mail nobody
approved, or sending it twice. They should go first regardless of how the rest is ordered.

---

## 8. Sources

- OpenWorker — <https://github.com/andrewyng/openworker> (README, `coworker/` layout,
  `docs/`); built on [`aisuite`](https://github.com/andrewyng/aisuite).
- QM — <https://github.com/yc-software/qm> (README, `docs/getting-started.md`,
  `docs/deploy-directory.md`, repo layout). Note: `adrs/` is present but empty
  (`.gitkeep`) as of this review, so no decision records were available.
- This repo at `eb56a06`: `CLAUDE.md`, `src/automation/spec.py`, `executor.py`,
  `harness/*`, `scheduler.py`, `browser_session.py`, `models.py`,
  `tools/linkedin_apply_tool.py`, `doc/automation-extensibility-design.md`.

Related internal docs: [`doc/improvements.md`](improvements.md) (2026-05-27 code-quality
audit — narrower scope, mostly internal-quality items), [`doc/saas-readiness.md`](saas-readiness.md),
[`doc/self-hosted-readiness.md`](self-hosted-readiness.md),
[`doc/automation-extensibility-design.md`](automation-extensibility-design.md).
