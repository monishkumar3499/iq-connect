"""
DAX against a Fabric / Power BI semantic model, as the person, through the
Power BI Execute Queries API. Ported from the prototype
(`fabric_iq/data.py::FabricClient.execute_dax`).
"""

from __future__ import annotations

from typing import Any

import httpx

from ..base import AdapterError, CredentialRejected, Rows

POWERBI_API = "https://api.powerbi.com/v1.0/myorg"


def _explain(response: httpx.Response) -> str:
    try:
        detail = response.json().get("error", {})
        message = detail.get("pbi.error", {}).get("details", [{}])[0].get("detail", {}).get(
            "value") or detail.get("message") or ""
    except (ValueError, AttributeError, IndexError):
        message = ""
    if response.status_code == 403:
        return ("Power BI refused (403): you may lack Build permission on this semantic model, "
                "or the tenant blocks the Execute Queries API."
                + (f" {message}" if message else ""))
    if response.status_code == 404:
        return "That semantic model doesn't exist or isn't visible to you (404)."
    return f"Power BI answered HTTP {response.status_code}." + (f" {message}" if message else "")


def execute_dax(http: httpx.Client, token: str, workspace_id: str, model_id: str,
                dax: str) -> Rows:
    try:
        response = http.post(
            f"{POWERBI_API}/groups/{workspace_id}/datasets/{model_id}/executeQueries",
            headers={"Authorization": f"Bearer {token}"},
            json={"queries": [{"query": dax}], "serializerSettings": {"includeNulls": True}},
        )
    except httpx.HTTPError as error:
        raise AdapterError(f"Could not reach Power BI: {type(error).__name__}") from None
    if response.status_code == 401:
        raise CredentialRejected("Power BI rejected the sign-in (401).")
    if response.status_code != 200:
        raise AdapterError(_explain(response))
    body: dict[str, Any] = response.json()
    result = (body.get("results") or [{}])[0]
    if result.get("error"):
        raise AdapterError(f"DAX: {result['error'].get('message', 'the query failed')}")
    rows = ((result.get("tables") or [{}])[0]).get("rows") or []
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    return Rows(columns, [tuple(row.get(c) for c in columns) for row in rows])
