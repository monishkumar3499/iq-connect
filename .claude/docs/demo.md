# Running the demo with real Claude

Verified working end to end on 2026-10-15 (Claude -> tunnel -> sign-in -> Allow -> tools).

## What is running

| Window | Command | Notes |
|---|---|---|
| 1. Tunnel | `devtunnel host iq-demo` | Named, persistent tunnel created with `devtunnel create iq-demo --allow-anonymous` + `devtunnel port create iq-demo -p 8000`. Public address: **`https://gsxtzpgc-8000.inc1.devtunnels.ms`** (use this form, NOT `...:8000`) |
| 2. Server | `python -m iq_connect` | From the project folder, `(.venv)` active. Restart after every code change (a running server keeps old code) |

`.env` (user maintains it; Claude never reads/writes it) needs only:
```
APP_ENV=dev
SSO_PROVIDER=dev
PUBLIC_BASE_URL=https://gsxtzpgc-8000.inc1.devtunnels.ms
DATABASE_URL=sqlite:///.local/iq.db
```
Everything else has defaults (`.env.example`). Key files are auto-created in `.local\` (dev).

## One-time per person
1. Open `https://gsxtzpgc-8000.inc1.devtunnels.ms/auth/login`, sign in (dev: any email).
   The user signed in as `parvat.r@jmangroup.com`.
2. Create a connection: dashboard -> **+ New connection** -> Demo (or the CLI
   `python -m iq_connect add-demo-connection EMAIL`, run from the PROJECT FOLDER with the venv).
3. In Claude: Settings -> Connectors -> Add custom connector, URL
   `https://gsxtzpgc-8000.inc1.devtunnels.ms/mcp` -> Connect -> sign in -> **Allow**.
   (Already done once; Claude registered via CIMD.)

## Demo script (in a fresh Claude chat)
1. "What IQ connections do I have?"
2. "Which country brings the most revenue? Exclude cancelled orders."
   (Expected: India 4300, United Kingdom 1825.50, Germany 1780.75.)
3. "Which orders are still processing, and for which customers?"
4. "Delete the cancelled orders." -> refused: read-only.
5. Show the dashboard (`/`): tiles, the activity list with the refused DELETE, Test connection.

## Troubleshooting seen so far
- `no such table: users` from a CLI command: it ran from `src\` or with global Python 3.12.
  Run from the project folder with `(.venv)` active.
- curl/PowerShell on this PC sometimes failed TLS to the tunnel ("untrusted root"); it went
  away on retry. Claude connects from Anthropic's cloud and is not affected.
- Browsers may show Microsoft's "developer tunnel" interstitial once -> Continue.
- If Claude reports "Authorization failed": read the server window from `>> GET /oauth/authorize`
  onwards; every request logs on arrival (`>>`) and completion (`<<`).
- Not demo-ready: Fabric (needs Entra app registration + ODBC Driver 18, never run for real),
  Snowflake and Databricks (not built; see roadmap steps 8 and 10).
