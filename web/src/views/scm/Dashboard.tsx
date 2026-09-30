import { Panel } from "../../components/common";
import { ErrorBox, Kpi, LineChart, Loading, PageHead, Severity, Table } from "../../components/scm";
import { EntityLink, useAsOf, useData } from "../../nav";
import { scm } from "../../scm";

export function Dashboard() {
  const { asOf } = useAsOf();
  const { data: d, error, loading } = useData(() => scm.dashboard(asOf), [asOf]);
  const k = d?.kpis ?? {};
  return (
    <div>
      <PageHead title="Control tower" sub={`Operations as of ${asOf}. Everything below is computed from records that existed on that date.`} />
      <ErrorBox error={error} />
      {loading && !d && <Loading />}
      {d && (
        <>
          <div className="kpis">
            <Kpi label="Open disruptions" value={k.open_disruptions} sub={`${k.new_disruptions_7d} new this week`} tone={k.open_disruptions > 0 ? "bad" : undefined} href="#/disruptions" />
            <Kpi label="Slipped purchase orders" value={k.slipped_pos} sub={`${k.overdue_pos} overdue · ${k.open_pos.toLocaleString()} open`} tone="warn" href="#/pos?status=slipped" />
            <Kpi label="Runs short of material" value={k.runs_at_risk} sub="next 21 days, stock below safety" tone={k.runs_at_risk ? "warn" : "good"} href="#/production" />
            <Kpi label="Commitments due" value={k.commitments_due_14d} sub="next 14 days" href="#/commitments?status=due" />
            <Kpi label="Decision success rate" value={`${Math.round((k.decision_success_rate ?? 0) * 100)}%`} sub={`${k.decisions_logged_live} decided with the agent`} tone="good" href="#/decisions" />
          </div>

          <div className="grid cols-2">
            <Panel title="Disruptions per month" right={<span className="muted small">last 12 months</span>}>
              <LineChart series={[{ name: "disruptions", color: "#a52a2a", points: d.trend_disruptions.map((r) => ({ x: r.month, y: r.n })) }]} />
            </Panel>
            <Panel title="Supplier on-time in full" right={<span className="muted small">all suppliers, monthly average</span>}>
              <LineChart yFmt={(v) => `${Math.round(v * 100)}%`}
                series={[{ name: "OTIF", color: "#1c6b3f", points: d.trend_otif.map((r) => ({ x: r.month, y: r.otif })) }]} />
            </Panel>
          </div>

          <div className="grid cols-2">
            <Panel title="Latest disruptions" right={<a href="#/disruptions" className="small">all disruptions →</a>}>
              <Table rows={d.recent_disruptions} empty="No disruptions detected." cols={[
                { key: "event_id", label: "event" },
                { key: "title", render: (r) => <a href={`#/disruptions/${r.event_id}`}>{String(r.title ?? r.event_type)}</a> },
                { key: "severity", render: (r) => <Severity v={r.severity} /> },
                { key: "detected_at", label: "detected" },
                { key: "end_date", label: "status", render: (r) => (r.end_date ? <span className="muted">closed</span> : <b className="bad-t">open</b>) },
              ]} />
            </Panel>
            <Panel title="Supplier watchlist" right={<span className="muted small">open POs that slipped</span>}>
              <Table rows={d.supplier_watchlist} empty="No supplier has slipped orders." cols={[
                { key: "supplier_id", label: "supplier" },
                "supplier_name",
                { key: "open_pos", label: "open", num: true },
                { key: "slipped", num: true },
                { key: "overdue", num: true },
              ]} />
            </Panel>
          </div>

          <div className="grid cols-2">
            <Panel title="Production runs short of material" right={<a href="#/production" className="small">production plan →</a>}>
              <Table rows={d.runs_at_risk} empty="Every upcoming run has stock above safety level." cols={[
                { key: "production_run_id", label: "run" }, "plant_id", "planned_start",
                { key: "rm_id", label: "material" },
                { key: "on_hand_units", label: "on hand", num: true },
                { key: "safety_stock_units", label: "safety", num: true },
              ]} />
            </Panel>
            <Panel title="Commitments coming due" right={<a href="#/commitments" className="small">all commitments →</a>}>
              <Table rows={d.commitments_due} empty="Nothing due." cols={[
                { key: "commitment_id", label: "id" }, { key: "counterparty_id", label: "to" }, "due_date",
                { key: "commitment_text", label: "promise", render: (r) => <span className="clip">{String(r.commitment_text)}</span> },
              ]} />
            </Panel>
          </div>

          {d.live_decisions.length > 0 && (
            <Panel title="Decisions made with the agent" right={<a href="#/decisions?source=live" className="small">decision log →</a>}>
              <Table rows={d.live_decisions} cols={[
                { key: "decision_id", label: "decision" }, { key: "event_id", label: "event", render: (r) => <EntityLink id={String(r.event_id)} /> },
                "decided_at", "decision_type",
                { key: "expected_cost", label: "expected cost", num: true, render: (r) => `$${Math.round(Number(r.expected_cost)).toLocaleString()}` },
                { key: "expected_stockout_days", label: "stockout days", num: true },
              ]} />
            </Panel>
          )}
        </>
      )}
    </div>
  );
}
