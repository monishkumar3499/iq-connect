/**
 * Add a new connection, steps 1-5: platform, workspace, warehouses, the optional
 * semantic-model question, and the models. Every resource connects on its own;
 * a failure shows on its card with Retry and never touches the others.
 *
 * A person's warehouses all live on one platform: once they have one, step 1 is
 * skipped and adding more starts from that platform's workspaces. Semantic models
 * always come from Microsoft Fabric. Every step has Back to the step before it --
 * worked out here, not from browser history, which holds the sign-in pages.
 */
import {
  ArrowRight,
  Building2,
  Check,
  CircleCheck,
  CirclePlus,
  Database,
  Lightbulb,
  ListFilter,
  RotateCw,
  Search,
  Shapes,
  Sparkles,
  Trash2,
} from "lucide-react";
import { useState } from "react";
import { Link, Navigate, useNavigate, useParams, useSearchParams } from "react-router-dom";

import {
  AccountCard,
  EmptyState,
  LoadFailure,
  Loading,
  Notice,
  Page,
  Panel,
  PlatformMark,
  SchemaChips,
  SetupSteps,
  SignedInAs,
  StatusBadge,
  type Back,
} from "@/components/layout";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  api,
  go,
  useAction,
  useApi,
  useFlow,
  withFlow,
  type Item,
  type Models,
  type PlatformList,
  type Resource,
  type Warehouses,
  type Workspaces,
} from "@/lib/api";
import { cn } from "@/lib/utils";

const connecting = (resources?: Resource[]) => !!resources?.some((r) => r.item?.status === "connecting");

// --- where Back goes ----------------------------------------------------------------------

/**
 * Leaving the setup from its first step: back to Claude's request inside its sign-in;
 * otherwise no Back link -- My connections is in the sidebar.
 */
export function useExit(): Back | null {
  const flow = useFlow();
  return flow ? { to: withFlow("/connect", flow), label: "Back to Claude's request" } : null;
}

/** The step before the workspaces: the platform, unless the platform is already settled. */
function useBeforeWorkspaces(): Back | null {
  const flow = useFlow();
  const exit = useExit();
  const found = useApi<PlatformList>("/api/setup/platforms");
  return found.data?.warehousePlatform ? exit : { to: withFlow("/app/setup", flow), label: "Back to platforms" };
}

/** A search box for long lists. */
export function Finder({ value, onChange, label }: { value: string; onChange: (value: string) => void; label: string }) {
  return (
    <div className="relative w-full sm:w-72">
      <Search className="absolute top-1/2 left-3.5 size-4 -translate-y-1/2 text-muted-foreground" />
      <Input value={value} onChange={(event) => onChange(event.target.value)} placeholder={label} aria-label={label} className="bg-card pl-10" />
    </div>
  );
}

export const matches = (text: string, filter: string) => text.toLowerCase().includes(filter.trim().toLowerCase());

// --- 1: platform -------------------------------------------------------------------------

const GOOD_TO_KNOW = [
  "All your data warehouses live on one platform: you pick it once, and adding more continues there.",
  "Semantic models always come from Microsoft Fabric, whichever platform your warehouses are on.",
  "Claude only ever reads, and only what your own account can see.",
];

