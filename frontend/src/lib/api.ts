/**
 * The one way the app talks to the server: typed calls to /api, the CSRF token on
 * every change, and errors as `ApiError` with the server's short sentence.
 *
 * Two errors are handled for every screen:
 *   401 signed_out -> to /auth/login, coming back to this exact screen
 *   409 needs_setup -> to the one-time setup page the server names
 * A 428 sign_in (the platform needs its own sign-in) is shown by the screen.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useLocation } from "react-router-dom";

// --- types (mirror backend/core/web/setup.py and account.py) -------------------------

export type Status = "pending" | "connecting" | "connected" | "failed";

export interface Session {
  signedIn: boolean;
  email?: string;
  name?: string;
  csrf?: string;
  isAdmin?: boolean;
  claude: { app: string } | null;
  mcpUrl: string;
  connectorsUrl: string;
}

export interface PlatformList {
  platforms: PlatformStatus[];
  /** Where the person's warehouses already are: more are added there, not elsewhere. */
  warehousePlatform: string | null;
}

export interface PlatformStatus {
  key: string;
  label: string;
  available: boolean;
  how: string;
  whyNot: string;
  setupUrl: string;
}

/** Step 2: the ways this platform can be signed in to, and the person's existing sign-ins. */
export interface SignInOptions {
  platform: { key: string; label: string };
  methods: {
    key: string;
    label: string;
    description: string;
    recommended: boolean;
    available: boolean;
    whyNot: string;
    redirectUri: string;
    fields: { name: string; label: string; placeholder: string; value: string; secret: boolean; required: boolean; hint: string }[];
  }[];
  accounts: { id: string; address: string; method: string; methodLabel: string; username: string; next: string }[];
}

export interface Platform {
  key: string;
  label: string;
  account: string;
  hasModels: boolean;
  hasCatalogs: boolean;
  warehouseNote: string;
  modelNote: string;
}

export interface Item {
  id: string;
  kind: "warehouse" | "model";
  platform: string;
  platformLabel: string;
  workspaceId: string;
  workspaceName: string;
  resourceId: string;
  name: string;
  catalog: string;
  schemas: string[];
  status: Status;
  error: string;
  connectionName: string | null;
  /** The account the saved connection signs in with ("" before it connects). */
  account: string;
}

export interface Resource {
  id: string;
  name: string;
  detail: string;
  item: Item | null;
}

/** `only`: the person sees just this workspace, so its picker was skipped. */
export interface Workspace {
  id: string;
  name: string;
  only: boolean;
}

export interface Workspaces {
  platform: Platform;
  workspaces: { id: string; name: string; detail: string }[];
  /** Where to add another of the person's own workspaces (Databricks, Snowflake); "" if none. */
  addUrl: string;
}

export interface Warehouses {
  platform: Platform;
  workspace: Workspace;
  catalogs: string[];
  catalog: string;
  warehouses: Resource[];
}

export interface Models {
  platform: Platform;
  workspace: Workspace;
  models: Resource[];
}

export interface SetupStatus {
  items: Item[];
  counts: Record<Status, number>;
  active: number;
  expired: boolean;
  ready: boolean;
}

export interface Connection {
  id: string;
  name: string;
  platform: string;
  kind: "warehouse" | "model";
  catalog: string;
  schemas: string[];
  models: string[];
  notes: string;
  status: string;
  account: string;
  workspace: string;
  createdAt: string | null;
  /** Where its schemas are changed (the guided setup's API); null when they can't be. */
  schemaEdit: { platform: string; workspaceId: string; resourceId: string; catalog: string } | null;
}

export interface Account {
  id: string;
  name: string;
  email: string;
  organization: string;
  isAdmin: boolean;
  signInMethod: string;
  createdAt: string | null;
  lastSignIn: string | null;
  signIns: { provider: string; label: string; account: string; connections: number; since: string | null }[];
}

export interface Dashboard {
  email: string;
  instructions: string;
  maxInstructions: number;
  microsoftAccount: string | null;
  stats: { answered: number; refused: number; calls: number; averageSeconds: number | null };
  connections: Connection[];
  activity: {
    at: string;
    tool: string;
    connection: string;
    outcome: string;
    rows: number | null;
    ms: number | null;
    statement: string;
    error: string;
  }[];
  apps: { clientId: string; name: string; since: string | null }[];
}

