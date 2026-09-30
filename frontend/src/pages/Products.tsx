import { AdviceBox, MemoryPanel } from "../components/Memory";
import { Package, Pencil } from "lucide-react";
import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { ForecastChart } from "../components/charts";
import { Bar, Card, DataTable, Drawer, EntityLink, ErrorBox, Kpi, KV, Loading, PageHead, Pager, Status, useAction } from "../components/ui";
import { api, fmt, Row, useDebounced, useQuery } from "../lib/api";

export function ProductsPage() {
  const nav = useNavigate();
  const [q, setQ] = useState("");
  const [category, setCategory] = useState("");
  const [abc, setAbc] = useState("");
  const [sort, setSort] = useState("units_13w:desc");
  const [offset, setOffset] = useState(0);
  const dq = useDebounced(q);
  useEffect(() => setOffset(0), [dq, category, abc, sort]);
  const { data: cats } = useQuery<Row[]>("/categories");
  const { data, error, loading } = useQuery<Row>("/products", { q: dq, category, abc, sort, limit: 50, offset });
  return (
    <div>
      <PageHead title="Products" icon={<Package size={20} />} sub="Every SKU with its category, sourcing, price, stock across warehouses and recent demand." />
      {cats && (
        <div className="kpis">
          {cats.map((c: Row) => (
            <button key={c.category} className={`kpi ${category === c.category ? "info" : ""}`} style={{ textAlign: "left", cursor: "pointer" }} onClick={() => setCategory(category === c.category ? "" : c.category)}>
              <div className="kpi-top"><span>{fmt.label(c.category)}</span></div>
              <div className="kpi-value">{fmt.n(c.products)}</div>
              <div className="kpi-sub">{fmt.compact(c.units)} units · {fmt.money(c.stock_value)}</div>
            </button>
          ))}
        </div>
      )}
      <Card flush>
        <div className="toolbar">
          <input className="input" style={{ width: 280 }} placeholder="Search SKU, product id or brand" value={q} onChange={(e) => setQ(e.target.value)} />
          <select className="input" value={abc} onChange={(e) => setAbc(e.target.value)}>
            <option value="">All ABC classes</option><option value="A">Class A</option><option value="B">Class B</option><option value="C">Class C</option>
          </select>
          {category && <button className="btn sm ghost" onClick={() => setCategory("")}>Category: {fmt.label(category)} ✕</button>}
        </div>
        <ErrorBox error={error} />
        {loading && !data ? <Loading /> : (
          <DataTable rows={data!.rows} sort={sort} onSort={setSort} onRow={(r) => nav(`/products/${r.product_id}`)} cols={[
            { key: "product_id", label: "Product", sort: true },
            { key: "sku", label: "SKU", sort: true },
            { key: "category", sort: true, render: (r) => fmt.label(r.category) },
            { key: "brand_family", label: "Brand", sort: true },
            { key: "abc_class", label: "ABC" },
            { key: "sourcing_mode", label: "Source", render: (r) => <Status value={r.sourcing_mode === "make" ? "make" : "buy"} tone={r.sourcing_mode === "make" ? "violet" : "info"} /> },
            { key: "supplier_id", label: "Supplier" },
            { key: "unit_price", label: "Price", num: true, sort: true, render: (r) => fmt.moneyFull(r.unit_price) },
            { key: "gross_margin_pct", label: "Margin", num: true, render: (r) => `${fmt.n(r.gross_margin_pct, 1)}%` },
            { key: "on_hand", label: "On hand", num: true, sort: true, render: (r) => fmt.n(r.on_hand) },
            { key: "stock_value", label: "Stock value", num: true, sort: true, render: (r) => fmt.money(r.stock_value) },
            { key: "units_13w", label: "Sold 13w", num: true, sort: true, render: (r) => fmt.n(r.units_13w) },
          ]} />
        )}
        <Pager page={data as any} onOffset={setOffset} />
      </Card>
    </div>
  );
}

