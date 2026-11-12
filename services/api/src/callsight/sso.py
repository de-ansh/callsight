"""Single sign-on with any OpenID Connect provider (Google Workspace, Microsoft Entra ID, Okta, Auth0,
Keycloak, Authentik, ...).

Authorization-code flow with PKCE, done by Authlib: it checks state and nonce and validates the ID
token's signature (against the provider's published keys), issuer, audience and expiry. Who may
sign in is decided here: a *verified* email whose domain an admin linked to an organization
(`python -m callsight.admin allow-domain`). The session is a signed cookie; API keys keep working.
"""

from __future__ import annotations

import logging
import time
from urllib.parse import quote

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from . import auth, store
from .config import Settings

log = logging.getLogger("callsight.sso")
SESSION_KEY = "user"
SESSION_HOURS = 8
router = APIRouter(include_in_schema=False)
_clients: dict[tuple[str, str], OAuth] = {}


def enabled(settings: Settings) -> bool:
    return bool(settings.oidc_issuer and settings.oidc_client_id)


def _client(settings: Settings):
    key = (settings.oidc_issuer, settings.oidc_client_id)
    if key not in _clients:
        oauth = OAuth()
        oauth.register(
            "oidc",
            server_metadata_url=f"{settings.oidc_issuer}/.well-known/openid-configuration",
            client_id=settings.oidc_client_id,
            client_secret=settings.oidc_client_secret,
            client_kwargs={"scope": "openid email profile", "code_challenge_method": "S256"},
        )
        _clients[key] = oauth
    return _clients[key].oidc


def _back_to_ui(error: str | None = None) -> RedirectResponse:
    return RedirectResponse("/#/signin" + (f"?error={quote(error)}" if error else ""), status_code=303)


def session_principal(request: Request) -> auth.Principal | None:
    """The signed-in user's organization, if the session is valid and their domain is still allowed."""
    user = (request.scope.get("session") or {}).get(SESSION_KEY)
    if not user or user.get("exp", 0) < time.time():
        return None
    settings = Settings.from_env()
    with store.connect(settings.database_url) as conn:
        org_id = auth.org_for_email(conn, user["email"])  # removing a domain revokes its sessions at once
        if org_id is None or str(org_id) != user.get("org_id"):
            return None
        admin = auth.is_email_admin(conn, org_id, user["email"])  # checked per request, so removal is immediate
    return auth.Principal(org_id, f"user:{user['email']}", admin)


@router.get("/auth/config")
def config() -> dict:
    return {"sso": enabled(Settings.from_env())}


@router.get("/auth/login")
async def login(request: Request):
    settings = Settings.from_env()
    if not enabled(settings):
        raise HTTPException(status_code=404, detail="Single sign-on is not configured")
    return await _client(settings).authorize_redirect(request, f"{settings.public_url}/auth/callback")


@router.get("/auth/callback")
async def callback(request: Request):
    settings = Settings.from_env()
    if not enabled(settings):
        raise HTTPException(status_code=404, detail="Single sign-on is not configured")
    try:
        token = await _client(settings).authorize_access_token(request)
    except OAuthError as exc:
        log.warning("sign-in failed: %s", exc.error)
        return _back_to_ui("Sign-in failed or was cancelled. Please try again.")
    claims = token.get("userinfo") or {}
    email = str(claims.get("email") or "").strip().lower()
    if not email or claims.get("email_verified") is not True:
        return _back_to_ui("Your identity provider did not confirm a verified email address.")
    with store.connect(settings.database_url) as conn:
        org_id = auth.org_for_email(conn, email)
        if org_id is None:
            log.info("sign-in refused for %s: domain not allowed", email)
            return _back_to_ui(f"{email.rpartition('@')[2]} is not allowed to sign in. Ask your administrator.")
        store.audit(conn, org_id, f"user:{email}", "user.sign_in", email)
    request.session.clear()  # a fresh session on every sign-in
    request.session[SESSION_KEY] = {"email": email, "org_id": str(org_id), "exp": int(time.time()) + SESSION_HOURS * 3600}
    return RedirectResponse("/", status_code=303)


@router.post("/auth/logout", status_code=204)
def logout(request: Request) -> Response:
    request.session.clear()
    return Response(status_code=204)
