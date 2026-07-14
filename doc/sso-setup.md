# SSO / OAuth Login Setup (Google & GitHub)

The app supports "Sign in with Google" and "Sign in with GitHub" in addition to
username/password. A provider's button appears on the login page **only when its
credentials are configured** — no credentials means no button (by design).

- **Provisioning:** any Google/GitHub user who signs in gets a new **non-admin**
  account with **no automations** (an admin grants access afterward in Admin →
  Users).
- **Account linking:** if the provider reports a **verified** email that matches
  an existing account with no OAuth link yet, the SSO identity attaches to that
  existing account (so a password user can also sign in via SSO).

Implementation: `src/oauth.py` (client registration + account resolution),
`src/routers/oauth.py` (redirect + callback), UI in `ui/app.js` / `ui/index.html`.

---

## The callback (redirect) URL

Each provider must be told exactly where to send the user back. The pattern is:

```
<YOUR_BASE_URL>/api/auth/oauth/<provider>/callback
```

For local dev on port 8000:

| Provider | Callback URL |
|---|---|
| Google | `http://localhost:8000/api/auth/oauth/google/callback` |
| GitHub | `http://localhost:8000/api/auth/oauth/github/callback` |

In production, swap in your real HTTPS base URL, e.g.
`https://auto.example.com/api/auth/oauth/google/callback`.

> The app derives this URL from the address you actually browse to. If you open
> the app at `http://127.0.0.1:8000`, it sends a `127.0.0.1` callback — which
> will **not** match a `localhost` registration. Pick one and be consistent
> (or register both).

To see the exact `redirect_uri` your running server sends:

```bash
curl -s -o /dev/null -w '%{redirect_url}\n' localhost:8000/api/auth/oauth/google/login
# the redirect_uri=... query param is what must be registered verbatim
```

---

## Google

1. Go to <https://console.cloud.google.com/apis/credentials>.
2. **Create Credentials → OAuth client ID**. Application type: **Web application**.
   (First time: configure the OAuth consent screen — "External", add your email
   as a test user while the app is in "Testing".)
3. Under **Authorized redirect URIs** (not "Authorized JavaScript origins"), add:
   `http://localhost:8000/api/auth/oauth/google/callback`
4. Create, then copy the **Client ID** and **Client secret** into `.env`:

   ```
   GOOGLE_CLIENT_ID=<client-id>
   GOOGLE_CLIENT_SECRET=<client-secret>
   ```

## GitHub

1. Go to <https://github.com/settings/developers> → **New OAuth App**
   (or a GitHub org's *Settings → Developer settings → OAuth Apps*).
2. **Homepage URL:** `http://localhost:8000`
3. **Authorization callback URL:** `http://localhost:8000/api/auth/oauth/github/callback`
4. Register, generate a **client secret**, and copy both into `.env`:

   ```
   GITHUB_CLIENT_ID=<client-id>
   GITHUB_CLIENT_SECRET=<client-secret>
   ```

---

## Apply the config

Providers are registered **at startup**, so restart the server after editing `.env`:

```bash
kill -9 $(lsof -ti:8000)          # stop the running server
uv run uvicorn src.main:app --reload --port 8000
```

Verify the provider(s) are now live:

```bash
curl -s localhost:8000/api/auth/providers
# {"providers":[{"name":"google","label":"Google"},{"name":"github","label":"GitHub"}]}
```

Reload the login page — the "Sign in with …" buttons should now appear.

---

## Troubleshooting

**`Error 400: redirect_uri_mismatch` (Google) / "The redirect_uri is not associated
with this application" (GitHub)**

The callback URL your app sends doesn't exactly match what's registered. Check:

- It's registered under **Authorized redirect URIs**, not JavaScript origins.
- Scheme, host, **port**, and path all match exactly — including no trailing slash.
- `localhost` vs `127.0.0.1`: browse to the same host you registered.
- Google can take a few minutes to propagate a newly added URI.

Print the exact URI the app sends with the `curl … /login` command above and
paste that string verbatim into the provider console.

**No buttons on the login page** — the provider isn't configured. `curl
localhost:8000/api/auth/providers` returns `{"providers":[]}`. Confirm both the
`*_CLIENT_ID` and `*_CLIENT_SECRET` are set for that provider and that you
restarted the server.

**`access_denied` / consent screen blocks you** — while a Google app is in
"Testing", only accounts added as **test users** on the consent screen can sign
in. Add your email there, or publish the app.

**New SSO user can't run anything** — expected: SSO accounts are provisioned with
no automations. An admin grants them in Admin → Users.
