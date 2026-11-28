"""
One-time OAuth2 authorization for the ``upwork_drafts`` automation (read-only).

Upwork has no anonymous job search (RSS feeds ended 2024-08-20; public pages
are behind Cloudflare), so keyword search uses the official GraphQL API with
YOUR OAuth2 token. This script runs the Authorization Code grant once and saves
the tokens to UPWORK_TOKEN_FILE (default ``data/upwork_token.json``,
git-ignored, mode 0600). Runs then refresh the 24 h access token themselves
while the refresh token is valid (2 weeks since its last use).

Before you run it
  1. Request an API key at https://www.upwork.com/developer/keys/apply
     (Key type: OAuth 2.0). Ask ONLY for read scopes:
       - Common Entities - Read-Only Access
       - Read marketplace Job Postings
     Do not request any proposal / write scope: upwork_drafts never submits.
  2. Put the key in .env:
       UPWORK_CLIENT_ID=...
       UPWORK_CLIENT_SECRET=...
       UPWORK_REDIRECT_URI=...   # the callback URL you registered with the key

Usage
    uv run python scripts/upwork_oauth.py

It prints an authorize URL. Open it, log in to Upwork yourself and approve;
Upwork redirects to your callback URL with ``?code=...``. Paste that full URL
(or just the code) back here.
"""
import os
import sys
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from dotenv import load_dotenv

from src.automation.tools.upwork_tool import (
    AUTHORIZE_URL,
    DEFAULT_TOKEN_FILE,
    TOKEN_URL,
    save_tokens,
)

load_dotenv()


def main() -> int:
    client_id = os.getenv("UPWORK_CLIENT_ID", "").strip()
    secret = os.getenv("UPWORK_CLIENT_SECRET", "").strip()
    redirect = os.getenv("UPWORK_REDIRECT_URI", "").strip()
    if not (client_id and secret and redirect):
        print("Set UPWORK_CLIENT_ID, UPWORK_CLIENT_SECRET and UPWORK_REDIRECT_URI in .env first "
              "(see the docstring at the top of this script).", file=sys.stderr)
        return 2
    print("1. Open this URL, log in to Upwork and approve the app:\n")
    print("   " + AUTHORIZE_URL + "?" + urlencode({"response_type": "code", "client_id": client_id,
                                                    "redirect_uri": redirect}))
    pasted = input("\n2. Paste the URL you were redirected to (or just the code): ").strip()
    code = (parse_qs(urlparse(pasted).query).get("code") or [pasted])[0].strip()
    if not code:
        print("No code given.", file=sys.stderr)
        return 2
    resp = httpx.post(TOKEN_URL, timeout=30, data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect,
        "client_id": client_id, "client_secret": secret})
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code >= 400 or not data.get("access_token"):
        print(f"Token exchange failed (HTTP {resp.status_code}): "
              f"{data.get('error_description') or data.get('error') or resp.text[:200]}", file=sys.stderr)
        return 1
    path = os.getenv("UPWORK_TOKEN_FILE", DEFAULT_TOKEN_FILE)
    save_tokens(path, data)
    print(f"\nSaved to {path}. upwork_drafts can now search Upwork (read-only).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
