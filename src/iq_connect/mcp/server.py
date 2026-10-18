"""
The MCP tools Claude sees. Same names and parameters for every platform; the
platform-specific work happens in the adapters, behind the pipeline.

Tool descriptions are part of the product: they are how Claude decides what to
call and how to write the SQL. Keep them short, concrete and accurate.
"""

from __future__ import annotations

import asyncio
import time
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from .. import __version__
from ..adapters.base import AdapterError, ConnectionContext, PlatformAdapter
from ..adapters.fabric.scopes import POWERBI_SCOPE
from ..adapters.registry import UnknownPlatform
from ..analysis import cache as metadata_cache
from ..analysis import profile, schema_map, sqlgen
from ..connections import service as connections
from ..policy.dax_guard import check_dax
from ..policy.sql_guard import (
    QueryRejected,
    check_object,
    check_query,
    resolve_table_name,
)
from ..settings import Settings
from . import render
from .bearer import current_caller
from .pipeline import Outcome, Pipeline

INSTRUCTIONS = """\
IQ Connect queries the person's own data connections (Microsoft Fabric, Databricks,
Snowflake), read-only and limited to the scope they chose for each connection.
Start with list_connections: it gives each connection's name, SQL dialect, scope and the
person's own notes for it -- follow those notes. Then call data_map once for the whole
structure (tables, keys, joins, modelling pattern), profile_tables on the tables you will
aggregate (grain, fan-out, NULL keys), and run_query in that connection's dialect -- batch
related statements in one call. Only SELECT queries run; anything else is refused with
the reason."""

# Tools answer in text only (structured_output=False on each): with structured
# output on, the SDK sends every answer twice, doubling what lands in Claude's
# context window.
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True,
                            openWorldHint=False)

# Limits that keep one tool answer within Claude's context budget and one call quick.
MAX_BATCH = 8
MAX_PROFILE_TABLES = 8
MAP_OBJECT_LIMIT = 60
MAP_ROWCOUNT_LIMIT = 60
# Measure expressions are the payload of semantic_model_map; don't cut them short.
WIDE_CELL = 2000

ConnectionName = Annotated[str, Field(
    description="Name of one of the person's connections, exactly as list_connections shows it.",
)]


def _with_notes(outcome: Outcome, body: str) -> str:
    """Repeat the person's notes for this connection beside its results."""
    if not outcome.custom_prompt.strip():
        return body
    return f"Notes for '{outcome.context.name}': {outcome.custom_prompt.strip()}\n\n{body}"


def _scope_text(ctx: ConnectionContext) -> str:
    parts = []
    if ctx.catalog:
        parts.append(f"catalog/database {ctx.catalog}")
    if ctx.schemas:
        parts.append("schemas " + ", ".join(ctx.schemas))
    models = [m.get("name") or m.get("id") for m in ctx.settings.get("semantic_models", [])]
    if models:
        parts.append("semantic models " + ", ".join(models)
                     + " (use semantic_model_map / query_semantic_model)")
    return "; ".join(parts) or "no scope set"


