# Roadmap

Update the status column as work lands. "Done" means it was run and tested, not just written.
Last updated 2026-10-15. Current totals: **374 tests pass, ruff clean.** Nothing is committed to
git yet (every file untracked) -- the user has been offered a first commit, not yet accepted.

| # | Step | What it delivers | Status |
|---|---|---|---|
| 0 | Foundations | `runtime.txt` (3.14), pinned `requirements.txt`, `.claude/` notes and design | Done (2026-10-14) |
| 1 | Skeleton | `src/iq_connect/` layout, `settings.py`, `main.py`, logging with request-arrival trace, `/healthz`, `/readyz`, `.env.example`, `requirements-dev.txt`, pytest | Done (2026-10-14) |
| 2 | Database | SQLAlchemy models, Alembic migration, pooled sessions. SQLite for now (user's choice), any DB via `DATABASE_URL` | Done (2026-10-14). PostgreSQL never actually run |
| 3 | Encryption | Envelope encryption (AES-256-GCM per secret, KEK-wrapped), `security/keys.py`, `credentials/service.py` | Done (2026-10-14) with the LOCAL key file only. Key Vault -> step 12 (no Azure subscription yet) |
| 4 | OAuth server | Discovery docs, CIMD + DCR, authorize, token, revoke, refresh rotation + reuse detection + 30 s grace | Done (2026-10-14). **Verified with real Claude 2026-10-15** (Claude used CIMD). Untested: concurrent-rotation refusal path. Missing: cleanup job, rate limits |
| 5 | Our SSO | Entra OIDC for our pages (or dev type-any-email), signed cookie session + CSRF, `/connect` page | Done (2026-10-14). Entra path only tested against a fake MSAL -- user has no Entra app registration yet. Sessions can't be revoked server-side (8 h) |
| 6 | Tool pipeline | `/mcp` (mcp 2.3) behind `McpGate`, 5-step pipeline, per-platform pools, limits, audit, `demo` platform | Done (2026-10-14). **Verified with real Claude 2026-10-15**: list_connections, list_objects, run_query, DELETE refused, out-of-scope refused, audit rows written |
| 7 | SQL policy | sqlglot allow-list guard per dialect + scope | Partly done in step 6 (`policy/sql_guard.py`). TODO: per-dialect adversarial suite, function deny rules (Snowflake `SYSTEM$...`, T-SQL `xp_`/`sp_`/`OPENQUERY`/`WAITFOR`, Databricks `reflect`/`java_method`/`read_files`), LATERAL FLATTEN on columns (refused today) |
| 8 | Snowflake adapter | | **BLOCKED for Claude, all sign-in types.** Password sign-in was stopped four times; on 2026-10-18 the adapter's session code with Snowflake OAuth was stopped too (partial file deleted). Left in place, unwired: `credentials/providers/snowflake.py` (Snowflake OAuth link + token exchange) and SNOWFLAKE_ACCOUNT / _OAUTH_CLIENT_ID / _SECRET settings. Do not retry. A teammate can write `adapters/snowflake/adapter.py` against the `PlatformAdapter` interface (see Fabric/Databricks adapters); the wizard shows Snowflake as "not available yet" |
| 9 | Fabric adapter | Entra-linked account, warehouse SQL over ODBC, browse, connection reuse | SQL part done (2026-10-14/15), all against FAKES (fake MSAL, fake ODBC) -- never real Fabric. Built: account linking, per-call access tokens (cached; rotated refresh token stored without version bump), `adapters/fabric/{adapter,api,pool,scopes}.py`, `add-fabric-connection` CLI, wizard flow. TODO: semantic models + DAX tools, `data_map`/`profile_tables`, real-tenant test (needs app registration + ODBC Driver 18 + delegated API permissions) |
| 10 | Databricks adapter | Databricks OAuth (U2M) sign-in, Unity Catalog browse, SQL Statement Execution API, wizard | Done 2026-10-15 against a FAKE workspace (`tests/fake_databricks.py`), never a real one. `credentials/providers/databricks.py` (`<host>/oidc/v1/authorize|token`, PKCE, scopes `all-apis offline_access`, refresh token stored encrypted as `Credential(provider="databricks", account=<host>)`, access tokens cached), `credentials/databricks_routes.py` (`/auth/platform/databricks/start|callback`), `adapters/databricks/{api,adapter}.py` (UC REST for list/describe -- no warehouse needed; statements with catalog + default schema, `on_wait_timeout=CANCEL`, row_limit+1), wizard path warehouse -> catalog -> schemas (re-validated on POST). Settings DATABRICKS_HOST, DATABRICKS_OAUTH_CLIENT_ID/_SECRET (one workspace per server). PAT sign-in remains blocked for Claude. Azure variant added 2026-10-18: with DATABRICKS_HOST + ENTRA_* (no Databricks OAuth app) the linked Microsoft account signs in (`AZURE_DATABRICKS_SCOPE`, `adapter.token_scopes_by_provider`, `/auth/platform/entra/start?platform=databricks` asks for that consent); `settings.databricks_enabled`. TODO: query ids for long statements |
| 11 | Web app | Dashboard + wizard (server-rendered, no JS) | Started 2026-10-15, see "Web pages" below. React app not started |
| 12 | Deploy | Dockerfile (+ ODBC Driver 18), compose, Key Vault, managed identity, tunnel guide | Not started (dev-tunnel steps are in `.claude/docs/demo.md`) |
| 13 | Hardening | metrics, SIEM export, load test, org admin policy, rate limits, housekeeping | Not started |
| 14 | EMA | `jwt-bearer` grant, trusted-issuer admin setting, xaa.dev test | Not started |

Open decisions: single vs multi-tenant; write access (assumed never); Databricks on Azure?
(decides step 10); Snowflake via External OAuth? (decides step 8). See `.claude/CLAUDE.md`.

## Web pages (step 11 so far)

| Page / action | File | Notes |
|---|---|---|
| `/` landing (signed out) / dashboard (signed in) | `web/dashboard.py` | Tiles (connections, answered, refused, avg time), connection cards, recent activity (30 audit rows), connected apps, setup section |
| Connection card: edit name + notes, delete | `web/dashboard.py` | POST `/app/connections/{id}/edit` and `/delete`, CSRF + owner check, fixed `?done=`/`?problem=` codes |
| Connection card: **Test connection** | `web/dashboard.py::test_connection` | Runs through the real pipeline (`pipeline.run(..., user_id=, allow_needs_signin=True)`) doing `list_objects`; result page: working / warning (schema with no readable tables) / not working; success sets status active; audited as `test_connection`. **No tests written yet** |
| Connection card: Sign-in row | `web/dashboard.py` | demo: "not needed"; Fabric: linked Microsoft account username (read from the encrypted secret) |
| Setup: "Sign in with a different Microsoft account" | link to `/auth/platform/entra/start?next=/` | Replaces the link (same credential row, version bumped). **No test yet** |
| Setup: **Remove sign-in** | POST `/app/microsoft/unlink` | Deletes the entra credential, sets its connections `needs_signin` + `credential_id=None`. **KNOWN GAP: relinking does NOT reattach those connections** (the reattach change was stopped by a safety check and reverted -- do not retry it in the same form). Suggested to the user: hide this button until fixed. **No tests** |
| Connected apps: Disconnect | POST `/app/apps/disconnect` | Revokes the user's refresh tokens for that client |
| Add demo connection (dev) | POST `/app/demo-connection` | |
| New-connection wizard | `web/wizard.py` | `/app/connections/new` (platform) -> `/new/demo` or `/new/fabric` (link Microsoft -> workspace -> warehouse -> schema checkboxes -> name/notes). Reachable from dashboard and from `/connect` during Claude's sign-in (`flow` carried, regex-validated, returns to `/connect?flow=`). On POST, warehouse re-fetched by id; schemas must be a subset of real ones |

## Tool set: target, from the prototype (review 2026-10-14)

The prototype (`C:\Users\ParvatR\Downloads\fabric-iq-feat-314\fabric-iq-feat-314`) had 7
Fabric, 10 Databricks and 8 Snowflake tools, each named per platform. Mapped onto ONE shared
set (same names everywhere; a capability decides which connections support a tool):

| Shared tool | Status | Replaces (prototype) |
|---|---|---|
| `list_connections` | built | `*_health` (partly), `snowflake_context` |
| `list_objects` | built | `snowflake_list_databases/_schemas/_tables`, `databricks_catalog_browse` |
| `describe_table` | built | `snowflake_describe_table`, `databricks_table_schema` |
| `run_query` | built; accepts a LIST (max 8), labelled `[n/total]`, one failure doesn't lose the others (2026-10-18) | `warehouse_query`, `snowflake_query`, `databricks_query`, `databricks_execute_sql_multi`. TODO: accept a LIST of statements, run concurrently, label `[n/total]`, one failure doesn't lose the others |
| `data_map` | built 2026-10-18 (`analysis/schema_map.py`, ported from prototype `schema.py`; `adapter.catalog()` per platform; 5-min metadata cache `analysis/cache.py`) | Fabric `warehouse_map`: tables, columns, row counts, keys, join graph (declared vs inferred), modelling pattern; cached. Port `fabric_iq/schema.py` logic |
| `profile_tables` | built 2026-10-18 (`analysis/profile.py`; SQL from `analysis/sqlgen.py` per dialect, still passes the read-only guard) | Fabric `warehouse_profile`: grain, key uniqueness, fan-out, NULL keys, date span, sample rows |
| `check_connection` | TODO | `*_health`. (The dashboard's Test connection now covers this for people; a tool for Claude is still open) |
| `get_query_result` | TODO (with first async adapter) | new |
| `semantic_model_map`, `query_semantic_model` | built 2026-10-18 (capability `semantic_model`, Fabric). Models are scoped per connection (`settings.semantic_models`, chosen in the wizard, re-validated on POST); DAX guard `policy/dax_guard.py` (ported); Power BI token minted only for DAX calls (`pipeline.run(token_scopes=...)`), so SQL never needs Power BI permission. Model list shown in list_connections | prototype DAX tools (+ DAX guard; DEFINE MEASURE allowed) |
| `open_settings` | TODO | new; answers IDEA.md -- could become an MCP Apps UI (SDK has `mcp.server.apps`) |

Deliberately NOT carried over: `snowflake_set_context`, `databricks_list_warehouses`
(connection settings instead), `databricks_volume_info` (reads raw files outside table scope),
`databricks_list_functions`, `databricks_catalog_info`/`_schema_info` (fold into `data_map`).

Prototype doctrine NOT yet ported: `SYSTEM_PROMPT.md` and `ANSWER_RULES.md` (re-injected
~once per question, see prototype `fabric_iq/prompt.py`). Both files are missing from the
prototype copy on this PC; get them from the original repo before porting.

## Backlog

Correctness / security gaps in what exists:
- **Remove sign-in strands Fabric connections** (see Web pages). Hide the button or fix.
- **No tests** for Test connection, change Microsoft account, Remove sign-in.
- CLI footgun: `python -m iq_connect ...` run from `src\` (or with global Python) uses another
  relative SQLite path and no `.env` -> "no such table: users". Fix: resolve `.env` and
  relative paths from the project root; say "database not set up" plainly.
  (`python -m src.iq_connect ...` from the project root also works today.)
- Org policy (`orgs.policy`) stored but enforced nowhere; `users.is_admin` never set.
- Calls failing before a connection resolves (unknown name, removed account) aren't audited.
- No rate limits (`/oauth/register`, `/oauth/token`, `/auth/*`, per-org tool calls).
- No housekeeping: expired OAuth flows/codes/refresh tokens, stale DCR/CIMD rows, audit retention.
- Browser sessions can't be revoked server-side (stateless cookie, 8 h).
- Pools and per-person limits are per copy, not global.
- `prometheus-client` pinned but `/metrics` doesn't exist.
- Untested for real: PostgreSQL, Entra, Fabric, concurrent refresh rotation.
- Fixed: `POST /auth/logout` CSRF (2026-10-15).
