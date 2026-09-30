import { BrainCircuit, CheckCircle2, History, MessageSquarePlus, OctagonAlert, RefreshCw, Search, ShieldAlert, Sparkles } from "lucide-react";
import { ReactNode, useEffect, useState } from "react";
import { api, fmt, Job, Row, useJob } from "../lib/api";
import { EntityLink, ErrorBox, Linkify, Loading, Spinner, Status, useAction } from "./ui";

const TYPE_TONE: Record<string, string> = { world: "info", experience: "ai", observation: "violet" };
export const TYPE_LABEL: Record<string, string> = { world: "fact", experience: "experience", observation: "observation" };

export function MemoryHit({ h, compact }: { h: Row; compact?: boolean }) {
  return (
    <div className="mem-hit">
      <div className="row" style={{ gap: 6 }}>
        <Status value={TYPE_LABEL[h.type] ?? h.type} tone={TYPE_TONE[h.type] ?? "plain"} />
        {h.when && <span className="small muted">{h.when}</span>}
        <span className="small muted mono">{h.document_id}</span>
        {h.source === "meridian" && <Status value="meridian" tone="good" />}
      </div>
      <div className="mem-text"><Linkify text={h.text} /></div>
      {!compact && h.records?.length > 0 && (
        <div className="row small" style={{ gap: 4 }}>{h.records.slice(0, 6).map((r: string) => <span key={r} className="pill plain"><EntityLink id={r} /></span>)}</div>
      )}
    </div>
  );
}

function List({ title, items, icon }: { title: string; items?: string[]; icon?: ReactNode }) {
  if (!items?.length) return null;
  return (
    <div>
      <div className="mem-label">{icon}{title}</div>
      <ul className="mem-list">{items.map((x, i) => <li key={i}><Linkify text={x} /></li>)}</ul>
    </div>
  );
}

/** Institutional memory for any record: structured brief (reflect), recalled memories (recall), and planner notes (retain). */
export function MemoryPanel({ entityId, title = "Institutional memory" }: { entityId: string; title?: string }) {
  const [tab, setTab] = useState<"brief" | "recall" | "note">("brief");
  const brief = useJob<Row>();
  const [recall, setRecall] = useState<Row | null>(null);
  const [recallErr, setRecallErr] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [note, setNote] = useState("");
  const { run, busy } = useAction();
  const loadRecall = async (query?: string) => {
    setRecallErr(null);
    setRecall(null);
    try {
      setRecall(await api.post(`/memory/entity/${entityId}/recall`, { query: query || undefined }));
    } catch (e) {
      setRecallErr((e as Error).message);
    }
  };
  useEffect(() => {
    brief.start(() => api.post<Job>(`/memory/entity/${entityId}/brief`, {}));
    loadRecall();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entityId]);
  const b = brief.result?.brief;
  return (
    <section className="card memory-card">
      <div className="card-head">
        <h3><BrainCircuit size={15} className="ai-t" />{title}<span className="sub">Hindsight · as of the business date</span></h3>
        <div className="seg">
          <button className={tab === "brief" ? "on" : ""} onClick={() => setTab("brief")}>Brief</button>
          <button className={tab === "recall" ? "on" : ""} onClick={() => setTab("recall")}>Memories{recall ? ` (${recall.results.length})` : ""}</button>
          <button className={tab === "note" ? "on" : ""} onClick={() => setTab("note")}>Add note</button>
        </div>
      </div>
      <div className="card-body">
        {tab === "brief" && (
          <>
            {brief.running && <div className="row muted small" style={{ marginBottom: 8 }}><Spinner />Reflecting over {entityId}'s history…</div>}
            {brief.running && <Loading />}
            <ErrorBox error={brief.error} />
            {b && (
              <div className="stack" style={{ gap: 12 }}>
                <div className="row between">
                  <Status value={`confidence ${b.confidence}`} tone={b.confidence === "high" ? "good" : b.confidence === "medium" ? "warn" : "plain"} />
                  <span className="small muted">{brief.result!.evidence_count} memories · {brief.result!.directives?.length ?? 0} directives · {brief.result!.cached ? `cached ${fmt.ago(brief.result!.cached_at ?? brief.result!.generated_at)}` : `${(brief.result!.latency_ms / 1000).toFixed(1)}s`}
                    <button className="btn ghost sm" onClick={() => brief.start(() => api.post<Job>(`/memory/entity/${entityId}/brief`, { force: true }))}><RefreshCw size={12} /></button></span>
                </div>
                <div className="prose"><Linkify text={b.summary} /></div>
                <List title="Patterns" items={b.patterns} icon={<History size={12} />} />
                <List title="Risks" items={b.risks} icon={<ShieldAlert size={12} />} />
                {b.past_decisions?.length > 0 && (
                  <div>
                    <div className="mem-label"><CheckCircle2 size={12} />Past decisions</div>
                    <ul className="mem-list">{b.past_decisions.map((d: Row, i: number) => <li key={i}><b>{d.when ? `${d.when}: ` : ""}</b><Linkify text={d.what} /> → <i><Linkify text={d.outcome} /></i></li>)}</ul>
                  </div>
                )}
                <List title="Open commitments" items={b.open_commitments} />
                <List title="Recommendations" items={b.recommendations} icon={<Sparkles size={12} />} />
                {brief.result!.evidence?.length > 0 && (
                  <details><summary className="small muted" style={{ cursor: "pointer" }}>Evidence ({brief.result!.evidence.length} of {brief.result!.evidence_count})</summary>
                    {brief.result!.evidence.map((h: Row, i: number) => <MemoryHit key={i} h={h} compact />)}</details>
                )}
              </div>
            )}
          </>
        )}
        {tab === "recall" && (
          <>
            <div className="row" style={{ flexWrap: "nowrap", marginBottom: 10 }}>
              <input className="input grow" placeholder={`Ask memory about ${entityId}…`} value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && loadRecall(q)} />
              <button className="btn sm" onClick={() => loadRecall(q)}><Search size={13} />Recall</button>
            </div>
            <ErrorBox error={recallErr} />
            {!recall && !recallErr && <Loading />}
            {recall && (
              <>
                <div className="row small muted" style={{ marginBottom: 6 }}>
                  {Object.entries(recall.by_type).map(([k, v]) => <Status key={k} value={`${v} ${TYPE_LABEL[k] ?? k}s`} tone={TYPE_TONE[k]} />)}
                  <span>{recall.latency_ms} ms</span>
                </div>
                <div className="mem-scroll">{recall.results.map((h: Row) => <MemoryHit key={h.id} h={h} />)}</div>
              </>
            )}
          </>
        )}
        {tab === "note" && (
          <div className="stack">
            <div className="small muted">Notes become memories: they are recorded in the audit log and retained to Hindsight, so future briefs, advice and the decision agent can use them.</div>
            <textarea className="input" placeholder={`e.g. Called ${entityId} — they confirmed the backlog clears by mid-January.`} value={note} onChange={(e) => setNote(e.target.value)} />
            <div><button className="btn primary" disabled={!!busy || note.trim().length < 5} onClick={async () => {
              if (await run("note", () => api.post("/memory/notes", { entity_id: entityId, note }), "Note saved — syncing to memory")) setNote("");
            }}><MessageSquarePlus size={14} />Save to memory</button></div>
          </div>
        )}
      </div>
    </section>
  );
}

