/**
 * The ways in: /setup (and /app/setup/welcome), Claude's Connect page (/connect),
 * the error page the sign-in redirects use, and the development sign-in form.
 */
import { ArrowRight, CirclePlus, LogOut, ShieldCheck } from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import {
  ConnectorAddress,
  Initials,
  Loading,
  MicrosoftLogo,
  Notice,
  Page,
  Panel,
  PlatformMark,
  SignedInAs,
  signInUrl,
  signOut,
  SplitPage,
  useSession,
} from "@/components/layout";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, go, useAction, useApi, useFlow, withFlow, type Dashboard } from "@/lib/api";

function greeting() {
  const hour = new Date().getHours();
  return hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
}

const PLATFORM_CHIPS = [
  { key: "fabric", label: "Fabric", tone: "bg-mint text-mint-ink ring-[#9be3d5]" },
  { key: "databricks", label: "Databricks", tone: "bg-blush text-blush-ink ring-[#f6bfd2]" },
  { key: "snowflake", label: "Snowflake", tone: "bg-lavender text-ink ring-[#d9d4f5]" },
];

/** Signed out: sign in with Microsoft, then the app (or Claude's request) continues. */
function Welcome() {
  const session = useSession();
  const flow = useFlow();
  const next = flow ? withFlow("/connect", flow) : "/app?welcome=1";
  return (
    <div className="flex flex-col gap-8 text-center">
      <div>
        <p className="text-base text-muted-foreground">{session.signedIn ? `Welcome back${session.name ? `, ${session.name.split(" ")[0]}` : ""}` : greeting()}</p>
        <h1 className="mt-2 text-[1.85rem] font-extrabold tracking-tight text-ink sm:text-[2.25rem]">
          Welcome to IQ Connect
        </h1>
        <p className="mt-3 text-sm text-muted-foreground">Securely connect Claude to your company data.</p>
      </div>
      <div className="flex flex-wrap justify-center gap-2.5">
        {PLATFORM_CHIPS.map((chip) => (
          <span key={chip.key} className={`flex items-center gap-2 rounded-2xl px-3 py-1.5 text-[13px] font-semibold ring-1 ${chip.tone}`}>
            <PlatformMark platform={chip.key} className="size-5 bg-transparent p-0.5" />
            {chip.label}
          </span>
        ))}
      </div>
      {session.signedIn ? (
        <div className="flex flex-col gap-3">
          <Button asChild size="lg">
            <Link to={next}>
              <Initials name={session.name} email={session.email} className="size-7 bg-primary-foreground text-primary" />
              Continue as {session.email} <ArrowRight />
            </Link>
          </Button>
          <Button variant="ghost" onClick={signOut}>
            <LogOut /> Use a different account
          </Button>
        </div>
      ) : (
        <div className="flex flex-col gap-3">
          <Button asChild size="lg" variant="outline" className="border-ink/15 bg-card text-ink shadow-button hover:bg-card hover:shadow-lift">
            <a href={signInUrl(next)}>
              <MicrosoftLogo /> Sign in with Microsoft
            </a>
          </Button>
          <p className="text-[13px] text-muted-foreground">
            Use your company account. Your IQ Connect profile is created the first time and remembered after that.
          </p>
        </div>
      )}
      <ConnectorAddress />
    </div>
  );
}

export function Landing() {
  return (
    <SplitPage>
      <Welcome />
    </SplitPage>
  );
}

// --- Claude's Connect page -------------------------------------------------------------

interface ConnectInfo {
  app: string;
  email: string;
  connections: { name: string; platform: string }[];
}

