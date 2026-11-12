/**
 * The app's frame: who is signed in, the brand, the two looks (the split entry page,
 * and the signed-in app with its sidebar), and the small pieces every screen shares.
 *
 * Width: every page fills the space from the sidebar to the right edge, on every
 * screen, and card grids count columns from that space (container queries), so cards
 * stay evenly spread and aligned whatever the monitor.
 */
import {
  AlertCircle,
  ArrowLeft,
  ArrowRight,
  Check,
  CheckCircle2,
  CirclePlus,
  Copy,
  Database,
  Layers,
  Loader2,
  LogOut,
  Menu,
  Settings,
  Snowflake,
  Sparkles,
  UserRound,
  X,
} from "lucide-react";
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { Link, Navigate, Outlet, useLocation } from "react-router-dom";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { api, ApiError, go, setCsrf, useFlow, withFlow, type Session, type Status } from "@/lib/api";
import { cn } from "@/lib/utils";

// --- session ---------------------------------------------------------------------------

const SessionContext = createContext<Session | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const flow = useFlow();
  const [session, setSession] = useState<Session | null>(null);
  useEffect(() => {
    api.get<Session>("/api/session", { flow }).then((found) => {
      setCsrf(found.csrf);
      setSession(found);
    });
  }, [flow]);
  if (!session) return <FullPageLoader />;
  return (
    <SessionContext.Provider value={session}>
      <ToastProvider>{children}</ToastProvider>
    </SessionContext.Provider>
  );
}

export function useSession(): Session {
  const session = useContext(SessionContext);
  if (!session) throw new Error("useSession outside SessionProvider");
  return session;
}

export async function signOut() {
  const done = await api.post<{ redirect: string }>("/api/logout");
  go(done.redirect);
}

/** Where Microsoft sign-in starts, coming back to `next` (a page of this site). */
export function signInUrl(next: string) {
  return `/auth/login?${new URLSearchParams({ next })}`;
}

// --- toasts: short confirmations that don't push the page around -------------------------

interface Toast {
  id: number;
  text: string;
  tone: "success" | "danger";
}

const ToastContext = createContext<(text: string, tone?: Toast["tone"]) => void>(() => {});

