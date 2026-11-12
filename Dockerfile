# IQ Connect: the React app and the FastAPI server (backend/) in one image. One service on
# port 8000 serves every platform (Fabric, Databricks, Snowflake) behind one connector URL.
#   docker build -t iq-connect .
# Settings come from environment variables (see .env.example); nothing secret is baked in.
# Locally: docker compose up --build (docker-compose.yml adds PostgreSQL).

# --- 1: build the web app -------------------------------------------------------------------
FROM node:22-bookworm-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# --- 2: the server ------------------------------------------------------------------------------
FROM python:3.14-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Microsoft ODBC Driver 18: Fabric warehouses are queried over ODBC (pyodbc).
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl gnupg ca-certificates \
 && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
    | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
 && echo "deb [arch=amd64,arm64 signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" \
    > /etc/apt/sources.list.d/microsoft-prod.list \
 && apt-get update \
 && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 unixodbc \
 && apt-get purge -y curl gnupg && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt
COPY backend ./backend
COPY --from=web /web/dist ./frontend/dist

# A non-root user; /data holds the local key files (and SQLite, if no DATABASE_URL is given).
RUN useradd --system --uid 10001 --home-dir /app iq \
 && mkdir /data && chown iq /data
USER iq
VOLUME /data

ENV PYTHONPATH=/app \
    APP_ENV=prod \
    HOST=0.0.0.0 \
    PORT=8000 \
    FRONTEND_DIR=/app/frontend/dist \
    DATABASE_URL=sqlite:////data/iq.db \
    LOCAL_KEY_FILE=/data/keys.json \
    LOCAL_SIGNING_KEY_FILE=/data/signing_keys.json \
    FORWARDED_ALLOW_IPS=*
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"

# Update the tables first (safe to repeat), then serve.
CMD ["sh", "-c", "python -m backend.main migrate && exec python -m backend.main"]
