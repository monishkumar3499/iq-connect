/**
 * /app/settings: the person's IQ Connect account (profile, the platform sign-ins
 * their connections use, Claude's access, instructions, activity), and deleting it.
 * One tab at a time (`?tab=`), so each view stays short and aligned.
 */
import {
  BookOpenText,
  Check,
  CheckCircle2,
  Clock,
  Copy,
  Fingerprint,
  History,
  KeyRound,
  LogOut,
  MessageSquareText,
  Plug,
  Settings as SettingsIcon,
  ShieldAlert,
  ShieldCheck,
  Trash2,
  TriangleAlert,
  UserRound,
  Wrench,
} from "lucide-react";
import { useState, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";

import {
  ConnectorAddress,
  EmptyState,
  formatDate,
  Initials,
  LoadFailure,
  Loading,
  Notice,
  Page,
  Panel,
  PlatformMark,
  signOut,
  StatTile,
  timeAgo,
  useToast,
} from "@/components/layout";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { api, go, useAction, useApi, type Account, type Dashboard } from "@/lib/api";
import { cn } from "@/lib/utils";

const PROVIDER_MARKS: Record<string, string> = { entra: "fabric", databricks: "databricks", snowflake: "snowflake" };

const TABS = [
  { key: "profile", label: "Profile", icon: UserRound },
  { key: "access", label: "Sign-ins & access", icon: KeyRound },
  { key: "instructions", label: "Instructions", icon: MessageSquareText },
  { key: "activity", label: "Activity", icon: History },
] as const;

type Tab = (typeof TABS)[number]["key"];

export function SettingsPage() {
  const account = useApi<Account>("/api/account");
  const board = useApi<Dashboard>("/api/dashboard");
  const toast = useToast();
  const [search, setSearch] = useSearchParams();
  const tab: Tab = TABS.some((t) => t.key === search.get("tab")) ? (search.get("tab") as Tab) : "profile";
  const done = (text: string) => {
    toast(text);
    account.reload();
    board.reload();
  };
  const me = account.data;
  const data = board.data;
  const failed = account.error ?? board.error;

  return (
    <Page icon={<SettingsIcon />} title="Settings" hook="Your IQ Connect account, the sign-ins your connections use, and what Claude has been doing.">
      <div role="tablist" aria-label="Settings" className="mb-6 flex max-w-full gap-1 overflow-x-auto rounded-2xl border bg-card p-1 shadow-soft sm:inline-flex">
        {TABS.map(({ key, label, icon: Icon }) => (
          <button
            key={key}
            role="tab"
            type="button"
            aria-selected={tab === key}
            onClick={() => {
              const next = new URLSearchParams(search);
              next.set("tab", key);
              setSearch(next, { replace: true });
            }}
            className={cn(
              "flex shrink-0 cursor-pointer items-center gap-2 rounded-xl px-4 py-2 text-sm font-semibold transition",
              tab === key ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:text-ink",
            )}
          >
            <Icon className="size-4" />
            {label}
          </button>
        ))}
      </div>
      {failed && <LoadFailure error={failed} />}
      {!failed && (!me || !data) && <Loading cards={2} />}
      {me && data && (
        <div role="tabpanel">
          {tab === "profile" && <ProfileTab me={me} data={data} />}
          {tab === "access" && <AccessTab me={me} data={data} onChanged={done} />}
          {tab === "instructions" && (
            <InstructionsTab key={data.instructions} text={data.instructions} max={data.maxInstructions} onSaved={() => done("Instructions saved. Claude uses them from its next question.")} />
          )}
          {tab === "activity" && <ActivityLog activity={data.activity} />}
        </div>
      )}
    </Page>
  );
}

// --- profile ---------------------------------------------------------------------------------

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-1 rounded-xl bg-muted/70 px-4 py-3">
      <dt className="text-xs font-semibold text-muted-foreground">{label}</dt>
      <dd className="min-w-0 truncate text-sm font-semibold">{children}</dd>
    </div>
  );
}

function CopyId({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    await navigator.clipboard.writeText(value);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1800);
  };
  return (
    <span className="flex min-w-0 items-center gap-1.5">
      <code className="min-w-0 truncate font-mono text-[13px]">{value}</code>
      <button type="button" onClick={copy} aria-label="Copy user ID" className="shrink-0 cursor-pointer rounded-md p-1 text-muted-foreground hover:bg-card hover:text-ink">
        {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
      </button>
    </span>
  );
}

