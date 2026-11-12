/**
 * /app, My connections: what Claude can read, and editing it. Name, notes and
 * schemas are read from the database on every tool call, so a change here reaches
 * Claude on its next question without reconnecting. Adding data lives in the
 * sidebar's "Add a new connection", never on this page.
 */
import { Activity, ArrowLeft, CalendarDays, CircleAlert, Database, Layers, ListFilter, Pencil, Shapes, Trash2 } from "lucide-react";
import { useState, type ReactNode } from "react";
import { Navigate, useSearchParams } from "react-router-dom";

import {
  EmptyState,
  formatDate,
  LoadFailure,
  Loading,
  Notice,
  Page,
  PLATFORM_LABELS,
  PlatformMark,
  SchemaChips,
  SignedInAs,
  StatTile,
  useToast,
} from "@/components/layout";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { api, useAction, useApi, type Connection, type Dashboard } from "@/lib/api";
import { cn } from "@/lib/utils";
import { Finder, matches, SchemaDialog } from "@/pages/Setup";

const KINDS = [
  { key: "all", label: "All", short: "All" },
  { key: "warehouse", label: "Data warehouses", short: "Warehouses" },
  { key: "model", label: "Semantic models", short: "Models" },
] as const;

type Kind = (typeof KINDS)[number]["key"];

export function MyConnections() {
  const found = useApi<Dashboard>("/api/dashboard");
  const toast = useToast();
  const [editing, setEditing] = useState<string | null>(null);
  const [kind, setKind] = useState<Kind>("all");
  const [filter, setFilter] = useState("");
  const [search] = useSearchParams();
  const data = found.data;

  const done = (text: string) => {
    toast(text);
    found.reload();
  };

  // Straight after signing in (`?welcome=1`, set by the sign-in button) a person with
  // nothing connected goes to adding data. Opened from the sidebar, this page always shows.
  if (data && data.connections.length === 0 && search.get("welcome")) return <Navigate replace to="/app/setup" />;
  const all = data?.connections ?? [];
  const count = (k: Kind) => (k === "all" ? all.length : all.filter((c) => c.kind === k).length);
  const shown = all.filter((c) => (kind === "all" || c.kind === kind) && matches(`${c.name} ${c.platform} ${c.workspace} ${c.catalog} ${c.account}`, filter));
  const attention = all.filter((c) => c.status !== "active").length;
  const current = all.find((c) => c.id === editing);

  return (
    <Page icon={<Layers />} title="My connections" hook="Everything Claude can read. Edit a connection and Claude uses the change from its next question.">
      {found.error && <LoadFailure error={found.error} />}
      {found.loading && <Loading cards={3} />}
      {data && all.length === 0 && (
        <EmptyState icon={<ArrowLeft />} title="No connections yet">
          Choose <strong className="text-ink">Add a new connection</strong> in the menu to connect a data warehouse and, if you like, a semantic model.
        </EmptyState>
      )}
      {data && all.length > 0 && (
        <div className="flex flex-col gap-6">
          <div className="grid grid-cols-2 gap-4 @3xl:grid-cols-4">
            <StatTile icon={<Layers />} label="Connections" value={count("all")} />
            <StatTile icon={<Database />} label="Data warehouses" value={count("warehouse")} tone="mint" />
            <StatTile icon={<Shapes />} label="Semantic models" value={count("model")} />
            <StatTile icon={<CircleAlert />} label="Need sign-in" value={attention} tone={attention ? "amber" : "mint"} />
          </div>

          <div className="flex flex-wrap items-center justify-between gap-3">
            <div role="tablist" aria-label="Show" className="flex w-full rounded-2xl border bg-card p-1 shadow-soft sm:w-auto">
              {KINDS.map((option) => (
                <button
                  key={option.key}
                  role="tab"
                  type="button"
                  aria-selected={kind === option.key}
                  onClick={() => setKind(option.key)}
                  className={cn(
                    "flex flex-1 cursor-pointer items-center justify-center gap-2 rounded-xl px-3 py-2 text-sm font-semibold whitespace-nowrap transition sm:flex-none sm:px-3.5",
                    kind === option.key ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:text-ink",
                  )}
                >
                  <span className="sm:hidden">{option.short}</span>
                  <span className="hidden sm:inline">{option.label}</span>
                  <span className={cn("rounded-full px-1.5 text-xs", kind === option.key ? "bg-primary-foreground/20" : "bg-muted")}>{count(option.key)}</span>
                </button>
              ))}
            </div>
            <Finder value={filter} onChange={setFilter} label="Find a connection" />
          </div>

          {shown.length === 0 ? (
            <EmptyState icon={<ListFilter />} title="Nothing matches">
              No connection matches what you picked. Try another filter or search.
            </EmptyState>
          ) : (
            <div className="grid gap-5 @2xl:grid-cols-2 @5xl:grid-cols-3 @[110rem]:grid-cols-4">
              {shown.map((connection) => (
                <ConnectionCard key={connection.id} connection={connection} onEdit={() => setEditing(connection.id)} />
              ))}
            </div>
          )}
        </div>
      )}
      {current && (
        <EditConnection
          key={current.id}
          connection={current}
          onClose={() => setEditing(null)}
          onChanged={done}
          onDeleted={(text) => {
            setEditing(null);
            done(text);
          }}
        />
      )}
    </Page>
  );
}

