import { AdviceBox, MemoryPanel } from "../components/Memory";
import { ArrowRight, Plus, ShoppingCart, Trash2, Undo2, Users, XCircle } from "lucide-react";
import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Bars, TrendArea } from "../components/charts";
import { Card, DataTable, Drawer, EntityLink, ErrorBox, Kpi, KV, Loading, PageHead, Pager, Seg, Status, Steps, useAction } from "../components/ui";
import { api, fmt, Row, useDebounced, useQuery } from "../lib/api";

const FLOW = ["confirmed", "allocated", "picking", "packed", "shipped", "delivered"];
const NEXT_LABEL: Record<string, string> = { allocated: "Allocate stock", picking: "Release to picking", packed: "Confirm picked & packed", shipped: "Ship", delivered: "Confirm delivery" };

function CreateOrder({ open, onClose }: { open: boolean; onClose: (id?: string) => void }) {
  const { data: lk } = useQuery<Row>("/lookup");
  const [customer, setCustomer] = useState("");
  const [wh, setWh] = useState("");
  const [req, setReq] = useState("");
  const [lines, setLines] = useState([{ product_id: "", quantity: 1 }]);
  const [avail, setAvail] = useState<Record<string, number | null>>({});
  const { run, busy } = useAction();
  const { data: cust } = useQuery<Row>(customer.length >= 7 ? `/customers/${customer}` : null);
  const warehouse = wh || cust?.customer?.home_warehouse_id || "";
  useEffect(() => {
    if (open) { setCustomer(""); setWh(""); setReq(""); setLines([{ product_id: "", quantity: 1 }]); }
  }, [open]);
  useEffect(() => {
    lines.forEach((l) => {
      if (/^IP\d{5}$/.test(l.product_id) && warehouse)
        api.get<Row>(`/products/${l.product_id}`).then((p) => {
          const s = p.stock.find((x: Row) => x.warehouse_id === warehouse);
          setAvail((a) => ({ ...a, [`${l.product_id}@${warehouse}`]: s ? s.available : 0 }));
        }).catch(() => setAvail((a) => ({ ...a, [`${l.product_id}@${warehouse}`]: null })));
    });
  }, [lines.map((l) => l.product_id).join(","), warehouse]);
  const submit = async () => {
    const r = await run("so", () => api.post<Row>("/sales-orders", { customer_id: customer, warehouse_id: wh || undefined, requested_date: req || undefined,
      lines: lines.filter((l) => l.product_id && l.quantity > 0) }), (x: Row) => `Order ${x.order.sales_order_id} confirmed`);
    if (r) onClose(r.order.sales_order_id);
  };
  return (
    <Drawer open={open} onClose={() => onClose()} wide title="New sales order"
      footer={<><button className="btn" onClick={() => onClose()}>Cancel</button><button className="btn primary" disabled={!!busy || !cust?.customer || !lines.some((l) => l.product_id)} onClick={submit}>Confirm order</button></>}>
      <div className="grid g3" style={{ marginBottom: 0 }}>
        <label className="field">Customer id<input className="input mono" value={customer} placeholder="CUS0001" onChange={(e) => setCustomer(e.target.value.toUpperCase())} /></label>
        <label className="field">Ship from
          <select className="input" value={wh} onChange={(e) => setWh(e.target.value)}><option value="">{cust?.customer ? `Home DC (${cust.customer.home_warehouse_id})` : "Customer's home DC"}</option>{lk?.warehouses.map((w: Row) => <option key={w.warehouse_id} value={w.warehouse_id}>{w.warehouse_id} · {w.warehouse_name}</option>)}</select>
        </label>
        <label className="field">Requested date<input className="input" type="date" min={lk?.today} value={req} onChange={(e) => setReq(e.target.value)} /></label>
      </div>
      {cust?.customer && <div className="note"><b>{cust.customer.customer_name}</b> · {cust.customer.segment} · {cust.customer.city} · {cust.customer.payment_terms} · {cust.customer.discount_pct}% contract discount · credit limit {fmt.money(cust.customer.credit_limit)}</div>}
      <Card title="Lines" flush actions={<button className="btn sm" onClick={() => setLines([...lines, { product_id: "", quantity: 1 }])}><Plus size={13} />Add line</button>}>
        <table>
          <thead><tr><th>Product id</th><th className="num">Quantity</th><th className="num">Available at {warehouse || "–"}</th><th /></tr></thead>
          <tbody>{lines.map((l, i) => {
            const a = avail[`${l.product_id}@${warehouse}`];
            return (
              <tr key={i}>
                <td><input className="input mono" value={l.product_id} placeholder="IP00001" onChange={(e) => setLines(lines.map((x, j) => (j === i ? { ...x, product_id: e.target.value.toUpperCase() } : x)))} /></td>
                <td className="num"><input className="input" type="number" min={1} style={{ width: 100, textAlign: "right" }} value={l.quantity} onChange={(e) => setLines(lines.map((x, j) => (j === i ? { ...x, quantity: Number(e.target.value) } : x)))} /></td>
                <td className="num">{a === undefined ? "–" : a === null ? <span className="bad-t">unknown product</span> : <span className={a >= l.quantity ? "good-t" : "warn-t"}>{fmt.n(a)}{a < l.quantity ? " (will backorder)" : ""}</span>}</td>
                <td><button className="iconbtn" disabled={lines.length === 1} onClick={() => setLines(lines.filter((_, j) => j !== i))}><Trash2 size={14} /></button></td>
              </tr>
            );
          })}</tbody>
        </table>
      </Card>
    </Drawer>
  );
}