function ProfileTab({ me, data }: { me: Account; data: Dashboard }) {
  return (
    <div className="flex flex-col gap-6">
      <section className="overflow-hidden rounded-2xl border bg-card shadow-soft">
        <div className="h-20 bg-linear-to-r from-lavender via-mint to-blush" />
        <div className="-mt-10 flex flex-wrap items-end gap-5 px-6">
          <Initials name={me.name} email={me.email} className="size-20 border-4 border-card text-2xl" />
          <div className="mr-auto min-w-0 pb-1">
            <h2 className="truncate text-2xl font-extrabold tracking-tight">{me.name || me.email}</h2>
            <p className="truncate text-muted-foreground">{me.email}</p>
          </div>
          {me.isAdmin && (
            <Badge variant="success" className="mb-2">
              <ShieldCheck /> IQ admin
            </Badge>
          )}
        </div>
        <dl className="grid gap-3 p-6 @xl:grid-cols-2 @4xl:grid-cols-3">
          <Fact label="User ID">
            <CopyId value={me.id} />
          </Fact>
          <Fact label="Organization">{me.organization || "—"}</Fact>
          <Fact label="Signs in with">{me.signInMethod}</Fact>
          <Fact label="Member since">{formatDate(me.createdAt)}</Fact>
          <Fact label="Last sign-in">{me.lastSignIn ? timeAgo(me.lastSignIn) : "—"}</Fact>
          <Fact label="Data access">Read-only, always</Fact>
        </dl>
      </section>

      <section>
        <h2 className="mb-3 text-sm font-bold text-ink">Last 7 days</h2>
        <div className="grid grid-cols-2 gap-4 @3xl:grid-cols-4">
          <StatTile icon={<CheckCircle2 />} label="Answered" value={data.stats.answered} tone="mint" />
          <StatTile icon={<ShieldAlert />} label="Refused (not read-only)" value={data.stats.refused} tone={data.stats.refused ? "amber" : "default"} />
          <StatTile icon={<Clock />} label="Average response" value={data.stats.averageSeconds ? `${data.stats.averageSeconds} s` : "—"} />
          <StatTile icon={<Plug />} label="Connections" value={data.connections.length} />
        </div>
      </section>

      <DangerZone email={me.email} />
    </div>
  );
}

function DangerZone({ email }: { email: string }) {
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState("");
  const action = useAction();
  const matches = typed.trim().toLowerCase() === email.toLowerCase();
  const remove = async () => {
    const done = await action.run(() => api.post<{ redirect: string }>("/api/account/delete", { confirm: typed }));
    if (done) go(done.redirect);
  };
  return (
    <section className="grid gap-4 @3xl:grid-cols-2">
      <div className="flex h-full items-center justify-between gap-4 rounded-2xl border bg-card p-5 shadow-soft">
        <div>
          <div className="flex items-center gap-2 text-sm font-bold">
            <Fingerprint className="size-4 text-mint-ink" /> Sign out
          </div>
          <p className="mt-1 text-sm text-muted-foreground">Your connections stay as they are.</p>
        </div>
        <Button variant="outline" onClick={signOut}>
          <LogOut /> Sign out
        </Button>
      </div>
      <div className="flex h-full items-center justify-between gap-4 rounded-2xl border border-destructive/30 bg-card p-5 shadow-soft">
        <div>
          <div className="flex items-center gap-2 text-sm font-bold text-destructive">
            <TriangleAlert className="size-4" /> Delete account
          </div>
          <p className="mt-1 text-sm text-muted-foreground">Removes your profile and every connection.</p>
        </div>
        <Button variant="outline" className="border-destructive/40 text-destructive hover:bg-blush hover:text-destructive" onClick={() => setOpen(true)}>
          <Trash2 /> Delete
        </Button>
      </div>
      <Dialog
        open={open}
        onOpenChange={(next) => {
          setOpen(next);
          setTyped("");
          action.setError(null);
        }}
      >
        <DialogContent>
          <div className="pr-6">
            <DialogTitle className="flex items-center gap-2">
              <TriangleAlert className="size-5 text-destructive" /> Delete your account?
            </DialogTitle>
            <DialogDescription className="mt-2">
              This removes your profile, every connection, your platform sign-ins and your instructions, and Claude loses access straight away. It can't be undone. The activity log is kept
              for auditing.
            </DialogDescription>
          </div>
          <Notice error={action.error} />
          <div className="grid gap-2">
            <Label htmlFor="confirm-email">
              Type <strong>{email}</strong> to confirm
            </Label>
            <Input id="confirm-email" value={typed} autoComplete="off" onChange={(event) => setTyped(event.target.value)} />
          </div>
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button variant="destructive" disabled={!matches || action.busy} onClick={remove}>
              <Trash2 /> Delete my account
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </section>
  );
}

