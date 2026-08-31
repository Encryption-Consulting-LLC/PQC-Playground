"""Auth routes — identity-based session lifecycle and deploy config discovery.

Identity is a real layer decoupled from the ESXi target; which ESXi host gets
used is a separate, shared org-wide setting (``core/esxi.py``). Login is always
required — there is no anonymous mode.
Both operators and guests are accounts in the users collection; the ``role``
on the account decides the feature set (``core/authz.py``).

  POST /auth/login          — admin-provisioned username/password; returns a JWT.
                              Guests sign in exclusively through here.
  GET  /auth/oidc/login     — employee SSO: returns the IdP redirect URL (if configured).
  POST /auth/oidc/callback  — completes the SSO code flow; returns a JWT.
  GET  /auth/config         — unauthenticated discovery: {"oidcEnabled"} (whether
                              to show the SSO button). Role/capabilities are
                              per-user — fetch them from GET /auth/me after login.
  GET  /auth/me             — the authenticated user's identity + capability list.
  POST /auth/logout         — client-side token discard acknowledgement. Tokens
                              are stateless JWTs, so there is nothing server-side
                              to drop; disabling the account is the kill switch.
  POST /auth/connect        — 410 Gone (the pre-Phase-B ESXi-credential login).
  POST /auth/guest          — 410 Gone (anonymous guest sessions were removed;
                              guests now log in with a username/password account).

Passwords travel only in the /auth/login request body; what's stored is an
Argon2id hash in the users collection. Account-backed tokens are re-checked
against the user document on every request (``core/authz.py``), so disabling
an account revokes access immediately despite JWT statelessness.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core import oidc
from app.core.authz import (
    AuthedUser,
    Role,
    allowed_product_templates,
    capabilities_for,
    get_current_user,
)
from app.core.db import now_ms, to_mongo, users_col
from app.core.db.models import UserDoc
from app.core.esxi import load_target
from app.core.identity import AuthProvenance, mint_token, verify_password
from app.core.settings import settings

router = APIRouter(prefix="/auth", tags=["auth"])


# --------------------------------------------------------------------------- #
# Config discovery (unauthenticated — called by the frontend on every load)   #
# --------------------------------------------------------------------------- #


@router.get("/config")
def get_config() -> dict:
    """Return unauthenticated deploy config — whether SSO is available.

    The frontend uses ``oidcEnabled`` to show the SSO button. Role/capabilities
    are per-user — fetch them from ``GET /auth/me`` after signing in.
    """
    return {"oidcEnabled": settings.oidc_enabled}


# --------------------------------------------------------------------------- #
# Session lifecycle                                                            #
# --------------------------------------------------------------------------- #


class LoginRequest(BaseModel):
    username: str
    password: str


async def _session_response(username: str, role: Role, auth: AuthProvenance) -> dict:
    """Uniform shape for every token-minting route (local login, OIDC)."""
    target = await load_target()
    return {
        "token": mint_token(username, role.value, auth),
        "username": username,
        "role": role.value,
        "capabilities": capabilities_for(role),
        "host": target.host if target else None,
    }


@router.post("/login")
async def login(req: LoginRequest) -> dict:
    """Password login against the users collection — operators and guests alike.

    A uniform 401 for unknown username / wrong password / disabled account /
    OIDC-only account — and ``verify_password`` burns one Argon2 verification
    even for unknown usernames, so responses don't oracle which part failed.
    """
    doc = await users_col().find_one({"username": req.username})
    valid = verify_password(req.password, (doc or {}).get("passwordHash"))
    if not valid or doc is None or doc.get("disabled"):
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    return await _session_response(doc["username"], Role(doc["role"]), "local")


@router.post("/logout")
def logout() -> dict:
    """Acknowledge a client-side token discard (stateless JWTs — nothing to drop)."""
    return {"status": "logged_out"}


@router.get("/me")
async def me(user: AuthedUser = Depends(get_current_user)) -> dict:
    """The authenticated identity and its capability allowlist (what ``useCan`` reads).

    ``products`` rides alongside because it answers a question capabilities
    cannot: capabilities are per-role and say *whether*, while the product
    catalogue is per-account and says *which*. It is the resolved set (an
    operator gets every product), so the palette needs no role branch — an id
    absent from this list is greyed out and undraggable, and the deploy route
    refuses it regardless of what the palette allowed.
    """
    return {
        "username": user.username,
        "role": user.role.value,
        "auth": user.auth,
        "capabilities": capabilities_for(user.role),
        "products": sorted(allowed_product_templates(user)),
    }


@router.post("/connect")
def connect_gone() -> None:
    """The pre-Phase-B ESXi-credential login. Kept as an explicit tombstone so
    stale clients get an actionable error instead of a bare 404."""
    raise HTTPException(
        status_code=410,
        detail="ESXi-credential login was removed — sign in via /auth/login or SSO.",
    )


@router.post("/guest")
def guest_gone() -> None:
    """Anonymous guest sessions were removed — guests now sign in with a
    username/password account (role ``guest``). Kept as an explicit tombstone
    so stale clients get an actionable error instead of a bare 404."""
    raise HTTPException(
        status_code=410,
        detail="Anonymous guest sessions were removed — sign in via /auth/login.",
    )


# --------------------------------------------------------------------------- #
# Employee SSO (generic OIDC — Keycloak / Azure AD)                            #
# --------------------------------------------------------------------------- #


class OidcCallbackRequest(BaseModel):
    code: str
    state: str


@router.get("/oidc/login")
async def oidc_login() -> dict:
    """Start the SSO code flow: the SPA redirects to the returned URL."""
    oidc.require_oidc()
    return {"url": await oidc.build_authorization_url()}


@router.post("/oidc/callback")
async def oidc_callback(req: OidcCallbackRequest) -> dict:
    """Complete the SSO code flow: verify state, exchange the code, validate
    the ID token, map IdP groups → Role, upsert the user, mint a session."""
    oidc.require_oidc()

    nonce = oidc.verify_state(req.state)
    tokens = await oidc.exchange_code(req.code)
    claims = await oidc.validate_id_token(tokens.get("id_token", ""), nonce)

    role = Role(oidc.map_groups_to_role(claims.get(settings.oidc_group_claim) or []))
    username = oidc.username_from_claims(claims)

    # Upsert: first SSO login creates the account; later logins refresh
    # email/role from the IdP but preserve a locally-set ``disabled`` flag.
    existing = await users_col().find_one({"username": username})
    if existing is None:
        doc = UserDoc(
            id=username,
            username=username,
            email=claims.get("email"),
            role=role.value,  # type: ignore[arg-type]
            auth="oidc",
            created_at=now_ms(),
            updated_at=now_ms(),
        )
        await users_col().insert_one(to_mongo(doc))
    else:
        if existing.get("disabled"):
            raise HTTPException(status_code=403, detail="This account is disabled.")
        await users_col().update_one(
            {"username": username},
            {
                "$set": {
                    "email": claims.get("email"),
                    "role": role.value,
                    "updatedAt": now_ms(),
                }
            },
        )
    return await _session_response(username, role, "oidc")
