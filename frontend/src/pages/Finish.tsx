/**
 * Add a new connection, steps 6-8: review, instructions for Claude, done.
 * Inside Claude's sign-in, Save returns to Claude through the server's own
 * `redirect` (Claude's validated callback with its original `state`).
 */
import {
  ArrowRight,
  BookOpenText,
  CheckCircle2,
  CircleAlert,
  ClipboardCheck,
  Database,
  ExternalLink,
  Layers,
  Loader2,
  MessageSquareText,
  Plus,
  RotateCw,
  Settings,
  Shapes,
  Sparkles,
  Trash2,
  WandSparkles,
} from "lucide-react";
import { useState, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";

import { LoadFailure, Loading, Notice, Page, Panel, PlatformMark, SetupSteps, SignedInAs, StatTile, StatusBadge } from "@/components/layout";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { api, go, useAction, useApi, useFlow, withFlow, type Item, type SetupStatus } from "@/lib/api";
import { cn } from "@/lib/utils";

const KINDS = {
  warehouse: { title: "Data warehouses", icon: Database },
  model: { title: "Semantic models", icon: Shapes },
} as const;

// --- 6: review -----------------------------------------------------------------------------

export function StatusStep() {
  const flow = useFlow();
  const found = useApi<SetupStatus>("/api/setup/status", { flow }, (d) => !!d?.counts.connecting);
  const action = useAction();
  const data = found.data;

  const act = async (item: Item, verb: "retry" | "remove") => {
    const done = await action.run(() => api.post<SetupStatus>(`/api/setup/items/${item.id}/${verb}`, { flow }));
    if (done) found.setData(done);
  };

  // More warehouses continue on their platform (step 1 skips itself once one exists).
  const sections = [
    { kind: "warehouse", connect: withFlow("/app/setup", flow), label: "Connect a warehouse" },
    { kind: "model", connect: withFlow("/app/setup/semantic-models", flow), label: "Connect a semantic model" },
  ] as const;

  return (
    <Page
      back={{ to: withFlow("/app/setup/semantic-models", flow), label: "Back to semantic models" }}
      icon={<ClipboardCheck />}
      step="review"
      eyebrow="Add a new connection · Review"
      title="Review your connections"
      hook="Green means checked with your own sign-in and saved. Fix or remove anything that failed to continue."
      aside={<SetupSteps current="review" />}
    >
      {found.error && <LoadFailure error={found.error} />}
      {found.loading && <Loading />}
      <Notice error={action.error} />
      {data?.expired && <Notice>This sign-in from Claude has expired. Go back to Claude and press Connect again.</Notice>}
      {data && (
        <div className="flex flex-col gap-6">
          <div className="grid grid-cols-2 gap-4 @3xl:grid-cols-4">
            <StatTile icon={<CheckCircle2 />} label="Connected" value={data.counts.connected} tone="mint" />
            <StatTile icon={<Loader2 />} label="In progress" value={data.counts.connecting} />
            <StatTile icon={<CircleAlert />} label="Not connected" value={data.counts.pending} tone={data.counts.pending ? "amber" : "default"} />
            <StatTile icon={<RotateCw />} label="Failed" value={data.counts.failed} tone={data.counts.failed ? "blush" : "default"} />
          </div>
          <div className="flex flex-col gap-6">
            {sections.map((section) => {
              const items = data.items.filter((item) => item.kind === section.kind);
              const { title, icon: Icon } = KINDS[section.kind];
              return (
                <section key={section.kind} aria-label={title} className="rounded-2xl border bg-card/70 p-5 shadow-soft">
                  <div className="mb-4 flex flex-wrap items-center gap-3">
                    <span className="grid size-10 shrink-0 place-items-center rounded-xl bg-lavender text-ink">
                      <Icon className="size-5" />
                    </span>
                    <h2 className="mr-auto min-w-0 text-lg font-bold">
                      {title} <span className="font-medium text-muted-foreground">· {items.length}</span>
                    </h2>
                    {items.length === 0 && <StatusBadge status="pending" />}
                    <Button asChild size="sm" variant={items.length === 0 ? "default" : "ghost"}>
                      <Link to={section.connect}>
                        <Plus /> {items.length === 0 ? section.label : "Add more"}
                      </Link>
                    </Button>
                  </div>
                  {items.length === 0 && (
                    <p className="rounded-2xl border border-dashed px-4 py-6 text-center text-sm text-muted-foreground">
                      {section.kind === "model" ? "No semantic model. That's fine: they're optional." : "No data warehouse yet."}
                    </p>
                  )}
                  <div className="grid gap-3 @3xl:grid-cols-2">
                    {items.map((item) => (
                      <StatusRow key={item.id} item={item} busy={action.busy} onAction={act} flow={flow} />
                    ))}
                  </div>
                </section>
              );
            })}
          </div>
          <div className="sticky bottom-4 z-10 flex flex-wrap items-center justify-end gap-4 rounded-2xl border bg-card/95 p-3 pl-5 shadow-float backdrop-blur">
            <p className="mr-auto text-sm text-muted-foreground">
              {data.ready
                ? `${data.active} connection${data.active === 1 ? "" : "s"} ready for Claude.`
                : !data.expired && "Connect at least one, and retry or remove any that failed."}
            </p>
            <Button asChild={data.ready} disabled={!data.ready}>
              {data.ready ? (
                <Link to={withFlow("/app/setup/instructions", flow)}>
                  Continue <ArrowRight />
                </Link>
              ) : (
                <span>
                  Continue <ArrowRight />
                </span>
              )}
            </Button>
          </div>
        </div>
      )}
    </Page>
  );
}

function StatusRow({ item, busy, onAction, flow }: { item: Item; busy: boolean; onAction: (item: Item, verb: "retry" | "remove") => void; flow: string }) {
  const page = item.kind === "model" ? "models" : "warehouses";
  return (
    <div className={cn("flex h-full min-w-0 flex-col gap-3 rounded-2xl border bg-card p-4", item.status === "failed" && "border-blush-ink/30")}>
      <div className="flex items-start gap-3.5">
        <PlatformMark platform={item.platform} className="size-10 p-2" />
        <div className="min-w-0 flex-1">
          <div className="truncate font-bold">{item.connectionName ?? item.name}</div>
          <div className="truncate text-sm text-muted-foreground">
            {item.platformLabel} · {item.workspaceName || item.workspaceId}
          </div>
        </div>
        <StatusBadge status={item.status} />
      </div>
      {item.status === "connected" && (
        <div className="flex min-w-0 flex-col gap-1 rounded-xl bg-muted/70 px-3.5 py-2.5">
          <SignedInAs account={item.account} />
          {item.schemas.length > 0 && <span className="truncate text-xs text-muted-foreground">Schemas: {item.schemas.join(", ")}</span>}
        </div>
      )}
      {item.status !== "connected" && item.status !== "connecting" && (
        <div className="mt-auto flex flex-wrap items-center gap-2 border-t pt-3">
          {item.error && <p className="w-full text-sm text-blush-ink">{item.error}</p>}
          <Button size="sm" variant="secondary" disabled={busy} onClick={() => onAction(item, "retry")}>
            <RotateCw /> {item.status === "failed" ? "Retry" : "Connect"}
          </Button>
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => onAction(item, "remove")}>
            <Trash2 /> Remove
          </Button>
          <Button asChild size="sm" variant="ghost">
            <Link to={withFlow(`/app/setup/${item.platform}/ws/${encodeURIComponent(item.workspaceId)}/${page}`, flow)}>Change</Link>
          </Button>
        </div>
      )}
    </div>
  );
}