export function SalesPage() {
  const nav = useNavigate();
  const [status, setStatus] = useState("");
  const [q, setQ] = useState("");
  const [sort, setSort] = useState("");
  const [offset, setOffset] = useState(0);
  const [create, setCreate] = useState(false);
  const dq = useDebounced(q);
  useEffect(() => setOffset(0), [status, dq, sort]);
  const { data: s } = useQuery<Row>("/sales-orders/summary");
  const { data, error, loading } = useQuery<Row>("/sales-orders", { status, q: dq, sort, limit: 50, offset });
  return (
    <div>
      <PageHead title="Sales orders" icon={<ShoppingCart size={20} />} sub="Customer orders from confirmation through allocation, picking, packing, shipping and delivery."
        actions={<button className="btn primary" onClick={() => setCreate(true)}><Plus size={14} />New order</button>} />
      {s && (
        <div className="grid g-side">
          <div className="kpis" style={{ gridTemplateColumns: "1fr 1fr", marginBottom: 0 }}>
            <Kpi label="Revenue · 30 days" value={fmt.money(s.kpis.revenue_30d)} tone="good" />
            <Kpi label="Orders · 30 days" value={fmt.n(s.kpis.orders_30d)} sub={`avg ${fmt.money(s.kpis.avg_order_value)}`} tone="info" />
            <Kpi label="To fulfil" value={fmt.n(s.open)} sub="confirmed → packed" tone={s.open ? "warn" : "good"} />
            <Kpi label="On-time delivery" value={fmt.pct(s.kpis.otd)} sub={`${fmt.n(s.in_transit)} in transit`} tone="good" />
          </div>
          <Card title="Daily orders" sub="last 90 days">
            <TrendArea data={s.daily} x="day" series={[{ key: "revenue", name: "Revenue", color: "#0e7490" }]} yFmt={fmt.money} height={200} />
          </Card>
        </div>
      )}
      <div style={{ height: 14 }} />
      <Card flush>
        <div className="toolbar">
          <input className="input" style={{ width: 260 }} placeholder="Order, customer id or name" value={q} onChange={(e) => setQ(e.target.value)} />
          <Seg value={status} onChange={setStatus} options={[["", "All"], ["open", "To fulfil"], ["shipped", "In transit"], ["delivered", "Delivered"], ["backordered", "Backordered"], ["cancelled", "Cancelled"]]} />
        </div>
        <ErrorBox error={error} />
        {loading && !data ? <Loading /> : (
          <DataTable rows={data!.rows} sort={sort} onSort={setSort} onRow={(r) => nav(`/sales/orders/${r.sales_order_id}`)} cols={[
            { key: "sales_order_id", label: "Order", sort: true }, { key: "customer_name", label: "Customer", sort: true }, { key: "segment" },
            { key: "warehouse_id", label: "From" }, { key: "channel" }, { key: "order_date", label: "Ordered", sort: true },
            { key: "promised_date", label: "Promised", sort: true }, { key: "lines", num: true },
            { key: "order_value", label: "Value", num: true, sort: true, render: (r) => fmt.moneyFull(r.order_value) },
            { key: "on_time", label: "OTD", render: (r) => r.on_time === null ? <span className="muted">–</span> : r.on_time ? <Status value="on_time" /> : <Status value="late" /> },
            { key: "status", sort: true, render: (r) => <>{<Status value={r.status} />}{r.backordered > 0 && <> <Status value="backorder" tone="warn" /></>}</> },
          ]} />
        )}
        <Pager page={data as any} onOffset={setOffset} />
      </Card>
      <CreateOrder open={create} onClose={(id) => { setCreate(false); if (id) nav(`/sales/orders/${id}`); }} />
    </div>
  );
}

