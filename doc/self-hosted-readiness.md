# Self-Hosted / Distributed Model — Readiness Roadmap

> Audit date: 2026-07-14
> Sales model: **ship the app to each customer; every customer runs their own
> instance in their own environment** (their server, their LLM keys, their data).
> This is the "self-hosted / on-prem" model — think GitLab CE/EE, n8n, Metabase,
> Ghost, Plex — *not* a multi-tenant SaaS.
> Companion doc: [saas-readiness.md](saas-readiness.md) (the multi-tenant path).

## TL;DR — the model change flips the priorities

In this model the customer's instance **is** their tenant. That single fact
deletes most of the multi-tenant SaaS blockers and promotes a different set.

**What drops off (was P0 for SaaS, now a non-issue or nice-to-have):**

| Concern | Why it no longer blocks |
|---|---|
| Multi-tenancy / org model | Each instance serves one customer — single-tenant is now *correct*, not a bug. |
| Cross-tenant data isolation | There is no other tenant on the box. In-company multi-user RBAC still matters (see P1-4), but the flat model is fine. |
| Self-serve signup | The customer installs it; the seeded admin creates their own team. No public signup. |
| Billing / quota / rate-limit / usage metering | The customer pays their own LLM + compute bill directly. You monetize via **licensing** (P0-3), not metered usage. |
| Horizontal scale, Redis queue, Postgres-required | One team on one box. SQLite + in-process tasks + single-instance scheduler are all *acceptable*. Offer Postgres as an option, don't require it. |
| Per-tenant credential isolation | The shared `data/shopee_state.json` etc. is fine — it's one customer's own account on their own box. |

**What becomes primary (the new job):**

| New pillar | State today | Severity |
|---|---|---|
| **Distribution & packaging** (one-command install, versioned releases) | Dockerfile + compose exist; no published image, no release process | 🔴 |
| **Install & first-run config UX** (no terminal, no hand-edited `.env`) | `.env` + CLI login scripts + `playwright install` — all terminal-only | 🔴 |
| **Licensing / monetization** (how you actually get paid) | None — nothing gates or meters the software | 🔴 |
| **Updates & migrations** (upgrade a live install without losing data) | Hand-rolled `ALTER TABLE` in `init_db()`, no Alembic, no version check | 🔴 |
| **Secure-by-default out of the box** | Ships `admin`/`admin` + `APP_SECRET=dev-insecure-change-me` | 🔴 |
| **Support & telemetry at a distance** (you can't SSH into their box) | Local Langfuse/Prometheus only; no phone-home, no support bundle | 🟠 |
| **Backups & data durability** (their responsibility, your tooling) | Docker volumes; no backup/restore command | 🟠 |

**Bonus: this model is legally & commercially cleaner.** The customer runs the
scrapers on *their own* accounts in *their own* environment, and their data / LLM
keys never touch your servers. That's a genuine selling point (privacy, data
residency, no vendor lock-in) *and* it shifts the Shopee/104/Tasker/X anti-automation
ToS exposure onto the operator rather than you. Say so in your marketing.

---

## P0 — Blockers (can't ship a sellable product without these)

### 1. Distribution & packaging

The product is now the *artifact you hand over*, not a URL. The foundation is good
(multi-stage `Dockerfile` with WeasyPrint fonts + healthcheck; `docker-compose.yml`
with app + Prometheus + named volumes), but there's no *release*.

**TODO**
- [ ] **Publish versioned Docker images** to a registry (GHCR/Docker Hub) tagged by
      semver — `agent-auto-system:1.2.0` + `:latest`. Today the image is only built
      locally (`image: agent-auto-system:local`).
- [ ] **One-command install**: a `docker compose up` that pulls the published image
      (not `build:`), plus a documented `curl | sh` or a short quickstart. The whole
      pitch is "run this one line."
- [ ] Ship a **`.env.example` → guided setup** (see P0-2) so install ≠ editing YAML.
- [ ] Decide the **minimum footprint** and document it: the browser automations
      (Playwright/Chromium — 6 of the automations) plus WeasyPrint make this a
      RAM/CPU-heavy container. Give real minimum specs (e.g. 2 vCPU / 4 GB) and note
      which automations need Chromium.
- [ ] Pin/rebuild for reproducibility; `uv sync --frozen` is already used in the
      builder stage — extend that discipline to a tagged, scanned release image.