const VERDICT: Record<string, { tone: string; icon: ReactNode; label: string }> = {
  proceed: { tone: "good", icon: <CheckCircle2 size={16} />, label: "Memory says: proceed" },
  caution: { tone: "warn", icon: <ShieldAlert size={16} />, label: "Memory says: caution" },
  stop: { tone: "bad", icon: <OctagonAlert size={16} />, label: "Memory says: stop" },
};

/** Pre-action guardrail: asks memory whether an action is wise given what happened before. */
export function AdviceBox({ action, entityIds, details, auto = true }: { action: string; entityIds: string[]; details?: string; auto?: boolean }) {
  const j = useJob<Row>();
  const key = `${action}|${entityIds.join(",")}|${details ?? ""}`;
  const ask = () => j.start(() => api.post<Job>("/memory/advise", { action, entity_ids: entityIds, details: details ?? "" }));
  useEffect(() => {
    if (auto && entityIds.every(Boolean) && entityIds.length) ask();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  const a = j.result?.advice;
  const v = a ? VERDICT[a.verdict] ?? VERDICT.caution : null;
  return (
    <div className={`advice ${v ? v.tone : ""}`}>
      {!j.job && !auto && <button className="btn sm ai" onClick={ask}><BrainCircuit size={13} />Check with memory</button>}
      {j.running && <div className="row small muted"><Spinner />Checking what happened before with {entityIds.join(", ")}…</div>}
      <ErrorBox error={j.error} />
      {a && v && (
        <div className="stack" style={{ gap: 8 }}>
          <div className="row" style={{ fontWeight: 700 }}>{v.icon}{v.label}<span className="small muted" style={{ fontWeight: 400 }}>· confidence {a.confidence} · {j.result!.evidence_count} memories</span></div>
          <div><Linkify text={a.headline} /></div>
          {a.reasons?.length > 0 && <ul className="mem-list">{a.reasons.slice(0, 5).map((r: string, i: number) => <li key={i}><Linkify text={r} /></li>)}</ul>}
          {a.commitments_at_risk?.length > 0 && <div className="small bad-t"><b>Commitments at risk:</b> {a.commitments_at_risk.join("; ")}</div>}
          {a.precedents?.length > 0 && (
            <details><summary className="small" style={{ cursor: "pointer" }}>Precedents ({a.precedents.length})</summary>
              <ul className="mem-list">{a.precedents.map((p: Row, i: number) => <li key={i}>{p.when && <b>{p.when}: </b>}<Linkify text={p.what_happened} />{p.lesson && <> — <i>{p.lesson}</i></>}</li>)}</ul></details>
          )}
          {a.alternative && <div className="small"><b>Alternative:</b> <Linkify text={a.alternative} /></div>}
        </div>
      )}
    </div>
  );
}