export function OrderDetail() {
  const { id = "" } = useParams();
  const nav = useNavigate();
  const { data: d, error, loading, reload } = useQuery<Row>(`/sales-orders/${id}`);
  const { data: lk } = useQuery<Row>("/lookup");
  const [carrier, setCarrier] = useState("CR01");
  const [rma, setRma] = useState<Row | null>(null);
  const [rmaQty, setRmaQty] = useState(1);
  const [reason, setReason] = useState("defective");
  const { run, busy } = useAction();
  if (loading && !d) return <Loading />;
  if (error) return <ErrorBox error={error} />;
  const o = d!.order;
  const advance = async () => {
    await run("adv", () => api.post(`/sales-orders/${id}/advance${d!.next === "shipped" ? `?carrier_id=${carrier}` : ""}`), `${id}: ${fmt.label(d!.next)}`);
    reload();
  };
  return (
    <div>
      <PageHead crumbs={[["Sales orders", "/sales"]]} title={<>Order <span className="mono">{id}</span> <Status value={o.status} /></>}
        sub={<><EntityLink id={o.customer_id}>{o.customer_name}</EntityLink> · {o.segment} · ship from <EntityLink id={o.warehouse_id} /> · {o.channel}</>}
        actions={<>
          {["confirmed", "allocated", "picking", "packed"].includes(o.status) && <button className="btn danger" disabled={!!busy} onClick={async () => { if (confirm(`Cancel ${id}? Allocations will be released.`)) { await run("c", () => api.post(`/sales-orders/${id}/cancel`), "Order cancelled"); reload(); } }}><XCircle size={14} />Cancel</button>}
          {d!.next === "shipped" && <select className="input" value={carrier} onChange={(e) => setCarrier(e.target.value)}>{lk?.carriers.map((c: Row) => <option key={c.carrier_id} value={c.carrier_id}>{c.carrier_name} ({c.mode})</option>)}</select>}
          {d!.next && o.status !== "cancelled" && <button className="btn primary" disabled={!!busy} onClick={advance}>{NEXT_LABEL[d!.next]}<ArrowRight size={14} /></button>}
        </>} />
      {o.status !== "cancelled" && <Card><Steps steps={FLOW} current={o.status} /></Card>}
      {d!.next === "shipped" && <div style={{ marginTop: 14 }}><AdviceBox action={`ship sales order ${id} with carrier ${carrier}`} entityIds={[carrier, o.warehouse_id, o.customer_id]} details={`Promised delivery ${o.promised_date}.`} /></div>}
      <div style={{ height: 14 }} />
      <div className="grid g-main">
        <Card title="Lines" flush>
          <DataTable rows={d!.lines} cols={[
            { key: "product_id", label: "Product" }, { key: "sku", label: "SKU" },
            { key: "qty_ordered", label: "Ordered", num: true }, { key: "qty_allocated", label: "Allocated", num: true }, { key: "qty_shipped", label: "Shipped", num: true },
            { key: "qty_backordered", label: "Backorder", num: true, render: (r) => r.qty_backordered ? <b className="warn-t">{r.qty_backordered}</b> : "–" },
            { key: "available_now", label: "Avail. now", num: true, render: (r) => fmt.n(r.available_now) },
            { key: "unit_price", label: "Price", num: true, render: (r) => fmt.moneyFull(r.unit_price) },
            { key: "rma", label: "", render: (r) => ["delivered", "shipped"].includes(o.status) && r.qty_shipped > 0 ? <button className="btn sm ghost" onClick={() => { setRma(r); setRmaQty(1); }}><Undo2 size={13} />Return</button> : null },
          ]} />
        </Card>
        <Card title="Order">
          <KV items={[["Ordered", o.order_date], ["Requested", o.requested_date], ["Promised", o.promised_date], ["Shipped", o.shipped_at ?? "–"], ["Delivered", o.delivered_at ?? "–"], ["Value", fmt.moneyFull(o.order_value)]]} />
          {d!.shipment && <div style={{ marginTop: 14 }}>Shipment <EntityLink id={d!.shipment.shipment_id} /> via {d!.shipment.carrier_name} · <Status value={d!.shipment.status} /><div className="small muted mono">{d!.shipment.tracking_no}</div></div>}
        </Card>
      </div>
      <div style={{ marginBottom: 14 }}><MemoryPanel entityId={o.customer_id} title={`Memory: ${o.customer_name}`} /></div>
      <div className="grid g3">
        <Card title="Warehouse tasks" flush><DataTable rows={d!.tasks} empty="No tasks." cols={[{ key: "task_id", label: "Task", render: (r) => <span className="mono">{r.task_id}</span> }, { key: "task_type", label: "Type" }, { key: "bin" }, { key: "status", render: (r) => <Status value={r.status} /> }]} /></Card>
        <Card title="Returns" flush><DataTable rows={d!.returns} empty="No returns." cols={[{ key: "rma_id", label: "RMA" }, { key: "product_id", label: "Product" }, { key: "qty", num: true }, { key: "status", render: (r) => <Status value={r.status} /> }]} /></Card>
        <Card title="History" flush>
          {d!.history.length === 0 ? <div className="empty">{o.origin === "derived" ? "Historical order from the dataset." : "No events."}</div> :
            <ul className="timeline" style={{ padding: 14 }}>{d!.history.map((h: Row, i: number) => <li key={i}><div>{h.summary}</div><div className="t">{fmt.ago(h.at)} · {h.actor}</div></li>)}</ul>}
        </Card>
      </div>
      <Drawer open={!!rma} onClose={() => setRma(null)} title={`Return ${rma?.product_id ?? ""}`}
        footer={<><button className="btn" onClick={() => setRma(null)}>Cancel</button><button className="btn primary" disabled={!!busy} onClick={async () => {
          const r = await run("rma", () => api.post<Row>("/returns", { sales_order_line_id: rma!.sales_order_line_id, qty: rmaQty, reason }), (x: Row) => `${x.rma.rma_id} authorized`);
          if (r) nav(`/returns/${r.rma.rma_id}`);
        }}>Authorize return</button></>}>
        <label className="field">Quantity (shipped {rma?.qty_shipped})<input className="input" type="number" min={1} max={rma?.qty_shipped} value={rmaQty} onChange={(e) => setRmaQty(Number(e.target.value))} /></label>
        <label className="field">Reason
          <select className="input" value={reason} onChange={(e) => setReason(e.target.value)}>{["defective", "damaged_in_transit", "wrong_item", "not_needed", "quality_issue"].map((x) => <option key={x} value={x}>{fmt.label(x)}</option>)}</select>
        </label>
      </Drawer>
    </div>
  );
}

