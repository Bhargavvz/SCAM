import { AlertOctagon, BrainCircuit, CheckCircle2, ShieldAlert, Sparkles, Zap } from "lucide-react";
import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Bars, Lines } from "../components/charts";
import { AdviceBox, MemoryPanel } from "../components/Memory";
import { Card, DataTable, EntityLink, ErrorBox, Kpi, KV, Linkify, Loading, Markdown, PageHead, Pager, Seg, Spinner, Status, useAction } from "../components/ui";
import { api, fmt, Job, Row, useDebounced, useJob, useQuery } from "../lib/api";

export function RiskPage() {
  const nav = useNavigate();
  const [status, setStatus] = useState("open");
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const dq = useDebounced(q);
  useEffect(() => setOffset(0), [status, dq]);
  const { data: s } = useQuery<Row>("/risk/summary");
  const { data, error, loading } = useQuery<Row>("/risk/events", { status, q: dq, limit: 40, offset });
  return (
    <div>
      <PageHead title="Risk & disruptions" icon={<AlertOctagon size={20} />}
        sub="Supplier delays, quality holds, congestion and shortages. The decision agent recalls what happened last time from memory, simulates every response, checks commitments, and recommends one — accepting it updates the orders and becomes a memory." />
      {s && (
        <>
          <div className="kpis">
            <Kpi label="Open disruptions" value={fmt.n(s.open)} tone={s.open ? "bad" : "good"} icon={<AlertOctagon size={15} />} />
            <Kpi label="New · 30 days" value={fmt.n(s.new_30d)} tone="warn" />
            <Kpi label="Decided with the agent" value={fmt.n(s.decided_in_meridian)} tone="good" icon={<BrainCircuit size={15} />} sub="written back to memory" />
          </div>
          <div className="grid g2">
            <Card title="Disruptions per month" sub="count and average severity">
              <Lines data={s.monthly} x="month" height={210} series={[{ key: "n", name: "Disruptions", color: "#dc2626" }]} />
            </Card>
            <Card title="How past responses turned out" sub="success rate and actual / expected cost" flush>
              <DataTable rows={s.outcomes} cols={[
                { key: "decision_type", label: "Response", render: (r) => fmt.label(r.decision_type) }, { key: "n", label: "Decisions", num: true },
                { key: "success_rate", label: "Success", num: true, render: (r) => fmt.pct(r.success_rate, 0) },
                { key: "cost_ratio", label: "Cost vs plan", num: true, render: (r) => <span className={r.cost_ratio > 1.3 ? "bad-t" : undefined}>{r.cost_ratio}×</span> }]} />
            </Card>
          </div>
        </>
      )}
      <Card flush>
        <div className="toolbar">
          <input className="input" style={{ width: 260 }} placeholder="Event, title or origin" value={q} onChange={(e) => setQ(e.target.value)} />
          <Seg value={status} onChange={setStatus} options={[["open", "Open"], ["recent", "Last 60 days"], ["all", "All"]]} />
        </div>
        <ErrorBox error={error} />
        {loading && !data ? <Loading /> : (
          <DataTable rows={data?.rows ?? []} onRow={(r) => nav(`/risk/${r.event_id}`)} cols={[
            { key: "event_id", label: "Event", render: (r) => <span className="mono">{r.event_id}</span> },
            { key: "title", wrap: true }, { key: "event_type", label: "Type", render: (r) => fmt.label(r.event_type) },
            { key: "severity", num: true, render: (r) => r.severity === null ? <span className="muted">pending</span> : <b className={r.severity >= 4 ? "bad-t" : r.severity >= 3 ? "warn-t" : undefined}>{r.severity}</b> },
            { key: "origin_entity_id", label: "Origin" }, { key: "detected_at", label: "Detected" },
            { key: "state", label: "Status", render: (r) => r.decision_id ? <Status value="decided" tone="good" /> : r.resolution_cached ? <Status value="analysed" tone="ai" /> : r.end_date ? <Status value="closed" tone="plain" /> : <Status value="open" tone="bad" /> },
            { key: "has_po", label: "Agent", render: (r) => r.has_po ? <Status value="simulatable" tone="info" /> : <span className="muted small">memory only</span> },
          ]} />
        )}
        <Pager page={data} onOffset={setOffset} />
      </Card>
    </div>
  );
}