export function Platforms() {
  const flow = useFlow();
  const exit = useExit();
  const found = useApi<PlatformList>("/api/setup/platforms");
  const settled = found.data?.platforms.find((p) => p.key === found.data?.warehousePlatform && p.available);

  if (settled) return <Navigate replace to={withFlow(`/app/setup/${settled.key}`, flow)} />;
  return (
    <Page
      back={exit}
      icon={<CirclePlus />}
      eyebrow="Add a new connection"
      title="Where does your data live?"
      hook="Pick the platform your data warehouses are on. You sign in with your own account, so Claude sees exactly what you can."
      step="platform"
      aside={
        <>
          <SetupSteps current="platform" />
          <Panel title="Good to know" icon={<Lightbulb />}>
            <ul className="flex flex-col gap-3 text-sm text-muted-foreground">
              {GOOD_TO_KNOW.map((line) => (
                <li key={line} className="flex gap-2.5">
                  <Check className="mt-0.5 size-4 shrink-0 text-mint-ink" />
                  {line}
                </li>
              ))}
            </ul>
          </Panel>
        </>
      }
    >
      {found.error && <LoadFailure error={found.error} />}
      {found.loading && <Loading cards={4} />}
      <div className="grid gap-5 @xl:grid-cols-2">
        {found.data?.platforms.map((platform) => {
          const next = withFlow(`/app/setup/${platform.key}`, flow);
          const to = platform.setupUrl ? `${platform.setupUrl}?${new URLSearchParams({ next })}` : platform.available ? next : "";
          const body = (
            <>
              <div className="flex items-start justify-between gap-3">
                <PlatformMark platform={platform.key} className="size-14 rounded-2xl p-3" />
                {to ? (
                  <span className="grid size-10 place-items-center rounded-full bg-lavender text-ink transition group-hover:bg-primary group-hover:text-primary-foreground">
                    <ArrowRight className="size-4" />
                  </span>
                ) : (
                  <Badge variant="outline">{platform.whyNot}</Badge>
                )}
              </div>
              <div>
                <div className="text-lg font-bold">{platform.label}</div>
                <div className="mt-1 text-sm text-muted-foreground">{platform.setupUrl ? platform.whyNot : platform.how}</div>
              </div>
              <div className="mt-auto flex flex-wrap gap-2 border-t pt-4">
                <Badge variant="default">
                  <Database /> Data warehouses
                </Badge>
                {platform.key === "fabric" && (
                  <Badge variant="default">
                    <Shapes /> Semantic models
                  </Badge>
                )}
              </div>
            </>
          );
          const tile = "group flex h-full min-w-0 flex-col gap-4 rounded-2xl border bg-card p-6 shadow-soft transition";
          return to ? (
            <Link key={platform.key} to={to} className={cn(tile, "hover:-translate-y-1 hover:border-ink/25 hover:shadow-lift")}>
              {body}
            </Link>
          ) : (
            <div key={platform.key} aria-disabled className={cn(tile, "opacity-60")}>
              {body}
            </div>
          );
        })}
      </div>
    </Page>
  );
}

// --- 2: sign in, workspace ---------------------------------------------------------------

export function WorkspacePicker() {
  const { platform = "" } = useParams();
  const [search] = useSearchParams();
  const flow = useFlow();
  const then = search.get("then") === "models" ? "models" : "";
  const found = useApi<Workspaces>(`/api/setup/${platform}/workspaces`, { flow, then });
  const beforeWorkspaces = useBeforeWorkspaces();
  const [filter, setFilter] = useState("");
  const page = (id: string) => withFlow(`/app/setup/${platform}/ws/${encodeURIComponent(id)}/${then || "warehouses"}`, flow);
  const data = found.data;

  if (data?.workspaces.length === 1 && !data.addUrl) return <Navigate replace to={page(data.workspaces[0].id)} />;
  const shown = data?.workspaces.filter((w) => matches(`${w.name} ${w.detail}`, filter)) ?? [];
  return (
    <Page
      back={then ? { to: withFlow("/app/setup/semantic-models", flow), label: "Back to semantic models" } : beforeWorkspaces}
      icon={<Building2 />}
      eyebrow={then ? "Semantic models · Microsoft Fabric" : `Add a new connection · ${data?.platform.label ?? ""}`}
      title={platform === "snowflake" ? "Choose an account" : "Choose a workspace"}
      hook={then ? "Which workspace holds the semantic model?" : "Pick the workspace that holds your data warehouses."}
      step={then ? "models" : "workspace"}
      aside={
        <>
          <SetupSteps current={then ? "models" : "workspace"} />
          {data && <AccountCard platform={data.platform.key} platformLabel={data.platform.label} account={data.platform.account} />}
        </>
      }
    >
      {found.error && <LoadFailure error={found.error} />}
      {found.loading && <Loading cards={4} />}
      {data && data.workspaces.length === 0 && (
        <EmptyState icon={<Building2 />} title="No workspaces here">
          Your account can't see any workspaces on {data.platform.label}. Ask your administrator for access.
        </EmptyState>
      )}
      {data && data.workspaces.length > 6 && (
        <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
          <Finder value={filter} onChange={setFilter} label="Find a workspace" />
          <span className="text-sm text-muted-foreground">
            {shown.length} of {data.workspaces.length} workspaces
          </span>
        </div>
      )}
      <div className="grid gap-4 @xl:grid-cols-2">
        {shown.map((workspace) => (
          <Link
            key={workspace.id}
            to={page(workspace.id)}
            className="group flex h-full min-w-0 items-center gap-4 rounded-2xl border bg-card p-5 shadow-soft transition hover:-translate-y-0.5 hover:border-ink/25 hover:shadow-lift"
          >
            <PlatformMark platform={platform} />
            <div className="min-w-0 flex-1">
              <div className="truncate font-bold">{workspace.name}</div>
              <div className="truncate text-sm text-muted-foreground">{workspace.detail || "Workspace"}</div>
            </div>
            <span className="grid size-9 shrink-0 place-items-center rounded-full bg-lavender text-ink transition group-hover:bg-primary group-hover:text-primary-foreground">
              <ArrowRight className="size-4" />
            </span>
          </Link>
        ))}
        {data?.addUrl && (
          <Link
            to={data.addUrl}
            className="group flex h-full min-w-0 items-center gap-4 rounded-2xl border border-dashed bg-card/60 p-5 transition hover:border-ink/25 hover:bg-card"
          >
            <span className="grid size-11 shrink-0 place-items-center rounded-xl bg-lavender text-ink">
              <CirclePlus className="size-5" />
            </span>
            <div className="min-w-0 flex-1">
              <div className="truncate font-bold">Add another {platform === "snowflake" ? "account" : "workspace"}</div>
              <div className="truncate text-sm text-muted-foreground">Sign in to another one of yours</div>
            </div>
          </Link>
        )}
      </div>
      {data && filter && shown.length === 0 && <p className="text-sm text-muted-foreground">No workspace matches “{filter}”.</p>}
    </Page>
  );
}

