# IQ Connect

One Claude connector for Microsoft Fabric, Databricks and Snowflake. People sign in with
company SSO, create named connections (platform, scope, custom prompt), and edit them later
in a settings app. Every query runs as that person, inside that connection's scope, read-only.

Design and build plan: `.claude/docs/architecture.md` and `.claude/docs/roadmap.md`.

## Set up (once)

Run in PowerShell, in this folder:

```
py -3.14 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pip install -e . --no-deps
copy .env.example .env
.venv\Scripts\python.exe -m alembic upgrade head
```

The last line creates the database (by default a SQLite file at `.local\iq.db`). Run it
again whenever new migrations arrive. To use another database, change `DATABASE_URL` in
`.env` (examples are in `.env.example`) and run it once more.

## Run

```
.venv\Scripts\python.exe -m iq_connect
```

Then open http://127.0.0.1:8000/healthz (should show `{"status":"ok"}`) and, in dev,
http://127.0.0.1:8000/docs for the API.

## Test

```
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m ruff check src tests
```