// --- 7: instructions -------------------------------------------------------------------------

const PLACEHOLDER = `Describe your data and business context so Claude can interpret your warehouse correctly. Include the purpose of each schema and important table, column meanings, primary and foreign keys, join relationships, date conventions, business definitions, metric calculations, filters, source-of-truth rules, data engineering transformations, staging and curated layers, and known data quality issues.

For example:
• raw: source-system data with minimal transformation.
• prep: cleaned and standardized datasets.
• curated: business-ready tables used for reporting.
• Revenue should be calculated using the approved net revenue definition.
• Join customer tables using the documented customer key, not customer names.
• When semantic models are connected, reconcile warehouse results against the relevant DAX measures when appropriate.`;

// An editable starting point (not applied unless the person picks it).
const EXAMPLE = `Schemas: raw holds source-system data, prep holds cleaned data, curated holds business-ready tables. Use curated for reporting.
Keys: join customers on customer_id, never on names.
Revenue: use the approved net revenue definition (net of returns and discounts).
Dates: the fiscal year starts in April; dates are UTC.
When a semantic model is connected, reconcile warehouse results against its DAX measures.`;

const WHAT_TO_INCLUDE = [
  ["Layers and schemas", "raw, prep, curated: what each holds"],
  ["Keys and joins", "Which columns join which tables"],
  ["Metric definitions", "Revenue, margin, active customer…"],
  ["Rules of thumb", "Fiscal year, currency, known gaps"],
];