function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const show = useCallback((text: string, tone: Toast["tone"] = "success") => {
    const id = Date.now() + Math.random();
    setToasts((now) => [...now, { id, text, tone }]);
    window.setTimeout(() => setToasts((now) => now.filter((t) => t.id !== id)), 4500);
  }, []);
  return (
    <ToastContext.Provider value={show}>
      {children}
      <div aria-live="polite" className="pointer-events-none fixed right-4 bottom-4 z-[60] flex w-[min(24rem,calc(100vw-2rem))] flex-col gap-2">
        {toasts.map((toast) => (
          <div
            key={toast.id}
            role="status"
            className={cn(
              "toast-in pointer-events-auto flex items-start gap-3 rounded-2xl border bg-card px-4 py-3 text-sm font-medium shadow-float",
              toast.tone === "danger" && "border-blush-ink/30",
            )}
          >
            {toast.tone === "success" ? <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-mint-ink" /> : <AlertCircle className="mt-0.5 size-4 shrink-0 text-blush-ink" />}
            <span className="flex-1">{toast.text}</span>
            <button type="button" aria-label="Dismiss" className="cursor-pointer text-muted-foreground hover:text-ink" onClick={() => setToasts((now) => now.filter((t) => t.id !== toast.id))}>
              <X className="size-4" />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast() {
  return useContext(ToastContext);
}

// --- brand -----------------------------------------------------------------------------

export function Logo({ className, small = false, to = "/setup", onNav = false }: { className?: string; small?: boolean; to?: string; onNav?: boolean }) {
  return (
    <Link to={to} className={cn("flex items-baseline gap-3", onNav ? "text-nav-foreground" : "text-ink", className)}>
      <span className={cn("leading-none font-extrabold tracking-tight whitespace-nowrap", small ? "text-[1.45rem]" : "text-[1.65rem]")}>
        IQ<span className="text-teal">·</span>Connect
      </span>
      <span
        className={cn(
          "border-l pl-3 font-medium whitespace-nowrap",
          onNav ? "border-nav-foreground/25 text-nav-foreground/80" : "border-ink/20 text-ink/80",
          small ? "text-xs" : "text-sm",
        )}
      >
        by JMAN Group
      </span>
    </Link>
  );
}

export function MicrosoftLogo({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 21 21" className={cn("size-5", className)} aria-hidden>
      <path fill="#f25022" d="M1 1h9v9H1z" />
      <path fill="#7fba00" d="M11 1h9v9h-9z" />
      <path fill="#00a4ef" d="M1 11h9v9H1z" />
      <path fill="#ffb900" d="M11 11h9v9h-9z" />
    </svg>
  );
}

const MARKS: Record<string, { tint: string; node: ReactNode }> = {
  fabric: {
    tint: "bg-mint",
    node: (
      <svg viewBox="0 0 32 32" className="size-full" aria-hidden>
        <defs>
          <linearGradient id="fab" x1="0" x2="1" y1="0" y2="1">
            <stop offset="0" stopColor="#1fb6a0" />
            <stop offset="1" stopColor="#0b6e5f" />
          </linearGradient>
        </defs>
        <path d="M6 7c0-1.7 1.3-3 3-3h16l-4 8H10l-4 8z" fill="url(#fab)" />
        <path d="M10 12h11l-4 8h-7z" fill="#57d3bd" />
        <path d="M6 20l4-8v13c0 1.7-1.3 3-3 3H6z" fill="#0b6e5f" />
      </svg>
    ),
  },
  databricks: {
    tint: "bg-blush",
    node: (
      <svg viewBox="0 0 32 32" className="size-full" aria-hidden>
        <path d="M16 4l12 6.5-12 6.5-12-6.5z" fill="#ff3621" />
        <path d="M4 15.5l12 6.5 12-6.5v3.5l-12 6.5-12-6.5z" fill="#ff3621" opacity=".85" />
        <path d="M4 21.5l12 6.5 12-6.5" stroke="#ff3621" strokeWidth="2.4" fill="none" opacity=".7" />
      </svg>
    ),
  },
  snowflake: { tint: "bg-[#e8f4fd]", node: <Snowflake className="size-full text-[#29b5e8]" strokeWidth={2.2} /> },
  demo: { tint: "bg-lavender", node: <Database className="size-full text-ink" strokeWidth={2} /> },
};

export const PLATFORM_LABELS: Record<string, string> = {
  fabric: "Microsoft Fabric",
  databricks: "Databricks",
  snowflake: "Snowflake",
  demo: "Demo data",
};

export function PlatformMark({ platform, className }: { platform: string; className?: string }) {
  const mark = MARKS[platform] ?? MARKS.demo;
  return (
    <span className={cn("grid size-11 shrink-0 place-items-center rounded-xl p-2.5", mark.tint, className)}>
      {mark.node}
    </span>
  );
}

export function Initials({ name, email, className }: { name?: string; email?: string; className?: string }) {
  const source = (name || email || "?").trim();
  const letters = source.includes(" ") ? source.split(/\s+/).slice(0, 2).map((part) => part[0]) : [source[0]];
  return (
    <span className={cn("grid size-9 shrink-0 place-items-center rounded-full bg-primary text-xs font-bold text-primary-foreground uppercase", className)}>
      {letters.join("")}
    </span>
  );
}

// --- the floating card collage ---------------------------------------------------------

const CARDS = [
  { tag: "Warehouse", title: "SalesWH", line: "dbo · sales · finance", chip: "Connected", tone: "success" },
  { tag: "Ask Claude", title: "Revenue by region, last quarter?", line: "Fabric · 312 ms" },
  { tag: "Semantic model", title: "Sales Model", line: "Net revenue · Margin %", chip: "DAX", tone: "default" },
  { tag: "Databricks", title: "Shared SQL · main", line: "Unity Catalog", chip: "Running", tone: "success" },
  { tag: "Policy", title: "Read-only, always", line: "Writes are refused" },
  { tag: "Audit", title: "Every query recorded", line: "Who · what · when", chip: "On", tone: "success" },
  { tag: "Ask Claude", title: "Top 10 customers by margin?", line: "Databricks · 1.2 s" },
  { tag: "Instructions", title: "Revenue = net of returns", line: "Applied to every answer" },
  { tag: "Single sign-on", title: "Microsoft Entra", line: "Your own permissions", chip: "SSO", tone: "default" },
] as const;

export function Collage({ className }: { className?: string }) {
  return (
    <div aria-hidden className={cn("collage pointer-events-none relative overflow-hidden select-none", className)}>
      <div className="collage-grid absolute -inset-24 grid grid-cols-3 gap-6 p-10">
        {[...CARDS, ...CARDS].map((card, index) => (
          <div
            key={index}
            className={cn("float-card rounded-2xl border border-card/80 bg-card/75 p-5 shadow-soft backdrop-blur", index % 4 === 3 && "opacity-60 blur-[1.5px]")}
          >
            <div className="mb-2 text-xs font-semibold text-ink/50">{card.tag}</div>
            <div className="text-[1.05rem] leading-snug font-bold text-ink/85">{card.title}</div>
            <div className="mt-1 text-sm text-ink/55">{card.line}</div>
            {"chip" in card && (
              <Badge variant={card.tone} className="mt-3">
                {card.chip}
              </Badge>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

// --- the entry look ----------------------------------------------------------------------

/** The landing look: the collage on the left, one panel on the right. */
export function SplitPage({ children }: { children: ReactNode }) {
  return (
    <div className="grid min-h-screen lg:grid-cols-[1.25fr_1fr]">
      <div className="relative hidden lg:block">
        <Collage className="absolute inset-0" />
        <Logo small className="absolute top-10 left-12 z-10" />
      </div>
      <main className="relative flex min-h-screen flex-col bg-page px-6 py-8 sm:px-12 lg:rounded-l-[2rem] lg:bg-card lg:shadow-[-30px_0_60px_-40px_rgb(var(--shadow)/0.25)]">
        <Logo small className="mb-10 lg:hidden" />
        <div className="mx-auto flex w-full max-w-md flex-1 flex-col justify-center">{children}</div>
        <footer className="mx-auto w-full max-w-md border-t pt-5 text-center text-[11px] text-muted-foreground">
          © {new Date().getFullYear()} JMAN Group · Read-only access to the data you choose
        </footer>
      </main>
    </div>
  );
}

// --- the signed-in app: sidebar + pages ----------------------------------------------------

// The one-time server setup pages: reachable before anyone can sign in (Microsoft
// sign-in itself needs the app registration entered there).
const SERVER_SETUP = ["/app/setup/microsoft"];

/** The frame of every signed-in screen (a layout route: the sidebar stays put). */
export function AppShell() {
  const session = useSession();
  const flow = useFlow();
  const { pathname } = useLocation();
  // The mobile menu belongs to the page it was opened on, so moving on closes it.
  const [menuOn, setMenuOn] = useState<string | null>(null);
  const serverSetup = SERVER_SETUP.includes(pathname);

  if (!session.signedIn && !serverSetup) {
    // Claude's Connect page shows its own welcome; everything else starts there.
    if (pathname === "/connect") return <Outlet />;
    return <Navigate replace to={flow ? withFlow("/connect", flow) : "/setup"} />;
  }
  return (
    <div className="collage min-h-screen">
      <aside className="fixed inset-y-0 left-0 z-30 hidden lg:flex">
        <Sidebar />
      </aside>
      {menuOn === pathname && (
        <div className="fixed inset-0 z-40 flex lg:hidden">
          <Sidebar onClose={() => setMenuOn(null)} />
          <button aria-label="Close menu" className="flex-1 bg-[#0d0b22]/40 backdrop-blur-sm" onClick={() => setMenuOn(null)} />
        </div>
      )}
      <div className="flex min-h-screen min-w-0 flex-col lg:pl-72">
        <header className="sticky top-0 z-20 flex items-center justify-between gap-3 bg-nav px-5 py-3 lg:hidden">
          <Logo small onNav to={session.signedIn ? "/app" : "/setup"} />
          <NavButton label="Open menu" onClick={() => setMenuOn(pathname)}>
            <Menu />
          </NavButton>
        </header>
        {flow && session.claude && session.signedIn && <ClaudeWaiting app={session.claude.app} flow={flow} here={pathname} />}
        <Outlet />
      </div>
    </div>
  );
}

function Sidebar({ onClose }: { onClose?: () => void }) {
  const session = useSession();
  const flow = useFlow();
  const { pathname } = useLocation();
  const serverSetup = SERVER_SETUP.includes(pathname);
  const items = [
    { to: "/app", label: "My connections", icon: Layers, active: pathname === "/app" },
    { to: "/app/setup", label: "Add a new connection", icon: CirclePlus, active: pathname.startsWith("/app/setup") && !serverSetup },
  ];
  return (
    <nav
      className={cn(
        "flex h-full flex-col bg-nav px-3.5 pt-7 pb-5 text-nav-foreground shadow-[20px_0_60px_-40px_rgb(13_11_34/0.6)]",
        // The phone menu needs a little more room so the logo and its close button fit.
        onClose ? "w-[min(21rem,90vw)]" : "w-72",
      )}
    >
      <div className="flex items-center justify-between gap-2 px-1.5">
        <Logo small onNav to={session.signedIn ? "/app" : "/setup"} />
        {onClose && (
          <NavButton label="Close menu" onClick={onClose}>
            <X />
          </NavButton>
        )}
      </div>
      {session.signedIn ? (
        <>
          <p className="mt-10 mb-2 px-3 text-[11px] font-semibold tracking-wider text-nav-muted uppercase">Your data</p>
          <ul className="flex flex-col gap-1">
            {items.map((item) => (
              <li key={item.to}>
                <NavItem to={withFlow(item.to, flow)} icon={item.icon} active={item.active}>
                  {item.label}
                </NavItem>
              </li>
            ))}
          </ul>
          <div className="mt-auto flex flex-col gap-3">
            <NavItem to={withFlow("/app/settings", flow)} icon={Settings} active={pathname === "/app/settings" || serverSetup}>
              Settings
            </NavItem>
            <div className="flex items-center gap-3 rounded-2xl bg-nav-raised p-3 ring-1 ring-nav-foreground/10">
              <Initials name={session.name} email={session.email} className="bg-nav-active text-nav-active-foreground" />
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm font-bold">{session.name || session.email}</div>
                <div className="truncate text-xs text-nav-muted">{session.email}</div>
              </div>
              <NavButton label="Sign out" onClick={signOut}>
                <LogOut />
              </NavButton>
            </div>
          </div>
        </>
      ) : (
        <p className="mt-auto px-3 text-xs text-nav-muted">One-time server setup. People sign in once this is done.</p>
      )}
    </nav>
  );
}

function NavItem({ to, icon: Icon, active, children }: { to: string; icon: typeof Layers; active: boolean; children: ReactNode }) {
  return (
    <Link
      to={to}
      aria-current={active ? "page" : undefined}
      className={cn(
        "flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-semibold transition",
        active ? "bg-nav-active text-nav-active-foreground shadow-[0_10px_30px_-14px_rgb(0_0_0/0.6)]" : "text-nav-foreground/75 hover:bg-nav-raised hover:text-nav-foreground",
      )}
    >
      <Icon className="size-[1.1rem]" />
      {children}
    </Link>
  );
}

/** An icon button on the violet nav (the shared ghost button is made for white pages). */
function NavButton({ label, onClick, children }: { label: string; onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      title={label}
      className="grid size-9 shrink-0 cursor-pointer place-items-center rounded-xl text-nav-foreground/80 transition outline-none hover:bg-nav-raised hover:text-nav-foreground focus-visible:ring-[3px] focus-visible:ring-ring/50 [&_svg]:size-4"
    >
      {children}
    </button>
  );
}

/** Inside Claude's sign-in: a reminder that Claude waits, and the way back to its request. */
function ClaudeWaiting({ app, flow, here }: { app: string; flow: string; here: string }) {
  return (
    <div className="bg-hero text-hero-foreground">
      <div className="flex w-full flex-wrap items-center gap-x-4 gap-y-1 px-5 py-2.5 text-sm sm:px-8 lg:px-10">
        <Sparkles className="size-4 text-[#57d3bd]" />
        <span>
          <strong>{app}</strong> is waiting: add your data, then save on the last step to return.
        </span>
        {here !== "/connect" && (
          <Link to={withFlow("/connect", flow)} className="ml-auto font-semibold text-[#57d3bd] hover:underline">
            Go to {app}'s request
          </Link>
        )}
      </div>
    </div>
  );
}

export interface Back {
  to: string;
  label: string;
}

/** One screen inside the app: Back, a header, actions on the right, an optional side rail. */
export function Page({
  back,
  icon,
  eyebrow,
  title,
  hook,
  actions,
  step,
  aside,
  children,
}: {
  back?: Back | null;
  icon?: ReactNode;
  eyebrow?: ReactNode;
  title: ReactNode;
  hook?: ReactNode;
  actions?: ReactNode;
  /** A guided-setup step: shown as a compact progress bar where the side rail isn't. */
  step?: SetupStep;
  aside?: ReactNode;
  children: ReactNode;
}) {
  return (
    <main className="w-full min-w-0 flex-1 px-5 py-6 sm:px-8 lg:px-10 lg:py-8">
      {back && (
        <Link to={back.to} className="group mb-5 inline-flex items-center gap-2 rounded-lg py-1 pr-2 text-sm font-semibold text-muted-foreground hover:text-ink">
          <span className="grid size-7 place-items-center rounded-full border bg-card transition group-hover:-translate-x-0.5 group-hover:border-ink/30">
            <ArrowLeft className="size-3.5" />
          </span>
          {back.label}
        </Link>
      )}
      <header className="mb-7 flex flex-wrap items-center justify-between gap-x-6 gap-y-4">
        <div className="flex min-w-0 items-center gap-4">
          {icon && <span className="hidden size-14 shrink-0 place-items-center rounded-2xl bg-card text-ink shadow-soft ring-1 ring-border sm:grid [&_svg]:size-6">{icon}</span>}
          <div className="min-w-0">
            {eyebrow && <p className="mb-1 text-xs font-bold tracking-[0.08em] text-mint-ink uppercase">{eyebrow}</p>}
            <h1 className="text-[1.75rem] leading-tight font-extrabold tracking-tight text-ink sm:text-[2rem]">{title}</h1>
            {hook && <p className="mt-1.5 max-w-2xl text-[0.95rem] text-muted-foreground">{hook}</p>}
          </div>
        </div>
        {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
      </header>
      {step && <SetupProgress current={step} className={aside ? "xl:hidden" : undefined} />}
      {aside ? (
        <div className="grid items-start gap-8 xl:grid-cols-[minmax(0,1fr)_20rem]">
          <div className="@container min-w-0">{children}</div>
          <aside className="flex flex-col gap-5 xl:sticky xl:top-8">{aside}</aside>
        </div>
      ) : (
        <div className="@container">{children}</div>
      )}
    </main>
  );
}

/** A panel for side rails, settings and dialogs. */
export function Panel({ title, icon, action, children, className }: { title?: ReactNode; icon?: ReactNode; action?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={cn("rounded-2xl border bg-card p-5 shadow-soft", className)}>
      {title && (
        <div className="mb-4 flex items-center gap-2.5">
          <h2 className="flex flex-1 items-center gap-2.5 text-sm font-bold text-ink [&_svg]:size-4 [&_svg]:text-mint-ink">
            {icon}
            {title}
          </h2>
          {action}
        </div>
      )}
      {children}
    </section>
  );
}

/** A number with a label, for the tops of pages. */
export function StatTile({ icon, label, value, tone = "default", className }: { icon: ReactNode; label: string; value: ReactNode; tone?: "default" | "mint" | "amber" | "blush"; className?: string }) {
  const tones = { default: "bg-lavender text-ink", mint: "bg-mint text-mint-ink", amber: "bg-amber text-amber-ink", blush: "bg-blush text-blush-ink" };
  return (
    <div className={cn("flex items-center gap-4 rounded-2xl border bg-card p-4 shadow-soft", className)}>
      <span className={cn("grid size-11 shrink-0 place-items-center rounded-xl [&_svg]:size-5", tones[tone])}>{icon}</span>
      <div className="min-w-0">
        <div className="text-2xl leading-none font-extrabold text-ink">{value}</div>
        <div className="mt-1 text-xs leading-snug font-medium text-muted-foreground">{label}</div>
      </div>
    </div>
  );
}

export function EmptyState({ icon, title, children }: { icon: ReactNode; title: string; children: ReactNode }) {
  return (
    <div className="flex flex-col items-center gap-3 rounded-3xl border border-dashed bg-card/60 px-6 py-14 text-center">
      <span className="grid size-14 place-items-center rounded-2xl bg-lavender text-ink [&_svg]:size-6">{icon}</span>
      <h2 className="text-lg font-bold">{title}</h2>
      <div className="max-w-md text-sm text-muted-foreground">{children}</div>
    </div>
  );
}

// --- the guided setup's steps ------------------------------------------------------------------

const SETUP_STEPS = [
  { key: "platform", label: "Platform", hint: "Where your warehouses live" },
  { key: "workspace", label: "Workspace", hint: "Pick one you can see" },
  { key: "warehouses", label: "Data warehouses", hint: "Tick, connect, limit schemas" },
  { key: "models", label: "Semantic models", hint: "Optional · Microsoft Fabric" },
  { key: "review", label: "Review", hint: "Everything green?" },
  { key: "instructions", label: "Instructions for Claude", hint: "Context for every answer" },
] as const;

export type SetupStep = (typeof SETUP_STEPS)[number]["key"];

/** The steps in the side rail (wide screens). */
export function SetupSteps({ current }: { current: SetupStep }) {
  const at = SETUP_STEPS.findIndex((step) => step.key === current);
  return (
    <Panel title="Add a new connection" className="hidden xl:block">
      <ol aria-label="Setup steps" className="relative flex flex-col gap-1">
        {SETUP_STEPS.map((step, index) => {
          const done = index < at;
          const now = index === at;
          return (
            <li key={step.key} aria-current={now ? "step" : undefined} className="relative flex gap-3 pb-3 last:pb-0">
              {index < SETUP_STEPS.length - 1 && (
                <span aria-hidden className={cn("absolute top-7 left-[0.8rem] h-[calc(100%-1.5rem)] w-px", done ? "bg-mint-ink/50" : "bg-border")} />
              )}
              <span
                className={cn(
                  "relative z-10 grid size-[1.65rem] shrink-0 place-items-center rounded-full text-[11px] font-bold",
                  done && "bg-mint text-mint-ink",
                  now && "bg-primary text-primary-foreground ring-4 ring-primary/15",
                  !done && !now && "border bg-card text-muted-foreground",
                )}
              >
                {done ? <Check className="size-3.5" strokeWidth={3} /> : index + 1}
              </span>
              <span className="pt-0.5">
                <span className={cn("block text-sm font-semibold", now ? "text-ink" : done ? "text-ink/80" : "text-muted-foreground")}>{step.label}</span>
                <span className="block text-xs text-muted-foreground">{step.hint}</span>
              </span>
            </li>
          );
        })}
      </ol>
    </Panel>
  );
}

/** The same steps as one bar, for laptops and phones where the rail sits below. */
function SetupProgress({ current, className }: { current: SetupStep; className?: string }) {
  const at = SETUP_STEPS.findIndex((step) => step.key === current);
  return (
    <div className={cn("mb-7 rounded-2xl border bg-card px-4 py-3 shadow-soft", className)}>
      <div className="mb-2 flex items-center justify-between gap-3 text-xs font-semibold">
        <span className="text-ink">
          Step {at + 1} of {SETUP_STEPS.length} · {SETUP_STEPS[at].label}
        </span>
        <span className="text-muted-foreground">{at < SETUP_STEPS.length - 1 ? `Next: ${SETUP_STEPS[at + 1].label}` : "Last step"}</span>
      </div>
      <div className="grid grid-cols-6 gap-1.5" aria-hidden>
        {SETUP_STEPS.map((step, index) => (
          <span key={step.key} className={cn("h-1.5 rounded-full", index < at ? "bg-mint-ink/70" : index === at ? "bg-primary" : "bg-muted")} />
        ))}
      </div>
    </div>
  );
}

/**
 * Which accounts are involved: the one signed in to IQ Connect and the one the
 * platform runs queries as. Both are usually company accounts and easy to mix up.
 */
export function AccountCard({ platform, platformLabel, account }: { platform: string; platformLabel: string; account?: string }) {
  const session = useSession();
  const same = !!account && account.toLowerCase() === session.email?.toLowerCase();
  return (
    <Panel title="Who is connecting" icon={<UserRound />}>
      <div className="flex flex-col gap-3">
        <div className="flex items-center gap-3">
          <Initials name={session.name} email={session.email} className="size-9" />
          <div className="min-w-0">
            <div className="text-xs font-semibold text-muted-foreground">IQ Connect</div>
            <div className="truncate text-sm font-semibold">{session.email}</div>
          </div>
        </div>
        <div className="flex items-center gap-3">
          <PlatformMark platform={platform} className="size-9 p-2" />
          <div className="min-w-0">
            <div className="text-xs font-semibold text-muted-foreground">{platformLabel}</div>
            <div className={cn("truncate text-sm font-semibold", !account && "text-muted-foreground")}>{account || "Not signed in yet"}</div>
          </div>
        </div>
        {account && (
          <p className={cn("rounded-xl px-3 py-2 text-xs", same ? "bg-mint text-mint-ink" : "bg-amber text-amber-ink")}>
            {same ? "The same company account for both." : `Two different accounts. Claude reads ${platformLabel} as ${account}.`}
          </p>
        )}
      </div>
    </Panel>
  );
}

/** "as alice@contoso.com": the account a connected resource signs in with. */
export function SignedInAs({ account, className }: { account: string; className?: string }) {
  if (!account) return null;
  return (
    <span title={`Signed in as ${account}`} className={cn("inline-flex max-w-full min-w-0 items-center gap-1.5 text-xs text-muted-foreground", className)}>
      <UserRound className="size-3.5 shrink-0" />
      <span className="truncate">{account}</span>
    </span>
  );
}

// --- small shared pieces ---------------------------------------------------------------

export function FullPageLoader() {
  return (
    <div className="grid min-h-screen place-items-center bg-page">
      <Loader2 className="size-7 animate-spin text-ink/40" aria-label="Loading" />
    </div>
  );
}

/** Placeholder cards in the shape of what is loading, so the page doesn't jump. */
export function Loading({ cards = 3 }: { cards?: number }) {
  return (
    <div role="status" aria-label="Loading" className="grid gap-4 @xl:grid-cols-2 @5xl:grid-cols-3">
      {Array.from({ length: cards }, (_, index) => (
        <div key={index} className="flex h-44 animate-pulse flex-col gap-4 rounded-2xl border bg-card p-5">
          <div className="flex items-center gap-3">
            <span className="size-11 rounded-xl bg-muted" />
            <span className="flex flex-1 flex-col gap-2">
              <span className="h-3.5 w-2/3 rounded-full bg-muted" />
              <span className="h-3 w-1/2 rounded-full bg-muted" />
            </span>
          </div>
          <span className="mt-auto h-12 rounded-xl bg-muted" />
        </div>
      ))}
    </div>
  );
}

export function Notice({ error, tone = "danger", children }: { error?: ApiError | null; tone?: "danger" | "success"; children?: ReactNode }) {
  const text = children ?? error?.message;
  if (!text) return null;
  const Icon = tone === "success" ? CheckCircle2 : AlertCircle;
  return (
    <div
      role={tone === "success" ? "status" : "alert"}
      className={cn("mb-5 flex items-start gap-2.5 rounded-xl px-4 py-3 text-sm font-medium", tone === "success" ? "bg-mint text-mint-ink" : "bg-blush text-blush-ink")}
    >
      <Icon className="mt-0.5 size-4 shrink-0" />
      <span>{text}</span>
    </div>
  );
}

const STATUS: Record<Status, { label: string; variant: "success" | "warning" | "default" | "danger" }> = {
  connected: { label: "Connected", variant: "success" },
  pending: { label: "Not connected", variant: "warning" },
  connecting: { label: "In progress", variant: "default" },
  failed: { label: "Failed", variant: "danger" },
};

export function StatusBadge({ status }: { status: Status }) {
  const { label, variant } = STATUS[status];
  return (
    <Badge variant={variant}>
      {status === "connecting" && <Loader2 className="animate-spin" />}
      {status === "connected" && <CheckCircle2 />}
      {label}
    </Badge>
  );
}

/**
 * A platform asks for its own sign-in (HTTP 428). Nothing chosen yet: straight to the Sign
 * in step, where the person picks how. Otherwise one button, then back here.
 */
export function SignInPrompt({ error }: { error: ApiError }) {
  const platform = error.data.platform as { key: string; label: string; signInLabel: string };
  const url = error.data.signInUrl as string;
  if (url.startsWith("/app/")) return <Navigate replace to={url} />;
  const microsoft = /microsoft/i.test(platform.signInLabel);
  return (
    <div className="flex flex-col items-center gap-6 rounded-3xl border bg-card p-8 text-center shadow-soft sm:p-10">
      <PlatformMark platform={platform.key} className="size-16 p-3.5" />
      <div>
        <h2 className="text-xl font-bold">Sign in to {platform.label}</h2>
        <p className="mt-1 text-sm text-muted-foreground">{error.message || "Claude only sees what your own account can see."}</p>
      </div>
      <Button size="lg" className="w-full max-w-sm" onClick={() => go(url)}>
        {microsoft && <MicrosoftLogo />}
        Continue with {platform.signInLabel} <ArrowRight />
      </Button>
      <p className="text-xs text-muted-foreground">No password or access token is stored.</p>
    </div>
  );
}

/** What a screen shows when its data failed to load. */
export function LoadFailure({ error }: { error: ApiError }) {
  if (error.code === "sign_in") return <SignInPrompt error={error} />;
  if (error.status === 401 || error.code === "needs_setup") return <Loading />;
  return <Notice error={error} />;
}

/** The address Claude's custom connector needs, with Copy and a link to Claude's settings. */
export function ConnectorAddress() {
  const session = useSession();
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    await navigator.clipboard.writeText(session.mcpUrl);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1800);
  };
  return (
    <div className="rounded-2xl border bg-muted/60 p-4 text-left">
      <div className="mb-2 flex items-center justify-between text-[11px] font-semibold text-muted-foreground">
        <span>Connector URL for Claude</span>
        <a href={session.connectorsUrl} target="_blank" rel="noopener noreferrer" className="text-ink hover:underline">
          Add to Claude ↗
        </a>
      </div>
      <div className="flex items-center gap-2">
        <code className="min-w-0 flex-1 truncate rounded-lg bg-card px-3 py-2 text-[13px]">{session.mcpUrl}</code>
        <Button variant="outline" size="icon" onClick={copy} aria-label="Copy connector URL">
          {copied ? <Check /> : <Copy />}
        </Button>
      </div>
    </div>
  );
}

/** A small list of schema names, trimmed to `max` with "+N more". */
export function SchemaChips({ schemas, max = 4 }: { schemas: string[]; max?: number }) {
  const shown = schemas.slice(0, max);
  return (
    <div className="flex flex-wrap gap-1.5">
      {shown.map((schema) => (
        <span key={schema} className="rounded-md bg-card px-2 py-0.5 text-xs font-semibold text-ink/80 ring-1 ring-border">
          {schema}
        </span>
      ))}
      {schemas.length > shown.length && <span className="px-1 text-xs leading-5 text-muted-foreground">+{schemas.length - shown.length} more</span>}
    </div>
  );
}

export function timeAgo(iso: string) {
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  for (const [unit, size] of [["d", 86400], ["h", 3600], ["min", 60]] as const) {
    if (seconds >= size) return `${Math.floor(seconds / size)} ${unit} ago`;
  }
  return "just now";
}

export function formatDate(iso: string | null) {
  return iso ? new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : "—";
}
