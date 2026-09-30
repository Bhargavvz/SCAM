import { useEffect, useState } from "react";
import { Row } from "../../api";
import { Panel } from "../../components/common";
import { Badge, ErrorBox, Kpi, LineChart, Loading, MemoryPanel, PageHead, Pager, Seg, Table } from "../../components/scm";
import { go, useAsOf, useData } from "../../nav";
import { scm } from "../../scm";

const pct = (v: unknown) => (v === null || v === undefined ? "-" : `${Math.round(Number(v) * 100)}%`);

export function SupplierList() {
  const { asOf } = useAsOf();
  const [sort, setSort] = useState<"slipped" | "otif" | "name">("slipped");
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  useEffect(() => setOffset(0), [sort, q, asOf]);
  const { data, error, loading } = useData(() => scm.suppliers({ as_of: asOf, sort, q, limit: 50, offset }), [asOf, sort, q, offset]);
  return (
    <div>
      <PageHead title="Suppliers" sub="Delivery performance over the last six months, open orders and commitments." />
      <div className="toolbar">
        <Seg value={sort} onChange={setSort} options={[["slipped", "Most slipped"], ["otif", "Lowest OTIF"], ["name", "Name"]]} />
        <input className="search" placeholder="Supplier id or name" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      <ErrorBox error={error} />
      {loading && !data ? <Loading /> : data && (
        <Panel>
          <Table rows={data.rows} onRow={(r) => go(`/suppliers/${r.supplier_id}`)} cols={[
            { key: "supplier_id", label: "supplier" }, "supplier_name", { key: "country_code", label: "country" },
            { key: "lead_time_days", label: "lead time", num: true },
            { key: "otif", label: "OTIF 6m", num: true, render: (r) => <span className={Number(r.otif) < 0.8 ? "bad-t" : undefined}>{pct(r.otif)}</span> },
            { key: "open_pos", label: "open POs", num: true },
            { key: "slipped", num: true, render: (r) => (Number(r.slipped) ? <Badge tone="warn">{String(r.slipped)}</Badge> : "0") },
            { key: "open_commitments", label: "commitments", num: true },
          ]} />
          <Pager total={data.total} limit={data.limit} offset={data.offset} onChange={setOffset} />
        </Panel>
      )}
    </div>
  );
}

export function SupplierDetail({ id }: { id: string }) {
  const { asOf } = useAsOf();
  const { data, error, loading } = useData(() => scm.supplier(id, asOf), [id, asOf]);
  if (loading && !data) return <Loading />;
  if (error) return <ErrorBox error={error} />;
  const s = data!.supplier as Row;
  const card = data!.scorecard as Row[];
  const last = card[card.length - 1] ?? {};
  const lt = data!.lead_time_trend as Row[];
  return (
    <div>
      <PageHead crumbs={[["Suppliers", "#/suppliers"]]} title={<>{String(s.supplier_name)} <span className="muted mono small">{id}</span></>}
        sub={`${s.country_code} · contracted lead time ${s.lead_time_days} days · reliability score ${s.reliability_score}`} />
      <div className="kpis">
        <Kpi label="OTIF last month" value={pct(last.otif_rate)} tone={Number(last.otif_rate) < 0.8 ? "bad" : "good"} sub={String(last.month ?? "").slice(0, 7)} />
        <Kpi label="Average delay" value={`${last.avg_delay_days ?? "-"} d`} />
        <Kpi label="Open orders" value={(data!.open_pos as Row[]).length} />
        <Kpi label="Open commitments" value={(data!.open_commitments as Row[]).length} tone={(data!.open_commitments as Row[]).length ? "warn" : undefined} />
        <Kpi label="Promises kept" value={last.commitments_made ? `${last.commitments_kept}/${last.commitments_made}` : "-"} sub="last month" />
      </div>
      <div className="grid split">
        <div>
          <Panel title="Delivery performance" right={<span className="muted small">monthly scorecard</span>}>
            <LineChart yFmt={(v) => `${Math.round(v * 100)}%`} series={[
              { name: "OTIF", color: "#1c6b3f", points: card.map((r) => ({ x: String(r.month), y: Number(r.otif_rate) })) },
              { name: "fill rate", color: "#1d4ed8", dashed: true, points: card.map((r) => ({ x: String(r.month), y: Number(r.fill_rate) })) },
            ]} />
          </Panel>
          {lt.length > 1 && (
            <Panel title="Actual lead time" right={<span className="muted small">order to receipt, days</span>}>
              <LineChart series={[{ name: "lead time", color: "#16181d", points: lt.map((r) => ({ x: String(r.ordered_month), y: Number(r.avg_lead_days) })) }]} />
            </Panel>
          )}
          <Panel title="Open orders">
            <Table rows={data!.open_pos as Row[]} empty="No open orders." cols={[{ key: "rm_purchase_order_id", label: "order" }, { key: "rm_id", label: "material" }, "plant_id", "promised_at", "expected_at", { key: "slip_days", label: "slip", num: true }]} />
          </Panel>
          <Panel title="Contracts">
            <Table rows={data!.contracts as Row[]} empty="No contracts in force." cols={["contract_id", "scope", "valid_from", "valid_to", "min_volume", "penalty_clause"]} />
          </Panel>
          <Panel title="Negotiations">
            <Table rows={data!.negotiations as Row[]} empty="No negotiations." cols={["negotiation_id", "topic", "started_at", "outcome", { key: "final_terms", render: (r) => <span className="clip">{String(r.final_terms ?? "-")}</span> }]} />
          </Panel>
          <Panel title="Catalog" right={<span className="muted small">materials this supplier can deliver</span>}>
            <Table rows={data!.catalog as Row[]} cols={[{ key: "rm_id", label: "material" }, "rm_name", { key: "sourcing_rank", label: "rank", num: true }, { key: "allocation_pct", label: "share", num: true }, { key: "unit_price", num: true }, "price_valid_to"]} />
          </Panel>
        </div>
        <div>
          <MemoryPanel entityId={id} />
          <Panel title="Open commitments">
            <Table rows={data!.open_commitments as Row[]} empty="No open commitments." cols={[{ key: "commitment_id", label: "id" }, "due_date", { key: "commitment_text", label: "promise" }]} />
          </Panel>
          <Panel title="Past disruptions">
            <Table rows={data!.events as Row[]} empty="None recorded." cols={[{ key: "event_id", label: "event" }, "event_type", "detected_at"]} />
          </Panel>
          <Panel title="Past decisions">
            <Table rows={data!.decisions as Row[]} empty="None recorded." cols={[{ key: "decision_id", label: "decision" }, "decision_type", "decided_at", "outcome_label"]} />
          </Panel>
        </div>
      </div>
    </div>
  );
}
