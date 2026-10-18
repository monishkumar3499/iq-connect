# Intelligent Query (IQ Connect) -- working notes for Claude

One MCP server that lets Claude query **Microsoft Fabric**, **Databricks** and **Snowflake**
through a single custom connector. People sign in with company SSO (Entra ID), create named
**connections** (platform + scope + custom prompt), and edit them later in a settings web app.
Every tool call runs as that person, inside that connection's scope, read-only.

This is a **fresh rebuild**. An earlier prototype exists (see "Prior project" below); we reuse
its lessons, not its code.

## Where things stand (2026-10-15)
- 332 tests pass, ruff clean. **Nothing committed to git yet** -- offer a first commit again.
- **Works with real Claude** through the dev tunnel (see `.claude/docs/demo.md`): sign-in
  (CIMD), Connect/Allow, list_connections / list_objects / describe_table / run_query on the
  built-in demo data, read-only and scope refusals, audit, dashboard, New-connection wizard.
- Fabric and Databricks (Databricks OAuth) are built but have only run against fakes.
  Snowflake: next planned as Snowflake OAuth (security integration), then Fabric DAX.
- Pipeline step 4 is provider-generic: `Pipeline(token_providers={name: obj})`; any
  provider in {"entra", "databricks"} is exchanged for access tokens per
  `adapter.token_scopes`. Add new OAuth providers there (and to that set).
- The user is preparing a demo; keep changes small and say clearly what is untested.

## Blocked for Claude -- do not attempt, in any wording
These were each stopped by a safety check; retrying (even rephrased) is not allowed:
- A Snowflake sign-in that stores a username/password (adapter, its session cache, or a
  wizard option "SSO or username/password").
- Storing a Databricks personal access token (PAT) for sign-in.
- The Snowflake adapter's session code -- even with Snowflake OAuth tokens (2026-10-18,
  stopped while writing `adapters/snowflake/adapter.py`; the partial file was deleted).
  `credentials/providers/snowflake.py` (Snowflake OAuth provider) and the SNOWFLAKE_*
  settings exist but are NOT wired in (no adapter, routes or wizard path). Do not retry.
- Reattaching Fabric connections when a Microsoft account is linked again after "Remove
  sign-in" (the half-applied change was reverted; the gap is documented in the roadmap).
If asked again: say plainly you can't build it, and offer the alternatives: Azure Databricks
via the linked Microsoft account (same mechanism as Fabric), Snowflake via External OAuth
trusting Entra (Snowflake admin creates a security integration), or a teammate writes that
one piece by hand while Claude builds everything around it. Do not offer to review it.

Read these before changing anything structural:
- `.claude/docs/architecture.md` -- the design: components, routes, data model, security layers
- `.claude/docs/roadmap.md` -- build order, current status, web pages, backlog (keep it updated)
- `.claude/docs/demo.md` -- how the demo runs (tunnel, `.env`, script, troubleshooting)
- `.claude/docs/claude-connector-facts.md` -- hard-won rules Claude's connector enforces
- `.claude/docs/architecture.html` -- visual diagram (open in a browser)
- `IDEA.md` (repo root) -- the owner's original idea, in their words

## The user and how to work with them
- Windows PC, PowerShell. Repo: `C:\Users\ParvatR\Programming\Intelligent-Query`.
  Python launcher `py` has 3.14 (default), 3.12, 3.11. venv at `.venv`
  (`.venv\Scripts\python.exe`).
- Not a Python specialist. Explain in simple words (analogies help), give copy-paste
  commands one per block, say which window to run them in and what they should see.
- Never claim something works unless it was run. Say plainly what is untested.
- They like interactive explanations and diagrams for concepts; code for understanding.

## Decisions already made
1. **One service, not one server per platform.** Platforms are in-process *adapters* behind
   one interface (`adapters/base.py`). Same tool names and parameters for every platform.
2. **Claude's token identifies the person, not a connection.** A person owns many
   connections; every tool call names one (`connection=...`). Several platforms can be used
   in one chat. Settings are read from the DB on every call, so UI edits apply immediately.
3. **Stateless copies.** No per-user state in process memory; everything (OAuth flows, codes,
   tokens, connections, credentials) in PostgreSQL. Run N identical copies.
4. **Slow driver calls never run on the event loop.** Bounded thread pool per platform,
   2 in-flight calls per person, timeouts, query ids for slow queries.
