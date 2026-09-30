import { ClipboardList, PackageCheck, Plus, Send, Trash2, XCircle } from "lucide-react";
import { useEffect, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { Bars } from "../components/charts";
import { Card, DataTable, Drawer, EntityLink, ErrorBox, Kpi, KV, Loading, PageHead, Pager, Seg, Status, Tabs, useAction } from "../components/ui";
import { api, fmt, Row, useDebounced, useQuery } from "../lib/api";

type Line = { product_id: string; quantity: number };

export function CreatePO({ open, onClose, preset }: { open: boolean; onClose: (id?: string) => void; preset?: { supplier_id: string; warehouse_id: string; lines: Line[] } }) {
  const { data: lk } = useQuery<Row>("/lookup");
  const [supplier, setSupplier] = useState("");
  const [wh, setWh] = useState("");
  const [expected, setExpected] = useState("");
  const [lines, setLines] = useState<Line[]>([{ product_id: "", quantity: 1 }]);
  const { run, busy } = useAction();
  useEffect(() => {
    if (open) {
      setSupplier(preset?.supplier_id ?? "");
      setWh(preset?.warehouse_id ?? "");
      setLines(preset?.lines?.length ? preset.lines : [{ product_id: "", quantity: 1 }]);
      setExpected("");
    }
  }, [open, preset]);
  const submit = async (submitNow: boolean) => {
    const r = await run("po", () => api.post<Row>("/purchase-orders", { supplier_id: supplier, warehouse_id: wh, expected_at: expected || undefined,
      lines: lines.filter((l) => l.product_id && l.quantity > 0), submit: submitNow }), (x: Row) => `${x.po.po_id} ${submitNow ? "issued" : "saved as draft"}`);
    if (r) onClose(r.po.po_id);
  };
  return (
    <Drawer open={open} onClose={() => onClose()} wide title="New purchase order"
      footer={<><button className="btn" onClick={() => onClose()}>Cancel</button>
        <button className="btn" disabled={!!busy || !supplier || !wh} onClick={() => submit(false)}>Save draft</button>
        <button className="btn primary" disabled={!!busy || !supplier || !wh || !lines.some((l) => l.product_id)} onClick={() => submit(true)}><Send size={14} />Issue to supplier</button></>}>
      <div className="grid g3" style={{ marginBottom: 0 }}>
        <label className="field">Supplier id<input className="input mono" value={supplier} onChange={(e) => setSupplier(e.target.value.toUpperCase())} placeholder="SUP0001" /></label>
        <label className="field">Deliver to
          <select className="input" value={wh} onChange={(e) => setWh(e.target.value)}><option value="">Select warehouse…</option>{lk?.warehouses.map((w: Row) => <option key={w.warehouse_id} value={w.warehouse_id}>{w.warehouse_id} · {w.warehouse_name}</option>)}</select>
        </label>
        <label className="field">Expected date<input className="input" type="date" min={lk?.today} value={expected} onChange={(e) => setExpected(e.target.value)} /></label>
      </div>
      <div className="small muted">Leave the date empty to use the supplier's contracted lead time. Unit costs default to the product's standard cost.</div>
      <Card title="Lines" flush actions={<button className="btn sm" onClick={() => setLines([...lines, { product_id: "", quantity: 1 }])}><Plus size={13} />Add line</button>}>
        <table>
          <thead><tr><th>Product id</th><th className="num">Quantity</th><th /></tr></thead>
          <tbody>
            {lines.map((l, i) => (
              <tr key={i}>
                <td><input className="input mono" value={l.product_id} placeholder="IP00001" onChange={(e) => setLines(lines.map((x, j) => (j === i ? { ...x, product_id: e.target.value.toUpperCase() } : x)))} /></td>
                <td className="num"><input className="input" type="number" min={1} style={{ width: 110, textAlign: "right" }} value={l.quantity} onChange={(e) => setLines(lines.map((x, j) => (j === i ? { ...x, quantity: Number(e.target.value) } : x)))} /></td>
                <td><button className="iconbtn" onClick={() => setLines(lines.filter((_, j) => j !== i))} disabled={lines.length === 1}><Trash2 size={14} /></button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    </Drawer>
  );
}

export function PurchasingPage() {
  const nav = useNavigate();
  const [params] = useSearchParams();
  const [tab, setTab] = useState<"goods" | "material" | "suggestions">("goods");
  const [status, setStatus] = useState(params.get("late") ? "late" : "active");
  const [q, setQ] = useState("");
  const [sort, setSort] = useState("");
  const [offset, setOffset] = useState(0);
  const [create, setCreate] = useState<{ supplier_id: string; warehouse_id: string; lines: Line[] } | null | false>(false);
  const dq = useDebounced(q);
  useEffect(() => setOffset(0), [tab, status, dq, sort]);
  const { data: sum } = useQuery<Row>("/purchase-orders/summary");
  const list = useQuery<Row>(tab !== "suggestions" ? "/purchase-orders" : null, { type: tab, status: status === "late" ? "" : status === "all" ? "" : status, late: status === "late", q: dq, sort, limit: 50, offset });
  const sugg = useQuery<Row[]>(tab === "suggestions" ? "/purchasing/suggestions" : null);
  return (
    <div>
      <PageHead title="Purchasing" icon={<ClipboardList size={20} />} sub="Purchase orders for finished goods (to warehouses) and raw materials (to plants), with receiving and reorder suggestions."
        actions={<button className="btn primary" onClick={() => setCreate(null)}><Plus size={14} />New purchase order</button>} />
      {sum && (
        <div className="grid g-side">
          <div className="kpis" style={{ gridTemplateColumns: "1fr 1fr", marginBottom: 0 }}>
            <Kpi label="Open goods POs" value={fmt.n(sum.goods.open)} sub={`${fmt.n(sum.goods.draft)} drafts`} tone="info" />
            <Kpi label="Overdue goods POs" value={fmt.n(sum.goods.late)} tone="warn" sub="past expected date" />
            <Kpi label="Open material POs" value={fmt.n(sum.material.open)} tone="info" sub={`${fmt.n(sum.material.last_30d)} placed in 30 days`} />
            <Kpi label="Slipped material POs" value={fmt.n(sum.material.slipped)} tone="bad" sub="expected after promised" />
          </div>
          <Card title="Purchase spend" sub="monthly, last 12 months">
            <Bars data={sum.spend_monthly} x="month" stacked yFmt={fmt.money} height={200}
              series={[{ key: "goods", name: "Finished goods", color: "#2563eb" }, { key: "material", name: "Raw materials", color: "#0e7490" }]} />
          </Card>
        </div>
      )}
      <div style={{ height: 14 }} />
      <Tabs value={tab} onChange={setTab} options={[["goods", "Finished goods POs"], ["material", "Raw material POs"], ["suggestions", "Reorder suggestions"]]} />
      <Card flush>
        {tab !== "suggestions" ? (
          <>
            <div className="toolbar">
              <input className="input" style={{ width: 260 }} placeholder="PO, supplier id or name" value={q} onChange={(e) => setQ(e.target.value)} />
              <Seg value={status} onChange={setStatus} options={[["active", "Active"], ["late", "Overdue"], ["draft", "Drafts"], ["received", "Received"], ["cancelled", "Cancelled"], ["all", "All"]]} />
            </div>
            <ErrorBox error={list.error} />
            {list.loading && !list.data ? <Loading /> : (
              <DataTable rows={list.data?.rows ?? []} sort={sort} onSort={setSort} onRow={(r) => nav(`/purchasing/${r.po_id}`)} cols={[
                { key: "po_id", label: "PO", sort: true }, { key: "supplier_name", label: "Supplier", sort: true }, { key: "ship_to", label: "Ship to" },
                { key: "ordered_at", label: "Ordered", sort: true }, { key: "expected_at", label: "Expected", sort: true },
                { key: "days_late", label: "Late", num: true, sort: true, render: (r) => r.days_late > 0 && r.status !== "received" ? <b className="bad-t">{r.days_late} d</b> : <span className="muted">–</span> },
                { key: "lines", num: true }, { key: "value", num: true, sort: true, render: (r) => fmt.money(r.value) },
                { key: "status", render: (r) => <Status value={r.status} /> },
              ]} />
            )}
            <Pager page={list.data as any} onOffset={setOffset} />
          </>
        ) : sugg.loading ? <Loading /> : (
          <>
            <div className="note" style={{ margin: 14 }}>Positions where available stock plus open orders is below minimum stock. The suggested quantity brings the position back to maximum stock. Click “Order” to open a pre-filled purchase order.</div>
            <DataTable rows={sugg.data ?? []} empty="Every position is covered by stock or open orders." cols={[
              { key: "product_id", label: "Product" }, { key: "sku", label: "SKU" }, { key: "warehouse_id", label: "WH" }, { key: "supplier_id", label: "Supplier" },
              { key: "on_hand", label: "On hand", num: true }, { key: "on_order", label: "On order", num: true }, { key: "min_stock", label: "Min", num: true },
              { key: "suggested_qty", label: "Suggest", num: true, render: (r) => <b>{fmt.n(r.suggested_qty)}</b> },
              { key: "est_value", label: "Value", num: true, render: (r) => fmt.money(r.est_value) },
              { key: "lead_time_days", label: "Lead time", num: true, render: (r) => `${r.lead_time_days} d` },
              { key: "go", label: "", render: (r) => <button className="btn sm" onClick={() => setCreate({ supplier_id: r.supplier_id, warehouse_id: r.warehouse_id, lines: [{ product_id: r.product_id, quantity: r.suggested_qty }] })}>Order</button> },
            ]} />
          </>
        )}
      </Card>
      <CreatePO open={create !== false} preset={create || undefined} onClose={(id) => { setCreate(false); if (id) nav(`/purchasing/${id}`); }} />
    </div>
  );
}

export function PODetail() {
  const { id = "" } = useParams();
  const { data: d, error, loading, reload } = useQuery<Row>(`/purchase-orders/${id}`);
  const [receiving, setReceiving] = useState(false);
  const [qty, setQty] = useState<Record<string, number>>({});
  const { run, busy } = useAction();
  if (loading && !d) return <Loading />;
  if (error) return <ErrorBox error={error} />;
  const po = d!.po;
  const goods = po.type === "goods";
  const open = ["open", "partial"].includes(po.status);
  const startReceive = () => {
    setQty(Object.fromEntries(d!.lines.map((l: Row) => [l.line_id, Math.max(0, l.quantity_ordered - (l.quantity_received ?? 0))])));
    setReceiving(true);
  };
  const receive = async () => {
    const lines = Object.entries(qty).filter(([, q]) => q > 0).map(([line_id, quantity]) => ({ line_id, quantity }));
    const r = await run("rcv", () => api.post(`/purchase-orders/${id}/receive`, { lines }), `Received ${lines.reduce((s, l) => s + l.quantity, 0)} units into ${po.ship_to}`);
    if (r) { setReceiving(false); reload(); }
  };
  return (
    <div>
      <PageHead crumbs={[["Purchasing", "/purchasing"]]} title={<>Purchase order <span className="mono">{id}</span> <Status value={po.status} /></>}
        sub={<><EntityLink id={po.supplier_id}>{po.supplier_name}</EntityLink> → {goods ? <EntityLink id={po.ship_to} /> : `plant ${po.ship_to}`} · {goods ? "finished goods" : "raw materials"}</>}
        actions={goods && <>
          {po.status === "draft" && <button className="btn primary" disabled={!!busy} onClick={async () => { await run("s", () => api.post(`/purchase-orders/${id}/submit`), "Issued to supplier"); reload(); }}><Send size={14} />Issue</button>}
          {["draft", "open"].includes(po.status) && <button className="btn danger" disabled={!!busy} onClick={async () => { if (confirm(`Cancel ${id}?`)) { await run("c", () => api.post(`/purchase-orders/${id}/cancel`), "Purchase order cancelled"); reload(); } }}><XCircle size={14} />Cancel</button>}
          {open && <button className="btn primary" onClick={startReceive}><PackageCheck size={14} />Receive goods</button>}
        </>} />
      <div className="grid g-main">
        <Card title="Lines" flush>
          <DataTable rows={d!.lines} cols={[
            { key: "item_id", label: "Item" }, { key: "item_name", label: "Description" },
            { key: "quantity_ordered", label: "Ordered", num: true, render: (r) => fmt.n(r.quantity_ordered) },
            { key: "quantity_received", label: "Received", num: true, render: (r) => fmt.n(r.quantity_received ?? 0) },
            { key: "uom", label: "UoM" }, { key: "unit_cost", label: "Unit cost", num: true, render: (r) => fmt.moneyFull(r.unit_cost) },
            { key: "total", label: "Total", num: true, render: (r) => fmt.moneyFull(r.unit_cost * r.quantity_ordered) },
          ]} />
        </Card>
        <Card title="Details">
          <KV items={[["Ordered", po.ordered_at], ["Expected", po.expected_at], ...(po.promised_at ? [["Promised", po.promised_at] as [string, string]] : []),
            ["Received", po.received_at ?? "not yet"], ["Days late", po.days_late > 0 && po.status !== "received" ? <b className="bad-t">{po.days_late}</b> : "–"], ["Value", fmt.moneyFull(po.value)]]} />
          {d!.shipment && <div style={{ marginTop: 14 }}>Inbound shipment <EntityLink id={d!.shipment.shipment_id} /> · <Status value={d!.shipment.status} /></div>}
        </Card>
      </div>
      <div className="grid g2">
        <Card title="Receipts" flush>
          <DataTable rows={d!.receipts} empty="Nothing received yet." cols={[{ key: "movement_id", label: "Movement", render: (r) => <span className="mono">{r.movement_id}</span> }, { key: "movement_at", label: "Date" }, { key: "item_id", label: "Item" }, { key: "quantity_change", label: "Qty", num: true }]} />
        </Card>
        <Card title={goods ? "Supplier date changes" : "Expected-date revisions"} flush>
          <DataTable rows={d!.revisions} empty="No revisions." cols={[{ key: "revised_at", label: "Revised" }, { key: "old_expected_at", label: "From" }, { key: "new_expected_at", label: "To" }, { key: "reason_code", label: "Reason", render: (r) => fmt.label(r.reason_code) }]} />
          {d!.disruptions.length > 0 && <div style={{ padding: 14 }} className="small">Linked disruptions: {d!.disruptions.map((e: Row) => `${e.event_id} (${e.title})`).join(", ")}</div>}
        </Card>
      </div>
      <Drawer open={receiving} onClose={() => setReceiving(false)} title={`Receive ${id} into ${po.ship_to}`}
        footer={<><button className="btn" onClick={() => setReceiving(false)}>Cancel</button><button className="btn primary" disabled={!!busy} onClick={receive}><PackageCheck size={14} />Post receipt</button></>}>
        <div className="note">Receiving posts stock to the ledger, updates the order to partial or received, closes the inbound shipment and creates a put-away task at the warehouse.</div>
        <table>
          <thead><tr><th>Item</th><th className="num">Outstanding</th><th className="num">Receive now</th></tr></thead>
          <tbody>{d!.lines.map((l: Row) => {
            const out = l.quantity_ordered - (l.quantity_received ?? 0);
            return (
              <tr key={l.line_id}>
                <td>{l.item_id}<div className="small muted">{l.item_name}</div></td>
                <td className="num">{fmt.n(out)}</td>
                <td className="num"><input className="input" type="number" min={0} max={out} style={{ width: 100, textAlign: "right" }} value={qty[l.line_id] ?? 0} onChange={(e) => setQty({ ...qty, [l.line_id]: Math.min(out, Math.max(0, Number(e.target.value))) })} /></td>
              </tr>
            );
          })}</tbody>
        </table>
      </Drawer>
    </div>
  );
}
