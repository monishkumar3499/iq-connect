"""
The two discovery documents Claude reads before signing in.

Written out by hand rather than generated from a URL type: several URL types
normalise "https://host" to "https://host/", and Claude compares `issuer` and
`authorization_servers` with the URL it fetched character for character. A
mismatch makes it abandon the sign-in without saying why.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .common import SCOPE

router = APIRouter(tags=["oauth"])


def protected_resource(request: Request) -> JSONResponse:
    settings = request.app.state.settings
    return JSONResponse({
        # Must equal the connector URL exactly.
        "resource": settings.mcp_url,
        "authorization_servers": [settings.issuer],
        "scopes_supported": [SCOPE],
        "bearer_methods_supported": ["header"],
    })


# RFC 9728 places the document at the well-known path *plus* the resource's
# path (/mcp); some clients ask for the bare path. Serve both.
router.add_api_route("/.well-known/oauth-protected-resource", protected_resource)
router.add_api_route("/.well-known/oauth-protected-resource/mcp", protected_resource)


@router.get("/.well-known/oauth-authorization-server")
def authorization_server(request: Request) -> JSONResponse:
    settings = request.app.state.settings
    base = settings.issuer
    document = {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "revocation_endpoint": f"{base}/oauth/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        # Claude is a public client: no secret, PKCE instead. Claude refuses
        # a server that does not list "none" here.
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": [SCOPE],
        # We add `iss` to every authorization response (RFC 9207), which
        # protects clients that talk to several servers from mix-ups.
        "authorization_response_iss_parameter_supported": True,
    }
    if settings.oauth_cimd_enabled:
        document["client_id_metadata_document_supported"] = True
    return JSONResponse(document)
