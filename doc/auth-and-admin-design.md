# Auth & Admin — Design & Implementation Plan

> **Status**: Plan · **Date**: 2026-06-25
> **Adds**: simple login + an admin page to the Agent Auto System.

Two features for the Agent Auto System:

1. **Login** — a simple username/password login. No user may view or run any
   automation without being logged in.
2. **Admin page** — manage accounts (create users, set/reset passwords),
   permissions (admin role + per-user automation allowlist), disable users,
   configure LLM API keys **at runtime**, and decide which automations are
   enabled (Form Fill, 利潤健檢, …).

Built on existing infrastructure (FastAPI + SQLModel + a vanilla-JS SPA) with the
lightest dependencies that fit — Starlette's built-in `SessionMiddleware`, `bcrypt`,
and `cryptography` (Fernet). No JWT, no `fastapi-users`.

## Decisions (locked)

| # | Decision | Choice |
|---|---|---|
| 1 | Auth mechanism | **Server-side session cookie** — Starlette `SessionMiddleware` (HttpOnly, signed), `bcrypt` for password hashing. No JWT / `fastapi-users` (overkill for an internal tool over a static UI). |
| 2 | Permission model | **Role + per-user allowlist** — two roles (`admin`/`user`); admin sets a global enabled-automations set; each user has an allowlist of automations they may run. |
| 3 | LLM key storage | **Encrypted at rest** — keys stored in DB, Fernet-encrypted with a key derived from `APP_SECRET`. UI shows masked/configured status only; plaintext never returned. |
| 4 | Admin UI location | **New view inside the existing SPA** (`index.html` + `app.js`), shown only to admins. |
| 5 | User management | **Admin-managed only** — no public signup. Users may change their own password. |
| 6 | Backward compatibility | Env API keys keep working (DB-first, **env fallback**); automations **default to all-enabled**; existing single-user installs get a seeded admin on first run. |

## Current state (what we build on)

- **No auth today.** FastAPI + SQLModel + SQLite (Postgres-swappable via `DATABASE_URL`). Routers: `jobs`, `runs`, `system`, `uploads` — all open.
- **Models:** only `Job` and `Run`. No `User`, no settings table.
- **LLM keys** read from **env vars** at `provider.resolve()` time (`os.getenv(cfg["env"])`) — not runtime-configurable.
- **"Enabled automations"** is implicit, spread across `_FLOW_MAP` (`executor.py`), `_CATALOG` (`system.py`), and the UI form — no on/off concept.
- **UI** is a vanilla-JS SPA (`ui/index.html` + `ui/app.js`), served statically; `navigate(page)` over `VALID_PAGES` nav tabs.
- **Lockfile already contains** `bcrypt` and `starlette`.

## Architecture

```
Browser ──login(username,pwd)──▶ POST /api/auth/login ──▶ verify bcrypt ──▶ session["user_id"]=id (signed cookie)
Browser ──any /api/* (cookie auto-sent)──▶ require_user dep ──▶ load active User from session ──▶ 401 if none
Run a job ──▶ trigger_run ──▶ check job_type ∈ enabled (global) AND (admin OR job_type ∈ user.allowlist) ──▶ 403 else
Admin APIs ──▶ require_admin dep ──▶ 403 if not is_admin
provider.resolve(provider) ──▶ settings_store.get_llm_key() ──▶ DB (Fernet-decrypted) → env fallback
```

## Dependencies

```bash
uv add bcrypt itsdangerous cryptography
```

- `bcrypt` — password hashing (already transitively in lockfile; pin explicitly).
- `itsdangerous` — required by Starlette `SessionMiddleware` for cookie signing.
- `cryptography` — Fernet symmetric encryption for stored API keys.

New environment variables:

| Var | Purpose | Notes |
|---|---|---|
| `APP_SECRET` | Cookie signing + Fernet key derivation | Required outside dev; rotation invalidates sessions and makes stored keys undecryptable. |
| `ADMIN_USERNAME` | First-run admin seed | Default `admin` (warn if used). |
| `ADMIN_PASSWORD` | First-run admin seed | Default `admin` (warn loudly if used). |

## 1. Data model — `src/models.py`

