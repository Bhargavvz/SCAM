import { useEffect, useMemo, useState } from "react";
import { Row } from "../../api";
import { money, Panel } from "../../components/common";
import { HBars } from "../../components/charts";
import { Badge, ErrorBox, KV, LineChart, Loading, MemoryPanel, PageHead, Pager, Seg, Series, Table } from "../../components/scm";
import { EntityLink, go, Linkify, Route, useAsOf, useData } from "../../nav";
import { scm } from "../../scm";

const COLORS = ["#16181d", "#1d4ed8", "#1c6b3f", "#a52a2a", "#c9962c", "#6b4fa0", "#2f7f86"];

// ---------------------------------------------------------------- materials
export function MaterialList() {
  const { asOf } = useAsOf();
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  useEffect(() => setOffset(0), [q, asOf]);
  const { data, error, loading } = useData(() => scm.materials({ as_of: asOf, q, limit: 60, offset }), [asOf, q, offset]);
  return (
    <div>
      <PageHead title="Materials" sub="Stock across plants from the latest weekly snapshot, sorted by shortage." />
      <div className="toolbar"><input className="search" placeholder="Material id or name" value={q} onChange={(e) => setQ(e.target.value)} /></div>
      <ErrorBox error={error} />
      {loading && !data ? <Loading /> : data && (
        <Panel>
          <Table rows={data.rows} onRow={(r) => go(`/materials/${r.rm_id}`)} cols={[
            { key: "rm_id", label: "material" }, "rm_name", { key: "rm_category", label: "category" }, "criticality",
            { key: "is_single_source", label: "sourcing", render: (r) => (r.is_single_source ? <Badge tone="warn">single source</Badge> : "multi") },
            { key: "on_hand", label: "on hand", num: true }, { key: "safety_stock", label: "safety", num: true },
            { key: "plants_below_safety", label: "plants short", num: true, render: (r) => (Number(r.plants_below_safety) ? <Badge tone="bad">{String(r.plants_below_safety)}</Badge> : "0") },
            { key: "min_days_of_cover", label: "min cover (d)", num: true },
          ]} />
          <Pager total={data.total} limit={data.limit} offset={data.offset} onChange={setOffset} />
        </Panel>
      )}
    </div>
  );
}