// --- calls -----------------------------------------------------------------------------

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public data: Record<string, unknown> = {},
  ) {
    super(message || "Something went wrong. Try again.");
  }
}

let csrfToken = "";

export function setCsrf(token: string | undefined) {
  csrfToken = token ?? "";
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method, credentials: "same-origin", headers: { Accept: "application/json" } };
  if (method !== "GET") {
    init.headers = { ...init.headers, "Content-Type": "application/json", "X-CSRF-Token": csrfToken };
    init.body = JSON.stringify(body ?? {});
  }
  const response = await fetch(path, init);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new ApiError(response.status, data.error ?? "error", data.message ?? "", data);
    leaveFor(error);
    throw error;
  }
  return data as T;
}

/** The errors that move the whole browser somewhere else. */
function leaveFor(error: ApiError) {
  const here = window.location.pathname + window.location.search;
  if (error.status === 401) {
    const platform = window.location.pathname.match(/^\/app\/setup\/(fabric|databricks|snowflake)\b/)?.[1];
    const query = new URLSearchParams({ next: here, ...(platform ? { platform } : {}) });
    window.location.assign(`/auth/login?${query}`);
  } else if (error.code === "needs_setup" && typeof error.data.setupUrl === "string") {
    window.location.assign(`${error.data.setupUrl}?${new URLSearchParams({ next: here })}`);
  }
}

export const api = {
  get: <T>(path: string, params: Record<string, string | undefined> = {}) => {
    const query = new URLSearchParams(
      Object.entries(params).filter((entry): entry is [string, string] => !!entry[1]),
    ).toString();
    return request<T>("GET", query ? `${path}?${query}` : path);
  },
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body),
  put: <T>(path: string, body?: unknown) => request<T>("PUT", path, body),
  patch: <T>(path: string, body?: unknown) => request<T>("PATCH", path, body),
  delete: <T>(path: string) => request<T>("DELETE", path),
};

/** Go where the server said: Claude's callback, a platform's sign-in, or a page here. */
export function go(url: string) {
  window.location.assign(url);
}

// --- hooks -----------------------------------------------------------------------------

/** Claude's sign-in id from the address, carried through every screen. */
export function useFlow(): string {
  const { search } = useLocation();
  return new URLSearchParams(search).get("flow") ?? "";
}

/** `path` with Claude's sign-in id (and any other params) kept. */
export function withFlow(path: string, flow: string, params: Record<string, string> = {}) {
  const query = new URLSearchParams(
    Object.entries({ ...params, flow }).filter(([, value]) => value),
  ).toString();
  return query ? `${path}?${query}` : path;
}

export interface Loaded<T> {
  data: T | null;
  error: ApiError | null;
  loading: boolean;
  reload: () => void;
  setData: (data: T) => void;
}

/** Load `path` (again whenever it or `params` change); while `poll(data)` is true, every 2 s. */
export function useApi<T>(
  path: string | null,
  params: Record<string, string | undefined> = {},
  poll: (data: T | null) => boolean = () => false,
): Loaded<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const key = JSON.stringify(params);
  const first = useRef(true);

  useEffect(() => {
    if (!path) return;
    let live = true;
    if (first.current) setLoading(true);
    api
      .get<T>(path, JSON.parse(key))
      .then((found) => live && (setData(found), setError(null)))
      .catch((failed: ApiError) => live && setError(failed))
      .finally(() => live && ((first.current = false), setLoading(false)));
    return () => {
      live = false;
    };
  }, [path, key, tick]);

  const polling = poll(data);
  useEffect(() => {
    if (!polling) return;
    const timer = window.setInterval(() => setTick((n) => n + 1), 2000);
    return () => window.clearInterval(timer);
  }, [polling]);

  const reload = useCallback(() => setTick((n) => n + 1), []);
  return { data, error, loading, reload, setData };
}

/** Run a change with a busy flag and its error; returns the result or null. */
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const run = useCallback(async <T,>(work: () => Promise<T>): Promise<T | null> => {
    setBusy(true);
    setError(null);
    try {
      return await work();
    } catch (failed) {
      setError(failed instanceof ApiError ? failed : new ApiError(0, "network", "Couldn't reach the server."));
      return null;
    } finally {
      setBusy(false);
    }
  }, []);
  return { busy, error, run, setError };
}
