"""A stand-in Databricks workspace (OAuth, Unity Catalog, SQL warehouses, statements)."""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx

HOST = "https://adb-1234.5.azuredatabricks.net"


class FakeDatabricks:
    def __init__(self) -> None:
        self.token_requests: list[dict[str, str]] = []
        self.statements: list[dict] = []
        self.valid_access_tokens: set[str] = set()
        self.rotate = False
        self.refresh_error: str | None = None
        self.statement_state = "SUCCEEDED"
        # Tokens from elsewhere this workspace trusts (e.g. Microsoft tokens on Azure).
        self.accepted_prefixes: list[str] = []
        self._counter = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=self.transport())

    def _issue(self) -> str:
        self._counter += 1
        token = f"dbx-at-{self._counter}"
        self.valid_access_tokens.add(token)
        return token

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/oidc/v1/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_requests.append(form)
            if form["grant_type"] == "authorization_code":
                return httpx.Response(200, json={"access_token": self._issue(),
                                                 "refresh_token": "dbx-rt-0",
                                                 "expires_in": 3600})
            if self.refresh_error:
                return httpx.Response(400, json={"error": self.refresh_error})
            body = {"access_token": self._issue(), "expires_in": 3600}
            if self.rotate:
                body["refresh_token"] = f"dbx-rt-{self._counter}"
            return httpx.Response(200, json=body)

        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if token not in self.valid_access_tokens and not any(
                token.startswith(prefix) for prefix in self.accepted_prefixes):
            return httpx.Response(401, json={"message": "Invalid access token"})

        if path == "/api/2.0/preview/scim/v2/Me":
            return httpx.Response(200, json={"userName": "alice@acme.com"})
        if path == "/api/2.0/sql/warehouses":
            return httpx.Response(200, json={"warehouses": [
                {"id": "wh1", "name": "Shared SQL", "state": "RUNNING"}]})
        if path == "/api/2.1/unity-catalog/catalogs":
            return httpx.Response(200, json={"catalogs": [{"name": "main"}, {"name": "hr"}]})
        if path == "/api/2.1/unity-catalog/schemas":
            catalog = request.url.params["catalog_name"]
            names = ["sales", "finance", "information_schema"] if catalog == "main" else ["people"]
            return httpx.Response(200, json={"schemas": [{"name": n} for n in names]})
        if path == "/api/2.1/unity-catalog/tables":
            schema = request.url.params["schema_name"]
            tables = {"sales": [("orders", "MANAGED"), ("v_revenue", "VIEW")]}.get(schema, [])
            listed = [{"name": n, "schema_name": schema, "table_type": t} for n, t in tables]
            if request.url.params.get("omit_columns") == "false" and schema == "sales":
                listed[0]["columns"] = [
                    {"name": "order_id", "type_text": "bigint", "nullable": False,
                     "position": 0},
                    {"name": "customer_id", "type_text": "bigint", "nullable": True,
                     "position": 1},
                ]
                listed[0]["table_constraints"] = [
                    {"primary_key_constraint": {"child_columns": ["order_id"]}},
                    {"foreign_key_constraint": {"child_columns": ["customer_id"],
                                                "parent_table": "main.sales.customers",
                                                "parent_columns": ["customer_id"]}},
                    {"foreign_key_constraint": {"child_columns": ["customer_id"],
                                                "parent_table": "other.crm.people",
                                                "parent_columns": ["id"]}},
                ]
                listed[1]["columns"] = [{"name": "revenue", "type_text": "double",
                                         "nullable": True, "position": 0}]
            return httpx.Response(200, json={"tables": listed})
        if path.startswith("/api/2.1/unity-catalog/tables/"):
            full_name = path.rsplit("/", 1)[1]
            if full_name != "main.sales.orders":
                return httpx.Response(404, json={"message": f"Table {full_name} not found"})
            return httpx.Response(200, json={
                "name": "orders", "table_type": "MANAGED", "comment": "One row per order",
                "columns": [
                    {"name": "amount", "type_text": "decimal(18,2)", "nullable": True,
                     "position": 1},
                    {"name": "order_id", "type_text": "bigint", "nullable": False,
                     "position": 0, "comment": "primary key"},
                ]})
        if path == "/api/2.0/sql/statements":
            body = json.loads(request.content)
            self.statements.append(body)
            if self.statement_state != "SUCCEEDED":
                return httpx.Response(200, json={"status": {
                    "state": self.statement_state,
                    "error": {"message": "[TABLE_OR_VIEW_NOT_FOUND] nope"}}})
            rows = [["North", "1200.50"], ["South", "800.00"], ["East", None]]
            return httpx.Response(200, json={
                "status": {"state": "SUCCEEDED"},
                "manifest": {"schema": {"columns": [{"name": "region"}, {"name": "revenue"}]}},
                "result": {"data_array": rows[: body["row_limit"]]},
            })
        return httpx.Response(404, json={"message": f"no fake for {path}"})