function ConnectionStatus({ connection }: { connection: Connection }) {
  return <Badge variant={connection.status === "active" ? "success" : "warning"}>{connection.status === "active" ? "Connected" : "Needs sign-in"}</Badge>;
}

function accountOf(connection: Connection) {
  return connection.account;
}

function ConnectionCard({ connection, onEdit }: { connection: Connection; onEdit: () => void }) {
  const model = connection.kind === "model";
  return (
    <article className="flex h-full min-w-0 flex-col rounded-2xl border bg-card shadow-soft transition hover:shadow-lift">
      <div className="flex items-start gap-3.5 p-5 pb-4">
        <PlatformMark platform={connection.platform} />
        <div className="min-w-0 flex-1">
          <h3 className="truncate text-base font-bold" title={connection.name}>
            {connection.name}
          </h3>
          <p className="truncate text-sm text-muted-foreground">
            {PLATFORM_LABELS[connection.platform] ?? connection.platform}
            {connection.workspace && ` · ${connection.workspace}`}
          </p>
        </div>
        <ConnectionStatus connection={connection} />
      </div>
      <div className="mx-5 flex flex-col gap-2.5 rounded-xl bg-muted/70 px-4 py-3">
        <div className="flex items-center justify-between gap-3 text-xs">
          <span className="flex min-w-0 items-center gap-1.5 font-semibold text-ink/80">
            {model ? <Shapes className="size-3.5 shrink-0" /> : <Database className="size-3.5 shrink-0" />}
            <span className="truncate">{model ? connection.models.join(", ") || "Semantic model" : connection.catalog || "Warehouse"}</span>
          </span>
          <span className="shrink-0 text-muted-foreground">{model ? "Semantic model" : "Data warehouse"}</span>
        </div>
        {!model && (connection.schemas.length > 0 ? <SchemaChips schemas={connection.schemas} /> : <span className="text-xs text-muted-foreground">No schemas</span>)}
        <SignedInAs account={accountOf(connection)} />
      </div>
      <p className={cn("mx-5 mt-3 line-clamp-2 min-h-10 text-sm", connection.notes ? "text-muted-foreground" : "text-muted-foreground/60 italic")}>
        {connection.notes || "No notes for Claude yet."}
      </p>
      <div className="mt-auto flex items-center justify-between gap-3 border-t px-5 py-3.5">
        <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <CalendarDays className="size-3.5" /> Added {formatDate(connection.createdAt)}
        </span>
        <Button size="sm" variant="outline" onClick={onEdit}>
          <Pencil /> Edit
        </Button>
      </div>
    </article>
  );
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-1 rounded-xl bg-muted/70 px-3.5 py-2.5">
      <dt className="text-xs font-semibold text-muted-foreground">{label}</dt>
      <dd className="line-clamp-2 min-w-0 text-sm font-semibold break-words">{children}</dd>
    </div>
  );
}