def build_mcp_server(settings: Settings, pipeline: Pipeline) -> MCPServer:
    server = MCPServer(
        name="iq-connect",
        title="IQ Connect",
        version=__version__,
        instructions=INSTRUCTIONS,
        log_level=settings.log_level.upper(),
    )

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def list_connections() -> str:
        """List your data connections: name, platform, SQL dialect, scope and your notes."""
        started = time.perf_counter()
        caller = current_caller()

        def load() -> list[tuple[ConnectionContext, str, str]]:
            with pipeline.db.session() as session:
                rows = connections.list_for_user(session, caller.user_id)
                return [(connections.context_of(r), r.custom_prompt, r.status) for r in rows]

        found = await asyncio.to_thread(load)
        await pipeline.audit_simple("list_connections", "ok", started)
        if not found:
            return ("You have no data connections yet. Add one in the IQ settings app, "
                    "then ask again.")
        lines = []
        for ctx, prompt, status in found:
            try:
                adapter = pipeline.registry.get(ctx.platform)
                kind = f"{adapter.label}, SQL dialect: {adapter.dialect}"
            except UnknownPlatform:
                kind = f"{ctx.platform} (not available on this server)"
            line = f"- {ctx.name}: {kind}. Scope: {_scope_text(ctx)}."
            if status != "active":
                line += " Needs sign-in again before use."
            if prompt.strip():
                line += f" Notes: {prompt.strip()}"
            lines.append(line)
        return "Your connections:\n" + "\n".join(lines)

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def list_objects(
        connection: ConnectionName,
        schema: Annotated[str | None, Field(
            description="Only this schema. Leave out to list every schema in scope.")] = None,
    ) -> str:
        """List the tables and views a connection can read."""

        def check(ctx: ConnectionContext, _: PlatformAdapter) -> None:
            if schema:
                check_object(ctx, schema)

        outcome = await pipeline.run(
            "list_objects", connection, check=check,
            work=lambda adapter, ctx, secret: adapter.list_objects(ctx, secret, schema),
        )
        allowed = {s.casefold() for s in outcome.context.schemas}
        objects = [o for o in outcome.value if o.schema.casefold() in allowed]
        if not objects:
            return _with_notes(outcome, "No tables or views found in this connection's scope.")
        body = "\n".join(f"- {o.schema}.{o.name} ({o.kind})" for o in objects)
        return _with_notes(outcome, f"{len(objects)} objects:\n{body}")

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def describe_table(
        connection: ConnectionName,
        table: Annotated[str, Field(
            description="schema.table, or just table when the connection has one schema.")],
    ) -> str:
        """Show a table's columns, types and size, to write correct SQL against it."""

        def check(ctx: ConnectionContext, _: PlatformAdapter) -> None:
            resolve_table_name(ctx, table)

        def work(adapter, ctx, secret):
            schema, name = resolve_table_name(ctx, table)
            return adapter.describe_table(ctx, secret, schema, name)

        outcome = await pipeline.run("describe_table", connection, check=check, work=work)
        info = outcome.value
        header = f"{info.schema}.{info.name} ({info.kind})"
        if info.row_estimate is not None:
            header += f", about {info.row_estimate} rows"
        if info.comment:
            header += f" -- {info.comment}"
        columns = "\n".join(
            f"- {c.name}: {c.type}{'' if c.nullable else ', not null'}"
            + (f" -- {c.comment}" if c.comment else "")
            for c in info.columns
        )
        return _with_notes(outcome, f"{header}\nColumns:\n{columns}")

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def run_query(
        connection: ConnectionName,
        sql: Annotated[str | list[str], Field(
            description="One SELECT in the connection's SQL dialect, or a list of them to run "
                        "in one call (headline, comparison and a validation check together).")],
        max_rows: Annotated[int, Field(
            description="Most rows to return per statement.", ge=1)] = settings.default_max_rows,
    ) -> str:
        """
        Run read-only SELECT queries on a connection. Pass a list to batch: each result
        is labelled [n/total], and one failing statement does not lose the others.
        Aggregate in SQL rather than pulling raw rows.
        """
        limit = min(max_rows, settings.max_rows_limit)
        statements = [s for s in ([sql] if isinstance(sql, str) else sql) if s.strip()]
        if not statements:
            return "No SQL given."
        if len(statements) > MAX_BATCH:
            return f"Send at most {MAX_BATCH} statements per call."

        async def one(statement: str) -> tuple[str, Outcome | None]:
            def check(ctx: ConnectionContext, adapter: PlatformAdapter) -> None:
                check_query(statement, adapter.dialect, ctx)

            try:
                outcome = await pipeline.run(
                    "run_query", connection, check=check, statement=statement,
                    work=lambda adapter, ctx, secret: adapter.run_query(
                        ctx, secret, statement, limit),
                )
            except ToolError as error:
                if len(statements) == 1:
                    raise
                return f"FAILED: {error}", None
            budget = settings.max_result_chars // len(statements)
            return render.table(outcome.value, budget), outcome

        results = [await one(statement) for statement in statements]
        first = next((outcome for _, outcome in results if outcome is not None), None)
        if len(results) == 1:
            return _with_notes(first, results[0][0])
        blocks = [f"[{n}/{len(results)}]\n{text}" for n, (text, _) in enumerate(results, 1)]
        body = "\n\n".join(blocks)
        return _with_notes(first, body) if first else body

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def data_map(
        connection: ConnectionName,
        tables: Annotated[str, Field(
            description="Optional comma-separated filter, e.g. 'fact_*' or 'sales.orders'. "
                        "Leave empty for everything in scope.")] = "",
        include_row_counts: Annotated[bool, Field(
            description="Row count per table (one batched query).")] = True,
        refresh: Annotated[bool, Field(
            description="Re-read metadata instead of the 5-minute cache.")] = False,
    ) -> str:
        """
        The whole connection in one call: every table and view, columns with types and
        nullability, row counts, primary keys, the join graph between tables (declared
        `(d)` or inferred from naming), and the modelling pattern (star / snowflake /
        data vault) with the rules that follow from it. Call this first for a data
        question, then profile_tables on the tables you will aggregate, then run_query.
        """
        patterns = schema_map.parse_filter(tables)

        def work(adapter, ctx, secret) -> str:
            if refresh:
                metadata_cache.forget(ctx)
            meta = metadata_cache.get(ctx)
            if meta is None:
                meta = adapter.catalog(ctx, secret)
                metadata_cache.put(ctx, meta)
            catalogue = schema_map.build(meta)
            if not catalogue:
                return ("No tables or views found in this connection's schemas "
                        f"({', '.join(ctx.schemas)}). They may be empty, or not shared with you.")
            headline, guidance = schema_map.detect_pattern(catalogue)
            selected = sorted(
                (t for t in catalogue.values() if schema_map.matches(t, patterns)),
                key=lambda t: (t.schema.lower(), t.name.lower()))
            if not selected:
                return (f"No object matches '{tables}'. Available: "
                        + ", ".join(sorted(catalogue)[:20]))
            if include_row_counts:
                _attach_counts(adapter, ctx, secret, selected)
            header = [
                f"# {ctx.name}: {ctx.catalog} ({adapter.label}, SQL dialect {adapter.dialect})",
                f"Schemas: {', '.join(ctx.schemas)} | objects: {len(selected)} of "
                f"{len(catalogue)} | pattern: {headline}",
            ]
            if guidance:
                header.append(guidance)
            header += [schema_map.LEGEND, ""]
            body, full = schema_map.render_best(selected, header, settings.max_result_chars,
                                                MAP_OBJECT_LIMIT)
            footer = []
            if not full:
                footer.append("_Summarised to fit: only keys, dates and measure candidates are "
                              "listed. Re-call with tables='<the few you need>' for complete "
                              "column lists -- do not guess a column that is not shown._")
            footer.append("_Next: profile_tables on the tables you will aggregate, then "
                          "run_query. Use the schema.table names above verbatim._")
            return body + "\n".join(footer)

        outcome = await pipeline.run("data_map", connection, work=work)
        return _with_notes(outcome, outcome.value)

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def profile_tables(
        connection: ConnectionName,
        tables: Annotated[str | list[str], Field(
            description="Tables to profile, e.g. ['sales.orders','sales.customers'] -- all "
                        "the tables in the query you are about to write, in one call.")],
        sample_rows: Annotated[int, Field(ge=0, le=50,
                                          description="Real rows to show per table.")] = 5,
        checks: Annotated[bool, Field(
            description="Measure grain and key cardinality (exact COUNT DISTINCT).")] = True,
    ) -> str:
        """
        Verify grain, key uniqueness, join fan-out, NULL keys (rows an INNER JOIN would
        drop) and the business-date range for one or more tables, plus a sample. Declared
        keys are intent, not proof -- this measures them. Call it before any query that
        aggregates across a join.
        """
        requested = [t.strip() for t in (tables.split(",") if isinstance(tables, str)
                                         else tables) if t and t.strip()]
        if not requested:
            return "No tables given. Use the schema.table names from data_map."

        def work(adapter, ctx, secret) -> str:
            meta = metadata_cache.get(ctx)
            if meta is None:
                meta = adapter.catalog(ctx, secret)
                metadata_cache.put(ctx, meta)
            catalogue = {k.lower(): t for k, t in schema_map.build(meta).items()}

            def run(sql: str, limit: int):
                check_query(sql, adapter.dialect, ctx)  # our own SQL obeys the same rules
                return adapter.run_query(ctx, secret, sql, limit)

            sections = []
            budget = settings.max_result_chars // max(1, len(requested))
            for name in requested[:MAX_PROFILE_TABLES]:
                try:
                    schema, table_name = resolve_table_name(ctx, name)
                    table = catalogue.get(f"{schema}.{table_name}".lower())
                    if table is None:
                        near = [k for k in catalogue if table_name.lower() in k][:5]
                        sections.append(f"## {name}\nNo such table in this connection."
                                        + (f" Did you mean: {', '.join(near)}?" if near else ""))
                        continue
                    sections.append(profile.profile_table(
                        table, adapter.dialect, run, sample_rows=sample_rows, checks=checks,
                        max_chars=budget))
                except (QueryRejected, AdapterError) as error:
                    # One bad table must not lose the others.
                    sections.append(f"## {name}\n{error}")
            if len(requested) > MAX_PROFILE_TABLES:
                sections.append(f"_Only the first {MAX_PROFILE_TABLES} tables were profiled._")
            return "\n\n".join(sections)

        outcome = await pipeline.run("profile_tables", connection, work=work)
        return _with_notes(outcome, outcome.value)

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def semantic_model_map(
        connection: ConnectionName,
        model: Annotated[str, Field(
            description="Semantic model name or id from list_connections. Empty when the "
                        "connection has exactly one.")] = "",
        include_columns: Annotated[bool, Field(
            description="Also list every column. Usually unnecessary.")] = False,
    ) -> str:
        """
        The analytical contract of a semantic model in one call: every measure with its
        full DAX expression, the tables, and the relationships with their cross-filter
        direction. Read this before writing DAX, and reuse published measures by name --
        they are the governed KPI definitions and match the published reports.
        """

        def work(adapter, ctx, secret) -> str:
            _needs_models(adapter)
            chosen = adapter.resolve_model(ctx, model)
            sections = adapter.model_map(ctx, secret, chosen["id"], include_columns)
            budget = settings.max_result_chars // max(1, len(sections))
            parts = [f"### Semantic model: {chosen.get('name') or chosen['id']}"]
            for title, rows in sections:
                parts.append(f"**{title}**: unavailable on this model." if rows is None else
                             f"**{title}** ({len(rows.rows)})\n"
                             + render.table(rows, budget, max_cell=WIDE_CELL))
            parts.append("_Query with query_semantic_model. Prefer published measures: "
                         'EVALUATE SUMMARIZECOLUMNS(dim[col], "KPI", [Measure Name]). Only '
                         "write your own arithmetic when no measure covers the KPI, and say "
                         "so if you do._")
            return "\n\n".join(parts)

        outcome = await pipeline.run("semantic_model_map", connection, work=work,
                                     token_scopes=(POWERBI_SCOPE,))
        return _with_notes(outcome, outcome.value)

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def query_semantic_model(
        connection: ConnectionName,
        dax: Annotated[str | list[str], Field(
            description="One DAX query, or a list to run in one call. Must start with "
                        "EVALUATE, or DEFINE ... EVALUATE.")],
        model: Annotated[str, Field(
            description="Semantic model name or id. Empty when the connection has one.")] = "",
        max_rows: Annotated[int, Field(ge=1, description="Most rows per query.")] = (
            settings.default_max_rows),
    ) -> str:
        """
        Run read-only DAX on a semantic model, including its published measures --
        e.g. to cross-check a warehouse number against the governed measure. Typical:
        EVALUATE SUMMARIZECOLUMNS(dim_customer[segment], "Revenue", [Total Revenue]).
        DEFINE MEASURE is allowed (query-scoped). A list is labelled [n/total] and one
        failing query does not lose the others.
        """
        limit = min(max_rows, settings.max_rows_limit)
        queries = [q for q in ([dax] if isinstance(dax, str) else dax) if q.strip()]
        if not queries:
            return "No DAX given."
        if len(queries) > MAX_BATCH:
            return f"Send at most {MAX_BATCH} queries per call."

        async def one(query: str) -> tuple[str, Outcome | None]:
            def check(ctx: ConnectionContext, adapter: PlatformAdapter) -> None:
                _needs_models(adapter)
                check_dax(query)
                adapter.resolve_model(ctx, model)

            def work(adapter, ctx, secret):
                chosen = adapter.resolve_model(ctx, model)
                return adapter.execute_dax(ctx, secret, chosen["id"], query, limit)

            try:
                outcome = await pipeline.run("query_semantic_model", connection, check=check,
                                             statement=query, work=work,
                                             token_scopes=(POWERBI_SCOPE,))
            except ToolError as error:
                if len(queries) == 1:
                    raise
                return f"FAILED: {error}", None
            budget = settings.max_result_chars // len(queries)
            return render.table(outcome.value, budget), outcome

        results = [await one(query) for query in queries]
        first = next((outcome for _, outcome in results if outcome is not None), None)
        if len(results) == 1:
            return _with_notes(first, results[0][0])
        body = "\n\n".join(f"[{n}/{len(results)}]\n{text}"
                           for n, (text, _) in enumerate(results, 1))
        return _with_notes(first, body) if first else body

    return server


def _needs_models(adapter) -> None:
    if "semantic_model" not in adapter.capabilities:
        raise AdapterError(f"{adapter.label} connections have no semantic models; use "
                           "run_query instead.")


def _attach_counts(adapter, ctx, secret, tables) -> None:
    """One batched count for every base table, or none: the map is useful without them."""
    targets = [t for t in tables if t.kind == "TABLE"]
    if not targets or len(targets) > MAP_ROWCOUNT_LIMIT:
        return
    sql = sqlgen.row_counts([(t.schema, t.name) for t in targets], adapter.dialect)
    try:
        check_query(sql, adapter.dialect, ctx)
        result = adapter.run_query(ctx, secret, sql, len(targets))
    except (QueryRejected, AdapterError):
        return
    counts = {str(row[0]).lower(): row[1] for row in result.rows}
    for table in targets:
        table.row_count = counts.get(table.qualified.lower())