function OptionsTable({ card }: { card: Row }) {
  const opts: Row[] = card.options ?? [];
  const feasible = opts.filter((o) => o.feasible && o.cost != null);
  return (
    <>
      <Bars data={feasible.map((o) => ({ ...o, label: fmt.label(o.action) }))} x="label" horizontal height={40 + feasible.length * 34} yFmt={fmt.money}
        colorBy={(r) => (r.action === card.recommended_action ? "#059669" : r.breaches_commitments?.length ? "#dc2626" : "#94a3b8")}
        series={[{ key: "cost", name: "Risk-adjusted cost" }]} />
      <DataTable rows={opts} cols={[
        { key: "action", render: (r) => <b>{fmt.label(r.action)}</b> },
        { key: "cost", label: "Cost", num: true, render: (r) => r.cost == null ? "–" : fmt.moneyFull(r.cost) },
        { key: "stockout_days", label: "Stockout days", num: true, render: (r) => r.stockout_days ?? "–" },
        { key: "arrival", render: (r) => fmt.date(r.arrival) }, { key: "risk", render: (r) => r.risk ? <Status value={r.risk} /> : "–" },
        { key: "state", label: "", render: (r) => !r.feasible ? <Status value="not feasible" tone="plain" /> : r.breaches_commitments?.length ? <Status value="breaks commitment" tone="bad" />
          : r.action === card.recommended_action ? <Status value="recommended" tone="good" /> : r.action === card.simulator_best_action ? <Status value="cheapest" tone="info" /> : null },
        { key: "note", wrap: true, render: (r) => <span className="small"><Linkify text={r.note ?? ""} /></span> },
      ]} />
    </>
  );
}