interface InstructionsInfo {
  text: string;
  max: number;
  connections: string[];
  claude: { app: string; email: string } | null;
  expired: boolean;
}

export function InstructionsStep() {
  const flow = useFlow();
  const navigate = useNavigate();
  const found = useApi<InstructionsInfo>("/api/setup/instructions", { flow });
  const [text, setText] = useState<string | null>(null);
  const action = useAction();
  const data = found.data;
  const value = text ?? data?.text ?? "";

  const save = async () => {
    const done = await action.run(() => api.post<{ redirect: string }>("/api/setup/finish", { flow, instructions: value }));
    if (!done) return;
    if (done.redirect.startsWith("/")) navigate(done.redirect);
    else go(done.redirect); // Claude's own callback, as the server built it
  };
  const cancel = async () => {
    const done = await action.run(() => api.post<{ redirect: string }>("/api/connect/deny", { flow }));
    if (done) go(done.redirect);
  };

  return (
    <Page
      back={{ to: withFlow("/app/setup/review", flow), label: "Back to review" }}
      icon={<MessageSquareText />}
      step="instructions"
      eyebrow="Add a new connection · Last step"
      title="Instructions for Claude"
      hook="Context Claude applies to every answer, for all your connections. Optional, and editable any time in Settings."
      aside={
        <>
          <SetupSteps current="instructions" />
          <Panel title="What to include" icon={<BookOpenText />}>
            <ul className="grid gap-2.5">
              {WHAT_TO_INCLUDE.map(([title, line]) => (
                <li key={title} className="rounded-xl bg-muted/70 px-3.5 py-2.5">
                  <div className="text-sm font-semibold">{title}</div>
                  <div className="text-xs text-muted-foreground">{line}</div>
                </li>
              ))}
            </ul>
          </Panel>
        </>
      }
    >
      {found.error && <LoadFailure error={found.error} />}
      {found.loading && <Loading />}
      {data?.expired && <Notice>This sign-in from Claude has expired. Go back to Claude and press Connect again.</Notice>}
      {data && (
        <div className="flex flex-col gap-5">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm text-muted-foreground">Claude will use:</span>
            {data.connections.map((name) => (
              <Badge key={name} variant="success">
                <CheckCircle2 /> {name}
              </Badge>
            ))}
          </div>
          <Notice error={action.error} />
          <div className="relative">
            <label htmlFor="instructions" className="sr-only">
              Instructions and context for Claude
            </label>
            <Textarea
              id="instructions"
              value={value}
              maxLength={data.max}
              onChange={(event) => setText(event.target.value)}
              placeholder={PLACEHOLDER}
              className="min-h-[26rem] rounded-2xl bg-card p-5 shadow-soft"
            />
            {!value && (
              <Button size="sm" variant="secondary" className="absolute right-4 bottom-10" onClick={() => setText(EXAMPLE)}>
                <WandSparkles /> Start from the example
              </Button>
            )}
            <span className="absolute right-4 bottom-3 rounded-md bg-card/90 px-2 text-xs text-muted-foreground">
              {value.length.toLocaleString()} / {data.max.toLocaleString()}
            </span>
          </div>
          <div className="flex flex-wrap items-center justify-end gap-3 rounded-2xl border bg-card/95 p-3 pl-5 shadow-soft">
            {data.claude && (
              <p className="mr-auto text-sm text-muted-foreground">
                {data.claude.app} will query these connections as <strong className="text-ink">{data.claude.email}</strong>, read-only.
              </p>
            )}
            {data.claude && (
              <Button variant="ghost" disabled={action.busy} onClick={cancel}>
                Cancel
              </Button>
            )}
            <Button size="lg" disabled={action.busy || data.expired} onClick={save}>
              {data.claude ? `Save and return to ${data.claude.app}` : "Save and finish"} <ArrowRight />
            </Button>
          </div>
        </div>
      )}
    </Page>
  );
}

