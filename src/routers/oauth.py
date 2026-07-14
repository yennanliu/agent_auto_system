"""OAuth SSO endpoints: redirect to the provider and handle its callback.

Kept separate from ``routers/auth.py`` because these are browser-redirect
(HTML) routes, not JSON API routes. They live under ``/api/auth/oauth/*`` and
are mounted open (no session required) alongside the login endpoints.

Flow:  GET /login  → 302 to provider  → provider → GET /callback
        → find/link/provision the user (src.oauth.resolve_sso_user)
        → set the same session cookie the password login uses
        → 302 back to the SPA at ``/``.
"""

import logging
from datetime import UTC, datetime

from authlib.integrations.starlette_client import OAuthError
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session

from src import oauth as oauth_mod
from src.database import get_session
from src.oauth import oauth, resolve_sso_user

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/auth/providers")
def providers():
    """Which SSO providers are configured — the UI renders a button per entry."""
    return {"providers": oauth_mod.configured_providers()}


@router.get("/auth/oauth/{provider}/login")
async def oauth_login(provider: str, request: Request):
    if not oauth_mod.is_enabled(provider):
        raise HTTPException(status_code=404, detail="Unknown or unconfigured provider")
    client = oauth.create_client(provider)
    redirect_uri = request.url_for("oauth_callback", provider=provider)
    return await client.authorize_redirect(request, str(redirect_uri))


async def _identity(provider: str, client, token: dict) -> dict:
    """Normalise a provider's profile into {sub, email, email_verified, name}."""
    if provider == "google":
        info = token.get("userinfo") or (await client.userinfo(token=token))
        return {
            "sub": str(info["sub"]),
            "email": info.get("email"),
            "email_verified": bool(info.get("email_verified")),
            "name": info.get("name"),
        }
    if provider == "github":
        gh = (await client.get("user", token=token)).json()
        email, verified = gh.get("email"), False
        # The public profile email may be null/unverified; pull the primary
        # verified address from the emails endpoint (needs the user:email scope).
        try:
            for entry in (await client.get("user/emails", token=token)).json():
                if entry.get("primary") and entry.get("verified"):
                    email, verified = entry.get("email"), True
                    break
        except Exception:  # noqa: BLE001 — email is best-effort; fall back to profile
            logger.warning("GitHub user/emails fetch failed", exc_info=True)
        return {
            "sub": str(gh["id"]),
            "email": email,
            "email_verified": verified,
            "name": gh.get("name") or gh.get("login"),
        }
    raise HTTPException(status_code=404, detail="Unknown provider")


@router.get("/auth/oauth/{provider}/callback", name="oauth_callback")
async def oauth_callback(
    provider: str, request: Request, session: Session = Depends(get_session)
):
    if not oauth_mod.is_enabled(provider):
        raise HTTPException(status_code=404, detail="Unknown or unconfigured provider")
    client = oauth.create_client(provider)
    try:
        token = await client.authorize_access_token(request)
    except OAuthError:
        logger.warning("OAuth callback failed for %s", provider, exc_info=True)
        return RedirectResponse(url="/?login_error=sso_failed")

    ident = await _identity(provider, client, token)
    user = resolve_sso_user(
        session,
        provider=provider,
        sub=ident["sub"],
        email=ident["email"],
        email_verified=ident["email_verified"],
        name=ident["name"],
    )
    if not user.is_active:
        return RedirectResponse(url="/?login_error=account_disabled")

    session.add(user)
    session.commit()
    session.refresh(user)

    request.session["user_id"] = user.id
    user.last_login_at = datetime.now(UTC)
    session.add(user)
    session.commit()
    return RedirectResponse(url="/")