// --- the picker both warehouses and models use ----------------------------------------------

function ResourcePicker({
  kind,
  resources,
  onConnect,
  onItem,
  extra,
  busy,
  next,
}: {
  kind: "warehouse" | "model";
  resources: Resource[];
  onConnect: (ids: string[]) => Promise<void>;
  onItem: (item: Item, action: "retry" | "remove") => Promise<void>;
  extra?: (resource: Resource) => React.ReactNode;
  busy: boolean;
  next: { to: string; label: string };
}) {
  const [picked, setPicked] = useState<string[]>([]);
  const [filter, setFilter] = useState("");
  const toggle = (id: string) => setPicked((now) => (now.includes(id) ? now.filter((x) => x !== id) : [...now, id]));
  const free = (r: Resource) => r.item?.status !== "connected" && r.item?.status !== "connecting";
  const shown = resources.filter((r) => matches(`${r.name} ${r.detail}`, filter));
  const available = shown.filter(free).map((r) => r.id);
  const allPicked = available.length > 0 && available.every((id) => picked.includes(id));
  const connected = resources.filter((r) => r.item?.status === "connected").length;
  const Icon = kind === "model" ? Shapes : Database;
  const noun = kind === "model" ? "semantic model" : "warehouse";

  if (resources.length === 0) {
    return (
      <EmptyState icon={<Icon />} title={`No ${noun}s here`}>
        Your account can't use any {noun} in this workspace yet.
      </EmptyState>
    );
  }
  return (
    <>
      <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
        {resources.length > 6 ? (
          <Finder value={filter} onChange={setFilter} label={`Find a ${noun}`} />
        ) : (
          <span className="text-sm text-muted-foreground">
            {resources.length} {noun}
            {resources.length === 1 ? "" : "s"} you can use
          </span>
        )}
        {available.length > 1 && (
          <Button variant="ghost" size="sm" onClick={() => setPicked(allPicked ? [] : available)}>
            {allPicked ? "Clear selection" : "Select all available"}
          </Button>
        )}
      </div>
      <div className="grid gap-4 @xl:grid-cols-2">
        {shown.map((resource) => {
          const item = resource.item;
          const selectable = free(resource);
          const ticked = picked.includes(resource.id);
          return (
            <div
              key={resource.id}
              onClick={selectable ? () => toggle(resource.id) : undefined}
              className={cn(
                "flex h-full min-w-0 flex-col gap-4 rounded-2xl border bg-card p-5 shadow-soft transition",
                selectable && "cursor-pointer hover:border-ink/25 hover:shadow-lift",
                ticked && "border-primary/60 ring-4 ring-primary/15",
                item?.status === "connected" && "border-mint-ink/30",
                item?.status === "failed" && "border-blush-ink/30",
              )}
            >
              <div className="flex items-start gap-3.5">
                {selectable ? (
                  <span className="grid size-11 shrink-0 place-items-center rounded-xl bg-muted">
                    <Checkbox checked={ticked} onClick={(event) => event.stopPropagation()} onCheckedChange={() => toggle(resource.id)} aria-label={resource.name} />
                  </span>
                ) : (
                  <span className={cn("grid size-11 shrink-0 place-items-center rounded-xl", item?.status === "connected" ? "bg-mint text-mint-ink" : "bg-lavender text-ink")}>
                    <Icon className="size-5" />
                  </span>
                )}
                <div className="min-w-0 flex-1">
                  <div className="truncate text-base font-bold">{resource.name}</div>
                  <div className="truncate text-sm text-muted-foreground">{resource.detail}</div>
                </div>
                {item && <StatusBadge status={item.status} />}
              </div>
              {item?.status === "connected" && (
                <div className="flex flex-col gap-2.5 rounded-xl bg-muted/70 px-3.5 py-3">
                  <SignedInAs account={item.account} />
                  {item.schemas.length > 0 && <SchemaChips schemas={item.schemas} max={6} />}
                </div>
              )}
              {item?.error && <p className="rounded-xl bg-blush px-3.5 py-2.5 text-sm text-blush-ink">{item.error}</p>}
              {item && (item.status === "failed" || item.status === "connected") && (
                <div className="mt-auto flex flex-wrap items-center gap-2 border-t pt-4" onClick={(event) => event.stopPropagation()}>
                  <Button size="sm" variant="secondary" disabled={busy} onClick={() => onItem(item, "retry")}>
                    <RotateCw /> {item.status === "failed" ? "Retry" : "Check again"}
                  </Button>
                  {item.status === "failed" && (
                    <Button size="sm" variant="ghost" disabled={busy} onClick={() => onItem(item, "remove")}>
                      <Trash2 /> Remove
                    </Button>
                  )}
                  {item.status === "connected" && extra?.(resource)}
                </div>
              )}
            </div>
          );
        })}
      </div>
      {filter && shown.length === 0 && <p className="mt-2 text-sm text-muted-foreground">Nothing matches “{filter}”.</p>}
      <div className="sticky bottom-4 z-10 mt-8 flex flex-wrap items-center gap-3 rounded-2xl border bg-card/95 p-3 pl-5 shadow-float backdrop-blur">
        <span className="mr-auto flex items-center gap-2 text-sm text-muted-foreground">
          {picked.length > 0 ? (
            <>
              <strong className="text-ink">{picked.length}</strong> selected
            </>
          ) : (
            <>
              <CircleCheck className="size-4 text-mint-ink" /> {connected} connected · tick more to add them
            </>
          )}
        </span>
        <Button
          disabled={busy || picked.length === 0}
          onClick={async () => {
            await onConnect(picked);
            setPicked([]);
          }}
        >
          Connect selected {picked.length > 0 && `(${picked.length})`}
        </Button>
        <Button asChild variant="outline">
          <Link to={next.to}>
            {next.label} <ArrowRight />
          </Link>
        </Button>
      </div>
    </>
  );
}