// --- 8: done (outside Claude's sign-in) ---------------------------------------------------------

interface DoneInfo {
  connections: { name: string; platform: string; active: boolean; kind: "warehouse" | "model"; account: string }[];
  chatUrl: string;
}

export function DoneStep() {
  const found = useApi<DoneInfo>("/api/setup/done");
  const data = found.data;
  return (
    <Page back={{ to: "/app/setup/instructions", label: "Back to instructions" }} icon={<Sparkles />} eyebrow="All set" title="You're connected" hook="Open Claude and ask your first question.">
      {found.error && <LoadFailure error={found.error} />}
      {found.loading && <Loading />}
      {data && (
        <div className="flex flex-col gap-6">
          <div className="flex flex-wrap items-center gap-6 rounded-2xl bg-hero p-8 text-hero-foreground shadow-float">
            <span className="grid size-16 place-items-center rounded-2xl bg-white/10">
              <CheckCircle2 className="size-9 text-[#57d3bd]" />
            </span>
            <div className="mr-auto">
              <div className="text-2xl font-extrabold">Claude can now read your data</div>
              <p className="mt-1 text-hero-foreground/70">Read-only, as you. Try: “What were our top 10 customers by revenue last quarter?”</p>
            </div>
            <div className="flex flex-wrap gap-3">
              <Button size="lg" className="bg-white text-[#1b1660] hover:bg-white/90" onClick={() => go(data.chatUrl)}>
                Open Claude <ExternalLink />
              </Button>
              <Button asChild size="lg" variant="ghost" className="text-white hover:bg-white/10 hover:text-white">
                <Link to="/app">
                  <Layers /> My connections
                </Link>
              </Button>
            </div>
          </div>
          <div className="grid gap-6 @3xl:grid-cols-2">
            {(["warehouse", "model"] as const).map((kind) => {
              const rows = data.connections.filter((c) => c.kind === kind);
              const { title, icon: Icon } = KINDS[kind];
              return (
                <Panel key={kind} title={title} icon={<Icon />}>
                  {rows.length === 0 && <p className="text-sm text-muted-foreground">None yet.</p>}
                  <div className="flex flex-col gap-2.5">
                    {rows.map((c) => (
                      <ConnectedRow key={c.name} platform={c.platform} name={c.name} account={c.account}>
                        <StatusBadge status={c.active ? "connected" : "failed"} />
                      </ConnectedRow>
                    ))}
                  </div>
                </Panel>
              );
            })}
          </div>
          <div className="grid gap-4 @3xl:grid-cols-3">
            {NEXT_STEPS.map(({ icon: Icon, title, text, to }) => (
              <Link key={title} to={to} className="group flex h-full flex-col gap-3 rounded-2xl border bg-card p-5 shadow-soft transition hover:-translate-y-0.5 hover:shadow-lift">
                <span className="grid size-10 place-items-center rounded-xl bg-lavender text-ink">
                  <Icon className="size-5" />
                </span>
                <div className="font-bold">{title}</div>
                <p className="text-sm text-muted-foreground">{text}</p>
                <span className="mt-auto flex items-center gap-1 text-sm font-semibold text-ink">
                  Open <ArrowRight className="size-4 transition group-hover:translate-x-0.5" />
                </span>
              </Link>
            ))}
          </div>
        </div>
      )}
    </Page>
  );
}

const NEXT_STEPS = [
  { icon: Layers, title: "Fine-tune a connection", text: "Rename it, add notes or limit its schemas in My connections.", to: "/app" },
  { icon: MessageSquareText, title: "Refine Claude's instructions", text: "Business rules and definitions Claude follows in every answer.", to: "/app/settings?tab=instructions" },
  { icon: Settings, title: "See what Claude asked", text: "Every query, refusal and timing, in Settings.", to: "/app/settings?tab=activity" },
];

function ConnectedRow({ platform, name, account, children }: { platform: string; name: string; account: string; children: ReactNode }) {
  return (
    <div className="flex min-w-0 items-center gap-3 rounded-2xl border bg-card p-4">
      <PlatformMark platform={platform} className="size-9 p-2" />
      <span className="min-w-0 flex-1 truncate font-bold">{name}</span>
      <span className="flex max-w-[50%] flex-col items-end gap-1">
        {children}
        <SignedInAs account={account} />
      </span>
    </div>
  );
}