export function RiskDetail() {
  const { id = "" } = useParams();
  const { data: d, error, loading, reload } = useQuery<Row>(`/risk/events/${id}`);
  const j = useJob<Row>();
  const { run, busy } = useAction();
  const [accepted, setAccepted] = useState<Row | null>(null);
  if (loading && !d) return <Loading />;
  if (error) return <ErrorBox error={error} />;
  const ev = d!.event;
  const ctx = d!.context;
  const card: Row | null = accepted?.card ?? j.result ?? d!.resolution;
  const acc = card?.accepted;
  return (
    <div>
      <PageHead crumbs={[["Risk & disruptions", "/risk"]]} title={<>{ev.title} <span className="muted mono" style={{ fontSize: 14 }}>{id}</span></>}
        sub={<>{fmt.label(ev.event_type)} · detected {ev.detected_at} · origin <EntityLink id={ev.origin_entity_id} /> · {ev.end_date ? `closed ${ev.end_date}` : <b className="bad-t">open</b>}</>}
        actions={ctx && !acc && <button className="btn ai" disabled={j.running} onClick={() => j.start(() => api.post<Job>(`/risk/events/${id}/resolve`, {}) as Promise<Job>)}>
          {j.running ? <Spinner /> : <Sparkles size={14} />}{card ? "Re-run agent" : "Resolve with agent"}</button>} />
      <div className="grid g-main">
        <div className="stack">
          {j.running && (
            <Card title="Decision agent working" icon={<BrainCircuit size={15} className="ai-t" />}>
              <div className="stack small">
                <div className="row"><Spinner />Recalling precedents from Hindsight, reading as-of ground truth, simulating options, checking commitments… {j.job?.elapsed_s}s</div>
                <Loading />
              </div>
            </Card>
          )}
          <ErrorBox error={j.error} />
          {!card && !j.running && (
            <Card title="Decision agent" icon={<BrainCircuit size={15} className="ai-t" />}>
              {ctx ? <div className="muted">The agent will analyse <EntityLink id={ctx.rpo_id} /> from <EntityLink id={ctx.supplier_id} /> ({ctx.rm_id} for plant {ctx.plant_id}) and the runs that depend on it: {ctx.run_ids.map((r: string) => <EntityLink key={r} id={r} />)}. Press “Resolve with agent”.</div>
                : <div className="muted">This event has no slipped material order with dependent production runs, so there is nothing to simulate. Use the memory panel to see what the organisation knows about it.</div>}
            </Card>
          )}
          {card && (
            <>
              <Card title="Recommendation" icon={<Zap size={15} className="ai-t" />} sub={`${card.memory_hits} memories · ${card.latency_s}s · ${card.usage?.calls ?? "?"} LLM calls`}
                actions={acc ? <Status value={`accepted as ${acc.decision_id}`} tone="good" /> : card.recommended_action && <button className="btn primary" disabled={!!busy} onClick={async () => {
                  const r = await run("acc", () => api.post<Row>(`/risk/events/${id}/accept`), (x: Row) => `Decision ${x.accepted.decision_id} recorded — orders updated, syncing to memory`);
                  if (r) { setAccepted(r); reload(); }
                }}><CheckCircle2 size={14} />Accept & execute</button>}>
                <div className="rec-hero">
                  <div><div className="small muted">Recommended</div><div className="rec-action">{fmt.label(card.recommended_action ?? "none")}</div></div>
                  <div><div className="small muted">Simulator cheapest</div><div className="rec-action muted">{fmt.label(card.simulator_best_action ?? "–")}</div></div>
                  <div><div className="small muted">Confidence</div><Status value={card.confidence} tone={card.confidence === "high" ? "good" : "warn"} /></div>
                  {card.deviates_from_simulator_best && <Status value="deviates from cheapest" tone="warn" />}
                </div>
                {acc && (
                  <div className="ai-box" style={{ marginTop: 12 }}>
                    <h5><CheckCircle2 size={12} />Executed</h5>
                    <div>Decision <EntityLink id={acc.decision_id}>{acc.decision_id}</EntityLink> · commitment <span className="mono">{acc.commitment_id}</span></div>
                    <ul className="mem-list">{acc.changes.map((c: string) => <li key={c}><Linkify text={c} /></li>)}</ul>
                    <div className="small muted">Recorded in the audit log and queued to Hindsight — the next disruption will recall this decision.</div>
                  </div>
                )}
                {!acc && card.recommended_action && ctx && (
                  <div style={{ marginTop: 12 }}><AdviceBox action={`${card.recommended_action.replace(/_/g, " ")} for disruption ${id}`} entityIds={[ctx.rpo_id, ctx.supplier_id, ctx.rm_id]} auto={false} /></div>
                )}
              </Card>
              <Card title="Why" icon={<Sparkles size={15} className="ai-t" />}>
                <Markdown text={card.rationale ?? ""} />
                {card.key_reasons?.length > 0 && <ul className="mem-list">{card.key_reasons.map((r: string, i: number) => <li key={i}><Linkify text={r} /></li>)}</ul>}
                {card.warnings?.length > 0 && <div className="note small" style={{ marginTop: 8 }}>{card.warnings.join(" · ")}</div>}
              </Card>
              <Card title="Options simulated" sub="deterministic cost and stockout model — numbers never come from the LLM" flush><OptionsTable card={card} /></Card>
              {card.precedent_assessments?.length > 0 && (
                <Card title="Precedents from memory" sub="past episodes and whether they apply today" flush>
                  <DataTable rows={card.precedent_assessments} cols={[
                    { key: "doc_id", label: "Memory", render: (r) => <span className="mono small">{r.doc_id ?? r.decision_id ?? "–"}</span> },
                    { key: "applies", label: "Applies", render: (r) => r.applies === false || r.applicable === false ? <Status value="no" tone="warn" /> : <Status value="yes" tone="good" /> },
                    { key: "reason", wrap: true, render: (r) => <Linkify text={r.reason ?? r.assessment ?? r.why ?? JSON.stringify(r).slice(0, 200)} /> }]} />
                </Card>
              )}
              {(card.guardrail_events?.length > 0 || card.open_commitments?.length > 0) && (
                <Card title="Guardrails & commitments" icon={<ShieldAlert size={15} />}>
                  {card.guardrail_events?.map((g: string, i: number) => <div key={i} className="note small" style={{ marginBottom: 6 }}><Linkify text={g} /></div>)}
                  {card.open_commitments?.length > 0 && <DataTable rows={card.open_commitments} cols={[{ key: "commitment_id", label: "Commitment" }, { key: "counterparty_id", label: "With" }, { key: "commitment_text", label: "Promise", wrap: true }, { key: "due_date", label: "Due" }]} />}
                </Card>
              )}
              <Card title="Evidence">
                <div className="row small">{(card.cited_record_ids ?? []).slice(0, 30).map((r: string) => <span key={r} className="pill plain"><EntityLink id={r} /></span>)}</div>
                <div className="small muted" style={{ marginTop: 6 }}>{(card.cited_doc_ids ?? []).length} memory documents cited · {card.dropped_future_hits ?? 0} future-dated memories withheld</div>
              </Card>
            </>
          )}
        </div>
        <div className="stack">
          <Card title="Event">
            <KV items={[["Root cause", fmt.label(ev.root_cause_code ?? "–")], ["Severity", ev.severity ?? "pending"], ["Started", ev.start_date ?? "–"], ["Anchor", ev.anchor_type ?? "–"]]} />
            {ev.root_cause_text && <div className="small" style={{ marginTop: 10 }}><Linkify text={ev.root_cause_text} /></div>}
          </Card>
          {ctx && (
            <Card title="Affected" flush>
              <div style={{ padding: 14 }}><KV items={[["Material PO", <EntityLink id={ctx.rpo_id} />], ["Supplier", <EntityLink id={ctx.supplier_id} />], ["Material", ctx.rm_id], ["Plant", ctx.plant_id]]} /></div>
              <DataTable rows={d!.affected_runs} cols={[{ key: "production_run_id", label: "Run" }, { key: "sku", label: "SKU" }, { key: "planned_start", label: "Planned" }, { key: "planned_qty", label: "Qty", num: true }]} />
            </Card>
          )}
          <MemoryPanel entityId={ctx?.supplier_id ?? ev.origin_entity_id} title={`Memory: ${ctx?.supplier_id ?? ev.origin_entity_id}`} />
          {d!.decisions.length > 0 && <Card title="Decisions on this event" flush><DataTable rows={d!.decisions} cols={[{ key: "decision_id", label: "Decision" }, { key: "decision_type", label: "Action", render: (r) => fmt.label(r.decision_type) }, { key: "decided_by_role", label: "By" }, { key: "outcome_label", label: "Outcome", render: (r) => <Status value={r.outcome_label ?? "pending"} /> }]} /></Card>}
          {d!.links.length > 0 && <Card title="Related events" flush><DataTable rows={d!.links} onRow={(r) => (window.location.href = `/risk/${r.other_id}`)} cols={[{ key: "other_id", label: "Event", render: (r) => <span className="mono">{r.other_id}</span> }, { key: "link_type", label: "Link" }, { key: "other_title", label: "Title", wrap: true }]} /></Card>}
        </div>
      </div>
    </div>
  );
}
