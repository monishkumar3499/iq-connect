/**
 * Add a new connection, step 2: how to sign in to the platform.
 *
 * Each platform offers its methods (Microsoft single sign-on first, recommended); a method
 * may ask for the person's own details -- their Databricks workspace URL, their Snowflake
 * account, or an app's client ID and secret. Secrets are sent once, stored encrypted on the
 * server and never shown again. The server answers with where to go next: the platform's
 * own sign-in page, or straight to the workspace.
 */
import { ArrowRight, KeyRound, Lightbulb, ShieldCheck } from "lucide-react";
import { useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";

import { LoadFailure, Loading, Notice, Page, Panel, PlatformMark, SetupSteps, type Back } from "@/components/layout";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { api, go, useAction, useApi, useFlow, withFlow, type PlatformList, type SignInOptions } from "@/lib/api";

function Field({ id, label, hint, ...props }: { id: string; label: string; hint?: string } & React.ComponentProps<typeof Input>) {
  return (
    <div className="grid gap-2">
      <Label htmlFor={id}>{label}</Label>
      <Input id={id} autoComplete="off" spellCheck={false} {...props} />
      {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
    </div>
  );
}

export function SignInStep() {
  const { platform = "" } = useParams();
  const flow = useFlow();
  const [search] = useSearchParams();
  const next = search.get("next") ?? "";
  const found = useApi<SignInOptions>(`/api/setup/${platform}/sign-in`, { flow });
  const platforms = useApi<PlatformList>("/api/setup/platforms");
  const [method, setMethod] = useState("");
  const [values, setValues] = useState<Record<string, string>>({});
  const action = useAction();
  const data = found.data;
  const chosen = data?.methods.find((m) => m.key === (method || data.methods.find((x) => x.available)?.key));
  const back: Back | null = platforms.data?.warehousePlatform
    ? flow
      ? { to: withFlow("/connect", flow), label: "Back to Claude's request" }
      : null
    : { to: withFlow("/app/setup", flow), label: "Back to platforms" };

  const value = (name: string, fallback: string) => values[`${chosen?.key}.${name}`] ?? fallback;
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!chosen) return;
    const filled = Object.fromEntries(chosen.fields.map((f) => [f.name, value(f.name, f.value)]));
    const done = await action.run(() => api.post<{ redirect: string }>(`/api/setup/${platform}/sign-in`, { method: chosen.key, values: filled, flow, next }));
    if (done) go(done.redirect);
  };

  return (
    <Page
      back={back}
      icon={<KeyRound />}
      eyebrow={`Add a new connection · ${data?.platform.label ?? ""}`}
      title={`Sign in to ${data?.platform.label ?? "the platform"}`}
      hook="Choose how IQ Connect signs in for you. Claude only ever reads, and only what this sign-in can see."
      step="workspace"
      aside={
        <>
          <SetupSteps current="workspace" />
          <Panel title="Good to know" icon={<Lightbulb />}>
            <ul className="flex flex-col gap-3 text-sm text-muted-foreground">
              <li>Single sign-on uses your company Microsoft account: nothing new to remember.</li>
              <li>Client secrets are stored encrypted and never shown again.</li>
              <li>No passwords or personal access tokens are ever stored.</li>
            </ul>
          </Panel>
        </>
      }
    >
      {found.error && <LoadFailure error={found.error} />}
      {found.loading && <Loading cards={2} />}
      {data && data.accounts.length > 0 && (
        <div className="mb-6 grid gap-3">
          <h2 className="text-sm font-semibold text-muted-foreground">Already signed in</h2>
          {data.accounts.map((account) => (
            <Link
              key={account.id}
              to={account.next}
              className="group flex min-w-0 items-center gap-4 rounded-2xl border bg-card p-4 shadow-soft transition hover:border-ink/25 hover:shadow-lift"
            >
              <PlatformMark platform={platform} />
              <div className="min-w-0 flex-1">
                <div className="truncate font-bold">{account.address || data.platform.label}</div>
                <div className="truncate text-sm text-muted-foreground">
                  {account.methodLabel}
                  {account.username && ` · ${account.username}`}
                </div>
              </div>
              <span className="grid size-9 shrink-0 place-items-center rounded-full bg-lavender text-ink transition group-hover:bg-primary group-hover:text-primary-foreground">
                <ArrowRight className="size-4" />
              </span>
            </Link>
          ))}
          <h2 className="mt-4 text-sm font-semibold text-muted-foreground">Or sign in again</h2>
        </div>
      )}
      {data && (
        <Card className="max-w-3xl">
          <Notice error={action.error} />
          <form onSubmit={submit} className="grid gap-5">
            <RadioGroup value={chosen?.key ?? ""} onValueChange={setMethod}>
              {data.methods.map((m) => (
                <label
                  key={m.key}
                  aria-disabled={!m.available}
                  className="flex cursor-pointer items-start gap-3 rounded-xl border p-4 has-[[data-state=checked]]:border-primary aria-disabled:cursor-not-allowed aria-disabled:opacity-60"
                >
                  <RadioGroupItem value={m.key} disabled={!m.available} className="mt-0.5" />
                  <span className="min-w-0">
                    <span className="flex flex-wrap items-center gap-2 font-semibold">
                      {m.label}
                      {m.recommended && (
                        <Badge variant="success">
                          <ShieldCheck /> Recommended
                        </Badge>
                      )}
                    </span>
                    <span className="block text-sm text-muted-foreground">{m.available ? m.description : m.whyNot}</span>
                  </span>
                </label>
              ))}
            </RadioGroup>
            {chosen?.fields.map((f) => (
              <Field
                key={`${chosen.key}.${f.name}`}
                id={`${chosen.key}-${f.name}`}
                label={f.label}
                required={f.required}
                type={f.secret ? "password" : "text"}
                autoComplete={f.secret ? "new-password" : "off"}
                placeholder={f.placeholder}
                hint={f.hint}
                value={value(f.name, f.value)}
                onChange={(event) => setValues({ ...values, [`${chosen.key}.${f.name}`]: event.target.value })}
              />
            ))}
            {chosen?.redirectUri && (
              <p className="text-xs text-muted-foreground">
                Redirect URL to register on that app: <code className="rounded bg-muted px-1.5 py-0.5">{chosen.redirectUri}</code>
              </p>
            )}
            <Button size="lg" type="submit" disabled={action.busy || !chosen?.available}>
              Continue <ArrowRight />
            </Button>
          </form>
        </Card>
      )}
    </Page>
  );
}
