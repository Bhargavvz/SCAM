import { ArrowLeftRight, Boxes, SlidersHorizontal } from "lucide-react";
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Bars, Donut, TrendArea } from "../components/charts";
import { Bar, Card, DataTable, Drawer, EntityLink, ErrorBox, Kpi, Loading, PageHead, Pager, Seg, Status, Tabs, useAction } from "../components/ui";
import { api, fmt, Row, useDebounced, useQuery } from "../lib/api";

function Movement({ open, onClose, preset, mode }: { open: boolean; onClose: () => void; preset: Row | null; mode: "adjust" | "transfer" }) {
  const { data: lk } = useQuery<Row>("/lookup");
  const [pid, setPid] = useState("");
  const [wh, setWh] = useState("");
  const [to, setTo] = useState("");
  const [qty, setQty] = useState("");
  const [reason, setReason] = useState("cycle count correction");
  const { run, busy } = useAction();
  useEffect(() => {
    if (open) {
      setPid(preset?.product_id ?? "");
      setWh(preset?.warehouse_id ?? "");
      setTo("");
      setQty("");
    }
  }, [open, preset]);
  const submit = async () => {
    const r = mode === "adjust"
      ? await run("m", () => api.post("/inventory/adjust", { product_id: pid, warehouse_id: wh, quantity_change: Number(qty), reason }), (x: Row) => `Posted ${x.movement_id}`)
      : await run("m", () => api.post("/inventory/transfer", { product_id: pid, from_warehouse_id: wh, to_warehouse_id: to, quantity: Number(qty) }), (x: Row) => `Transfer ${x.transfer_id} posted`);
    if (r) onClose();
  };
  return (
    <Drawer open={open} onClose={onClose} title={mode === "adjust" ? "Adjust stock" : "Transfer stock"}
      footer={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={!!busy || !pid || !wh || !qty || (mode === "transfer" && !to)} onClick={submit}>Post movement</button></>}>
      <label className="field">Product id<input className="input mono" value={pid} onChange={(e) => setPid(e.target.value.toUpperCase())} placeholder="IP00001" /></label>
      <label className="field">{mode === "adjust" ? "Warehouse" : "From warehouse"}
        <select className="input" value={wh} onChange={(e) => setWh(e.target.value)}><option value="">Select…</option>{lk?.warehouses.map((w: Row) => <option key={w.warehouse_id} value={w.warehouse_id}>{w.warehouse_id} · {w.warehouse_name}</option>)}</select>
      </label>
      {mode === "transfer" && (
        <label className="field">To warehouse
          <select className="input" value={to} onChange={(e) => setTo(e.target.value)}><option value="">Select…</option>{lk?.warehouses.filter((w: Row) => w.warehouse_id !== wh).map((w: Row) => <option key={w.warehouse_id} value={w.warehouse_id}>{w.warehouse_id} · {w.warehouse_name}</option>)}</select>
        </label>
      )}
      <label className="field">{mode === "adjust" ? "Quantity change (+ found, − lost/damaged)" : "Quantity"}<input className="input" type="number" value={qty} onChange={(e) => setQty(e.target.value)} /></label>
      {mode === "adjust" && <label className="field">Reason<input className="input" value={reason} onChange={(e) => setReason(e.target.value)} /></label>}
      <div className="note">Posts to the inventory ledger on the business date, updates stock immediately and re-evaluates alerts. Negative stock is refused.</div>
    </Drawer>
  );
}

