"""Signing people in to our own pages: the dev provider, sessions, Entra claim rules."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from browser import Browser
from iq_connect.db.models import Org, User
from iq_connect.identity.providers import (
    EntraProvider,
    SignInFailed,
    identity_from_entra_claims,
)
from iq_connect.identity.routes import safe_next
from iq_connect.main import create_app
from iq_connect.settings import Settings

TENANT = "11111111-2222-3333-4444-555555555555"


def test_dev_sign_in_creates_the_person_and_a_session(app):
    browser = Browser(app)
    assert browser.sign_in("Alice@Example.com", next_path="/somewhere") == "/somewhere"
    home = browser.http.get("/")
    assert "alice@example.com" in home.text
    with app.state.db.session() as session:
        user = session.scalars(select(User)).one()
        assert user.email == "alice@example.com"
        assert user.last_login_at is not None


def test_signing_in_twice_is_the_same_person(app):
    Browser(app).sign_in("alice@example.com")
    Browser(app).sign_in("alice@example.com")
    Browser(app).sign_in("bob@example.com")
    with app.state.db.session() as session:
        assert len(session.scalars(select(User)).all()) == 2
        assert len(session.scalars(select(Org)).all()) == 1


def test_sign_out_ends_the_session(app):
    browser = Browser(app)
    browser.sign_in()
    browser.http.post("/auth/logout", data={"csrf": browser.csrf_on("/")})
    assert "Sign in" in browser.http.get("/").text


def test_sign_out_from_another_site_is_refused(app):
    browser = Browser(app)
    browser.sign_in()
    assert browser.http.post("/auth/logout").status_code == 403
    assert "alice@example.com" in browser.http.get("/").text


def test_tampered_session_cookie_counts_as_signed_out(app):
    browser = Browser(app)
    browser.sign_in()
    cookie = browser.http.cookies["iq_session"]
    browser.http.cookies.set("iq_session", cookie[:-3] + "xyz")
    assert "Sign in" in browser.http.get("/").text


def test_callback_without_a_login_we_started_is_refused(app):
    # Login CSRF: an attacker sends the victim's browser a link that would
    # finish the *attacker's* sign-in, so the victim works as the attacker.
    code = app.state.identity.make_code("mallory@example.com", "Mallory")
    victim = Browser(app)
    response = victim.http.get("/auth/callback/dev", params={"code": code, "nonce": "x"})
    assert response.status_code == 400
    assert "iq_session" not in victim.http.cookies


def test_callback_with_wrong_nonce_is_refused(app):
    browser = Browser(app)
    browser.http.get("/auth/login")
    code = app.state.identity.make_code("mallory@example.com", "Mallory")
    response = browser.http.get("/auth/callback/dev", params={"code": code, "nonce": "wrong"})
    assert response.status_code == 400


def test_forged_dev_code_is_refused(app):
    browser = Browser(app)
    start = browser.http.get("/auth/login")
    nonce = start.headers["location"].split("nonce=")[1]
    response = browser.http.get("/auth/callback/dev", params={"code": "forged", "nonce": nonce})
    assert response.status_code == 403


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("/connect?flow=abc", "/connect?flow=abc"),
        ("https://evil.example/", "/"),
        ("//evil.example/path", "/"),
        ("/\\evil.example", "/"),
        ("javascript:alert(1)", "/"),
        ("", "/"),
        (None, "/"),
    ],
)
def test_next_never_leaves_the_site(target, expected):
    assert safe_next(target) == expected


def test_pages_escape_what_they_show(app):
    browser = Browser(app)
    browser.sign_in("<script>alert(1)</script>@example.com")
    text = browser.http.get("/").text
    assert "<script>alert(1)" not in text
    assert "&lt;script&gt;" in text


def test_pages_cannot_be_framed_or_cached(app):
    response = TestClient(app).get("/")
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"


# --- settings and the Entra provider ------------------------------------------


def entra_settings(settings: Settings, **overrides) -> Settings:
    values = {**settings.model_dump(), "sso_provider": "entra", "entra_tenant": TENANT,
              "entra_client_id": "app-id", "entra_client_secret": "secret", **overrides}
    return Settings(_env_file=None, **values)


def test_dev_sign_in_is_refused_outside_dev(settings):
    values = {**settings.model_dump(), "app_env": "prod",
              "public_base_url": "https://iq.example.com"}
    with pytest.raises(ValidationError, match="SSO_PROVIDER=dev"):
        Settings(_env_file=None, **values)


def test_entra_needs_its_credentials(settings):
    with pytest.raises(ValidationError, match="ENTRA_CLIENT_ID"):
        entra_settings(settings, entra_client_secret="")


def test_entra_for_any_organisation_needs_an_allow_list(settings):
    with pytest.raises(ValidationError, match="SSO_ALLOWED_TENANTS"):
        entra_settings(settings, entra_tenant="organizations")
    ok = entra_settings(settings, entra_tenant="organizations", sso_allowed_tenants=TENANT)
    assert ok.allowed_tenants == {TENANT}


def test_dev_sign_in_routes_do_not_exist_with_entra(settings):
    client = TestClient(create_app(entra_settings(settings)))
    assert client.get("/auth/dev-login").status_code == 404
    assert client.post("/auth/dev-login", data={"email": "a@b.c"}).status_code == 404


def test_entra_claims_become_an_identity():
    identity = identity_from_entra_claims(
        {"tid": TENANT.upper(), "oid": "oid-1", "preferred_username": "alice@acme.com",
         "name": "Alice"},
        frozenset({TENANT}),
    )
    assert identity.issuer == f"https://login.microsoftonline.com/{TENANT}/v2.0"
    assert (identity.subject, identity.email) == ("oid-1", "alice@acme.com")


@pytest.mark.parametrize(
    "claims",
    [
        {"tid": "99999999-0000-0000-0000-000000000000", "oid": "oid-1"},  # another company
        {"tid": TENANT},  # no oid: a personal account or a broken token
        {"oid": "oid-1"},
    ],
)
def test_entra_claims_from_elsewhere_are_refused(claims):
    with pytest.raises(SignInFailed):
        identity_from_entra_claims(claims, frozenset({TENANT}))


class FakeMsal:
    def __init__(self, result: dict):
        self.result = result

    def initiate_auth_code_flow(self, scopes, redirect_uri, prompt):
        assert redirect_uri.endswith("/auth/callback/entra")
        return {"auth_uri": "https://login.microsoftonline.com/authorize?x=1", "state": "s"}

    def acquire_token_by_auth_code_flow(self, flow, params):
        assert flow["state"] == "s"
        return self.result


def test_entra_round_trip_through_our_routes(settings):
    app = create_app(entra_settings(settings))
    from iq_connect.db.models import Base

    Base.metadata.create_all(app.state.db.engine)
    provider: EntraProvider = app.state.identity
    provider._app = FakeMsal({"id_token_claims": {
        "tid": TENANT, "oid": "oid-1", "preferred_username": "alice@acme.com"}})

    browser = TestClient(app, follow_redirects=False)
    start = browser.get("/auth/login", params={"next": "/"})
    assert start.headers["location"].startswith("https://login.microsoftonline.com/")
    done = browser.get("/auth/callback/entra", params={"code": "c", "state": "s"})
    assert done.status_code == 303
    assert "alice@acme.com" in browser.get("/").text


def test_entra_error_is_shown_not_trusted(settings):
    app = create_app(entra_settings(settings))
    app.state.identity._app = FakeMsal({"error": "invalid_grant"})
    browser = TestClient(app, follow_redirects=False)
    browser.get("/auth/login")
    response = browser.get("/auth/callback/entra", params={"code": "c", "state": "s"})
    assert response.status_code == 403