- [ ] Consider a non-Docker path (native `uv` install) for customers who won't run
      Docker — or explicitly declare Docker the only supported target.

### 2. Install & first-run configuration UX

This is the single biggest gap for *non-technical* buyers. Everything is
terminal-driven today:
- Config is a hand-edited `.env` (LLM keys, admin creds, integration creds, paths).
- Browser deps need `uv run playwright install chromium`.
- Each browser integration needs a **separate CLI login script** run by hand:
  `scripts/shopee_login.py`, `scripts/tasker_login.py`, `scripts/104_login.py`
  (opens a headed Chromium, solve captcha, press Enter).

A customer running their own instance should never touch a terminal after install.

**TODO**
- [ ] **In-app first-run setup wizard**: on first boot with no config, walk the admin
      through setting the admin password, entering LLM keys (the admin LLM-keys UI
      already exists — make it the onboarding step), and choosing enabled automations.
- [ ] **In-app "Connect account" flows** to replace the three CLI login scripts. A
      button that launches the headed/remote browser login and saves the encrypted
      session — same mechanism, no terminal. (This was also recommended for SaaS, but
      here it's about *usability*, not multi-tenancy.)
- [ ] Bundle Chromium in the image (or auto-run `playwright install` on first start)
      so the browser automations work out of the box.
- [ ] **Config precedence** that favors the UI: DB-stored settings already beat env
      (`settings_store.get_llm_key`) — extend that so a customer never *has* to edit
      `.env`; env becomes the advanced/automation path only.
- [ ] A `/health`-style **readiness page** in the UI showing what's configured vs.
      missing (keys present, Chromium installed, integrations connected) — turns
      "why doesn't it work" into a self-service checklist.

### 3. Licensing & monetization

You're selling software that runs on someone else's machine — so there's nothing
today that (a) proves entitlement, (b) gates paid features, or (c) expires a trial.
This has to be designed deliberately; grep confirms **zero** licensing/version/edition
code exists.

**TODO**
- [ ] **Decide the model**: perpetual license, annual subscription with a license key,
      or open-core (free Community edition + paid Pro automations/features). Open-core
      fits this codebase well — the automation catalog is a natural paywall line.
- [ ] **License key + activation**: a signed license (offline-verifiable, e.g. Ed25519
      signature over `{customer, edition, expiry, features}`) checked at startup and
      surfaced in the UI. Offline verification matters — customers on-prem may not have
      outbound internet.
- [ ] **Edition/feature gating** wired into the automation allowlist you already have
      (`settings_store.ALL_AUTOMATIONS` + `assert_can_run`) — Pro automations simply
      aren't enabled without a Pro license.
- [ ] **Trial** support (time-limited license) with graceful expiry (read-only or
      disabled runs, not data loss).
- [ ] Keep it *light* — heavy DRM annoys legitimate customers and is trivially cracked;
      the goal is honest-customer entitlement + a clear upgrade path, not unbreakable
      copy protection.

### 4. Updates, versioning & migrations

Customers will run version N and you'll ship N+1. You **cannot** tell them "delete the
DB" — their data is the product's value. Today migrations are hand-rolled
`ALTER TABLE … ADD COLUMN` wrapped in `try/except: pass` in `init_db()`
(`database.py:22-55`), with a SQLite-only `julianday()` backfill (`database.py:60`).
No version tracking, no rollback, no down-migrations.

**TODO**
- [ ] **Alembic** with real, ordered, reversible migrations that run automatically on
      container start (`alembic upgrade head` before uvicorn). This is *more* critical
      here than in SaaS — you don't control the upgrade cadence or the data.
- [ ] **App version** surfaced in `/health` and the UI footer (there's no version marker
      in `src/` today; `pyproject.toml` is `0.1.0`). Customers and your support need to
      know what they're running.
- [ ] **Upgrade path testing** in CI: boot vN's DB, apply vN+1 migrations, assert it
      works. Migration bugs are catastrophic when they hit customer data you can't see.
- [ ] **Release channels / changelog / update notice**: an in-app "a new version is
      available" banner (opt-in version check) + a published CHANGELOG.
- [ ] Backward-compatible config: never require a customer to rewrite `.env` on upgrade.

### 5. Secure-by-default out of the box

An internal tool behind the corporate firewall can get away with `admin`/`admin`. A
product a customer exposes on *their* network (possibly the internet) cannot. Today
`.env.example` ships `ADMIN_PASSWORD=admin` and `APP_SECRET=dev-insecure-change-me`,
and those are the runtime fallback defaults (`main.py:22,36`) with only a log warning.

**TODO**
- [ ] **Generate a random `APP_SECRET` on first boot** if unset (persist it to the DB /
      a data-dir file) instead of falling back to the shared dev default — otherwise
      every install ships with the same cookie-signing + key-encryption secret, which is
      a real vulnerability once the code is distributed publicly.
- [ ] **Force admin password change on first login**; never leave `admin`/`admin`
      usable. Print a one-time random password to the container logs, or require setup
      via the wizard (P0-2).
- [ ] `SessionMiddleware(https_only=True, same_site="lax")` and ship TLS guidance
      (reverse-proxy / Caddy auto-HTTPS in the compose file).
- [ ] SSRF blocklist in `WebScraperTool` (RFC-1918 + cloud metadata) — the operator's
      internal network is now the thing at risk.
- [ ] A short **hardening checklist** in the docs (change secrets, put behind TLS, don't
      expose Prometheus :9090 publicly — today compose maps it to the host).

---

## P1 — Major (reliability & fit for real customers)

### 6. Support & telemetry at a distance

You can't log into a customer's box. When something breaks, you need signal.

**TODO**
- [ ] **Opt-in phone-home telemetry**: version, edition, enabled automations, run
      success/failure counts, error types (no PII, no scraped data, no keys). Off by
      default with clear disclosure; critical for knowing what's deployed and what's
      failing in the field.
- [ ] **Support bundle**: a "Download diagnostics" button that packages recent logs +
      sanitized config + version + `/health` output for the customer to send you.
- [ ] **Structured JSON logging** (still missing — unstructured stdout today) so the
      support bundle is actually parseable.
- [ ] **Error tracking** (self-hostable Sentry / GlitchTip) as an *option* the customer
      can point at their own instance, or an opt-in shared one.
- [ ] Keep Langfuse/Prometheus **local to the customer** (they already are) — document
      how the customer reads their own traces/metrics; don't route them to you.

### 7. Data durability & backups

The customer's SQLite file (or Postgres) and `uploads/`/`reports/` volumes are their
data. `reconcile_stale_runs()` (marks in-flight runs failed on restart) is good, but
there's no backup story.

**TODO**
- [ ] A **backup command** (`docker compose exec app python -m scripts.backup`) that
      snapshots the DB + uploads/reports to a single archive, and a documented restore.
- [ ] Document the volume layout and a "back this up" list (compose already names
      `app_data` / `app_uploads` / `app_reports`).
- [ ] SQLite is fine for a single team, but document **when to switch to Postgres**
      (many concurrent users / heavy history) and make the switch a one-line
      `DATABASE_URL` change (already supported) — plus Alembic (P0-4) so the switch is
      clean.
- [ ] Data-retention controls (prune old runs) the customer can run themselves.

### 8. Modularity — shipping (and letting customers add) automations

Still important, for two reasons now: (a) new automations are your *release content*
and upsell surface; (b) some self-hosted customers will want to add their own.

The problems from the SaaS audit are unchanged: adding a job type touches **~8 files /
~11 hand-synced registries** (CLAUDE.md's "6" undercounts and omits `flow_steps.py`),
there's **no plugin/registry system**, and drift already exists (`tw104_apply` is
missing its `workflows` entry in `system.py` `_CATALOG`). There's also a likely-broken
`pipeline` (`pipeline.py:55` unpacks a 2-tuple from a 3-tuple return, `executor.py:143`).

**TODO**
- [ ] **Single per-automation manifest** (schema + metadata + flow/crew + steps +
      validator + rubric) that drives dispatch, catalog, allowlist, overview grid, and
      the UI form — one source of truth instead of 11.
- [ ] **Registration by discovery** (decorator / entry-point scan) so an automation
      self-registers by existing.
- [ ] **Schema-driven UI forms** (replace the two hand-written `switch(jobType)` blocks
      in the 3,083-line `ui/app.js`) — the lever that could eventually let customers
      define their own automations, an open-core Pro feature.
- [ ] **Conformance test** that all registries agree (would have caught the `tw104`
      drift). Fix the `pipeline.py:55` unpack bug.
- [ ] A **scaffold/generator** for a new automation (flow + crew + manifest + tests),
      publishable as an extension SDK if you want a community/marketplace later.

### 9. In-company multi-user & RBAC

One box, but often several employees. The existing model (session cookies, admin +
per-user automation allowlist, per-user run scoping) is *about right* for one trust
boundary — polish it rather than rebuild it.

**TODO**
- [ ] Scope `GET /jobs` + job edit/delete by owner (or make job sharing explicit) —
      today any logged-in user sees/edits/runs every job (`jobs.py:47,72,102`). Lower
      severity here (same company) but still a footgun.
- [ ] Keep the admin/user roles; add an audit log of who ran/changed what (useful when
      a team shares one instance).

---

## P2 — Polish (conversion, retention, professionalism)

- [ ] **Self-hosting docs**: install, upgrade, system requirements, TLS setup,
      backup/restore, troubleshooting, per-automation setup (esp. the browser + Gmail
      app-password + integration-login steps). Today's docs are contributor-facing.
- [ ] **In-app onboarding**: sample/templated jobs, empty-state guidance, "run your
      first automation" wizard.
- [ ] **Notifications** (email/webhook/Slack) on run completion/failure — the operator
      isn't watching the dashboard live.
- [ ] Carry over open UX items from [improvements.md](improvements.md) §9: auto-open the
      run stream after triggering, a Retry button on failed runs, runs-list pagination,
      and a confirm dialog before bulk-delete.
- [ ] **Customer-facing API + API keys** for customers who want to script their own
      instance (nice-to-have; single-instance so no tenant scoping needed).
- [ ] **Legal**: EULA / license agreement, a privacy note that data stays on their box,
      and clear language that the operator is responsible for how they use the scrapers
      against third-party sites' terms.
- [ ] Ground `costs.py` pricing so the customer's own cost dashboard is accurate (they
      care — it's their money).

---

## Suggested sequencing

1. **Ship-ability (P0):** published versioned image + one-command compose + Alembic
   migrations + secure-by-default (random secret, forced password). Without these you
   can't hand it to a stranger.
2. **Install UX (P0):** first-run wizard + in-app account-connect flows + bundled
   Chromium — so a non-engineer can stand it up.
3. **Get paid (P0):** licensing/activation + edition gating on the automation allowlist.
4. **Operate at a distance (P1):** opt-in telemetry + support bundle + structured logs +
   backup/restore.
5. **Grow the catalog (P1):** automation manifest + registry + schema-driven forms +
   conformance test + scaffold.
6. **Polish (P2):** docs, onboarding, notifications, EULA.

## Quick wins (do anytime)

- [ ] Generate a random `APP_SECRET` on first boot; stop shipping the shared dev default.
- [ ] Force admin password change on first login; stop shipping usable `admin`/`admin`.
- [ ] Surface the app version in `/health` and the UI footer.
- [ ] Publish the Docker image to a registry and switch compose to `image:` not `build:`.
- [ ] Add the registry-conformance test; fix the `pipeline.py:55` unpack bug.
- [ ] SSRF blocklist in `WebScraperTool`.
- [ ] Don't expose Prometheus :9090 to the host by default in the shipped compose file.

---

## Which model should you pick?

Not a code question, but it shapes everything above:

| | Multi-tenant SaaS ([saas-readiness.md](saas-readiness.md)) | Self-hosted (this doc) |
|---|---|---|
| Biggest build cost | Tenancy, isolation, billing, scale | Packaging, licensing, install UX, updates |
| Who holds the data & keys | You | The customer (privacy/residency win) |
| Who pays LLM/compute | You (must meter & mark up) | The customer directly |
| Scraping ToS exposure | Largely yours | The operator's |
| Ops burden | High (you run it 24/7) | Low (customer runs it) |
| Support | Direct access to logs/DB | Blind — needs telemetry + support bundles |
| Revenue mechanic | Subscriptions + metered usage | License keys / open-core |
| Time-to-first-sale | Longer (must build the platform) | Shorter (this codebase is close) |

Given where the code is today (solid single-tenant app, already Dockerized, no billing
or tenancy), **self-hosted is by far the shorter path to a first paying customer** — the
main net-new work is licensing, a friendly installer/wizard, Alembic, and
secure-by-default. The multi-tenant SaaS is the bigger long-term market but a much larger
build.
</content>