export function InventoryPage() {
  const [params, setParams] = useSearchParams();
  const tab = (params.get("tab") ?? "stock") as "stock" | "movements" | "materials";
  const [status, setStatus] = useState(params.get("status") ?? "");
  const [wh, setWh] = useState("");
  const [category, setCategory] = useState("");
  const [q, setQ] = useState(params.get("q") ?? "");
  const [sort, setSort] = useState("");
  const [offset, setOffset] = useState(0);
  const [mtype, setMtype] = useState("");
  const [drawer, setDrawer] = useState<{ mode: "adjust" | "transfer"; preset: Row | null } | null>(null);
  const dq = useDebounced(q);
  useEffect(() => setOffset(0), [status, wh, category, dq, sort, tab, mtype]);
  const { data: s, reload: reloadSummary } = useQuery<Row>("/inventory/summary");
  const { data: lk } = useQuery<Row>("/lookup");
  const stock = useQuery<Row>(tab === "stock" ? "/inventory/stock" : null, { status, warehouse_id: wh, category, q: dq, sort, limit: 50, offset });
  const moves = useQuery<Row>(tab === "movements" ? "/inventory/movements" : null, { warehouse_id: wh, movement_type: mtype, q: dq, limit: 50, offset });
  const mats = useQuery<Row>(tab === "materials" ? "/inventory/materials" : null, { q: dq, status: status === "low" ? "short" : "" });
  const close = () => {
    setDrawer(null);
    reloadSummary();
    stock.reload();
    moves.reload();
  };
  const k = s?.kpis;
  return (
    <div>
      <PageHead title="Inventory" icon={<Boxes size={20} />} sub="Finished goods by warehouse from the live ledger, and raw materials by plant from the weekly snapshot."
        actions={<><button className="btn" onClick={() => setDrawer({ mode: "transfer", preset: null })}><ArrowLeftRight size={14} />Transfer</button>
          <button className="btn primary" onClick={() => setDrawer({ mode: "adjust", preset: null })}><SlidersHorizontal size={14} />Adjust stock</button></>} />
      {k && (
        <div className="kpis">
          <Kpi label="Units on hand" value={fmt.compact(k.units)} sub={`${fmt.n(k.positions)} stock positions`} />
          <Kpi label="Inventory value" value={fmt.money(k.value)} sub="at unit cost" />
          <Kpi label="Below minimum" value={fmt.n(k.low)} tone="warn" sub="positions" />
          <Kpi label="Out of stock" value={fmt.n(k.out_of_stock)} tone={k.out_of_stock ? "bad" : "good"} sub="positions" />
          <Kpi label="Above maximum" value={fmt.n(k.excess)} tone="info" sub="positions" />
          <Kpi label="Materials below safety" value={fmt.n(s!.raw_materials.below_safety)} tone={s!.raw_materials.below_safety ? "bad" : "good"} sub={`of ${s!.raw_materials.positions} plant positions`} />
        </div>
      )}
      {s && (
        <div className="grid g3">
          <Card title="Warehouse utilization" flush>
            <div className="stack" style={{ padding: 14, gap: 9 }}>
              {s.by_warehouse.map((w: Row) => (
                <div key={w.warehouse_id} className="row" style={{ flexWrap: "nowrap" }}>
                  <span className="mono" style={{ width: 42 }}>{w.warehouse_id}</span>
                  <div className="grow"><Bar value={Math.min(w.utilization, 1.5)} max={1.5} tone={w.utilization > 1 ? "bad" : w.utilization > 0.85 ? "warn" : "good"} /></div>
                  <span className="small muted" style={{ width: 46, textAlign: "right" }}>{fmt.pct(w.utilization, 0)}</span>
                </div>
              ))}
            </div>
          </Card>
          <Card title="Value by category"><Donut data={s.by_category} nameKey="category" valueKey="value" fmtValue={fmt.money} height={170} /></Card>
          <Card title="Inbound vs outbound" sub="units per month"><Bars data={s.flow} x="month" series={[{ key: "received", name: "Received", color: "#2563eb" }, { key: "shipped", name: "Shipped", color: "#059669" }]} height={200} /></Card>
        </div>
      )}
      <Tabs value={tab} onChange={(t) => setParams({ tab: t })} options={[["stock", "Stock positions"], ["movements", "Movement ledger"], ["materials", "Raw materials"]]} />
      <Card flush>
        <div className="toolbar">
          <input className="input" style={{ width: 240 }} placeholder={tab === "materials" ? "Material id or name" : "Product id or SKU"} value={q} onChange={(e) => setQ(e.target.value)} />
          {tab !== "materials" && (
            <select className="input" value={wh} onChange={(e) => setWh(e.target.value)}><option value="">All warehouses</option>{lk?.warehouses.map((w: Row) => <option key={w.warehouse_id} value={w.warehouse_id}>{w.warehouse_id} · {w.region}</option>)}</select>
          )}
          {tab === "stock" && (
            <>
              <select className="input" value={category} onChange={(e) => setCategory(e.target.value)}><option value="">All categories</option>{lk?.categories.map((c: string) => <option key={c} value={c}>{fmt.label(c)}</option>)}</select>
              <Seg value={status} onChange={setStatus} options={[["", "All"], ["attention", "Needs attention"], ["low", "Low"], ["out", "Out"], ["excess", "Excess"], ["ok", "OK"]]} />
            </>
          )}
          {tab === "movements" && <Seg value={mtype} onChange={setMtype} options={[["", "All"], ["receipt", "Receipts"], ["sale", "Sales"], ["transfer", "Transfers"], ["adjustment", "Adjustments"], ["return", "Returns"]]} />}
          {tab === "materials" && <Seg value={status} onChange={setStatus} options={[["", "All"], ["low", "Below safety"]]} />}
        </div>
        {tab === "stock" && (stock.loading && !stock.data ? <Loading /> : <>
          <ErrorBox error={stock.error} />
          <DataTable rows={stock.data?.rows ?? []} sort={sort} onSort={setSort} cols={[
            { key: "product_id", label: "Product", sort: true }, { key: "sku", label: "SKU" }, { key: "category", render: (r) => fmt.label(r.category) },
            { key: "warehouse_id", label: "WH", sort: true },
            { key: "on_hand", label: "On hand", num: true, sort: true, render: (r) => fmt.n(r.on_hand) },
            { key: "allocated", label: "Allocated", num: true, render: (r) => fmt.n(r.allocated) },
            { key: "available", label: "Available", num: true, sort: true, render: (r) => fmt.n(r.available) },
            { key: "min_stock", label: "Min", num: true, sort: true, render: (r) => fmt.n(r.min_stock) },
            { key: "on_order", label: "On order", num: true, sort: true, render: (r) => fmt.n(r.on_order) },
            { key: "weeks_of_cover", label: "Cover (wk)", num: true, sort: true, render: (r) => r.weeks_of_cover === null ? <span className="muted">no demand</span> : fmt.n(r.weeks_of_cover, 1) },
            { key: "value", label: "Value", num: true, sort: true, render: (r) => fmt.money(r.value) },
            { key: "status", render: (r) => <Status value={r.status} /> },
            { key: "act", label: "", render: (r) => <button className="btn sm ghost" onClick={() => setDrawer({ mode: "adjust", preset: r })}>Adjust</button> },
          ]} />
          <Pager page={stock.data as any} onOffset={setOffset} />
        </>)}
        {tab === "movements" && (moves.loading && !moves.data ? <Loading /> : <>
          <DataTable rows={moves.data?.rows ?? []} cols={[
            { key: "movement_id", label: "Movement", render: (r) => <span className="mono">{r.movement_id}</span> }, { key: "movement_at", label: "Date" },
            { key: "movement_type", label: "Type", render: (r) => <Status value={r.movement_type} tone={r.movement_type === "receipt" ? "info" : r.movement_type === "sale" ? "good" : "plain"} /> },
            { key: "product_id", label: "Product" }, { key: "sku", label: "SKU" }, { key: "warehouse_id", label: "WH" },
            { key: "quantity_change", label: "Qty", num: true, render: (r) => <b className={r.quantity_change < 0 ? "bad-t" : "good-t"}>{r.quantity_change > 0 ? "+" : ""}{fmt.n(r.quantity_change)}</b> },
            { key: "ref", label: "Reference", render: (r) => r.purchase_order_line_id ?? r.transfer_id ?? "–" },
            { key: "app_created", label: "Source", render: (r) => r.app_created ? <Status value="meridian" tone="ai" /> : <span className="muted">dataset</span> },
          ]} />
          <Pager page={moves.data as any} onOffset={setOffset} />
        </>)}
        {tab === "materials" && (mats.loading && !mats.data ? <Loading /> : <>
          <div className="small muted" style={{ padding: "8px 14px" }}>Weekly snapshot of {mats.data?.snapshot_date}</div>
          <DataTable rows={mats.data?.rows ?? []} cols={[
            { key: "rm_id", label: "Material", render: (r) => <span className="mono">{r.rm_id}</span> }, { key: "rm_name", label: "Name" },
            { key: "rm_category", label: "Category" }, { key: "plant_id", label: "Plant" },
            { key: "on_hand", label: "On hand", num: true, render: (r) => `${fmt.n(r.on_hand, 1)} ${r.uom}` },
            { key: "safety_stock", label: "Safety", num: true, render: (r) => fmt.n(r.safety_stock, 1) },
            { key: "level", label: "", render: (r) => <Bar value={r.on_hand} max={Math.max(r.safety_stock * 2, r.on_hand)} tone={r.status === "ok" ? "good" : "bad"} /> },
            { key: "days_of_cover", label: "Cover (days)", num: true },
            { key: "criticality", render: (r) => <Status value={r.criticality} tone={r.criticality === "high" ? "bad" : "plain"} /> },
            { key: "is_single_source", label: "Sourcing", render: (r) => r.is_single_source ? <Status value="single source" tone="warn" /> : "multi" },
            { key: "status", render: (r) => <Status value={r.status} /> },
          ]} />
        </>)}
      </Card>
      <Movement open={!!drawer} mode={drawer?.mode ?? "adjust"} preset={drawer?.preset ?? null} onClose={close} />
    </div>
  );
}