function useItemAction(reload: () => void) {
  const action = useAction();
  const flow = useFlow();
  const onItem = async (item: Item, verb: "retry" | "remove") => {
    await action.run(() => api.post(`/api/setup/items/${item.id}/${verb}`, { flow }));
    reload();
  };
  return { action, onItem };
}

// --- 3: warehouses -----------------------------------------------------------------------------

export function WarehousesStep() {
  const { platform = "", workspace = "" } = useParams();
  const [search, setSearch] = useSearchParams();
  const flow = useFlow();
  const base = `/api/setup/${platform}/ws/${encodeURIComponent(workspace)}/warehouses`;
  const found = useApi<Warehouses>(base, { flow, catalog: search.get("catalog") ?? "" }, (d) => connecting(d?.warehouses));
  const { action, onItem } = useItemAction(found.reload);
  const beforeWorkspaces = useBeforeWorkspaces();
  const [schemasOf, setSchemasOf] = useState<Resource | null>(null);
  const data = found.data;
  const catalogLabel = platform === "snowflake" ? "Database" : "Catalog";

  const connect = async (ids: string[]) => {
    const done = await action.run(() => api.post<Warehouses>(base, { ids, catalog: data?.catalog ?? "", flow }));
    if (done) found.setData(done);
  };
  // With one workspace the picker was skipped, so Back skips it too.
  const back = data && !data.workspace.only ? { to: withFlow(`/app/setup/${platform}`, flow), label: "Back to workspaces" } : beforeWorkspaces;

  return (
    <Page
      back={back}
      icon={<Database />}
      eyebrow={data ? `${data.platform.label} · ${data.workspace.name}` : "Add a new connection"}
      title="Choose data warehouses"
      hook="Tick the ones Claude may read and connect them. Each is checked with your sign-in before it turns green."
      step="warehouses"
      actions={
        data?.platform.hasCatalogs && data.catalogs.length > 0 ? (
          <label className="flex items-center gap-3 rounded-2xl border bg-card py-1.5 pr-1.5 pl-4 text-sm font-semibold shadow-soft">
            {catalogLabel}
            <select
              value={data.catalog}
              onChange={(event) => setSearch(new URLSearchParams({ catalog: event.target.value, ...(flow ? { flow } : {}) }))}
              className="h-9 rounded-xl border bg-background px-3 font-medium"
            >
              {data.catalogs.map((catalog) => (
                <option key={catalog}>{catalog}</option>
              ))}
            </select>
          </label>
        ) : undefined
      }
      aside={
        <>
          <SetupSteps current="warehouses" />
          {data && <AccountCard platform={data.platform.key} platformLabel={data.platform.label} account={data.platform.account} />}
          {data?.platform.warehouseNote && (
            <Panel title="About this list" icon={<Lightbulb />}>
              <p className="text-sm text-muted-foreground">{data.platform.warehouseNote}</p>
            </Panel>
          )}
        </>
      }
    >
      {found.error && <LoadFailure error={found.error} />}
      {found.loading && <Loading cards={2} />}
      <Notice error={action.error} />
      {data && (
        <>
          <ResourcePicker
            kind="warehouse"
            resources={data.warehouses}
            busy={action.busy}
            onConnect={connect}
            onItem={onItem}
            extra={(resource) => (
              <Button size="sm" variant="ghost" onClick={() => setSchemasOf(resource)}>
                <ListFilter /> Limit schemas
              </Button>
            )}
            next={{ to: withFlow("/app/setup/semantic-models", flow), label: "Next" }}
          />
          {schemasOf && (
            <SchemaDialog
              name={schemasOf.name}
              load={`${base}/${encodeURIComponent(schemasOf.id)}/schemas`}
              params={{ flow, catalog: data.catalog }}
              onClose={() => setSchemasOf(null)}
              onSave={async (schemas) => {
                const done = await api.post<Warehouses>(`${base}/${encodeURIComponent(schemasOf.id)}/schemas`, { schemas, catalog: data.catalog, flow });
                found.setData(done);
                const item = done.warehouses.find((w) => w.id === schemasOf.id)?.item;
                return item?.status === "failed" ? item.error || "That warehouse couldn't be checked." : null;
              }}
            />
          )}
        </>
      )}
    </Page>
  );
}