Two new tables, auto-created by `SQLModel.metadata.create_all` (no `ALTER` needed; the
`init_db()` try/except pattern still applies for any future columns).

```python
class User(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    password_hash: str
    is_admin: bool = False
    is_active: bool = True
    allowed_automations: str = "[]"        # JSON list of job_type; "*" = all
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_login_at: datetime | None = None

class Setting(SQLModel, table=True):
    key: str = Field(primary_key=True)     # e.g. "llm_key:openai", "enabled_automations"
    value: str                             # JSON; API-key values Fernet-encrypted
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
```

## 2. Auth core — `src/auth.py` (new)

- `hash_password(pw)` / `verify_password(pw, hash)` — bcrypt.
- `SessionMiddleware` registered in `main.py` (HttpOnly, signed, `secret=APP_SECRET`).
  Login writes `session["user_id"]`; logout clears it.
- FastAPI dependencies:
  - `current_user(request)` → active `User` from session, else `None`.
  - `require_user` → 401 if no/inactive user.
  - `require_admin` → 403 if not `is_admin`.

## 3. Runtime config store — `src/settings_store.py` (new)

- `get_setting`/`set_setting` over the `Setting` table.
- `get_llm_key(provider, env_name)` → **DB value (Fernet-decrypted) first, env fallback**
  → existing `.env` keys keep working unchanged.
- `set_llm_key`/`clear_llm_key` (encrypts before store).
- `get_enabled_automations()` / `set_enabled_automations()` — default = **all enabled**.

**Single change in `provider.resolve()`** (`src/automation/harness/provider.py`):
replace `api_key = os.getenv(cfg["env"])` with
`api_key = settings_store.get_llm_key(effective_provider, cfg["env"])` (lazy import to
avoid a cycle). Also set `os.environ[cfg["env"]]` from the resolved value, since
litellm/Gemini paths read the env var directly.

## 4. Routers

**`src/routers/auth.py` (new)**

| Route | Auth | Purpose |
|---|---|---|
| `POST /api/auth/login` | open | username + password → set session cookie |
| `POST /api/auth/logout` | user | clear session |
| `GET /api/auth/me` | open | current user + `is_admin` + effective allowed/enabled automations (drives UI) |
| `POST /api/auth/password` | user | self-service password change |

**`src/routers/admin.py` (new — every route `Depends(require_admin)`)**

| Route | Purpose |
|---|---|
| `GET/POST/PATCH/DELETE /api/admin/users` | list / create / update (role, `is_active`, allowlist) / delete |
| `POST /api/admin/users/{id}/password` | admin reset password |
| `GET/PUT /api/admin/llm-keys` | per-provider `{configured, masked}`; set/clear (plaintext never returned) |
| `GET/PUT /api/admin/automations` | global enabled-automations set |

**Enforcement on existing routers** — add `Depends(require_user)` when including
`jobs`, `runs`, `uploads`, `system`. In `create_job` **and** `trigger_run`, reject with
403 if `job_type` ∉ global-enabled, or (non-admin) ∉ user's allowlist. `/health` and
`/api/auth/login` stay open.

## 5. Bootstrap — `src/main.py`

- Register `SessionMiddleware`.
- In `lifespan` after `init_db()`: if there are zero users, seed an admin from
  `ADMIN_USERNAME`/`ADMIN_PASSWORD` (warn loudly if defaults are in use).

## 6. Frontend — `ui/index.html` + `ui/app.js`

- **Boot:** `GET /api/auth/me`. On 401 → render a login-only view and hide nav. Add a
  global `fetch` 401 handler that bounces back to login.
- **Admin view:** new `admin` entry in `VALID_PAGES`; nav tab shown only when
  `user.is_admin`. Three sections:
  - **Users** — table + create/edit (role, active toggle, reset password), allowlist
    checkboxes built from `ALL_TYPES`.
  - **LLM Keys** — per provider: configured badge + set/clear input.
  - **Automations** — global enable toggles.
- Filter the Run/landing automation lists to `enabled ∩ allowed` for non-admins.

## Enforcement matrix

