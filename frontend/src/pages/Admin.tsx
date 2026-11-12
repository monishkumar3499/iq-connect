/**
 * One-time server setup: the Microsoft (Entra) app registration. Its secret goes in, is
 * stored encrypted, and never comes back out. (Databricks workspaces and Snowflake accounts
 * are each person's own: entered in the setup's Sign in step.)
 */
import { ArrowRight, CheckCircle2 } from "lucide-react";
import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { LoadFailure, Loading, Notice, Page, PlatformMark, useSession } from "@/components/layout";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, useAction, useApi } from "@/lib/api";

/** Where to continue: the `next` the wizard passed (this site only), else the wizard. */
function useNext() {
  const [search] = useSearchParams();
  const next = search.get("next") ?? "";
  return next.startsWith("/") && !next.startsWith("//") && !next.startsWith("/\\") ? next : "/app/setup";
}

/** Back to where the person came from; before anyone can sign in, the entry page. */
function useBack(next: string) {
  const session = useSession();
  return session.signedIn ? { to: next, label: "Back" } : { to: "/setup", label: "Back to sign-in" };
}

function Field({ id, label, hint, ...props }: { id: string; label: string; hint?: string } & React.ComponentProps<typeof Input>) {
  return (
    <div className="grid gap-2">
      <Label htmlFor={id}>{label}</Label>
      <Input id={id} autoComplete="off" spellCheck={false} {...props} />
      {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
    </div>
  );
}

function SetupCode({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  return (
    <Field
      id="code"
      label="Setup code"
      placeholder="XXXX-XXXX-XXXX"
      value={value}
      onChange={(event) => onChange(event.target.value)}
      hint="Shown in the server window, or run: python -m backend.main microsoft-setup-code"
    />
  );
}

function Connected({ chips, onContinue, inSettings }: { chips: string[]; onContinue: () => void; inSettings: boolean }) {
  return (
    <Card className="mb-6 max-w-3xl">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="success">
          <CheckCircle2 /> Connected
        </Badge>
        {chips.map((chip) => (
          <Badge key={chip} variant="outline">
            {chip}
          </Badge>
        ))}
      </div>
      {inSettings && <p className="text-sm text-muted-foreground">Set in the server's .env, so it can only be changed there.</p>}
      <Button className="self-start" onClick={onContinue}>
        Continue <ArrowRight />
      </Button>
    </Card>
  );
}

// --- Microsoft ---------------------------------------------------------------------------------

interface MicrosoftInfo {
  status: { clientId: string; tenant: string; source: string } | null;
  needsCode: boolean;
  redirectUris: string[];
}

export function MicrosoftSetup() {
  const next = useNext();
  const back = useBack(next);
  const navigate = useNavigate();
  const found = useApi<MicrosoftInfo>("/api/admin/microsoft");
  const [form, setForm] = useState({ clientId: "", clientSecret: "", tenant: "", setupCode: "" });
  const action = useAction();
  const data = found.data;
  const set = (key: keyof typeof form) => (event: React.ChangeEvent<HTMLInputElement>) => setForm({ ...form, [key]: event.target.value });
  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (await action.run(() => api.post("/api/admin/microsoft", form))) window.location.assign(next);
  };

  return (
    <Page back={back} eyebrow="Server setup" title="Connect Microsoft" hook="One-time setup for Microsoft sign-in, Fabric and Azure Databricks.">
      {found.error && <LoadFailure error={found.error} />}
      {found.loading && <Loading />}
      {data?.status && (
        <Connected
          chips={[`App ${data.status.clientId}`, `Tenant ${data.status.tenant || "any company"}`]}
          inSettings={data.status.source === "settings"}
          onContinue={() => navigate(next)}
        />
      )}
      {data && data.status?.source !== "settings" && (
        <Card className="max-w-3xl">
          <div className="flex items-center gap-3">
            <PlatformMark platform="fabric" />
            <h2 className="text-lg font-bold">{data.status ? "Replace the app registration" : "Your Entra app"}</h2>
          </div>
          <Notice error={action.error} />
          <form onSubmit={save} className="grid gap-5">
            <Field id="client_id" label="Application (client) ID" required value={form.clientId} onChange={set("clientId")} placeholder="11111111-2222-3333-4444-555555555555" />
            <Field id="secret" label="Client secret value" type="password" required autoComplete="new-password" value={form.clientSecret} onChange={set("clientSecret")} />
            <Field id="tenant" label="Directory (tenant) ID — optional" value={form.tenant} onChange={set("tenant")} hint="Only for a single-tenant app. Empty: people from any company can sign in (the app must be multi-tenant)." />
            {data.needsCode && <SetupCode value={form.setupCode} onChange={(setupCode) => setForm({ ...form, setupCode })} />}
            <Button size="lg" type="submit" disabled={action.busy}>
              Save and continue
            </Button>
          </form>
          <details className="text-sm text-muted-foreground">
            <summary className="cursor-pointer font-semibold text-ink">Where do I find these?</summary>
            <ol className="mt-3 list-decimal space-y-1.5 pl-5">
              <li>Azure portal → Microsoft Entra ID → App registrations → your app (any organizational directory).</li>
              <li>Overview: Application (client) ID. Certificates &amp; secrets: a new client secret's Value.</li>
              <li>
                Authentication → Web redirect URIs:
                {data.redirectUris.map((uri) => (
                  <code key={uri} className="mt-1 block rounded-md bg-muted px-2 py-1 text-xs">
                    {uri}
                  </code>
                ))}
              </li>
              <li>API permissions (delegated, admin consent): Power BI Service, Azure SQL Database user_impersonation, AzureDatabricks user_impersonation.</li>
            </ol>
          </details>
        </Card>
      )}
    </Page>
  );
}
