"""
Command line:

    python -m iq_connect                                  run the service
    python -m iq_connect add-demo-connection EMAIL        give someone the sample database
    python -m iq_connect add-fabric-connection EMAIL      pick a Fabric warehouse
                                                          (Microsoft account linked first)
"""

from __future__ import annotations

import argparse
import sys

import uvicorn

from .settings import get_settings


def serve() -> None:
    settings = get_settings()
    uvicorn.run(
        "iq_connect.main:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        # Our own logging setup and request trace replace uvicorn's, which
        # would otherwise log every request a second time.
        log_config=None,
        access_log=False,
    )


def add_demo_connection(email: str) -> int:
    """Until the settings app exists (step 11), this is how to get a connection to try."""
    from sqlalchemy import func, select

    from .connections import service
    from .db.models import User
    from .db.session import Database

    settings = get_settings()
    if not settings.demo_enabled:
        print("The demo platform is switched off (DEMO_PLATFORM=false).")
        return 1
    db = Database(settings.database_url)
    try:
        with db.session() as session:
            users = session.scalars(
                select(User).where(func.lower(User.email) == email.strip().lower())
            ).all()
            if len(users) != 1:
                print(f"Found {len(users)} people with email {email}. They must sign in once "
                      "first (open the service's /auth/login page).")
                return 1
            user = users[0]
            names = {c.name.lower() for c in service.list_for_user(session, user.id)}
            if "demo sales" in names:
                print(f"{email} already has the 'Demo sales' connection.")
                return 0
            service.create(
                session, user, platform="demo", name="Demo sales",
                scope={"catalog": "demo", "schemas": ["sales"]},
                custom_prompt="Sample data for trying IQ Connect. Amounts are in USD.",
            )
        print(f"Added the 'Demo sales' connection for {email}.")
        return 0
    finally:
        db.dispose()


def add_fabric_connection(email: str) -> int:
    from .credentials.providers.entra import EntraTokens
    from .db.session import Database
    from .security.crypto import SecretBox
    from .security.keys import make_key_encryption_key
    from .setup.fabric import SetupFailed
    from .setup.fabric import add_fabric_connection as run_setup

    settings = get_settings()
    if not settings.entra_configured:
        print("Fabric needs the ENTRA_CLIENT_ID / ENTRA_CLIENT_SECRET / ENTRA_TENANT settings.")
        return 1
    db = Database(settings.database_url)
    try:
        name = run_setup(
            email=email, db=db, box=SecretBox(make_key_encryption_key(settings)),
            entra=EntraTokens(settings), public_url=settings.public_base_url,
        )
    except SetupFailed as error:
        print(error)
        return 1
    finally:
        db.dispose()
    print(f"Added the Fabric connection '{name}' for {email}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m iq_connect")
    commands = parser.add_subparsers(dest="command")
    demo = commands.add_parser("add-demo-connection", help="give a person the sample database")
    demo.add_argument("email")
    fabric = commands.add_parser("add-fabric-connection",
                                 help="pick a Fabric warehouse with a linked Microsoft account")
    fabric.add_argument("email")
    args = parser.parse_args(argv)

    if args.command == "add-demo-connection":
        return add_demo_connection(args.email)
    if args.command == "add-fabric-connection":
        return add_fabric_connection(args.email)
    serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