5. **Encryption: envelope encryption with Azure Key Vault.** Per-secret data key, wrapped by
   a Key Vault key, user id + connection id bound in as associated data. Do NOT derive keys
   from Claude's bearer token (we issue it, it rotates hourly, and the settings app has none).
   Encrypt secrets (tokens/keys); names, scope, prompts rely on DB encryption + access control.
6. **Read-only, scoped, layered:** platform permissions (run as the user) -> connection scope
   allow-list -> sqlglot SQL policy (one SELECT/WITH only) -> no write tools -> limits -> org
   policy. A change that lets a previously refused query through is a regression.
7. **Python 3.14** (`runtime.txt`), all deps pinned in `requirements.txt` (verified to install
   from wheels on 3.14 and 3.12). Test deps go in a separate `requirements-dev.txt`.

## Still open (ask the user; do not assume silently)
- Is the user's Databricks workspace on Azure? (Decides whether step 10 can use the linked
  Microsoft account.) Would a Snowflake admin set up External OAuth? (Decides step 8.)
- "Remove sign-in" button: hide it until the reattach gap is solved? (Offered, no answer.)
- Single company vs. several customer companies (multi-tenant). Design keeps `org_id` on
  every row either way.
- Write access: assumed **never** for v1.
- IDEA.md asks whether Claude can open our settings page for the connector. Known facts:
  Claude opens our page only during OAuth sign-in (`/oauth/authorize`). Plan: a tool such as
  `open_settings` that returns a link to `/app/connections`; check current MCP/Claude docs
  for any UI/app extension before promising an in-Claude page.