| Surface | Anonymous | User (not allowed) | User (allowed) | Admin |
|---|---|---|---|---|
| Login / health | ✅ | ✅ | ✅ | ✅ |
| View runs/jobs | ❌ 401 | ✅ (own only) | ✅ (own only) | ✅ (all) |
| Run automation | ❌ | ❌ 403 | ✅ | ✅ |
| Admin APIs / page | ❌ | ❌ 403 | ❌ 403 | ✅ |

### Activity scoping

`Run.user_id` records who triggered each execution (set in `trigger_run`).
The Activity list (`GET /api/runs`) and every run-level action
(`GET/DELETE /api/runs/{id}`, `…/cancel`, `…/stream`, bulk `DELETE /api/runs`)
filter to the caller's own runs for non-admins; **admins see all** runs, each
tagged with its `owner` username in the UI. A non-admin requesting another
user's run by ID gets **404** (existence is not leaked). The run exports
(`…/report.pdf`, `…/leads.csv`) and `…/steps` apply the same check.

Jobs are scoped the same way on `Job.created_by` (`auth.assert_job_visible`):
`GET /api/jobs` and `/api/schedules` list a non-admin's own jobs, and every
`/api/jobs/{id}` route (read, patch, delete, run, overview) returns **404** for
someone else's job. A job with no recorded owner is admin-only. `/api/stats`
and `/api/overview*` aggregate over the caller's own runs for non-admins.
`tests/integration/test_authz_consistency.py` walks the route table and fails
if a new `{job_id}`/`{run_id}` route is not covered.

## Implementation phases (each independently shippable)

1. **Auth backbone** ✅ *(done)* — deps; `User`/`Setting` models; `auth.py`;
   `SessionMiddleware`; admin seed; login/logout/me endpoints; gate `/api/*`;
   login view. *Outcome: the system is locked behind login.*
2. **Admin: users & permissions** ✅ *(done)* — admin router users CRUD +
   allowlist + last-admin guards; `assert_can_run` enforcement on `create_job`
   and `trigger_run`; admin UI Users section.
3. **Admin: runtime LLM keys & automation toggles** ✅ *(done)* —
   `settings_store` (Fernet-encrypted keys, enabled set); `provider.resolve()`
   reads DB→env; keys + automations endpoints and UI; non-admin run/landing
   filtered to enabled ∩ allowlist.

> **Status:** all three phases implemented on branch `feat/auth-and-admin`.
> 293 unit+integration tests pass; enforcement verified end-to-end on a live
> server (non-admin 403 on admin APIs; allowed+enabled → 201; not-allowed → 403;
> globally-disabled blocks everyone; DB key masked + used by `resolve()`).

## Tests

- **Unit:** bcrypt round-trip; Fernet encrypt/decrypt; key resolver (DB beats env);
  allowlist check helper.
- **Integration:** 401 without cookie; login sets cookie and `me` returns the user;
  non-admin blocked from admin APIs (403); run blocked when automation disabled or not
  in allowlist; runtime-set key picked up by `resolve()`.

## Risks / notes

- Static `/ui` assets stay public (no secrets in JS); the gate is on `/api` and the UI
  self-hides. Acceptable for an internal tool.
- Backward compatibility preserved: env keys still work (fallback); automations default
  to all-enabled; existing installs get a seeded admin.
- `APP_SECRET` rotation invalidates sessions and makes stored keys undecryptable —
  documented; fail fast if unset outside dev.

## Files touched

| File | Change |
|---|---|
| `pyproject.toml` | add `bcrypt`, `itsdangerous`, `cryptography` |
| `src/models.py` | add `User`, `Setting` |
| `src/auth.py` | **new** — hashing, session deps |
| `src/settings_store.py` | **new** — DB settings + key resolver + Fernet |
| `src/routers/auth.py` | **new** — login/logout/me/password |
| `src/routers/admin.py` | **new** — users / llm-keys / automations |
| `src/automation/harness/provider.py` | key lookup via `settings_store` |
| `src/routers/jobs.py`, `runs.py` | `require_user` + per-automation 403 checks |
| `src/main.py` | `SessionMiddleware`, router gating, admin seed |
| `ui/index.html`, `ui/app.js` | login view + admin view + 401 handling |
| `tests/unit`, `tests/integration` | auth/permission/key tests |
