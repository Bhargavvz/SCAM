// HTTP client, formatting helpers and data hooks.
import { useCallback, useEffect, useRef, useState } from "react";

export type Row = Record<string, any>;
export type Page<T = Row> = { total: number; rows: T[]; limit: number; offset: number };

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method,
    credentials: "same-origin",
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401 && !path.startsWith("/login")) window.dispatchEvent(new Event("meridian:signed-out"));
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const j = await res.json();
      msg = typeof j.detail === "string" ? j.detail : Array.isArray(j.detail) ? j.detail.map((d: Row) => `${d.loc?.slice(-1)[0]}: ${d.msg}`).join("; ") : msg;
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, msg);
  }
  return res.json() as Promise<T>;
}

export const api = {
  get: <T = Row>(path: string, params?: Record<string, unknown>) => request<T>("GET", params ? `${path}?${qs(params)}` : path),
  post: <T = Row>(path: string, body: unknown = {}) => request<T>("POST", path, body),
  patch: <T = Row>(path: string, body: unknown) => request<T>("PATCH", path, body),
};

export function qs(p: Record<string, unknown>) {
  return Object.entries(p)
    .filter(([, v]) => v !== undefined && v !== null && v !== "" && v !== false)
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
    .join("&");
}

// ---------------------------------------------------------------- formatting
export const fmt = {
  n: (v: unknown, d = 0) => (v === null || v === undefined || v === "" ? "–" : Number(v).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: 0 })),
  money: (v: unknown) => {
    if (v === null || v === undefined || v === "") return "–";
    const n = Number(v);
    const a = Math.abs(n);
    if (a >= 1e9) return `$${(n / 1e9).toFixed(2)}B`;
    if (a >= 1e6) return `$${(n / 1e6).toFixed(2)}M`;
    if (a >= 1e4) return `$${(n / 1e3).toFixed(1)}K`;
    return `$${n.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
  },
  moneyFull: (v: unknown) => (v === null || v === undefined ? "–" : `$${Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 })}`),
  pct: (v: unknown, d = 1) => (v === null || v === undefined ? "–" : `${(Number(v) * 100).toFixed(d)}%`),
  compact: (v: unknown) => {
    if (v === null || v === undefined) return "–";
    const n = Number(v);
    const a = Math.abs(n);
    if (a >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
    if (a >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
    if (a >= 1e4) return `${(n / 1e3).toFixed(1)}K`;
    return n.toLocaleString(undefined, { maximumFractionDigits: 1 });
  },
  date: (v: unknown) => (v ? String(v).slice(0, 10) : "–"),
  ago: (iso: string) => {
    const s = (Date.now() - new Date(iso).getTime()) / 1000;
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)}d ago`;
  },
  label: (v: unknown) => String(v ?? "").replace(/_/g, " "),
};

// ---------------------------------------------------------------- hooks
export function useQuery<T = Row>(path: string | null, params?: Record<string, unknown>) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const key = path ? `${path}?${params ? qs(params) : ""}` : null;
  useEffect(() => {
    if (!key) return;
    let live = true;
    setLoading(true);
    setError(null);
    fetch(`/api${key.endsWith("?") ? key.slice(0, -1) : key}`, { credentials: "same-origin" })
      .then(async (r) => {
        if (r.status === 401) window.dispatchEvent(new Event("meridian:signed-out"));
        if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail ?? r.statusText);
        return r.json();
      })
      .then((d) => live && setData(d))
      .catch((e) => live && setError(String(e.message ?? e)))
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [key, tick]);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data, error, loading, reload, setData };
}

export function useDebounced<T>(value: T, ms = 250): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

export function useMutation<A extends unknown[], R>(fn: (...a: A) => Promise<R>) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(true);
  useEffect(() => () => {
    alive.current = false;
  }, []);
  const run = async (...a: A): Promise<R | undefined> => {
    setBusy(true);
    setError(null);
    try {
      return await fn(...a);
    } catch (e) {
      if (alive.current) setError((e as Error).message);
      throw e;
    } finally {
      if (alive.current) setBusy(false);
    }
  };
  return { run, busy, error, setError };
}

// ---------------------------------------------------------------- entity routing
const ROUTES: [RegExp, (id: string) => string][] = [
  [/^IP\d+$/, (id) => `/products/${id}`],
  [/^SUP\d+$/, (id) => `/suppliers/${id}`],
  [/^CUS\d+$/, (id) => `/sales/customers/${id}`],
  [/^SO\d+$/, (id) => `/sales/orders/${id}`],
  [/^R?PO\d+$/, (id) => `/purchasing/${id}`],
  [/^SH\d+$/, (id) => `/logistics/${id}`],
  [/^RMA\d+$/, (id) => `/returns/${id}`],
  [/^W\d{3}$/, (id) => `/warehouses/${id}`],
  [/^PR\d+$/, (id) => `/production/${id}`],
  [/^RM\d+$/, (id) => `/inventory?tab=materials&q=${id}`],
  [/^CR\d+$/, (id) => `/logistics?carrier=${id}`],
];

export function routeFor(id: string): string | null {
  const hit = ROUTES.find(([re]) => re.test(id));
  return hit ? hit[1](id) : null;
}

export const ID_PATTERN = /\b((?:IP|SUP|CUS|SO|RPO|PO|SH|RMA|PR|RM|CR)\d{2,}|W\d{3})\b/;