export function CustomersPage() {
  const nav = useNavigate();
  const [q, setQ] = useState("");
  const [segment, setSegment] = useState("");
  const [sort, setSort] = useState("revenue_12m:desc");
  const [offset, setOffset] = useState(0);
  const dq = useDebounced(q);
  useEffect(() => setOffset(0), [dq, segment, sort]);
  const { data, error, loading } = useQuery<Row>("/customers", { q: dq, segment, sort, limit: 50, offset });
  return (
    <div>
      <PageHead title="Customers" icon={<Users size={20} />} sub="Accounts with 12-month revenue, order frequency, open orders and returns." />
      <Card flush>
        <div className="toolbar">
          <input className="input" style={{ width: 260 }} placeholder="Customer, id or city" value={q} onChange={(e) => setQ(e.target.value)} />
          <Seg value={segment} onChange={setSegment} options={[["", "All"], ["Distributor", "Distributors"], ["Retail chain", "Retail"], ["OEM", "OEM"], ["E-commerce", "E-commerce"]]} />
        </div>
        <ErrorBox error={error} />
        {loading && !data ? <Loading /> : (
          <DataTable rows={data!.rows} sort={sort} onSort={setSort} onRow={(r) => nav(`/sales/customers/${r.customer_id}`)} cols={[
            { key: "customer_id", label: "Customer", sort: true }, { key: "customer_name", label: "Name", sort: true }, { key: "segment" }, { key: "city" },
            { key: "home_warehouse_id", label: "Home DC" }, { key: "payment_terms", label: "Terms" },
            { key: "orders_12m", label: "Orders 12m", num: true, sort: true }, { key: "revenue_12m", label: "Revenue 12m", num: true, sort: true, render: (r) => fmt.money(r.revenue_12m) },
            { key: "last_order", label: "Last order", sort: true }, { key: "open_orders", label: "Open", num: true }, { key: "returns_12m", label: "Returns", num: true, sort: true },
          ]} />
        )}
        <Pager page={data as any} onOffset={setOffset} />
      </Card>
    </div>
  );
}