// --- sign-ins and access -------------------------------------------------------------------------

function AccessTab({ me, data, onChanged }: { me: Account; data: Dashboard; onChanged: (text: string) => void }) {
  const action = useAction();
  const unlink = async () => {
    if (!window.confirm("Remove your Microsoft sign-in? Fabric connections will need you to sign in again.")) return;
    if (await action.run(() => api.post("/api/microsoft/unlink"))) onChanged("Microsoft account removed.");
  };
  const disconnect = async (clientId: string) => {
    if (await action.run(() => api.post("/api/apps/disconnect", { clientId }))) onChanged("Disconnected. Claude loses access within the hour.");
  };
  return (
    <div className="flex flex-col gap-6">
      <Notice error={action.error} />
      <Panel title="Platform sign-ins" icon={<KeyRound />}>
        <p className="-mt-2 mb-4 text-sm text-muted-foreground">
          The accounts Claude's queries run as. They can differ from the account you use for IQ Connect (<strong className="text-ink">{me.email}</strong>).
        </p>
        {me.signIns.length === 0 ? (
          <p className="rounded-xl border border-dashed px-4 py-6 text-center text-sm text-muted-foreground">No platform sign-ins yet. They are added when you connect data.</p>
        ) : (
          <div className="grid gap-3 @3xl:grid-cols-2">
            {me.signIns.map((login) => (
              <div key={`${login.provider}-${login.account}`} className="flex h-full items-center gap-3 rounded-2xl border p-4">
                <PlatformMark platform={PROVIDER_MARKS[login.provider] ?? "demo"} className="size-11 p-2.5" />
                <div className="min-w-0 flex-1">
                  <div className="text-xs font-semibold text-muted-foreground">{login.label}</div>
                  <div className="truncate text-sm font-bold">{login.account || "Account name not available"}</div>
                  <div className="text-xs text-muted-foreground">
                    Used by {login.connections} connection{login.connections === 1 ? "" : "s"} · since {formatDate(login.since)}
                  </div>
                </div>
                {login.provider === "entra" && (
                  <Button size="sm" variant="ghost" disabled={action.busy} onClick={unlink}>
                    Remove
                  </Button>
                )}
              </div>
            ))}
          </div>
        )}
      </Panel>

      <div className={cn("grid items-start gap-6", me.isAdmin && "@3xl:grid-cols-2")}>
        <Panel title="Claude's access" icon={<Plug />}>
          <div className="flex flex-col gap-3">
            {data.apps.length === 0 && <p className="text-sm text-muted-foreground">No app is connected yet. Add the connector below in Claude.</p>}
            {data.apps.map((app) => (
              <div key={app.clientId} className="flex items-center justify-between gap-3 rounded-xl border px-4 py-3">
                <div>
                  <div className="font-semibold">{app.name}</div>
                  {app.since && <div className="text-xs text-muted-foreground">connected {timeAgo(app.since)}</div>}
                </div>
                <Button size="sm" variant="ghost" disabled={action.busy} onClick={() => disconnect(app.clientId)}>
                  Disconnect
                </Button>
              </div>
            ))}
            <ConnectorAddress />
          </div>
        </Panel>
        {me.isAdmin && (
          <Panel title="Server setup" icon={<Wrench />}>
            <p className="mb-3 text-sm text-muted-foreground">You're an IQ admin: you can change how this server signs people in.</p>
            <div className="flex flex-col gap-2">
              <Button asChild variant="outline" className="justify-start">
                <Link to="/app/setup/microsoft?next=/app/settings?tab=access">
                  <PlatformMark platform="fabric" className="size-5 bg-transparent p-0" /> Microsoft app
                </Link>
              </Button>
            </div>
          </Panel>
        )}
      </div>
    </div>
  );
}