function EditConnection({
  connection,
  onClose,
  onChanged,
  onDeleted,
}: {
  connection: Connection;
  onClose: () => void;
  onChanged: (text: string) => void;
  onDeleted: (text: string) => void;
}) {
  const [name, setName] = useState(connection.name);
  const [notes, setNotes] = useState(connection.notes);
  const [limiting, setLimiting] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; warn: boolean; lines: string[]; relink: boolean } | null>(null);
  const action = useAction();
  const model = connection.kind === "model";
  const dirty = name !== connection.name || notes !== connection.notes;
  const base = `/api/connections/${connection.id}`;
  const scopeLabel = connection.platform === "snowflake" ? "Database" : connection.platform === "databricks" ? "Catalog" : "Warehouse";

  const save = async () => {
    if (await action.run(() => api.patch(base, { name, notes }))) {
      onChanged(`Saved “${name.trim()}”. Claude uses it from its next question.`);
      onClose();
    }
  };
  const test = async () => {
    setResult(null);
    const done = await action.run(() => api.post<typeof result>(`${base}/test`));
    if (done) setResult(done);
  };
  const remove = async () => {
    if (await action.run(() => api.delete(base))) onDeleted(`Deleted “${connection.name}”. Claude can no longer read it.`);
  };

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-3xl gap-0 p-0">
        <div className="flex items-start gap-4 border-b p-6 pr-14">
          <PlatformMark platform={connection.platform} className="size-14 rounded-2xl p-3" />
          <div className="min-w-0 flex-1">
            <DialogTitle className="truncate text-xl">{connection.name}</DialogTitle>
            <DialogDescription asChild>
              <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1">
                <ConnectionStatus connection={connection} />
                <SignedInAs account={accountOf(connection)} />
              </div>
            </DialogDescription>
          </div>
        </div>

        <div className="grid gap-6 p-6 md:grid-cols-[1fr_1.15fr]">
          <section className="flex min-w-0 flex-col gap-3">
            <h3 className="text-sm font-bold">Details</h3>
            <dl className="grid grid-cols-2 gap-2.5">
              <Fact label="Platform">{PLATFORM_LABELS[connection.platform] ?? connection.platform}</Fact>
              <Fact label="Type">{model ? "Semantic model" : "Data warehouse"}</Fact>
              <Fact label="Workspace">{connection.workspace || "—"}</Fact>
              {model ? <Fact label="Model">{connection.models.join(", ") || "—"}</Fact> : <Fact label={scopeLabel}>{connection.catalog || "—"}</Fact>}
              <Fact label="Signed in as">{accountOf(connection) || "Not linked"}</Fact>
              <Fact label="Added">{formatDate(connection.createdAt)}</Fact>
            </dl>
            {!model && (
              <div className="flex flex-col gap-3 rounded-xl border p-4">
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <div className="text-sm font-bold">Schemas Claude can read</div>
                    <div className="text-xs text-muted-foreground">{connection.schemas.length} selected</div>
                  </div>
                  <Button size="sm" variant="secondary" onClick={() => setLimiting(true)}>
                    <ListFilter /> Limit schemas
                  </Button>
                </div>
                {connection.schemas.length > 0 && <SchemaChips schemas={connection.schemas} max={12} />}
              </div>
            )}
          </section>

          <section className="flex min-w-0 flex-col gap-4">
            <h3 className="text-sm font-bold">Edit</h3>
            <div className="grid gap-2">
              <Label htmlFor="conn-name">Name Claude uses</Label>
              <Input id="conn-name" value={name} maxLength={100} onChange={(event) => setName(event.target.value)} />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="conn-notes">Notes for Claude</Label>
              <Textarea
                id="conn-notes"
                value={notes}
                maxLength={2000}
                onChange={(event) => setNotes(event.target.value)}
                placeholder="What this connection holds, currency, fiscal year, known gaps…"
                className="min-h-36"
              />
              <span className="text-right text-xs text-muted-foreground">{notes.length.toLocaleString()} / 2,000</span>
            </div>
          </section>
        </div>

        {(action.error || result) && (
          <div className="px-6 pb-4">
            <Notice error={action.error} />
            {result && (
              <div className={cn("rounded-xl px-4 py-3 text-sm", result.ok ? "bg-mint text-mint-ink" : result.warn ? "bg-amber text-amber-ink" : "bg-blush text-blush-ink")}>
                {result.lines.map((line) => (
                  <p key={line}>{line}</p>
                ))}
                {result.relink && (
                  <a className="font-semibold underline" href={`/auth/platform/entra/start?${new URLSearchParams({ next: "/app" })}`}>
                    Sign in with Microsoft again
                  </a>
                )}
              </div>
            )}
          </div>
        )}

        <div className="flex flex-wrap items-center gap-2 rounded-b-2xl border-t bg-muted/40 px-6 py-4">
          {confirmDelete ? (
            <span className="flex flex-wrap items-center gap-2">
              <span className="text-sm font-semibold text-destructive">Delete for good?</span>
              <Button size="sm" variant="destructive" disabled={action.busy} onClick={remove}>
                Yes, delete
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setConfirmDelete(false)}>
                Keep it
              </Button>
            </span>
          ) : (
            <Button variant="ghost" className="text-destructive hover:bg-blush hover:text-destructive" disabled={action.busy} onClick={() => setConfirmDelete(true)}>
              <Trash2 /> Delete
            </Button>
          )}
          <div className="ml-auto flex flex-wrap gap-2">
            <Button variant="outline" disabled={action.busy} onClick={test}>
              <Activity /> Test
            </Button>
            <Button disabled={action.busy || !dirty || !name.trim()} onClick={save}>
              Save changes
            </Button>
          </div>
        </div>

        {limiting && (
          <SchemaDialog
            name={connection.name}
            load={`${base}/schemas`}
            onClose={() => setLimiting(false)}
            onSave={async (schemas) => {
              await api.put(`${base}/schemas`, { schemas });
              onChanged(`Schemas of “${connection.name}” updated and checked. Claude uses them from its next question.`);
              return null;
            }}
          />
        )}
      </DialogContent>
    </Dialog>
  );
}