const SAVED = "\u0000saved";

/**
 * Pick the schemas Claude may read in one warehouse (any platform). `load` answers
 * {schemas, ticked}; `onSave` saves and returns a problem to show, or null when done.
 */
export function SchemaDialog({
  name,
  load,
  params,
  onSave,
  onClose,
}: {
  name: string;
  load: string;
  params?: Record<string, string>;
  onSave: (schemas: string[]) => Promise<string | null>;
  onClose: () => void;
}) {
  const found = useApi<{ schemas: string[]; ticked: string[] }>(load, params);
  const [ticked, setTicked] = useState<string[] | null>(null);
  const [filter, setFilter] = useState("");
  const [problem, setProblem] = useState("");
  const action = useAction();
  const all = found.data?.schemas ?? [];
  const chosen = ticked ?? found.data?.ticked ?? [];
  const shown = all.filter((schema) => matches(schema, filter));
  const save = async () => {
    setProblem("");
    // `run` answers null when the request itself failed (its error is shown), so
    // success needs its own value.
    const result = await action.run(async () => (await onSave(chosen)) ?? SAVED);
    if (result === SAVED) onClose();
    else if (result) setProblem(result);
  };
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-xl">
        <div className="pr-6">
          <DialogTitle>Limit schemas · {name}</DialogTitle>
          <DialogDescription className="mt-1">Claude can read only the schemas ticked here, from its next question.</DialogDescription>
        </div>
        {found.error ? <LoadFailure error={found.error} /> : <Notice error={action.error} />}
        <Notice>{problem}</Notice>
        {found.loading && !found.error && <p className="py-6 text-center text-sm text-muted-foreground">Listing schemas as you…</p>}
        {found.data && (
          <>
            <div className="flex flex-wrap items-center justify-between gap-3">
              {all.length > 8 ? (
                <Finder value={filter} onChange={setFilter} label="Find a schema" />
              ) : (
                <span className="text-sm text-muted-foreground">
                  {all.length} schema{all.length === 1 ? "" : "s"} you can read
                </span>
              )}
              <div className="flex gap-1">
                <Button size="sm" variant="ghost" onClick={() => setTicked(all)}>
                  Select all
                </Button>
                <Button size="sm" variant="ghost" onClick={() => setTicked([])}>
                  Clear
                </Button>
              </div>
            </div>
            <div className="grid max-h-[45vh] gap-2 overflow-y-auto pr-1 sm:grid-cols-2">
              {shown.map((schema) => (
                <label key={schema} className={cn("flex cursor-pointer items-center gap-3 rounded-xl border px-4 py-3 transition hover:bg-muted", chosen.includes(schema) && "border-primary/50 bg-lavender/60")}>
                  <Checkbox checked={chosen.includes(schema)} onCheckedChange={(on) => setTicked(on === true ? [...chosen, schema] : chosen.filter((s) => s !== schema))} />
                  <span className="truncate font-semibold">{schema}</span>
                </label>
              ))}
            </div>
            <div className="flex flex-wrap items-center justify-between gap-3 border-t pt-4">
              <span className="text-sm text-muted-foreground">
                {chosen.length} of {all.length} selected
              </span>
              <div className="flex gap-2">
                <Button variant="ghost" onClick={onClose}>
                  Cancel
                </Button>
                <Button disabled={action.busy || chosen.length === 0} onClick={save}>
                  {action.busy ? "Checking…" : "Save and check"}
                </Button>
              </div>
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

// --- 4: the semantic-model question, and the models ------------------------------------------

const WHY_MODELS =
  "Semantic models provide business definitions, relationships and DAX measures that complement warehouse data. Connecting a model lets Claude cross-check warehouse results against governed business measures, perform reconciliation and produce more reliable, context-aware answers.";

const MODEL_GAINS = ["Business definitions and DAX measures", "Relationships between tables", "Reconcile warehouse numbers against governed measures"];

export function ModelsQuestion() {
  const flow = useFlow();
  const navigate = useNavigate();
  const found = useApi<{ setupUrl: string; account: string; connected: string[]; warehousePlatform: string | null }>("/api/setup/question", { flow });
  const settled = found.data?.warehousePlatform;
  const yes = () => {
    const next = withFlow("/app/setup/fabric", flow, { then: "models" });
    if (found.data?.setupUrl) go(`${found.data.setupUrl}?${new URLSearchParams({ next })}`);
    else navigate(next);
  };
  return (
    <Page
      back={settled ? { to: withFlow(`/app/setup/${settled}`, flow), label: "Back to data warehouses" } : { to: withFlow("/app/setup", flow), label: "Back to platforms" }}
      icon={<Shapes />}
      eyebrow="Add a new connection · Optional"
      title="Add a semantic model too?"
      hook="Your warehouses stay connected either way. You can add a model later from Add a new connection."
      step="models"
      aside={
        <>
          <SetupSteps current="models" />
          {found.data && <AccountCard platform="fabric" platformLabel="Microsoft Fabric" account={found.data.account} />}
        </>
      }
    >
      {found.error && <LoadFailure error={found.error} />}
      <div className="grid gap-5 @3xl:grid-cols-[1.6fr_1fr]">
        <div className="flex h-full flex-col gap-6 rounded-2xl border bg-linear-to-br from-card via-card to-lavender p-7 shadow-soft">
          <div className="flex items-center gap-4">
            <span className="grid size-14 shrink-0 place-items-center rounded-2xl bg-primary text-primary-foreground">
              <Sparkles className="size-7" />
            </span>
            <div>
              <h2 className="text-xl font-bold">Yes, connect a semantic model</h2>
              <p className="flex items-center gap-2 text-sm text-muted-foreground">
                <PlatformMark platform="fabric" className="size-5 bg-transparent p-0" /> Microsoft Fabric
              </p>
            </div>
          </div>
          <p className="text-[0.95rem] leading-relaxed text-muted-foreground">{WHY_MODELS}</p>
          <ul className="grid gap-2">
            {MODEL_GAINS.map((gain) => (
              <li key={gain} className="flex gap-2.5 rounded-xl bg-card/80 px-3.5 py-2.5 text-sm font-semibold text-ink ring-1 ring-border">
                <Check className="mt-0.5 size-4 shrink-0 text-mint-ink" /> {gain}
              </li>
            ))}
          </ul>
          {found.data && found.data.connected.length > 0 && (
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <span className="text-muted-foreground">Already connected:</span>
              {found.data.connected.map((name) => (
                <Badge key={name} variant="success">
                  {name}
                </Badge>
              ))}
            </div>
          )}
          <Button size="lg" className="mt-auto self-start" onClick={yes} disabled={found.loading}>
            Connect semantic models <ArrowRight />
          </Button>
        </div>
        <div className="flex h-full flex-col gap-5 rounded-2xl border bg-card/70 p-7">
          <span className="grid size-14 place-items-center rounded-2xl bg-muted text-ink/60">
            <Database className="size-7" />
          </span>
          <div>
            <h2 className="text-xl font-bold">Not now</h2>
            <p className="mt-2 text-[0.95rem] text-muted-foreground">Claude works with your data warehouses alone. Review what you've connected next.</p>
          </div>
          <Button asChild size="lg" variant="outline" className="mt-auto self-start">
            <Link to={withFlow("/app/setup/review", flow)}>Skip for now</Link>
          </Button>
        </div>
      </div>
    </Page>
  );
}

export function ModelsStep() {
  const { platform = "", workspace = "" } = useParams();
  const flow = useFlow();
  const base = `/api/setup/${platform}/ws/${encodeURIComponent(workspace)}/models`;
  const found = useApi<Models>(base, { flow }, (d) => connecting(d?.models));
  const { action, onItem } = useItemAction(found.reload);
  const data = found.data;
  const question = { to: withFlow("/app/setup/semantic-models", flow), label: "Back to semantic models" };

  if (found.error?.code === "no_models") return <Navigate replace to={question.to} />;
  return (
    <Page
      back={data && !data.workspace.only ? { to: withFlow(`/app/setup/${platform}`, flow, { then: "models" }), label: "Back to workspaces" } : question}
      icon={<Shapes />}
      eyebrow={data ? `${data.platform.label} · ${data.workspace.name}` : "Semantic models"}
      title="Choose semantic models"
      hook="Each model becomes its own connection, checked with a read-only query before it turns green."
      step="models"
      aside={
        <>
          <SetupSteps current="models" />
          {data && <AccountCard platform={data.platform.key} platformLabel={data.platform.label} account={data.platform.account} />}
          {data?.platform.modelNote && (
            <Panel title="About this list" icon={<Lightbulb />}>
              <p className="text-sm text-muted-foreground">{data.platform.modelNote}</p>
            </Panel>
          )}
        </>
      }
    >
      {found.error && <LoadFailure error={found.error} />}
      {found.loading && <Loading cards={2} />}
      <Notice error={action.error} />
      {data && (
        <ResourcePicker
          kind="model"
          resources={data.models}
          busy={action.busy}
          onItem={onItem}
          onConnect={async (ids) => {
            const done = await action.run(() => api.post<Models>(base, { ids, flow }));
            if (done) found.setData(done);
          }}
          next={{ to: withFlow("/app/setup/review", flow), label: "Next: review" }}
        />
      )}
    </Page>
  );
}