// --- instructions ------------------------------------------------------------------------------

const INCLUDE = [
  ["Layers and schemas", "raw, prep, curated: what each holds"],
  ["Keys and joins", "Which columns join which tables"],
  ["Metric definitions", "Revenue, margin, active customer…"],
  ["Rules of thumb", "Fiscal year, currency, known gaps"],
];

function InstructionsTab({ text, max, onSaved }: { text: string; max: number; onSaved: () => void }) {
  const [value, setValue] = useState(text);
  const action = useAction();
  return (
    <div className="grid items-start gap-6 @4xl:grid-cols-[minmax(0,1fr)_18rem]">
      <Panel title="Instructions for Claude" icon={<MessageSquareText />}>
        <p className="-mt-2 mb-4 text-sm text-muted-foreground">Sent with every answer, for all your connections.</p>
        <Notice error={action.error} />
        <Textarea
          value={value}
          maxLength={max}
          onChange={(event) => setValue(event.target.value)}
          placeholder="For example: amounts are in GBP; the fiscal year starts in April; use curated.* tables for reporting."
          className="min-h-72"
          aria-label="Instructions for Claude"
        />
        <div className="mt-3 flex items-center justify-end gap-3">
          <span className="text-xs text-muted-foreground">
            {value.length.toLocaleString()} / {max.toLocaleString()}
          </span>
          <Button variant="ghost" disabled={action.busy || value === text} onClick={() => setValue(text)}>
            Undo changes
          </Button>
          <Button disabled={action.busy || value === text} onClick={async () => (await action.run(() => api.put("/api/instructions", { text: value }))) && onSaved()}>
            Save
          </Button>
        </div>
      </Panel>
      <Panel title="What to include" icon={<BookOpenText />}>
        <ul className="grid gap-2.5">
          {INCLUDE.map(([title, line]) => (
            <li key={title} className="rounded-xl bg-muted/70 px-3.5 py-2.5">
              <div className="text-sm font-semibold">{title}</div>
              <div className="text-xs text-muted-foreground">{line}</div>
            </li>
          ))}
        </ul>
      </Panel>
    </div>
  );
}

// --- activity ----------------------------------------------------------------------------------

function ActivityLog({ activity }: { activity: Dashboard["activity"] }) {
  const tone = { ok: "success", rejected: "warning", error: "danger" } as const;
  if (activity.length === 0) {
    return (
      <EmptyState icon={<History />} title="Nothing yet">
        Ask Claude a question: every call it makes through IQ Connect appears here.
      </EmptyState>
    );
  }
  return (
    <Panel title="Recent activity" icon={<History />}>
      <p className="-mt-2 mb-4 text-sm text-muted-foreground">The last 30 calls, newest first. Hover a row for its full statement.</p>
      <div className="-mx-5 overflow-x-auto">
        <table className="w-full min-w-[44rem] text-left text-sm">
          <thead className="bg-muted/60 text-xs text-muted-foreground">
            <tr>
              {["When", "Tool", "Connection", "Result", "Rows", "Time", "Statement"].map((h) => (
                <th key={h} className="px-5 py-2.5 font-semibold">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {activity.map((row, index) => (
              <tr key={index} className="border-t align-middle transition hover:bg-muted/40">
                <td className="px-5 py-3 whitespace-nowrap text-muted-foreground">{timeAgo(row.at)}</td>
                <td className="px-5 py-3 font-semibold whitespace-nowrap">{row.tool}</td>
                <td className="px-5 py-3">{row.connection || "—"}</td>
                <td className="px-5 py-3">
                  <Badge variant={tone[row.outcome as keyof typeof tone] ?? "default"}>{row.outcome}</Badge>
                </td>
                <td className="px-5 py-3 tabular-nums">{row.rows ?? "—"}</td>
                <td className="px-5 py-3 whitespace-nowrap tabular-nums">{row.ms != null ? `${row.ms} ms` : "—"}</td>
                <td className="max-w-[24rem] truncate px-5 py-3 font-mono text-xs text-muted-foreground" title={row.statement || row.error}>
                  {row.statement || row.error || "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}