export function MaterialDetail({ id }: { id: string }) {
  const { asOf } = useAsOf();
  const { data, error, loading } = useData(() => scm.material(id, asOf), [id, asOf]);
  const series = useMemo<Series[]>(() => {
    const t = (data?.trend as Row[]) ?? [];
    const plants = Array.from(new Set(t.map((r) => String(r.plant_id))));
    return plants.slice(0, 6).map((p, i) => ({
      name: p, color: COLORS[i % COLORS.length],
      points: t.filter((r) => r.plant_id === p).map((r) => ({ x: String(r.snapshot_date), y: Number(r.on_hand_units) })),
    }));
  }, [data]);
  if (loading && !data) return <Loading />;
  if (error) return <ErrorBox error={error} />;
  const m = data!.material as Row;
  return (
    <div>
      <PageHead crumbs={[["Materials", "#/materials"]]} title={<>{String(m.rm_name)} <span className="muted mono small">{id}</span></>}
        sub={`${m.rm_category} · ${m.uom} · criticality ${m.criticality}${m.is_single_source ? " · single source" : ""}${m.is_hazmat ? " · hazmat" : ""}`} />
      <div className="grid split">
        <div>
          <Panel title="Stock by plant" right={<span className="muted small">latest weekly snapshot</span>}>
            <HBars items={(data!.stock as Row[]).map((s) => ({
              label: String(s.plant_id), value: Number(s.on_hand_units),
              display: `${Math.round(Number(s.on_hand_units)).toLocaleString()} / safety ${Math.round(Number(s.safety_stock_units)).toLocaleString()} · ${s.days_of_cover}d`,
              tone: Number(s.on_hand_units) < Number(s.safety_stock_units) ? "bad" : "good",
            }))} />
          </Panel>
          <Panel title="On-hand trend" right={<span className="muted small">26 weeks</span>}><LineChart series={series} /></Panel>
          <Panel title="Qualified suppliers">
            <Table rows={data!.suppliers as Row[]} empty="No supplier has a valid price today." cols={[{ key: "supplier_id", label: "supplier" }, "supplier_name", { key: "sourcing_rank", label: "rank", num: true }, { key: "allocation_pct", label: "share", num: true }, { key: "lead_time_days", label: "lead time", num: true }, { key: "unit_price", num: true }]} />
          </Panel>
          <Panel title="Open orders">
            <Table rows={data!.open_pos as Row[]} empty="No open orders." cols={[{ key: "rm_purchase_order_id", label: "order" }, { key: "supplier_id", label: "supplier" }, "plant_id", "expected_at", { key: "slip_days", label: "slip", num: true }]} />
          </Panel>
          <Panel title="Runs that consume it" right={<span className="muted small">next 30 days</span>}>
            <Table rows={data!.upcoming_runs as Row[]} empty="None planned." cols={[{ key: "production_run_id", label: "run" }, "plant_id", "product_id", "planned_start", { key: "planned_qty", num: true }]} />
          </Panel>
        </div>
        <div>
          <MemoryPanel entityId={id} />
          <Panel title="Substitutes">
            <Table rows={data!.substitutes as Row[]} empty="No approved substitute." cols={[{ key: "rm_id", label: "material" }, "rm_name", "criticality"]} />
          </Panel>
          <Panel title="Past disruptions"><Table rows={data!.events as Row[]} empty="None." cols={[{ key: "event_id", label: "event" }, "event_type", "detected_at"]} /></Panel>
          <Panel title="Past decisions"><Table rows={data!.decisions as Row[]} empty="None." cols={[{ key: "decision_id", label: "decision" }, "decision_type", "outcome_label"]} /></Panel>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- production
export function Production({ route }: { route: Route }) {
  const { asOf } = useAsOf();
  const [plant, setPlant] = useState(route.query.get("plant") ?? "");
  const [q, setQ] = useState(route.query.get("q") ?? "");
  const { data, error, loading } = useData(() => scm.production({ as_of: asOf, days: 30, plant_id: plant }), [asOf, plant]);
  const risky = useMemo(() => new Set(((data?.at_risk as Row[]) ?? []).map((r) => String(r.production_run_id))), [data]);
  const runs = ((data?.runs as Row[]) ?? []).filter((r) => !q || String(r.production_run_id).includes(q.toUpperCase()) || String(r.sku ?? "").toLowerCase().includes(q.toLowerCase()));
  return (
    <div>
      <PageHead title="Production plan" sub="Runs from last week through the next 30 days. Runs whose materials are below safety stock are flagged." />
      <ErrorBox error={error} />
      {loading && !data ? <Loading /> : data && (
        <>
          <Panel title="Plants">
            <Table rows={data.plants as Row[]} onRow={(r) => setPlant(plant === r.plant_id ? "" : String(r.plant_id))}
              rowClass={(r) => (r.plant_id === plant ? "selected" : undefined)}
              cols={["plant_id", "plant_name", "region", { key: "capacity_units_per_week", label: "capacity / wk", num: true }, { key: "runs_planned", label: "runs next 30d", num: true }, { key: "rm_delays_90d", label: "material delays 90d", num: true }]} />
          </Panel>
          <div className="grid split">
            <Panel title={`Runs${plant ? ` at ${plant}` : ""}`} right={<input className="search sm" placeholder="Run or SKU" value={q} onChange={(e) => setQ(e.target.value)} />}>
              <Table rows={runs} rowClass={(r) => (risky.has(String(r.production_run_id)) ? "risk" : undefined)} cols={[
                { key: "production_run_id", label: "run" }, "plant_id", "sku", "planned_start", { key: "planned_qty", label: "qty", num: true },
                { key: "state", render: (r) => r.actual_start ? (Number(r.delay_days) > 0 ? <Badge tone="warn">started +{String(r.delay_days)}d</Badge> : <Badge tone="good">started</Badge>)
                  : risky.has(String(r.production_run_id)) ? <Badge tone="bad">material short</Badge> : <Badge>planned</Badge> },
                { key: "rm_shortage_rm_id", label: "short of" },
              ]} />
            </Panel>
            <div>
              <Panel title="Material shortfalls">
                <Table rows={data.at_risk as Row[]} empty="No run is short." cols={[{ key: "production_run_id", label: "run" }, { key: "rm_id", label: "material" }, { key: "on_hand_units", label: "on hand", num: true }, { key: "safety_stock_units", label: "safety", num: true }]} />
              </Panel>
              <Panel title="Why runs were late" right={<span className="muted small">last 90 days</span>}>
                <HBars items={(data.delay_reasons as Row[]).map((r) => ({ label: String(r.delay_reason_code).replace(/_/g, " "), value: Number(r.n), tone: r.delay_reason_code === "rm_shortage" ? "bad" : "ink" }))} />
              </Panel>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- demand
export function Demand() {
  const { asOf } = useAsOf();
  const { data, error, loading } = useData(() => scm.demand(asOf), [asOf]);
  const [cat, setCat] = useState<string>("");
  const weekly = (data?.weekly as Row[]) ?? [];
  const cats = Array.from(new Set(weekly.map((r) => String(r.category))));
  const sel = cat || cats[0] || "";
  const rows = weekly.filter((r) => r.category === sel);
  return (
    <div>
      <PageHead title="Demand" sub="Forecast against actual demand by product category, and where the forecast has been biased." />
      <ErrorBox error={error} />
      {loading && !data ? <Loading /> : data && (
        <div className="grid split">
          <Panel title="Forecast vs actual" right={<select value={sel} onChange={(e) => setCat(e.target.value)}>{cats.map((c) => <option key={c}>{c}</option>)}</select>}>
            <LineChart height={220} series={[
              { name: "actual", color: "#16181d", points: rows.map((r) => ({ x: String(r.week_start), y: Number(r.actual) })) },
              { name: "forecast", color: "#1d4ed8", dashed: true, points: rows.map((r) => ({ x: String(r.week_start), y: Number(r.forecast) })) },
              { name: "unfulfilled", color: "#a52a2a", points: rows.map((r) => ({ x: String(r.week_start), y: Number(r.unfulfilled) })) },
            ]} />
          </Panel>
          <Panel title="Forecast bias, last 12 months">
            <HBars items={(data.bias_by_category as Row[]).map((r) => ({
              label: String(r.category), value: Math.abs(Number(r.bias)),
              display: `${Number(r.bias) > 0 ? "+" : ""}${Math.round(Number(r.bias) * 100)}%`,
              tone: Math.abs(Number(r.bias)) > 0.1 ? "bad" : "good",
            }))} />
            <div className="muted small" style={{ marginTop: 8 }}>+ means we forecast more than customers bought.</div>
          </Panel>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- commitments
type CStatus = "open" | "due" | "overdue" | "fulfilled" | "breached" | "renegotiated" | "all";

export function Commitments({ route }: { route: Route }) {
  const { asOf } = useAsOf();
  const [status, setStatus] = useState<CStatus>((route.query.get("status") as CStatus) ?? "open");
  const [q, setQ] = useState(route.query.get("q") ?? "");
  const [offset, setOffset] = useState(0);
  useEffect(() => setOffset(0), [status, q, asOf]);
  const { data, error, loading } = useData(() => scm.commitments({ as_of: asOf, status, q, limit: 50, offset }), [asOf, status, q, offset]);
  return (
    <div>
      <PageHead title="Commitments" sub="Promises made to and by suppliers and customers. The agent refuses any action that would break an open one." />
      <div className="toolbar">
        <Seg value={status} onChange={setStatus} options={[["open", "Open"], ["due", "Due in 14 days"], ["overdue", "Overdue"], ["fulfilled", "Fulfilled"], ["breached", "Breached"], ["renegotiated", "Renegotiated"], ["all", "All"]]} />
        <input className="search" placeholder="Id, counterparty or text" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      <ErrorBox error={error} />
      {loading && !data ? <Loading /> : data && (
        <Panel right={<div className="row small">{data.summary.map((s) => <span key={String(s.status)} className="muted">{String(s.status)} {Number(s.n).toLocaleString()}</span>)}</div>}>
          <Table rows={data.rows} cols={[
            { key: "commitment_id", label: "id" }, { key: "counterparty_id", label: "to" }, "made_by_role", "made_at", "due_date",
            { key: "commitment_text", label: "promise", render: (r) => <span>{r.is_volume_commitment ? <Badge tone="warn">volume</Badge> : null} <Linkify text={String(r.commitment_text)} /></span> },
            { key: "status", render: (r) => <Badge tone={r.status === "fulfilled" ? "good" : r.status === "breached" ? "bad" : r.status === "open" ? "accent" : "muted"}>{String(r.status)}</Badge> },
            { key: "decision_id", label: "from decision" },
            { key: "source", render: (r) => (r.source === "live" ? <Badge tone="ink">agent</Badge> : <span className="muted">history</span>) },
          ]} />
          <Pager total={data.total} limit={data.limit} offset={data.offset} onChange={setOffset} />
        </Panel>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- decisions
export function Decisions({ route }: { route: Route }) {
  const { asOf } = useAsOf();
  const [source, setSource] = useState<"" | "live" | "historical">((route.query.get("source") as "live") ?? "");
  const [offset, setOffset] = useState(0);
  useEffect(() => setOffset(0), [source, asOf]);
  const { data, error, loading } = useData(() => scm.decisions({ as_of: asOf, source, limit: 50, offset }), [asOf, source, offset]);
  return (
    <div>
      <PageHead title="Decision log" sub="Every response to a disruption, what it was expected to cost, and what it actually cost once the outcome was known." />
      <div className="toolbar"><Seg value={source} onChange={setSource} options={[["", "All"], ["live", "Made with the agent"], ["historical", "History"]]} /></div>
      <ErrorBox error={error} />
      {loading && !data ? <Loading /> : data && (
        <>
          <Panel title="How each response type turns out" right={<span className="muted small">actual ÷ expected cost; above 1 means it cost more than planned</span>}>
            <Table rows={data.accuracy} cols={[
              "decision_type", { key: "n", num: true },
              { key: "success_rate", label: "success", num: true, render: (r) => `${Math.round(Number(r.success_rate) * 100)}%` },
              { key: "cost_ratio", label: "cost ratio", num: true, render: (r) => <span className={Number(r.cost_ratio) > 1.2 ? "bad-t" : undefined}>{String(r.cost_ratio ?? "-")}×</span> },
              { key: "extra_stockout_days", label: "extra stockout days", num: true },
            ]} />
          </Panel>
          <Panel>
            <Table rows={data.rows} onRow={(r) => go(`/decisions/${r.decision_id}`)} cols={[
              { key: "decision_id", label: "decision" }, { key: "event_id", label: "event" }, "decided_at", "decision_type",
              { key: "expected_cost", label: "expected", num: true, render: (r) => money(Number(r.expected_cost)) },
              { key: "actual_cost", label: "actual", num: true, render: (r) => (r.actual_cost === null ? <span className="muted">pending</span> : money(Number(r.actual_cost))) },
              { key: "outcome_label", label: "outcome", render: (r) => (r.outcome_label ? <Badge tone={r.outcome_label === "success" ? "good" : r.outcome_label === "failed" ? "bad" : "warn"}>{String(r.outcome_label)}</Badge> : <span className="muted">not yet known</span>) },
              { key: "source", render: (r) => (r.source === "live" ? <Badge tone="ink">agent</Badge> : <span className="muted">history</span>) },
            ]} />
            <Pager total={data.total} limit={data.limit} offset={data.offset} onChange={setOffset} />
          </Panel>
        </>
      )}
    </div>
  );
}

export function DecisionDetail({ id }: { id: string }) {
  const { asOf } = useAsOf();
  const { data: d, error, loading } = useData(() => scm.decision(id, asOf), [id, asOf]);
  if (loading && !d) return <Loading />;
  if (error) return <ErrorBox error={error} />;
  const ev = d!.event as Row | null;
  return (
    <div>
      <PageHead crumbs={[["Decision log", "#/decisions"]]} title={<>{String(d!.decision_type).replace(/_/g, " ")} <span className="muted mono small">{id}</span></>}
        sub={<>Decided {String(d!.decided_at)} by {String(d!.decided_by_role ?? "planner")}{ev ? <> for <EntityLink id={String(ev.event_id)}>{String(ev.title ?? ev.event_id)}</EntityLink></> : null}</>} />
      <div className="grid split">
        <div>
          <Panel title="Rationale"><p className="prose"><Linkify text={String(d!.rationale_text ?? "-")} /></p></Panel>
          <Panel title="Options considered">
            <Table rows={(d!.options as Row[]) ?? []} empty="Not recorded." cols={Object.keys(((d!.options as Row[]) ?? [])[0] ?? { action: 1 }).slice(0, 7)} />
          </Panel>
          {d!.lesson_text ? <Panel title="Lesson"><p className="prose">{String(d!.lesson_text)}</p></Panel> : null}
        </div>
        <div>
          <Panel title="Expected vs actual">
            <KV items={[
              ["expected cost", money(Number(d!.expected_cost))], ["actual cost", d!.actual_cost === null ? "pending" : money(Number(d!.actual_cost))],
              ["expected stockout", `${d!.expected_stockout_days ?? "-"} days`], ["actual stockout", d!.actual_stockout_days === null ? "pending" : `${d!.actual_stockout_days} days`],
              ["outcome", String(d!.outcome_label ?? "not yet known")], ["attribution", String(d!.outcome_attribution ?? "-")],
            ]} />
          </Panel>
          <Panel title="Commitments it created"><Table rows={d!.commitments as Row[]} empty="None." cols={["commitment_id", "counterparty_id", "due_date", "status"]} /></Panel>
        </div>
      </div>
    </div>
  );
}