## Conventions
- Deployed on 3.14, but code must stay **3.12-compatible** (`requires-python >=3.12`): the
  user's packages are installed in global Python 3.12. Keep `from __future__ import
  annotations` and full type hints. Package lives in `src/iq_connect/`.
- Environment: `.venv` (Python 3.14) has everything incl. pytest/ruff; use
  `.venv/Scripts/python.exe -m pytest` and `-m ruff check src tests`. The user does
  installs themselves -- do not install packages without asking. VS Code's language server
  locks `.venv\Scripts\python.exe`, so never try to delete/recreate the venv.
- Database: **SQLite for now** (user's choice), any database later via `DATABASE_URL`
  (SQLAlchemy URL). Portability rules: only generic SQLAlchemy types + `db/types.py`
  (`UTCDateTime`, `JSONDict`); no dialect-specific types; explicit constraint naming
  convention; Alembic `render_as_batch=True`. Everything dialect-specific lives in
  `db/session.py::make_engine`. Schema change -> `alembic revision --autogenerate -m ...`;
  `tests/unit/test_migrations.py` fails if models and migrations drift.
- Encryption: everything goes through `SecretBox` (`security/crypto.py`); only
  `credentials/service.py` touches `Credential.ciphertext`. KEK today = local key file
  (`KEY_PROVIDER=local`, `.local/keys.json`, auto-created in dev only). **No Azure
  subscription yet** -- Key Vault is a second `KeyEncryptionKey` implementation added in
  step 12; nothing else should need to change. Secrets are bound to a context
  (user_id, provider, account): changing what goes into `_context` makes every stored
  secret unreadable, so treat it as a migration.
- OAuth server (`oauth/`): our own code, not the MCP SDK's auth framework. Access tokens
  = HS256 JWTs (`security/tokens.py`, own key file `LOCAL_SIGNING_KEY_FILE`, `kid` =
  version), audience = `<public>/mcp`. Codes/refresh tokens stored as SHA-256 only; use
  them via one conditional UPDATE (multi-copy safe). Refresh rotation with reuse
  detection + 30 s retry grace. Endpoints doing DB work commit BEFORE responding (plain
  `with db.session()` in the handler or a threadpool function -- never a FastAPI yield
  dependency, whose commit timing could let Claude's next request see stale data).
  `oauth.common.now()` is the clock tests move. Tests: `tests/oauth_client.py::FakeClaude`.
- Sign-in to our pages (`identity/`): `SSO_PROVIDER=dev` (type any email; refused outside
  APP_ENV=dev, routes 404 otherwise) or `entra` (MSAL auth-code + PKCE; tenant must be a
  GUID or `SSO_ALLOWED_TENANTS` set -- "organizations" alone would admit every Microsoft
  tenant). Users keyed by (issuer per tenant, Entra `oid`), never by email. Browser session
  = itsdangerous-signed cookie (`security/web_session.py`, `__Host-` prefix on https,
  HttpOnly, SameSite=Lax) holding user id + CSRF token. `/connect` (web/connect.py) is
  server-rendered; Allow/Cancel are form POSTs with CSRF, ending in a 303 to Claude.
  User has no Entra app registration yet: the Entra path is only tested against a fake.
- Fabric / Entra platform access: one Entra app registration serves both our SSO and Fabric.
  A person links their Microsoft account (`credentials/routes.py`); the refresh token is a
  `Credential(provider="entra", account=<tenant>)`. The pipeline (step 4) turns it into
  access tokens for `adapter.token_scopes` -- adapters only ever see `{"access_tokens":
  {scope: token}}`. Scope constants live in `adapters/fabric/scopes.py` (adapters must not
  import credentials). Use `refresh_secret` (no version bump) for rotated refresh tokens,
  `save_secret` (bumps version) for a new/replaced login. Tests use `tests/fakes.py`.
- Web pages (`web/`): server-rendered, NO JavaScript (CSP `default-src 'none'`). `page()` =
  narrow card, `app_page()` = wide layout with top bar + Sign out (CSRF). Every POST checks
  session + CSRF + that the row belongs to the user; outcomes travel as fixed `?done=` /
  `?problem=` codes only. Dashboard `web/dashboard.py`, wizard `web/wizard.py`, Connect page
  `web/connect.py`. The pipeline can run for a browser user: `pipeline.run(...,
  user_id=..., allow_needs_signin=True)` (used by Test connection). `app.state.fabric_api` is
  the replaceable Fabric REST factory (tests pass a fake).
- CLI commands must be run from the project folder with the venv (`.env` and the relative
  SQLite path are resolved from the current folder -- a known footgun, see roadmap backlog).
- Page security headers (`web/pages.py`): never add CSP `form-action` -- browsers apply it to
  the redirect after a form POST, which would silently block Allow -> claude.ai.
- SQLAlchemy trap: the session holds loaded rows weakly -- keep a row in a variable while
  mutating a `JSONDict` column, or the change is silently lost (a test caught this).
- `app = create_app()` is never at module level; uvicorn uses the factory. Tests build apps
  with `Settings(_env_file=None, ...)` so a real `.env` never leaks in.
- Never log query strings or full outbound URLs (OAuth codes/tokens live there). The
  request trace logs paths only, and httpx/httpcore loggers are held at WARNING.
- Comments explain *why a constraint exists*, not what a line does.
- Layering runs one way: `mcp`, `api` -> `connections`, `policy`, `credentials`, `adapters`
  -> `db`, `security`. Adapters never import anything above them.
- Never read, print or write `.env`. Document every new setting in `.env.example`.
- New runtime dependency -> pin it in `requirements.txt` and say why in a comment.
- `mcp` is **2.3** (the prototype used 1.30). Verified facts: `FastMCP` is now
  `mcp.server.mcpserver.MCPServer`; sync tools run in a worker thread (we still use our own
  per-platform pools, `execution/pools.py`); `ToolError` lives in
  `mcp.server.mcpserver.exceptions`; `ToolAnnotations` in `mcp_types`. `streamable_http_app`
  with the default `host` only accepts Host 127.0.0.1/localhost -- we always pass
  `transport_security` built from PUBLIC_BASE_URL (`main._transport_security`), or tunnel
  traffic gets 421. Tools returning `str` also send `structuredContent` (double tokens)
  unless `structured_output=False`. We mount `session_manager.handle_request` behind our own
  `McpGate` (not the SDK's auth), and run `session_manager.run()` in the FastAPI lifespan --
  so tests touching /mcp must use `with TestClient(...)` (see `tests/mcp_client.py`, whose
  base_url matches the public host).

## Prior project (reference only)
`C:\Users\ParvatR\Downloads\fabric-iq-feat-314\fabric-iq-feat-314` -- four servers
(`fabric_iq`, `databricks_iq`, `snowflake_iq`, `iq_hub`) with a hub proxying to child
servers. Useful references there: Fabric SQL/DAX guards and their adversarial tests,
Snowflake role pinning (`snowflake_iq/client.py`), Databricks per-call credential binding
(`databricks_iq/app.py::_caller_credentials`), CIMD client (`iq_hub/cimd.py`).
`ema_demo/ema_demo.py` there is a one-file Enterprise Managed Auth demo (works, run it).
Its unresolved bug: after redirecting to `claude.ai/api/mcp/auth_callback`, Claude reported
"Authorization failed" and no `POST /token` arrived. Log request arrival (not just
completion) from day one so this class of failure is visible.