export function CustomerDetail() {
  const { id = "" } = useParams();
  const { data: d, error, loading } = useQuery<Row>(`/customers/${id}`);
  if (loading && !d) return <Loading />;
  if (error) return <ErrorBox error={error} />;
  const c = d!.customer;
  return (
    <div>
      <PageHead crumbs={[["Customers", "/sales/customers"]]} title={<>{c.customer_name} <span className="muted mono" style={{ fontSize: 14 }}>{id}</span></>}
        sub={`${c.segment} · ${c.city}, ${c.region} · home DC ${c.home_warehouse_id} · customer since ${c.since}`} />
      <div className="kpis">
        <Kpi label="Revenue · 12 months" value={fmt.money(c.revenue_12m)} tone="good" />
        <Kpi label="Orders · 12 months" value={fmt.n(c.orders_12m)} sub={`last ${c.last_order ?? "–"}`} />
        <Kpi label="On-time delivery" value={d!.service.delivered ? fmt.pct(d!.service.on_time / d!.service.delivered) : "–"} sub={`avg cycle ${d!.service.avg_cycle_days ?? "–"} days`} />
        <Kpi label="Open orders" value={fmt.n(c.open_orders)} />
        <Kpi label="Credit limit" value={fmt.money(c.credit_limit)} sub={`${c.payment_terms} · ${c.discount_pct}% discount`} />
      </div>
      <div style={{ marginBottom: 14 }}><MemoryPanel entityId={id} /></div>
      <div className="grid g-main">
        <Card title="Monthly revenue"><Bars data={d!.monthly} x="month" series={[{ key: "revenue", name: "Revenue", color: "#0e7490" }]} yFmt={fmt.money} height={220} /></Card>
        <Card title="Top products" flush><DataTable rows={d!.top_products} cols={[{ key: "product_id", label: "Product" }, { key: "sku", label: "SKU" }, { key: "units", num: true }, { key: "revenue", num: true, render: (r) => fmt.money(r.revenue) }]} /></Card>
      </div>
      <div className="grid g-main">
        <Card title="Orders" flush><DataTable rows={d!.orders} cols={[{ key: "sales_order_id", label: "Order" }, { key: "order_date", label: "Ordered" }, { key: "delivered_at", label: "Delivered" }, { key: "order_value", label: "Value", num: true, render: (r) => fmt.moneyFull(r.order_value) }, { key: "status", render: (r) => <Status value={r.status} /> }]} /></Card>
        <Card title="Returns" flush><DataTable rows={d!.returns} empty="No returns." cols={[{ key: "rma_id", label: "RMA" }, { key: "product_id", label: "Product" }, { key: "reason", render: (r) => fmt.label(r.reason) }, { key: "status", render: (r) => <Status value={r.status} /> }]} /></Card>
      </div>
    </div>
  );
}
