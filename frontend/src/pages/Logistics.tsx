import { AdviceBox, MemoryPanel } from "../components/Memory";
import { MapPin, Truck } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { Bars, Lines, PALETTE } from "../components/charts";
import { Card, DataTable, EntityLink, ErrorBox, Kpi, KV, Loading, PageHead, Pager, Seg, Status, Tabs, useAction } from "../components/ui";
import { api, fmt, Row, useDebounced, useQuery } from "../lib/api";

export function LogisticsPage() {
  const nav = useNavigate();
  const [params] = useSearchParams();
  const [tab, setTab] = useState("shipments");
  const [direction, setDirection] = useState("outbound");
  const [status, setStatus] = useState("");
  const [carrier, setCarrier] = useState(params.get("carrier") ?? "");
  const [late, setLate] = useState(false);
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const dq = useDebounced(q);
  useEffect(() => setOffset(0), [direction, status, carrier, late, dq]);
  const { data: o } = useQuery<Row>("/logistics/overview");
  const { data, error, loading } = useQuery<Row>(tab === "shipments" ? "/shipments" : null, { direction, status, carrier_id: carrier, late, q: dq, limit: 50, offset });
  const carrierSeries = useMemo(() => {
    if (!o) return { rows: [], keys: [] as string[] };
    const by: Record<string, Row> = {};
    o.carrier_monthly.forEach((r: Row) => { by[r.month] = { ...(by[r.month] ?? { month: r.month }), [r.carrier_id]: r.on_time }; });
    return { rows: Object.values(by), keys: o.carriers.map((c: Row) => c.carrier_id) };
  }, [o]);
  return (
    <div>
      <PageHead title="Logistics & transportation" icon={<Truck size={20} />} sub="Inbound and outbound shipments, carrier performance, lanes and freight cost." />
      {o && (
        <div className="kpis">
          <Kpi label="In transit" value={fmt.n(o.kpis.in_transit)} tone="info" />
          <Kpi label="Late in transit" value={fmt.n(o.kpis.late_in_transit)} tone={o.kpis.late_in_transit ? "bad" : "good"} sub="past promised date" />
          <Kpi label="On-time delivery · 30d" value={fmt.pct(o.kpis.otd_30d)} tone="good" sub="outbound" />
          <Kpi label="Freight · 30 days" value={fmt.money(o.kpis.freight_30d)} />
          <Kpi label="Exceptions" value={fmt.n(o.kpis.exceptions)} tone={o.kpis.exceptions ? "warn" : "good"} />
        </div>
      )}
      {o && (
        <div className="grid g2">
          <Card title="Carrier on-time rate" sub="outbound, monthly">
            <Lines data={carrierSeries.rows} x="month" yFmt={(v) => fmt.pct(v, 0)} height={230}
              series={carrierSeries.keys.map((k: string, i: number) => ({ key: k, name: o.carriers.find((c: Row) => c.carrier_id === k)?.carrier_name ?? k, color: PALETTE[i] }))} />
          </Card>
          <Card title="Freight cost" sub="monthly">
            <Bars data={o.freight_monthly} x="month" stacked yFmt={fmt.money} height={230} series={[{ key: "outbound", name: "Outbound", color: "#0e7490" }, { key: "inbound", name: "Inbound", color: "#94a3b8" }]} />
          </Card>
        </div>
      )}
      <Tabs value={tab} onChange={setTab} options={[["shipments", "Shipments"], ["carriers", "Carriers"], ["lanes", "Lanes"]]} />
      <Card flush>
        {tab === "shipments" && (
          <>
            <div className="toolbar">
              <input className="input" style={{ width: 240 }} placeholder="Shipment, tracking no, order, city" value={q} onChange={(e) => setQ(e.target.value)} />
              <Seg value={direction} onChange={setDirection} options={[["outbound", "Outbound"], ["inbound", "Inbound"], ["", "All"]]} />
              <Seg value={status} onChange={setStatus} options={[["", "Any status"], ["booked", "Booked"], ["in_transit", "In transit"], ["delivered", "Delivered"], ["exception", "Exception"]]} />
              <select className="input" value={carrier} onChange={(e) => setCarrier(e.target.value)}><option value="">All carriers</option>{o?.carriers.map((c: Row) => <option key={c.carrier_id} value={c.carrier_id}>{c.carrier_name}</option>)}</select>
              <label className="row small"><input type="checkbox" checked={late} onChange={(e) => setLate(e.target.checked)} />Late only</label>
            </div>
            <ErrorBox error={error} />
            {loading && !data ? <Loading /> : (
              <DataTable rows={data!.rows} onRow={(r) => nav(`/logistics/${r.shipment_id}`)} cols={[
                { key: "shipment_id", label: "Shipment" }, { key: "direction", render: (r) => <Status value={r.direction} tone="plain" /> },
                { key: "ref", label: "Order", render: (r) => <EntityLink id={r.sales_order_id ?? r.purchase_order_id} /> },
                { key: "carrier_name", label: "Carrier" }, { key: "mode" }, { key: "origin_name", label: "From" }, { key: "destination_name", label: "To" },
                { key: "units", num: true }, { key: "ship_date", label: "Shipped" }, { key: "promised_date", label: "Promised" },
                { key: "days_late", label: "Late", num: true, render: (r) => r.days_late > 0 ? <b className="bad-t">{r.days_late} d</b> : <span className="muted">–</span> },
                { key: "status", render: (r) => <Status value={r.status} /> },
              ]} />
            )}
            <Pager page={data as any} onOffset={setOffset} />
          </>
        )}
        {tab === "carriers" && o && <DataTable rows={o.carriers} onRow={(r) => { setCarrier(r.carrier_id); setTab("shipments"); }} cols={[
          { key: "carrier_id", label: "Carrier" }, { key: "carrier_name", label: "Name" }, { key: "mode" }, { key: "shipments", label: "Shipments 90d", num: true },
          { key: "on_time", label: "On time", num: true, render: (r) => <b className={r.on_time < 0.8 ? "bad-t" : "good-t"}>{fmt.pct(r.on_time)}</b> },
          { key: "avg_transit_days", label: "Avg transit", num: true, render: (r) => `${r.avg_transit_days} d` }, { key: "cost_per_unit", label: "Cost / unit", num: true, render: (r) => fmt.moneyFull(r.cost_per_unit) },
        ]} />}
        {tab === "lanes" && o && <DataTable rows={o.lanes} cols={[
          { key: "origin_id", label: "Origin" }, { key: "origin_region", label: "Region" }, { key: "destination", label: "Destination" },
          { key: "shipments", num: true }, { key: "avg_km", label: "Avg km", num: true }, { key: "avg_transit_days", label: "Transit", num: true, render: (r) => `${r.avg_transit_days} d` },
          { key: "on_time", label: "On time", num: true, render: (r) => fmt.pct(r.on_time) }, { key: "freight", num: true, render: (r) => fmt.money(r.freight) },
        ]} />}
      </Card>
    </div>
  );
}

