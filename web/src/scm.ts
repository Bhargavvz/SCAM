// Client for the supply chain management API (api/scm.py). Every read is as of a business date.
import { CapabilityAnswer, DecisionCard, Row } from "./api";

export type Page<T = Row> = { total: number; rows: T[]; limit: number; offset: number };

export interface Job<T = unknown> {
  id: string;
  kind: string;
  key: string;
  status: "queued" | "running" | "done" | "error";
  result: T | null;
  error: string | null;
  cached: boolean;
  elapsed_s: number;
  created: number;
  finished: number | null;
  meta: Row;
}

export interface AppInfo { today: string; data_start: string; data_end: string; bank_id: string; llm: string; compact: boolean }

export interface Dashboard {
  as_of: string;
  kpis: Record<string, number>;
  trend_disruptions: { month: string; n: number; avg_severity: number }[];
  trend_otif: { month: string; otif: number; delay: number }[];
  recent_disruptions: Row[];
  supplier_watchlist: Row[];
  runs_at_risk: Row[];
  commitments_due: Row[];
  live_decisions: Row[];
}

export interface AcceptResult { writeback: Row; mock_actions: string[]; card: DecisionCard }
export type AskResult = (CapabilityAnswer & { mode: "ask" }) | (Row & { mode: "history"; answer: string });

async function call<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, body === undefined ? undefined : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (res.status === 401) window.dispatchEvent(new Event("scm:signed-out"));
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail ?? j);
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

const qs = (p: Record<string, unknown>) =>
  Object.entries(p)
    .filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
    .join("&");

const get = <T,>(path: string, p: Record<string, unknown>) => call<T>(`/api${path}?${qs(p)}`);

export const scm = {
  app: () => call<AppInfo>("/api/app"),
  dashboard: (as_of: string) => get<Dashboard>("/dashboard", { as_of }),
  disruptions: (p: Record<string, unknown>) => get<Page>("/disruptions", p),
  disruption: (id: string, as_of: string) => get<Row>(`/disruptions/${id}`, { as_of }),
  pos: (p: Record<string, unknown>) => get<Page>("/pos", p),
  po: (id: string, as_of: string) => get<Row>(`/pos/${id}`, { as_of }),
  checkAction: (id: string, action: string, as_of: string) => call<Row>(`/api/pos/${id}/check-action`, { action, as_of }),
  suppliers: (p: Record<string, unknown>) => get<Page>("/suppliers", p),
  supplier: (id: string, as_of: string) => get<Row>(`/suppliers/${id}`, { as_of }),
  materials: (p: Record<string, unknown>) => get<Page>("/materials", p),
  material: (id: string, as_of: string) => get<Row>(`/materials/${id}`, { as_of }),
  production: (p: Record<string, unknown>) => get<Row>("/production", p),
  demand: (as_of: string) => get<Row>("/demand", { as_of }),
  commitments: (p: Record<string, unknown>) => get<Page & { summary: Row[] }>("/commitments", p),
  decisions: (p: Record<string, unknown>) => get<Page & { accuracy: Row[] }>("/decisions", p),
  decision: (id: string, as_of: string) => get<Row>(`/decisions/${id}`, { as_of }),
  search: (q: string, as_of: string) => get<{ results: { id: string; type: string; label: string }[] }>("/search", { q, as_of }),
  resolve: (event_id: string, as_of: string, force = false) => call<Job<DecisionCard>>("/api/agent/resolve", { event_id, as_of, force }),
  accept: (event_id: string, as_of: string) => call<AcceptResult>("/api/agent/accept", { event_id, as_of }),
  brief: (entity_id: string, as_of: string, force = false) => call<Job<CapabilityAnswer>>("/api/agent/brief", { entity_id, as_of, force }),
  askAgent: (question: string, as_of: string, mode: "ask" | "history") => call<Job<AskResult>>("/api/agent/ask", { question, as_of, mode }),
  job: <T,>(id: string) => call<Job<T>>(`/api/jobs/${id}`),
};