export function Connect() {
  const session = useSession();
  const flow = useFlow();
  const info = useApi<ConnectInfo>(session.signedIn ? "/api/connect" : null, { flow });
  // For the account each connection runs as (the Connect API lists names only).
  const mine = useApi<Dashboard>(session.signedIn ? "/api/dashboard" : null);
  const action = useAction();

  const decide = async (decision: "approve" | "deny") => {
    const done = await action.run(() => api.post<{ redirect: string }>(`/api/connect/${decision}`, { flow }));
    if (done) go(done.redirect);
  };

  if (!session.signedIn) {
    return (
      <SplitPage>
        <Welcome />
      </SplitPage>
    );
  }
  const found = info.data;
  const accounts = Object.fromEntries((mine.data?.connections ?? []).map((c) => [c.name, c.account]));
  return (
    <Page
      icon={<ShieldCheck />}
      eyebrow="Connect to Claude"
      title={found ? `Allow ${found.app} to read your data?` : "Connect to Claude"}
      hook="Read-only. Claude runs every query as you, inside the connections listed here."
    >
      {info.error && <Notice error={info.error} />}
      {info.loading && !info.error && <Loading />}
      <Notice error={action.error} />
      {found && (
        <div className="grid items-start gap-6 @4xl:grid-cols-[minmax(0,1fr)_22rem]">
          <Panel title={found.connections.length ? `${found.connections.length} connection${found.connections.length === 1 ? "" : "s"} Claude can use` : "Nothing connected yet"}>
            {found.connections.length === 0 ? (
              <div className="flex flex-col items-start gap-4">
                <p className="text-sm text-muted-foreground">
                  Add a data warehouse (and, if you like, a semantic model) so {found.app} has something to query.
                </p>
                <Button asChild size="lg">
                  <Link to={withFlow("/app/setup", flow)}>
                    <CirclePlus /> Add a new connection
                  </Link>
                </Button>
              </div>
            ) : (
              <ul className="grid gap-3 @2xl:grid-cols-2">
                {found.connections.map((c) => (
                  <li key={c.name} className="flex h-full items-center gap-3 rounded-xl border bg-card px-4 py-3">
                    <PlatformMark platform={c.platform} className="size-9 p-2" />
                    <div className="min-w-0">
                      <div className="truncate font-semibold">{c.name}</div>
                      <SignedInAs account={accounts[c.name] ?? ""} />
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </Panel>
          <Panel>
            <div className="flex flex-col gap-5">
              <ShieldCheck className="size-12 rounded-2xl bg-mint p-2.5 text-mint-ink" />
              <div>
                <h2 className="text-lg font-bold">{found.app} will act as you</h2>
                <p className="mt-1 text-sm text-muted-foreground">
                  Signed in to IQ Connect as <strong className="text-ink">{found.email}</strong>. Nothing is ever written to your data.
                </p>
              </div>
              <div className="flex flex-col gap-2">
                <Button size="lg" disabled={action.busy} onClick={() => decide("approve")}>
                  {found.connections.length ? `Allow and return to ${found.app}` : "Allow without data for now"}
                </Button>
                <Button variant="ghost" disabled={action.busy} onClick={() => decide("deny")}>
                  Cancel
                </Button>
              </div>
              {found.connections.length > 0 && (
                <Link to={withFlow("/app/setup", flow)} className="text-sm font-semibold text-ink hover:underline">
                  + Add more data first
                </Link>
              )}
            </div>
          </Panel>
        </div>
      )}
    </Page>
  );
}

// --- errors and the development sign-in --------------------------------------------------

export function ErrorPage() {
  const [message, setMessage] = useState<{ title?: string; text?: string } | null>(null);
  useEffect(() => {
    api.get<{ title?: string; text?: string }>("/api/flash").then(setMessage, () => setMessage({}));
  }, []);
  return (
    <SplitPage>
      {message && (
        <div className="flex flex-col gap-5 text-center">
          <h1 className="text-[1.65rem] font-extrabold tracking-tight text-ink">{message.title ?? "Something went wrong"}</h1>
          <p className="text-muted-foreground">{message.text ?? "Go back to Claude and press Connect again."}</p>
          <Button asChild variant="outline">
            <Link to="/setup">Start again</Link>
          </Button>
        </div>
      )}
    </SplitPage>
  );
}

/** APP_ENV=dev only: the server answers 404 elsewhere, so this never shows in production. */
export function DevSignIn() {
  const nonce = new URLSearchParams(window.location.search).get("nonce") ?? "";
  return (
    <SplitPage>
      <form method="post" action="/auth/dev-login" className="flex flex-col gap-5">
        <div className="text-center">
          <h1 className="text-[1.65rem] font-extrabold tracking-tight text-ink">Development sign-in</h1>
          <p className="mt-2 text-sm text-muted-foreground">Stands in for Microsoft sign-in while APP_ENV=dev.</p>
        </div>
        <input type="hidden" name="nonce" value={nonce} />
        <div className="grid gap-2">
          <Label htmlFor="email">Email</Label>
          <Input id="email" name="email" type="email" required defaultValue="alice@example.com" />
        </div>
        <div className="grid gap-2">
          <Label htmlFor="name">Name</Label>
          <Input id="name" name="name" defaultValue="Alice" />
        </div>
        <Button size="lg" type="submit">
          Sign in
        </Button>
      </form>
    </SplitPage>
  );
}
