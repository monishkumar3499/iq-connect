# IQ Connect: Working Rules for Claude

IQ Connect is one custom MCP server (one Claude custom connector) that connects Claude to
Microsoft Fabric, Databricks and Snowflake. Everything about the project -- status,
architecture, onboarding, tools, platforms, security, data model, routes, settings,
structure, conventions, running with real Claude, connector rules, deployment, roadmap,
known gaps and open decisions -- is in `README.md`.

Read the relevant sections before making structural changes. Keep `README.md` updated as
work lands, particularly the status table, roadmap, known gaps and configuration sections.
Do not create additional documentation files.

## What the product must do (owner's requirements, 2026-11-11)

**Who may use it.** IQ Connect belongs to JMAN and is used by JMAN and its clients: any
company (any Entra tenant) may sign in. Do not pin the Microsoft app to one tenant or make
"the first person to sign in" an admin.
- People sign in to the IQ Connect web app with their company Microsoft account (Entra ID
  SSO, a multi-tenant app registration).
- The first sign-in registers the person automatically (an `orgs` row per Entra tenant, a
  `users` row keyed by tenant + Entra object id, never by email). Every later sign-in shows
  their own connections and settings (My connections), editable at any time.
- IQ admins come only from `ADMIN_EMAILS` (or from saving the Microsoft app with the setup
  code). `SSO_ALLOWED_TENANTS` / a tenant GUID in `ENTRA_TENANT` may narrow access, but the
  default is every company.

**How each platform signs in** (chosen by the person in the setup's Sign in step; SSO is the
default and marked recommended):

| Platform | Main choice | Alternative |
|---|---|---|
| Microsoft Fabric | Microsoft SSO (Entra delegated, queries run as the person) | Service principal: tenant ID, client ID, client secret entered by the person, stored encrypted on their credential; queries run as that app |
| Databricks (Azure 90% of the time) | Microsoft SSO for Azure Databricks (`AzureDatabricks/user_impersonation`) | Databricks OAuth (U2M, PKCE) with an account-console App connection |
| Snowflake | Microsoft SSO through Snowflake External OAuth (one Entra scope per role) | Snowflake OAuth (code + PKCE) for a custom security integration |

- **Each person brings their own workspace/account**: their Databricks workspace URL, their
  Snowflake account identifier, their OAuth app or service principal. These are stored per
  person (`platform_accounts` + encrypted `credentials`), never per server. Server `.env`
  values (`DATABRICKS_*`, `SNOWFLAKE_*`) are only defaults that pre-fill the forms.
- **Warehouse connections** can be on any platform; **semantic model connections** always come
  from Fabric (whatever platform the warehouses are on).
- Every tool call runs inside the connection's scope, read-only, and is audited. The common
  MCP tool interface stays identical across platforms.

**Deployment.** One service on port 8000 serves all three platforms behind one connector URL
(the AWS plan in README section 18: Route 53 -> ALB -> one EC2 in the private subnet ->
container port 8000). The same image runs locally with `docker compose up --build`
(`docker-compose.yml`: PostgreSQL + the service).

## Code layout

Python lives in `backend/` (no `src/`, no `iq_connect` package). Run it from the project
folder: `python -m backend.main` (the server), `python -m backend.main migrate`, etc.

```
backend/
  main.py                 create_app() + the command line
  core/
    auth/                 our OAuth server for Claude, Microsoft SSO, browser sessions, /mcp gate
    configuration/        settings, the Microsoft app registration, health, logging, checks
    connection_manager/   connections, encrypted credentials, platform accounts, platform
                          sign-in routes, the guided setup engine and driver base
    dynamic_registry/     the platform plugin registry, adapter interface, MCP tools, pipeline
    security/             encryption, tokens, read-only SQL/DAX policy, audit
    web/                  the JSON API and serving the React app
  platforms/<name>/       one folder per platform: auth.py, client.py, tools/ (query.py = the
                          common tools, discovery.py = setup driver, __init__.py = PLATFORM plugin)
  shared/                 database.py (engine, sessions, column types), models.py, migrations/
frontend/                 the React app
```

Adding a platform = adding a folder under `backend/platforms/` with a `PLATFORM` plugin.
Core code must not import a specific platform (the CLI checks are the one exception).

## Restricted implementation patterns

These restrictions remain in effect:

- Do not store usernames and passwords for any platform, password-based session caches, or a
  combined "SSO or username/password" option. ("Snowflake auth" means Snowflake OAuth.)
- Do not store Databricks personal access tokens (PATs). Use OAuth (Microsoft SSO or
  Databricks OAuth).
- Do not change the Microsoft account re-linking behaviour to reattach previously
  disconnected Fabric connections without resolving and testing the documented issue first
  (README section 20).
- Snowflake: never accept "any role" scopes (`session:role-any`, `session:scope-any`), never
  silently elevate privileges, refuse connections whose opened role differs from the pinned
  one or that have secondary roles active.
- Platform addresses a person types are checked before any token is sent: Databricks hosts
  only on Databricks domains, Snowflake only as an account identifier on
  `snowflakecomputing.com`.
- Secrets (client secrets, refresh tokens) are envelope-encrypted, never logged, never
  returned by the API.

A Fabric service principal is an approved alternative, entered by the person for their own
connections only (never a shared server credential). When a restricted pattern is requested,
explain the restriction and offer the supported alternative.

## The user and how to work with them

- The user works on Windows and uses PowerShell. Docker runs inside WSL (Ubuntu).
- The virtual environment is `.venv` (`.venv\Scripts\python.exe`, Python 3.14).
- Explain technical steps in simple language. Give copy-pasteable commands in separate code
  blocks, say which terminal to use and what output to expect.
- The user likes interactive explanations and diagrams, with code examples where useful.
- Never claim that something works unless it has actually been run. Say exactly what was run
  and what remains untested. Never claim real provider sign-in works from mocks or offline
  checks.
- The user demos frequently: keep changes focused, run lint and type checks, report results.
- The user installs packages themselves. Do not install packages without asking.
- Never delete or recreate `.venv`.
- Never read, print or write `.env`. Document every new setting in `.env.example` and
  `README.md` section 11.
- Run migration autogeneration against a scratch database, never the user's `.local/iq.db`.
- Check `README.md` section 20 for unresolved decisions rather than making assumptions.
- Follow the conventions in `README.md` section 13.

## Engineering and delivery rules

- Inspect existing implementations before changing them; reuse; no duplicate logic.
- Keep the Claude OAuth flow, callback validation, state handling, PKCE, token rotation and
  their security guarantees intact.
- Keep provider configuration failures explicit and actionable.
- There is no `tests/` folder in the repository (owner's decision, 2026-11-11: keep the repo
  production-only). Verify changes with: `ruff check backend`, `npm run lint` and
  `npm run typecheck` in `frontend/`, `python -m backend.main check-config`, and against the
  real platforms with `python -m backend.main check-connections <email>` after signing in
  through the web app (README section 15). Temporary tests may be written in a scratch folder
  outside the repository; do not add them to the repo.
- Real verification of each platform needs the owner's browser sign-in; record its date and
  result in README section 1 only after it has really run.
- Preserve database records; add migrations only when needed.
- Do not commit changes unless explicitly asked.
