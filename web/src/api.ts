// Typed client for the FastAPI backend (api/server.py).

export type Row = Record<string, unknown>;

export interface Option {
  action: string;
  feasible: boolean;
  supported?: boolean;
  arrival?: string | null;
  stockout_days?: number;
  cost?: number;
  risk?: string;
  score?: number;
  service_impact_units?: number;
  breaches_commitments?: string[];
  note?: string;
}

export interface DecisionCard {
  run_id: string;
  as_of: string;
  report: string;
  situation_summary: string;
  options: Option[];
  simulator_best_action: string | null;
  recommended_action: string | null;
  deviates_from_simulator_best: boolean;
  confidence: string;
  rationale: string;
  key_reasons: string[];
  precedents: Row[];
  open_commitments: Row[];
  cited_doc_ids: string[];
  cited_record_ids: string[];
  unverifiable_citations: string[];
  ungrounded_claims: string[];
  guardrail_events: string[];
  warnings: string[];
  reflect_mode: string | null;
  reflection: string | null;
  memory_hits: number;
  dropped_future_hits: number;
  writeback: Row | null;
  mock_actions: string[];
  usage: { calls?: number; input_tokens?: number; output_tokens?: number; cost_usd?: number | null };
  latency_s: number;
  trace: Row[];
}

export interface CapabilityAnswer {
  question: string;
  capability: string;
  as_of: string;
  budget: string;
  reflect_mode: string | null;
  answer: string;
  structured: Row | null;
  ground_truth: Record<string, Row[]>;
  cited_doc_ids: string[];
  cited_record_ids: string[];
  unverifiable_citations: string[];
  commitments_checked: boolean | null;
  warnings: string[];
  confidence: string;
  latency_s: number;
}

export interface QAResult {
  question: string;
  as_of: string;
  answer: string;
  cited_doc_ids: string[];
  cited_record_ids: string[];
  unverifiable_citations: string[];
  confidence: string;
  memory_hits: number;
  latency_s: number;
}

export interface Scenario {
  id: string;
  day0: string;
  report: string;
  context: { rpo_id: string; run_ids: string[]; rm_id: string };
  supplier_id: string;
  rm_id: string;
  gold_best_action: string;
  trap: { its_decision_id: string; its_action: string; its_outcome: string; why_misleading: string } | null;
}

export interface DemoSpec {
  id: string;
  title: string;
  kind: string;
  expect: Row;
  question?: string;
  report?: string;
  scenario?: string;
}

export interface DemoResult {
  card?: DecisionCard;
  cards?: DecisionCard[];
  answer?: QAResult;
  capability?: CapabilityAnswer;
  error?: string;
}

export interface Check {
  check: string;
  expected: unknown;
  actual: unknown;
  ok: boolean;
}

export interface Config {
  llm_provider: string;
  llm_model: string;
  llm_compact: boolean;
  hindsight_base_url: string;
  bank_id: string;
  default_as_of: string;
  mental_models_in_reflect: boolean;
}

export interface MemoryDoc {
  doc_id: string;
  date: string;
  doc_type: string;
  context: string;
  content: string;
  superseded_by: { doc_id: string; date: string }[];
}

export interface HoldoutRow {
  scenario_id: string;
  gold: string;
  chosen: string | null;
  correct: boolean;
  trap: boolean;
  trap_pass: boolean | null;
  precedent_recall: number | null;
  commitments_recall: number | null;
  latency_s: number | null;
}

export interface QuestionSummary {
  n: number;
  accuracy: number | null;
  by_type: Record<string, { n: number; accuracy: number | null; mean_score: number | null }>;
  by_hop_count: Record<string, { n: number; accuracy: number | null }>;
  single_hop_fact_recall_accuracy: number | null;
  citations: Record<string, number | null>;
  future_doc_citations: number;
  latency: { mean_s: number | null };
}

export interface Scorecard {
  questions: QuestionSummary | null;
  questions_hindsight_mode: QuestionSummary | null;
  holdout: {
    n: number;
    accuracy: number | null;
    trap_n: number;
    trap_accuracy: number | null;
    precedent_recall: number | null;
    commitments_recall: number | null;
    simulator_parity_ok: number;
    errors: number;
    latency: { mean_s: number | null; p50_s: number | null; max_s: number | null };
    tokens: { mean_input: number | null; mean_output: number | null };
    per_scenario: HoldoutRow[];
  } | null;
  patterns: {
    per_pattern: Record<string, { on?: boolean; off?: boolean }>;
    detected_with_mental_models: number;
    detected_without_mental_models: number;
  } | null;
}

export interface Coverage {
  memory_docs_stored: number;
  memory_docs_total: number;
  holdout_docs: number;
  eval_questions: number;
  holdout_scenarios: number;
  planted_patterns: number;
  disruption_events: number;
  decisions: number;
  commitments: number;
  negotiations: number;
  rm_purchase_orders: number;
}

export interface Results {
  scorecard_md: string | null;
  demo_report_md: string | null;
  cards: string[];
  scorecard: Scorecard | null;
  coverage: Coverage;
}

async function call<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, body === undefined ? undefined : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (res.status === 401 && !path.startsWith("/api/login")) window.dispatchEvent(new Event("scm:signed-out"));
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

export const api = {
  config: () => call<Config>("/api/config"),
  scenarios: () => call<Scenario[]>("/api/scenarios"),
  decide: (body: { report: string; as_of?: string; rpo_id?: string; run_ids?: string[]; rm_id?: string; writeback: boolean }) =>
    call<DecisionCard>("/api/decide", body),
  ask: (question: string, as_of?: string) => call<CapabilityAnswer>("/api/ask", { question, as_of }),
  question: (question: string, as_of: string) => call<QAResult>("/api/question", { question, as_of }),
  demoSpecs: () => call<DemoSpec[]>("/api/demo/specs"),
  demoRun: (id: string) => call<{ spec: DemoSpec; result: DemoResult; checks: Check[] }>("/api/demo/run", { id }),
  doc: (id: string, asOf: string) => call<MemoryDoc>(`/api/docs/${id}?as_of=${encodeURIComponent(asOf)}`),
  results: () => call<Results>("/api/results"),
  resultCard: (id: string) => call<DecisionCard>(`/api/results/cards/${id}`),
  session: () => call<{ auth_required: boolean; user: string | null }>("/api/session"),
  login: (username: string, password: string) => call<{ ok: boolean; user: string | null }>("/api/login", { username, password }),
  logout: () => call<{ ok: boolean }>("/api/logout", {}),
  resetLive: () => call<{ ok: boolean; message: string }>("/api/live/reset", {}),
};
