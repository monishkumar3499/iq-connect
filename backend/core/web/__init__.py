"""
The web app: a React single-page app (frontend/, built to frontend/dist) and the
JSON API it calls (setup.py, account.py). FastAPI serves both from one origin, so
the session cookie, CSRF token and OAuth redirects work exactly as before.

The app's addresses are the wizard's real paths (/setup, /app/setup/fabric?flow=...),
so every sign-in redirect lands back on the right screen; the server answers each
of them with index.html and the app routes from there.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, Response

from backend.core.web import account, setup
from backend.core.web.common import ApiError, api_error_response

# Pages show who is signed in and act with their CSRF token: never cached, never
# framed (clickjacking), scripts and styles only from this site. No `form-action`
# rule on purpose: browsers apply it to where a submission *redirects*, and the
# sign-in flows redirect to Microsoft, Databricks and Claude.
PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'"),
}
# The app's own screens; everything under /app is the app's.
APP_PATHS = ("/setup", "/connect", "/error", "/app", "/app/{rest:path}")


def frontend_dir(configured: str) -> Path:
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[3] / "frontend" / "dist"


def install(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, api_error_response)
    app.include_router(setup.router)
    app.include_router(account.router)
    dist = frontend_dir(app.state.settings.frontend_dir)
    router = APIRouter(include_in_schema=False)

    @router.get("/")
    def home() -> Response:
        return RedirectResponse("/setup", status_code=307)

    def index(_: Request) -> Response:
        page = dist / "index.html"
        if not page.is_file():
            return PlainTextResponse("The web app isn't built yet. Run `npm run build` in "
                                     "the frontend folder.", status_code=503,
                                     headers=PAGE_HEADERS)
        return FileResponse(page, headers=PAGE_HEADERS, media_type="text/html")

    for path in APP_PATHS:
        router.add_api_route(path, index, methods=["GET"])

    @router.get("/assets/{name}")
    def asset(name: str) -> Response:
        # Built file names carry a content hash, so they may be cached for good.
        found = (dist / "assets" / name).resolve()
        if found.parent != (dist / "assets").resolve() or not found.is_file():
            return Response(status_code=404)
        return FileResponse(found, headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff"})

    @router.get("/favicon.svg")
    def favicon() -> Response:
        found = dist / "favicon.svg"
        return FileResponse(found) if found.is_file() else Response(status_code=404)

    app.include_router(router)
    app.state.serve_app_page = index
