import { Building2 } from "lucide-react";
import { useEffect, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { Bars, Lines } from "../components/charts";
import { Card, DataTable, ErrorBox, Kpi, KV, Linkify, Loading, PageHead, Pager, Seg, Status, Tabs } from "../components/ui";
import { fmt, Row, useDebounced, useQuery } from "../lib/api";

export function SuppliersPage() {
  const nav = useNavigate();
  const [params] = useSearchParams();
  const [q, setQ] = useState("");
  const [tier, setTier] = useState("");
  const [country, setCountry] = useState("");
  const [sort, setSort] = useState(params.get("sort") ?? "spend_12m:desc");
  const [offset, setOffset] = useState(0);
  const dq = useDebounced(q);
  useEffect(() => setOffset(0), [dq, tier, country, sort]);
  const { data, error, loading } = useQuery<Row>("/suppliers", { q: dq, tier, country, sort, limit: 50, offset });
  const { data: rep } = useQuery<Row>("/reports/suppliers");
  return (
    <div>
      <PageHead title="Suppliers" icon={<Building2 size={20} />} sub="Supplier base with delivery performance (last 6 complete months), spend, open orders and active contracts." />
      {rep && (
        <div className="grid g2">
          <Card title="Average on-time in full" sub="all suppliers, monthly">
            <Lines data={rep.monthly} x="month" series={[{ key: "otif", name: "OTIF", color: "#059669" }]} yFmt={(v) => fmt.pct(v, 0)} height={200} />
          </Card>
          <Card title="Performance by country" sub="last 12 months">
            <Bars data={rep.by_country} x="country_code" series={[{ key: "otif", name: "OTIF", color: "#0e7490" }]} yFmt={(v) => fmt.pct(v, 0)} height={200} />
          </Card>
        </div>
      )}
      <Card flush>
        <div className="toolbar">
          <input className="input" style={{ width: 260 }} placeholder="Supplier id or name" value={q} onChange={(e) => setQ(e.target.value)} />
          <Seg value={tier} onChange={setTier} options={[["", "All tiers"], ["preferred", "Preferred"], ["approved", "Approved"], ["at_risk", "At risk"]]} />
          <select className="input" value={country} onChange={(e) => setCountry(e.target.value)}>
            <option value="">All countries</option>{["CA", "CN", "DE", "MX", "PL", "US", "VN"].map((c) => <option key={c}>{c}</option>)}
          </select>
        </div>
        <ErrorBox error={error} />
        {loading && !data ? <Loading /> : (
          <DataTable rows={data!.rows} sort={sort} onSort={setSort} onRow={(r) => nav(`/suppliers/${r.supplier_id}`)} cols={[
            { key: "supplier_id", label: "Supplier", sort: true }, { key: "supplier_name", label: "Name", sort: true }, { key: "country_code", label: "Country" },
            { key: "tier", render: (r) => <Status value={r.tier} /> },
            { key: "otif_6m", label: "OTIF 6m", num: true, sort: true, render: (r) => fmt.pct(r.otif_6m, 0) },
            { key: "delay_6m", label: "Avg delay", num: true, sort: true, render: (r) => r.delay_6m === null ? "–" : `${r.delay_6m} d` },
            { key: "quality_ppm_6m", label: "Quality PPM", num: true, sort: true, render: (r) => fmt.n(r.quality_ppm_6m) },
            { key: "lead_time_days", label: "Lead time", num: true, sort: true, render: (r) => `${r.lead_time_days} d` },
            { key: "spend_12m", label: "Spend 12m", num: true, sort: true, render: (r) => fmt.money(r.spend_12m) },
            { key: "open_pos", label: "Open POs", num: true, sort: true },
            { key: "active_contracts", label: "Contracts", num: true },
          ]} />
        )}
        <Pager page={data as any} onOffset={setOffset} />
      </Card>
    </div>
  );
}

export function SupplierDetail() {
  const { id = "" } = useParams();
  const [tab, setTab] = useState("orders");
  const { data: d, error, loading } = useQuery<Row>(`/suppliers/${id}`);
  if (loading && !d) return <Loading />;
  if (error) return <ErrorBox error={error} />;
  const s = d!.supplier;
  const last = d!.scorecard[d!.scorecard.length - 1] ?? {};
  return (
    <div>
      <PageHead crumbs={[["Suppliers", "/suppliers"]]} title={<>{s.supplier_name} <span className="muted mono" style={{ fontSize: 14 }}>{id}</span></>}
        sub={<>{s.country_code} · contracted lead time {s.lead_time_days} days · reliability score {s.reliability_score} · <Status value={s.tier} /></>} />
      <div className="kpis">
        <Kpi label="OTIF · 6 months" value={fmt.pct(s.otif_6m, 0)} tone={s.tier === "at_risk" ? "bad" : "good"} sub={`last month ${fmt.pct(last.otif_rate, 0)}`} />
        <Kpi label="Average delay" value={s.delay_6m === null ? "–" : `${s.delay_6m} d`} sub="when late" />
        <Kpi label="Quality" value={`${fmt.n(s.quality_ppm_6m)} ppm`} sub="defects per million" />
        <Kpi label="Spend · 12 months" value={fmt.money(s.spend_12m)} />
        <Kpi label="Open orders" value={fmt.n(s.open_pos)} sub={`${s.active_contracts} active contracts`} />
      </div>
      <div className="grid g2">
        <Card title="Delivery performance" sub="monthly scorecard">
          <Lines data={d!.scorecard} x="month" yFmt={(v) => fmt.pct(v, 0)} yDomain={[0, 1]} height={220}
            series={[{ key: "otif_rate", name: "OTIF", color: "#059669" }, { key: "fill_rate", name: "Fill rate", color: "#2563eb", dashed: true }]} />
        </Card>
        <Card title="Actual lead time" sub="order to receipt, days">
          <Lines data={d!.lead_time} x="month" height={220} yFmt={(v) => fmt.n(v, 0)}
            series={[{ key: "actual_lead_days", name: "Lead time", color: "#0f172a" }, { key: "avg_late_days", name: "Days late", color: "#dc2626" }]} />
        </Card>
      </div>
      <Tabs value={tab} onChange={setTab} options={[["orders", `Purchase orders (${d!.purchase_orders.length})`], ["contracts", `Contracts (${d!.contracts.length})`], ["catalog", "Catalog"], ["negotiations", "Negotiations"], ["commitments", "Commitments"], ["disruptions", `Disruptions (${d!.disruptions.length})`]]} />
      <Card flush>
        {tab === "orders" && <DataTable rows={d!.purchase_orders} cols={[
          { key: "po_id", label: "PO" }, { key: "type", render: (r) => <Status value={r.type} tone="plain" /> }, { key: "ship_to", label: "Ship to" },
          { key: "ordered_at", label: "Ordered" }, { key: "expected_at", label: "Expected" }, { key: "received_at", label: "Received" },
          { key: "status", render: (r) => <Status value={r.status} /> }]} />}
        {tab === "contracts" && <DataTable rows={d!.contracts} empty="No contracts." cols={[
          { key: "contract_id", label: "Contract", render: (r) => <span className="mono">{r.contract_id}</span> }, { key: "scope" }, { key: "valid_from", label: "From" }, { key: "valid_to", label: "To" },
          { key: "price_terms", label: "Price terms", wrap: true }, { key: "min_volume", label: "Min volume", num: true }, { key: "penalty_clause", label: "Penalty", wrap: true },
          { key: "state", render: (r) => <Status value={r.state} /> }]} />}
        {tab === "catalog" && <DataTable rows={[...d!.materials, ...d!.products.map((p: Row) => ({ rm_id: p.product_id, rm_name: `${p.sku} (${fmt.label(p.category)})`, unit_price: p.unit_cost }))]} empty="Nothing supplied." cols={[
          { key: "rm_id", label: "Item" }, { key: "rm_name", label: "Name" }, { key: "sourcing_rank", label: "Rank", num: true },
          { key: "allocation_pct", label: "Share %", num: true }, { key: "lead_time_days", label: "Lead time", num: true }, { key: "moq", label: "MOQ", num: true },
          { key: "unit_price", label: "Price", num: true, render: (r) => fmt.moneyFull(r.unit_price) }]} />}
        {tab === "negotiations" && <DataTable rows={d!.negotiations} empty="No negotiations." cols={[
          { key: "negotiation_id", label: "Id", render: (r) => <span className="mono">{r.negotiation_id}</span> }, { key: "topic" }, { key: "started_at", label: "Started" },
          { key: "our_ask", label: "Our ask", wrap: true }, { key: "their_offer", label: "Their offer", wrap: true }, { key: "outcome", render: (r) => <Status value={r.outcome} /> }]} />}
        {tab === "commitments" && <DataTable rows={d!.commitments} empty="No commitments." cols={[
          { key: "commitment_id", label: "Id", render: (r) => <span className="mono">{r.commitment_id}</span> }, { key: "made_at", label: "Made" }, { key: "due_date", label: "Due" },
          { key: "commitment_text", label: "Commitment", wrap: true, render: (r) => <Linkify text={r.commitment_text} /> }, { key: "status", render: (r) => <Status value={r.status} /> }]} />}
        {tab === "disruptions" && <DataTable rows={d!.disruptions} empty="No disruptions recorded." cols={[
          { key: "event_id", label: "Event", render: (r) => <span className="mono">{r.event_id}</span> }, { key: "event_type", label: "Type", render: (r) => fmt.label(r.event_type) },
          { key: "title", wrap: true }, { key: "detected_at", label: "Detected" }, { key: "severity", num: true },
          { key: "end_date", label: "Status", render: (r) => r.end_date ? <Status value="closed" /> : <Status value="open" tone="bad" /> }]} />}
      </Card>
      <div style={{ marginTop: 14 }}><KV items={[["Supplier id", id], ["Country", s.country_code], ["Contracted lead time", `${s.lead_time_days} days`], ["Reliability score", s.reliability_score]]} /></div>
    </div>
  );
}