export function ShipmentDetail() {
  const { id = "" } = useParams();
  const { data: d, error, loading, reload } = useQuery<Row>(`/shipments/${id}`);
  const [scan, setScan] = useState("in_transit");
  const [loc, setLoc] = useState("");
  const { run, busy } = useAction();
  if (loading && !d) return <Loading />;
  if (error) return <ErrorBox error={error} />;
  const s = d!.shipment;
  const open = ["in_transit", "booked", "exception"].includes(s.status);
  return (
    <div>
      <PageHead crumbs={[["Logistics", "/logistics"]]} title={<>Shipment <span className="mono">{id}</span> <Status value={s.status} /></>}
        sub={<>{s.carrier_name} · {s.mode} · tracking <span className="mono">{s.tracking_no}</span></>} />
      <div className="grid g-main">
        <Card title="Shipment">
          <KV items={[["Direction", fmt.label(s.direction)], ["Order", <EntityLink id={s.sales_order_id ?? s.purchase_order_id} />], ["From", s.origin_name], ["To", s.destination_name],
            ["Units", fmt.n(s.units)], ["Distance", s.distance_km ? `${fmt.n(s.distance_km)} km` : "–"], ["Shipped", s.ship_date ?? "–"], ["Promised", s.promised_date],
            ["Delivered", s.delivered_at ?? "–"], ["Days late", s.days_late > 0 ? <b className="bad-t">{s.days_late}</b> : "–"], ["Freight", fmt.moneyFull(s.freight_cost)]]} />
        </Card>
        <Card title="Tracking" icon={<MapPin size={14} />}>
          <ul className="timeline">{d!.timeline.map((t: Row, i: number) => (
            <li key={i}><div><b>{fmt.label(t.status)}</b> · {t.location}</div><div className="t">{String(t.at).replace("T", " ").slice(0, 16)}{t.note ? ` · ${t.note}` : ""}</div></li>
          ))}</ul>
          {open && (
            <div className="stack" style={{ gap: 8, marginTop: 8 }}>
              <div className="row" style={{ flexWrap: "nowrap" }}>
                <select className="input" value={scan} onChange={(e) => setScan(e.target.value)}>{["picked_up", "in_transit", "out_for_delivery", "delivered", "exception"].map((x) => <option key={x} value={x}>{fmt.label(x)}</option>)}</select>
                <input className="input grow" placeholder="Location" value={loc} onChange={(e) => setLoc(e.target.value)} />
              </div>
              <button className="btn" disabled={!!busy || !loc} onClick={async () => { await run("scan", () => api.post(`/shipments/${id}/events`, { status: scan, location: loc }), "Tracking event recorded"); setLoc(""); reload(); }}>Record carrier scan</button>
            </div>
          )}
        </Card>
      </div>
      <MemoryPanel entityId={s.carrier_id} title={`Memory: ${s.carrier_name}`} />
    </div>
  );
}
