"""
`python -m iq_connect add-fabric-connection EMAIL` -- create a Fabric connection.

Everything is chosen from what the person can actually see, using their own
linked Microsoft account: their workspaces, that workspace's warehouses, that
warehouse's schemas. Nothing is typed in that could point somewhere else.
The web wizard (`web/wizard.py`) does the same steps in the browser.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import func, select

from ..adapters.base import AdapterError, ConnectionContext
from ..adapters.fabric.adapter import FabricAdapter
from ..adapters.fabric.api import FabricApi
from ..adapters.fabric.scopes import FABRIC_API_SCOPE, SQL_SCOPE
from ..connections import service as connections
from ..credentials.linked import NotLinked, microsoft_token
from ..credentials.providers.entra import EntraTokens
from ..db.models import User
from ..db.session import Database
from ..security.crypto import SecretBox

Ask = Callable[[str], str]
Say = Callable[[str], None]


class SetupFailed(Exception):
    pass


def warehouse_probe(warehouse: dict[str, str]) -> ConnectionContext:
    """A connection-shaped description of a warehouse, to list its schemas."""
    return ConnectionContext(
        id="setup", name=warehouse["name"], platform="fabric",
        scope={"catalog": warehouse["name"], "schemas": []},
        settings={"sql_endpoint": warehouse["sql_endpoint"]}, credential_version=None,
    )


def choose(options: list[str], prompt: str, ask: Ask, say: Say, *, many: bool = False) -> list[int]:
    for number, option in enumerate(options, start=1):
        say(f"  {number}. {option}")
    while True:
        answer = ask(prompt).replace(" ", "")
        try:
            picks = [int(part) - 1 for part in answer.split(",") if part]
        except ValueError:
            picks = []
        if picks and all(0 <= p < len(options) for p in picks) and (many or len(picks) == 1):
            return sorted(set(picks))
        say("Please enter " + ("numbers separated by commas." if many else "one number."))


def add_fabric_connection(
    *, email: str, db: Database, box: SecretBox, entra: EntraTokens, public_url: str,
    ask: Ask = input, say: Say = print,
    api_factory: Callable[[str], Any] = FabricApi,
    adapter: FabricAdapter | None = None,
) -> str:
    adapter = adapter or FabricAdapter(login_timeout=30, query_timeout=60)

    with db.session() as session:
        users = session.scalars(
            select(User).where(func.lower(User.email) == email.strip().lower())
        ).all()
        if len(users) != 1:
            raise SetupFailed(f"Found {len(users)} people with email {email}. They must sign "
                              f"in once first: {public_url}/auth/login")
        user_id = users[0].id
        existing = {c.name.lower() for c in connections.list_for_user(session, user_id)}

    try:
        credential_id, api_token = microsoft_token(db, box, entra, user_id, FABRIC_API_SCOPE)
        api = api_factory(api_token)
        workspaces = api.workspaces()
        if not workspaces:
            raise SetupFailed("Your Microsoft account can't see any Fabric workspaces.")
        say("Workspaces you can see:")
        workspace = workspaces[choose([w["name"] for w in workspaces], "Workspace number: ",
                                      ask, say)[0]]

        warehouses = [w for w in api.warehouses(workspace["id"]) if w.get("sql_endpoint")]
        if not warehouses:
            raise SetupFailed(f"No warehouses with a SQL endpoint in {workspace['name']}.")
        say(f"Warehouses in {workspace['name']}:")
        warehouse = warehouses[choose([w["name"] for w in warehouses], "Warehouse number: ",
                                      ask, say)[0]]

        _, sql_token = microsoft_token(db, box, entra, user_id, SQL_SCOPE)
        schemas = adapter.list_schemas(warehouse_probe(warehouse),
                                       {"access_tokens": {SQL_SCOPE: sql_token}})
    except NotLinked:
        raise SetupFailed("No Microsoft account is linked yet. Sign in and link it at "
                          f"{public_url}/auth/platform/entra/start") from None
    except AdapterError as error:
        raise SetupFailed(str(error)) from None
    if not schemas:
        raise SetupFailed(f"{warehouse['name']} has no schemas you can read.")

    say(f"Schemas in {warehouse['name']} (Claude will only see the ones you pick):")
    picked = [schemas[i] for i in choose(schemas, "Schema numbers, e.g. 1,3: ", ask, say,
                                          many=True)]

    name = ask(f"Connection name [{warehouse['name']}]: ").strip() or warehouse["name"]
    while name.lower() in existing:
        name = ask(f"You already have '{name}'. Another name: ").strip()
    notes = ask("Notes for Claude about this data (optional): ").strip()

    with db.session() as session:
        user = session.get(User, user_id)
        connections.create(
            session, user, platform="fabric", name=name, custom_prompt=notes,
            scope={"catalog": warehouse["name"], "schemas": picked},
            settings={"workspace_id": workspace["id"], "warehouse_id": warehouse["id"],
                      "sql_endpoint": warehouse["sql_endpoint"]},
            credential_id=credential_id,
        )
    return name
