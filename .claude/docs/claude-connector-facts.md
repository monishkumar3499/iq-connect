# Claude custom-connector facts

Learned building the prototype and verified against Claude's behaviour/docs. Violating any of
these usually fails silently ("Couldn't register" / "Authorization failed" with no detail).

## Reachability
- Claude connects **from Anthropic's cloud**. `localhost` can never be the connector URL.
  Locally, run a dev tunnel (e.g. `devtunnel host -p 8000 --allow-anonymous`) and set
  `PUBLIC_BASE_URL` to the tunnel URL. The tunnel must allow anonymous access.
- If metadata advertises localhost (wrong `PUBLIC_BASE_URL`), Claude says
  "Couldn't register with ... sign-in service".
- Endpoints must answer within seconds.
- Claude caches discovery ~5 minutes. After fixing metadata: remove and re-add the connector.
- A running server keeps old code until restarted; check the live
  `/.well-known/oauth-authorization-server` to confirm what is deployed.

## Discovery
- `POST /mcp` without a valid token -> `401` with
  `WWW-Authenticate: Bearer resource_metadata="<public>/.well-known/oauth-protected-resource"`.
- Protected Resource Metadata: `resource` must equal the connector URL exactly
  (`<public>/mcp`); `authorization_servers` lists our issuer.
- Authorization Server Metadata (RFC 8414) must include: `code_challenge_methods_supported:
  ["S256"]`, `registration_endpoint`, `"none"` in `token_endpoint_auth_methods_supported`
  (Claude is a public client), `grant_types_supported`.
- **No trailing slash** on `issuer` or `authorization_servers` entries. Some URL types add one;
  a strict client comparing it to the fetched URL drops the flow silently. Write these
  documents by hand.

## Client registration
- Claude uses **CIMD** (client_id is an HTTPS URL to a JSON document on claude.ai) only when AS
  metadata has `client_id_metadata_document_supported: true` AND `"none"` among auth methods;
  otherwise it uses **DCR** (`/register`). Support both.
- Only fetch CIMD documents from an allow-list of hosts (claude.ai, claude.com) -- anything else
  is an SSRF hole.
- A CIMD client has no stored scope; default it to the required scope or the token endpoint
  answers `invalid_scope`.
- Redirect URIs: `https://claude.ai/api/mcp/auth_callback` (web/desktop). Claude Code uses
  loopback redirects (`http://127.0.0.1:<any port>/...`) -- match loopback ports loosely.

## Token endpoint
- Accepts `application/x-www-form-urlencoded`.
- Claude sends `resource` (RFC 8707); accept and bind the audience to it.
- PKCE S256 required.
- A dead/rotated refresh token must return `invalid_grant` (Claude then re-runs sign-in).

## Enterprise Managed Auth (EMA) -- later phase
- Team/Enterprise plans. Claude gets a signed identity assertion (ID-JAG, JWT) from the
  company IdP and posts it to `/token` with
  `grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer`, `assertion`, `client_id`,
  usually `resource`.
- Advertise `urn:ietf:params:oauth:grant-type:jwt-bearer` in `grant_types_supported` or Claude
  never offers EMA.
- Validate: JWKS signature, `iss` on the org's trusted-issuer allow-list, `aud` = our issuer,
  `exp`, `sub`, `client_id`, single-use `jti`. Any failure -> `invalid_grant`.
- **DCR is not supported with EMA**; use CIMD or Anthropic-held credentials.
- EMA proves who the user is; it does NOT give us platform (Fabric/Snowflake) credentials.
  Realistic plan: EMA signs the person in silently, platform links are made once and reused.
- Test without a Claude org: Okta's playground https://xaa.dev
- Docs: https://claude.com/docs/connectors/building/enterprise-managed-auth

## How this project meets these rules (step 4)
- Paths: `/oauth/register`, `/oauth/authorize`, `/oauth/token`, `/oauth/revoke`; PRM at
  both `/.well-known/oauth-protected-resource` and `.../oauth-protected-resource/mcp`.
- `OAUTH_REDIRECT_HOSTS` (claude.ai, claude.com) + loopback any port; `OAUTH_CIMD_HOSTS`
  for CIMD fetches; `OAUTH_CIMD_ENABLED=false` forces DCR.
- Every authorization response (code or error) carries `iss` (RFC 9207, advertised).
- Unknown scopes are ignored and `iq` granted (refusing broke sign-ins in the prototype).

## Verified with real Claude (2026-10-15)
- Claude registered with **CIMD** (`oauth_clients.kind = cimd`, name "Claude"), then
  authorize -> our `/connect` -> Allow -> `/oauth/token` -> tools all worked first time.
- Dev tunnels: give Claude the `https://<id>-8000.<region>.devtunnels.ms` form, not
  `https://<id>.<region>.devtunnels.ms:8000`. Requests through the tunnel were accepted by
  our host check (no 421).
- Restarting our server does not disconnect Claude: its refresh token lives in the database.

## Debugging
- Log `>> METHOD /path` when a sign-in request ARRIVES and `<< status in Ns` when it ends.
  Uvicorn only logs once a response starts, so a hung request is otherwise invisible.
- Log "token exchange for client X" and "redirecting to <claude callback> for client X".
