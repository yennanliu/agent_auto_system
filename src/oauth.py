"""OAuth (SSO) sign-in for Google and GitHub via Authlib.

A provider is registered only when its client id/secret are configured (env
vars), so the login UI shows a button only for providers that actually work.
The OAuth network dance lives in ``routers/oauth.py``; the pure, unit-tested
account-resolution logic (find → link → provision) lives here in
``resolve_sso_user``.

Account policy (see the SSO branch design):
* **Open provisioning** — any Google/GitHub user may sign in; a brand-new,
  non-admin account with no automations is created for them.
* **Link by verified email** — if the provider reports a *verified* email that
  matches an existing account with no OAuth link yet, the SSO identity attaches
  to that account instead of creating a duplicate.
"""

import logging
import os
import re

from authlib.integrations.starlette_client import OAuth
from sqlmodel import Session, select

from src.models import User

logger = logging.getLogger(__name__)

oauth = OAuth()

# name -> human label, populated by _register() for configured providers only.
_PROVIDERS: dict[str, str] = {}


def _register() -> None:
    """Register OAuth clients for whichever providers are configured via env."""
    google_id = os.getenv("GOOGLE_CLIENT_ID")
    google_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    if google_id and google_secret:
        oauth.register(
            name="google",
            client_id=google_id,
            client_secret=google_secret,
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )
        _PROVIDERS["google"] = "Google"

    github_id = os.getenv("GITHUB_CLIENT_ID")
    github_secret = os.getenv("GITHUB_CLIENT_SECRET")
    if github_id and github_secret:
        oauth.register(
            name="github",
            client_id=github_id,
            client_secret=github_secret,
            authorize_url="https://github.com/login/oauth/authorize",
            access_token_url="https://github.com/login/oauth/access_token",
            api_base_url="https://api.github.com/",
            client_kwargs={"scope": "read:user user:email"},
        )
        _PROVIDERS["github"] = "GitHub"

    if _PROVIDERS:
        logger.info("SSO enabled for: %s", ", ".join(sorted(_PROVIDERS)))


_register()


def configured_providers() -> list[dict]:
    """Providers with credentials configured — drives which buttons the UI shows."""
    return [{"name": name, "label": label} for name, label in _PROVIDERS.items()]


def is_enabled(provider: str) -> bool:
    return provider in _PROVIDERS


def _unique_username(session: Session, preferred: str) -> str:
    """A username slug guaranteed unique in the users table.

    Derived from the email local-part / display name, sanitised, with a numeric
    suffix appended on collision.
    """
    base = re.sub(r"[^a-zA-Z0-9._-]", "", (preferred or "").split("@")[0]).strip("._-")
    base = base[:40] or "user"
    candidate = base
    n = 1
    while session.exec(select(User).where(User.username == candidate)).first():
        n += 1
        candidate = f"{base}{n}"
    return candidate


def resolve_sso_user(
    session: Session,
    *,
    provider: str,
    sub: str,
    email: str | None,
    email_verified: bool,
    name: str | None,
) -> User:
    """Find, link, or provision the User for an SSO identity.

    Resolution order:
    1. Existing account already linked to this ``(provider, sub)`` → log in.
    2. Verified email matching an existing account that has no OAuth link yet →
       attach this identity to it (password → SSO linking).
    3. Otherwise → provision a fresh non-admin account (open policy).

    The returned User is added to the session but **not committed**; the caller
    commits after setting the session cookie.
    """
    # 1. Already linked.
    user = session.exec(
        select(User).where(
            User.oauth_provider == provider, User.oauth_sub == sub
        )
    ).first()
    if user:
        # Keep email fresh if the provider now reports a verified one.
        if email and email_verified and user.email != email:
            user.email = email
            session.add(user)
        return user

    # 2. Link by verified email to an unlinked existing account. Match the email
    #    column or a legacy account whose username IS the email address (accounts
    #    predating the email column).
    if email and email_verified:
        existing = session.exec(
            select(User).where(
                (User.email == email) | (User.username == email)
            )
        ).first()
        if existing and not existing.oauth_provider:
            existing.oauth_provider = provider
            existing.oauth_sub = sub
            if not existing.email:
                existing.email = email
            session.add(existing)
            logger.info(
                "Linked %s SSO identity to existing account %r", provider, existing.username
            )
            return existing

    # 3. Provision. Only trust the email onto the new account if verified — that
    #    applies to both the stored email and the derived username slug.
    trusted_email = email if email_verified else None
    username = _unique_username(session, trusted_email or name or f"{provider}-{sub}")
    user = User(
        username=username,
        password_hash=None,  # SSO-only: no password login
        email=trusted_email,
        oauth_provider=provider,
        oauth_sub=sub,
        is_admin=False,
        is_active=True,
        allowed_automations="[]",  # no automations until an admin grants them
    )
    session.add(user)
    logger.info("Provisioned new %s SSO account %r", provider, username)
    return user