export function ProductDetail() {
  const { id = "" } = useParams();
  const { data: d, error, loading, reload } = useQuery<Row>(`/products/${id}`);
  const { data: f } = useQuery<Row>("/forecast", { level: "product", key: id, horizon: 12 });
  const [edit, setEdit] = useState(false);
  const [price, setPrice] = useState("");
  const [rop, setRop] = useState("");
  const { run, busy } = useAction();
  if (loading && !d) return <Loading />;
  if (error) return <ErrorBox error={error} />;
  const p = d!.product;
  const onHand = d!.stock.reduce((s: number, r: Row) => s + r.on_hand, 0);
  const avail = d!.stock.reduce((s: number, r: Row) => s + r.available, 0);
  const onOrder = d!.stock.reduce((s: number, r: Row) => s + r.on_order, 0);
  return (
    <div>
      <PageHead crumbs={[["Products", "/products"]]} title={<>{p.sku} <span className="muted mono" style={{ fontSize: 14 }}>{id}</span></>}
        sub={<>{fmt.label(p.category)} · {p.brand_family} · class {p.abc_class} · {p.sourcing_mode === "make" ? `made at ${p.plant_id ?? "plant"}` : <>bought from <EntityLink id={p.supplier_id}>{p.supplier_name}</EntityLink></>}</>}
        actions={<button className="btn" onClick={() => { setPrice(String(p.unit_price)); setRop(String(p.reorder_point)); setEdit(true); }}><Pencil size={14} />Edit</button>} />
      <div className="kpis">
        <Kpi label="On hand" value={fmt.n(onHand)} sub={`${fmt.n(avail)} available`} />
        <Kpi label="On order" value={fmt.n(onOrder)} sub="open purchase orders" />
        <Kpi label="Unit price" value={fmt.moneyFull(p.unit_price)} sub={`cost ${fmt.moneyFull(p.unit_cost)} · ${fmt.n(p.gross_margin_pct, 1)}% margin`} />
        <Kpi label="Reorder point" value={fmt.n(p.reorder_point)} sub="min stock per warehouse" />
        <Kpi label="Return rate" value={d!.returns.rate === null ? "–" : fmt.pct(d!.returns.rate, 2)} sub={`${d!.returns.rmas} RMAs · ${fmt.n(d!.returns.units_sold)} sold`} />
      </div>
      <div className="grid g-main">
        <Card title="Demand & forecast" sub={f?.model ? `${f.model.method}${f.accuracy?.model_wape != null ? ` · backtest error ${fmt.pct(f.accuracy.model_wape)}` : ""}` : "weekly units"}>
          {f ? <ForecastChart history={f.history.slice(-52)} forecast={f.forecast} height={260} /> : <Loading />}
        </Card>
        <Card title="Stock by warehouse" flush>
          <DataTable rows={d!.stock} cols={[
            { key: "warehouse_id", label: "WH" },
            { key: "on_hand", label: "On hand", num: true, render: (r) => fmt.n(r.on_hand) },
            { key: "min_stock", label: "Min", num: true, render: (r) => fmt.n(r.min_stock) },
            { key: "level", label: "", render: (r) => <Bar value={r.on_hand} max={r.max_stock} tone={r.status === "low" || r.status === "out" ? "bad" : r.status === "excess" ? "warn" : "good"} /> },
            { key: "status", render: (r) => <Status value={r.status} /> },
          ]} />
        </Card>
      </div>
      <div style={{ marginBottom: 14 }}><MemoryPanel entityId={id} /></div>
      <div className="grid g2">
        <Card title="Recent purchase orders" flush>
          <DataTable rows={d!.purchase_orders} empty="No purchase orders." cols={[
            { key: "purchase_order_id", label: "PO" }, { key: "warehouse_id", label: "WH" }, { key: "ordered_at", label: "Ordered" },
            { key: "quantity_ordered", label: "Qty", num: true }, { key: "quantity_received", label: "Received", num: true },
            { key: "status", render: (r) => <Status value={r.status} /> },
          ]} />
        </Card>
        <Card title="Recent sales orders" flush>
          <DataTable rows={d!.sales_orders} empty="No sales orders." cols={[
            { key: "sales_order_id", label: "Order" }, { key: "customer_name", label: "Customer" }, { key: "order_date", label: "Date" },
            { key: "qty_ordered", label: "Qty", num: true }, { key: "status", render: (r) => <Status value={r.status} /> },
          ]} />
        </Card>
      </div>
      <div className="grid g2">
        <Card title="Bill of materials" sub="current version" flush>
          <DataTable rows={d!.bom} empty={p.sourcing_mode === "make" ? "No BOM in effect." : "Bought-in product — no BOM."} cols={[
            { key: "rm_id", label: "Material" }, { key: "rm_name", label: "Name" }, { key: "qty_per_unit", label: "Per unit", num: true },
            { key: "uom", label: "UoM" }, { key: "criticality", render: (r) => <Status value={r.criticality} tone={r.criticality === "high" ? "bad" : "plain"} /> },
          ]} />
        </Card>
        <Card title="Production runs" flush>
          <DataTable rows={d!.production_runs} empty="No production runs." cols={[
            { key: "production_run_id", label: "Run" }, { key: "plant_id", label: "Plant" }, { key: "planned_start", label: "Planned" },
            { key: "planned_qty", label: "Qty", num: true }, { key: "delay_days", label: "Delay", num: true },
          ]} />
        </Card>
      </div>
      <Drawer open={edit} onClose={() => setEdit(false)} title={`Edit ${p.sku}`}
        footer={<><button className="btn" onClick={() => setEdit(false)}>Cancel</button>
          <button className="btn primary" disabled={!!busy} onClick={async () => {
            const r = await run("save", () => api.patch(`/products/${id}`, { unit_price: Number(price), reorder_point: Number(rop) }), "Product updated");
            if (r) { setEdit(false); reload(); }
          }}>Save changes</button></>}>
        <label className="field">Unit price ($)<input className="input" type="number" step="0.01" value={price} onChange={(e) => setPrice(e.target.value)} /></label>
        <label className="field">Reorder point (min stock per warehouse)<input className="input" type="number" value={rop} onChange={(e) => setRop(e.target.value)} /></label>
        <div className="note">Changing the reorder point updates min stock in every warehouse and re-evaluates low-stock alerts and reorder suggestions immediately. The change is recorded in the audit log.</div>
      </Drawer>
    </div>
  );
}
