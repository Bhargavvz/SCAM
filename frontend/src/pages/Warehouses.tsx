import { AdviceBox, MemoryPanel } from "../components/Memory";
import { CheckCircle2, Warehouse as WarehouseIcon } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Bars, Donut } from "../components/charts";
import { Bar, Card, DataTable, ErrorBox, Kpi, Loading, PageHead, Status, Tabs, useAction } from "../components/ui";
import { api, fmt, Row, useQuery } from "../lib/api";

export function WarehousesPage() {
  const nav = useNavigate();
  const { data, error, loading } = useQuery<Row[]>("/warehouses");
  if (loading && !data) return <Loading />;
  if (error) return <ErrorBox error={error} />;
  return (
    <div>
      <PageHead title="Warehouses" icon={<WarehouseIcon size={20} />} sub="Distribution centres: capacity, stock, inbound receiving, outbound fulfilment and open floor tasks." />
      <div className="grid g3">
        {data!.map((w) => (
          <Link key={w.warehouse_id} to={`/warehouses/${w.warehouse_id}`} className="card" style={{ color: "inherit", textDecoration: "none" }}>
            <div className="card-head"><h3>{w.warehouse_name}<span className="sub">{w.warehouse_id} · {w.region}</span></h3><Status value={w.utilization > 1 ? "over capacity" : w.utilization > 0.85 ? "near capacity" : "ok"} tone={w.utilization > 1 ? "bad" : w.utilization > 0.85 ? "warn" : "good"} /></div>
            <div className="card-body stack" style={{ gap: 10 }}>
              <div>
                <div className="row between small"><span className="muted">Utilization</span><b>{fmt.pct(w.utilization, 0)}</b></div>
                <Bar value={Math.min(w.utilization, 1.5)} max={1.5} tone={w.utilization > 1 ? "bad" : w.utilization > 0.85 ? "warn" : "good"} />
                <div className="small muted">{fmt.compact(w.units)} units of {fmt.compact(w.capacity_units)} capacity · {fmt.money(w.value)}</div>
              </div>
              <div className="kv" style={{ gridTemplateColumns: "repeat(4, 1fr)" }}>
                <div><dt>Inbound 7d</dt><dd>{fmt.n(w.inbound_due_7d)}</dd></div>
                <div><dt>To ship</dt><dd>{fmt.n(w.outbound_open)}</dd></div>
                <div><dt>Tasks</dt><dd>{fmt.n(w.open_tasks)}</dd></div>
                <div><dt>Shipped 30d</dt><dd>{fmt.compact(w.shipped_30d)}</dd></div>
              </div>
            </div>
          </Link>
        ))}
      </div>
    </div>
  );
}

export function WarehouseDetail() {
  const { id = "" } = useParams();
  const nav = useNavigate();
  const [tab, setTab] = useState("receiving");
  const { data: d, error, loading, reload } = useQuery<Row>(`/warehouses/${id}`);
  const { run, busy } = useAction();
  if (loading && !d) return <Loading />;
  if (error) return <ErrorBox error={error} />;
  const w = d!.warehouse;
  const openTasks = d!.tasks.filter((t: Row) => t.status === "open");
  return (
    <div>
      <PageHead crumbs={[["Warehouses", "/warehouses"]]} title={<>{w.warehouse_name} <span className="muted mono" style={{ fontSize: 14 }}>{id}</span></>} sub={`${w.region} region · capacity ${fmt.n(w.capacity_units)} units`} />
      <div className="kpis">
        <Kpi label="Utilization" value={fmt.pct(w.utilization, 0)} tone={w.utilization > 1 ? "bad" : w.utilization > 0.85 ? "warn" : "good"} sub={`${fmt.compact(w.units)} units`} />
        <Kpi label="Stock value" value={fmt.money(w.value)} />
        <Kpi label="Receiving due · 14d" value={fmt.n(d!.receiving.length)} tone="info" sub="open purchase orders" />
        <Kpi label="Orders to fulfil" value={fmt.n(d!.picking.length)} tone={d!.picking.length ? "warn" : "good"} />
        <Kpi label="Open tasks" value={fmt.n(openTasks.length)} sub="put-away, pick, inspect" />
      </div>
      <div className="grid g-main">
        <Card title="Daily throughput" sub="units, last 60 days">
          <Bars data={d!.activity} x="day" height={220} series={[{ key: "received", name: "Received", color: "#2563eb" }, { key: "shipped", name: "Shipped", color: "#059669" }]} />
        </Card>
        <Card title="Stock by category"><Donut data={d!.by_category} nameKey="category" valueKey="value" height={170} fmtValue={fmt.money} /></Card>
      </div>
      <div style={{ marginBottom: 14 }}><MemoryPanel entityId={id} /></div>
      <Tabs value={tab} onChange={setTab} options={[["receiving", `Receiving (${d!.receiving.length})`], ["picking", `Pick & pack (${d!.picking.length})`], ["shipping", "Shipping"], ["tasks", `Tasks (${openTasks.length} open)`]]} />
      <Card flush>
        {tab === "receiving" && <DataTable rows={d!.receiving} empty="Nothing due in the next 14 days." onRow={(r) => nav(`/purchasing/${r.purchase_order_id}`)} cols={[
          { key: "purchase_order_id", label: "PO" }, { key: "supplier_name", label: "Supplier" }, { key: "expected_at", label: "Expected" },
          { key: "units_due", label: "Units due", num: true }, { key: "status", render: (r) => <Status value={r.status} /> }]} />}
        {tab === "picking" && <DataTable rows={d!.picking} empty="No orders waiting." onRow={(r) => nav(`/sales/orders/${r.sales_order_id}`)} cols={[
          { key: "sales_order_id", label: "Order" }, { key: "customer_name", label: "Customer" }, { key: "promised_date", label: "Promised" },
          { key: "units", num: true }, { key: "order_value", label: "Value", num: true, render: (r) => fmt.moneyFull(r.order_value) }, { key: "status", render: (r) => <Status value={r.status} /> }]} />}
        {tab === "shipping" && <DataTable rows={d!.shipping} empty="No recent shipments." onRow={(r) => nav(`/logistics/${r.shipment_id}`)} cols={[
          { key: "shipment_id", label: "Shipment" }, { key: "sales_order_id", label: "Order" }, { key: "carrier_id", label: "Carrier" }, { key: "destination_name", label: "To" },
          { key: "units", num: true }, { key: "ship_date", label: "Shipped" }, { key: "status", render: (r) => <Status value={r.status} /> }]} />}
        {tab === "tasks" && <DataTable rows={d!.tasks} empty="No tasks yet — they appear when goods are received, orders released to picking, or returns received." cols={[
          { key: "task_id", label: "Task", render: (r) => <span className="mono">{r.task_id}</span> }, { key: "task_type", label: "Type", render: (r) => <Status value={r.task_type} tone="violet" /> },
          { key: "ref_id", label: "Reference" }, { key: "units", num: true }, { key: "bin" }, { key: "created_at", label: "Created", render: (r) => fmt.ago(r.created_at) },
          { key: "status", render: (r) => <Status value={r.status} /> },
          { key: "act", label: "", render: (r) => r.status === "open" ? <button className="btn sm" disabled={!!busy} onClick={async () => { await run(r.task_id, () => api.post(`/warehouse-tasks/${r.task_id}/complete`), `${r.task_id} completed`); reload(); }}><CheckCircle2 size={13} />Complete</button> : null }]} />}
      </Card>
    </div>
  );
}
