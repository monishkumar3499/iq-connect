"""Stand-ins for Microsoft and the Fabric ODBC driver, used by the Fabric tests."""

from __future__ import annotations

from typing import Any

TENANT = "11111111-2222-3333-4444-555555555555"


class FakeMsal:
    """Plays Entra: auth-code linking and refresh-token redemption."""

    def __init__(self) -> None:
        self.redeemed: list[tuple[str, str]] = []  # (refresh_token, scope)
        self.rotate = False
        self.fail_with: str | None = None
        self.link_claims = {
            "tid": TENANT, "oid": "oid-alice", "preferred_username": "alice@acme.com",
        }
        self._counter = 0

    def initiate_auth_code_flow(self, scopes, redirect_uri, prompt=None):
        return {"auth_uri": "https://login.microsoftonline.com/authorize?x=1", "state": "s",
                "scopes": scopes, "redirect_uri": redirect_uri}

    def acquire_token_by_auth_code_flow(self, flow, params):
        if self.fail_with:
            return {"error": self.fail_with}
        return {"access_token": "link-at", "refresh_token": "rt-0",
                "id_token_claims": dict(self.link_claims)}

    def acquire_token_by_refresh_token(self, refresh_token, scopes):
        self.redeemed.append((refresh_token, scopes[0]))
        if self.fail_with:
            return {"error": self.fail_with, "error_description": "AADSTS: nope"}
        self._counter += 1
        result = {"access_token": f"at-{scopes[0]}-{self._counter}", "expires_in": 3600}
        if self.rotate:
            result["refresh_token"] = f"rt-{self._counter}"
        return result


class FakeCursor:
    def __init__(self, database: FakeWarehouse, session: FakeSession) -> None:
        self.db = database
        self.session = session
        self.description = None
        self._rows: list[tuple] = []

    def execute(self, sql: str, *params: Any) -> None:
        self.db.statements.append((sql, params))
        if self.db.error:
            raise self.db.error
        if self.session.fail_next:
            error, self.session.fail_next = self.session.fail_next, None
            raise error
        columns, rows = self.db.answer(sql, params)
        self.description = [(c,) for c in columns] if columns else None
        self._rows = list(rows)

    def fetchmany(self, size: int) -> list[tuple]:
        return self._rows[:size]

    def fetchall(self) -> list[tuple]:
        return self._rows

    def close(self) -> None:
        pass


class FakeSession:
    def __init__(self, database: FakeWarehouse) -> None:
        self.db = database
        self.autocommit = False
        self.timeout = 0
        self.closed = False
        # Set to make this connection's next statement fail (e.g. it died idle).
        self.fail_next: Exception | None = None

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.db, self)

    def close(self) -> None:
        self.closed = True


class FakeWarehouse:
    """Plays pyodbc.connect for a Fabric warehouse with sales.orders and sales.customers."""

    def __init__(self) -> None:
        self.connects: list[tuple[str, dict]] = []
        self.statements: list[tuple[str, tuple]] = []
        self.sessions: list[FakeSession] = []
        self.error: Exception | None = None

    def __call__(self, connection_string: str, **kwargs: Any) -> FakeSession:
        self.connects.append((connection_string, kwargs))
        session = FakeSession(self)
        self.sessions.append(session)
        return session

    def answer(self, sql: str, params: tuple) -> tuple[list[str], list[tuple]]:
        upper = sql.upper()
        if "REFERENTIAL_CONSTRAINTS" in upper:
            return ["fs", "ft", "fc", "ts", "tt", "tc"], [
                ("sales", "fact_orders", "customer_key", "sales", "dim_customer", "customer_key")]
        if "TABLE_CONSTRAINTS" in upper:
            return ["s", "t", "c"], [("sales", "dim_customer", "customer_key")]
        if "C.NUMERIC_SCALE" in upper and "ORDER BY C.TABLE_SCHEMA" in upper:
            return ["c"] * 10, [
                ("sales", "dim_customer", "BASE TABLE", "customer_key", "int", "NO", 1,
                 None, 10, 0),
                ("sales", "dim_customer", "BASE TABLE", "name", "varchar", "YES", 2, 100,
                 None, None),
                ("sales", "fact_orders", "BASE TABLE", "order_key", "bigint", "NO", 1, None,
                 19, 0),
                ("sales", "fact_orders", "BASE TABLE", "customer_key", "int", "YES", 2,
                 None, 10, 0),
                ("sales", "fact_orders", "BASE TABLE", "order_date", "date", "NO", 3, None,
                 None, None),
                ("sales", "fact_orders", "BASE TABLE", "amount", "decimal", "YES", 4, None,
                 18, 2),
            ]
        if "INFORMATION_SCHEMA.SCHEMATA" in upper:
            return ["SCHEMA_NAME"], [("dbo",), ("sales",), ("sys",), ("INFORMATION_SCHEMA",)]
        if "INFORMATION_SCHEMA.COLUMNS" in upper:
            return ["c"] * 7, [("order_id", "int", "NO", None, 10, 0, "BASE TABLE"),
                               ("amount", "decimal", "YES", None, 18, 2, "BASE TABLE")]
        if "INFORMATION_SCHEMA.TABLES" in upper:
            return ["s", "t", "k"], [("sales", "customers", "BASE TABLE"),
                                     ("sales", "orders", "BASE TABLE")]
        return ["region", "revenue"], [("North", 1200), ("South", 800), ("East", 300)]
