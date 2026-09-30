import { LineChart as LineIcon } from "lucide-react";
import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { ForecastChart } from "../components/charts";
import { Card, DataTable, ErrorBox, Kpi, Loading, PageHead, Seg, Status } from "../components/ui";
import { fmt, Row, useQuery } from "../lib/api";

export function Forecasting() {
  const [params, setParams] = useSearchParams();
  const level = params.get("level") ?? "total";
  const key = params.get("key") ?? "";
  const [horizon, setHorizon] = useState("12");
  const [pid, setPid] = useState(level === "product" ? key : "");
  const { data: ov } = useQuery<Row>("/forecast/overview");
  const { data: lk } = useQuery<Row>("/lookup");
  const { data: f, error, loading } = useQuery<Row>(level === "total" || key ? "/forecast" : null, { level, key: level === "total" ? undefined : key, horizon });
  const set = (l: string, k = "") => setParams(k ? { level: l, key: k } : { level: l });
  const acc = f?.accuracy;
  return (
    <div>
      <PageHead title="Demand forecasting" icon={<LineIcon size={20} />}
        sub="Weekly demand forecast from three years of history. Holt-Winters for regular series, Syntetos-Boylan for intermittent SKUs. Each forecast is backtested on the last 13 weeks against the planner forecast." />
      <Card flush>
        <div className="toolbar">
          <Seg value={level} onChange={(l) => set(l, l === "category" ? lk?.categories?.[0] ?? "" : l === "warehouse" ? "W001" : l === "product" ? pid : "")}
            options={[["total", "Total"], ["category", "Category"], ["warehouse", "Warehouse"], ["product", "Product"]]} />
          {level === "category" && <select className="input" value={key} onChange={(e) => set("category", e.target.value)}>{lk?.categories.map((c: string) => <option key={c} value={c}>{fmt.label(c)}</option>)}</select>}
          {level === "warehouse" && <select className="input" value={key} onChange={(e) => set("warehouse", e.target.value)}>{lk?.warehouses.map((w: Row) => <option key={w.warehouse_id} value={w.warehouse_id}>{w.warehouse_id} · {w.warehouse_name}</option>)}</select>}
          {level === "product" && <><input className="input mono" style={{ width: 130 }} placeholder="IP00001" value={pid} onChange={(e) => setPid(e.target.value.toUpperCase())} onKeyDown={(e) => e.key === "Enter" && set("product", pid)} /><button className="btn sm" onClick={() => set("product", pid)}>Forecast</button></>}
          <div className="grow" />
          <Seg value={horizon} onChange={setHorizon} options={[["4", "4 wk"], ["12", "12 wk"], ["26", "26 wk"]]} />
        </div>
        <div className="card-body">
          <ErrorBox error={error} />
          {loading && (level === "total" || key) ? <Loading /> : f && <ForecastChart history={f.history} forecast={f.forecast} height={320} />}
          {level === "product" && !key && <div className="empty">Enter a product id, or pick one from the fastest movers below.</div>}
        </div>
      </Card>
      {f && (
        <div className="kpis" style={{ marginTop: 14 }}>
          <Kpi label="Next 4 weeks" value={fmt.n(f.next_4w)} sub={<>units · last 4 weeks {fmt.n(f.last_4w)}</>} tone="info" />
          <Kpi label="Same 4 weeks last year" value={fmt.n(f.same_4w_last_year)} sub="seasonal reference" />
          <Kpi label="Model error (13-wk backtest)" value={acc?.model_wape != null ? fmt.pct(acc.model_wape) : "–"} sub={`bias ${acc ? fmt.pct(acc.model_bias) : "–"}`}
            tone={acc && acc.model_wape != null && acc.planner_wape != null && acc.model_wape < acc.planner_wape ? "good" : undefined} />
          <Kpi label="Planner error" value={acc?.planner_wape != null ? fmt.pct(acc.planner_wape) : "–"} sub={`bias ${acc ? fmt.pct(acc.planner_bias) : "–"}`} />
          <Kpi label="Method" value={<span style={{ fontSize: 15 }}>{f.model?.method}</span>} sub={f.model?.alpha != null ? `α ${f.model.alpha}${f.model.beta != null ? ` β ${f.model.beta} γ ${f.model.gamma}` : ""}` : undefined} />
        </div>
      )}
      <div className="grid g-main">
        <Card title="Category outlook" sub="next 4 / 12 weeks, with backtest accuracy" flush>
          {!ov ? <Loading /> : <DataTable rows={ov.categories} onRow={(r) => set("category", r.category)} cols={[
            { key: "category", render: (r) => fmt.label(r.category) }, { key: "next_4w", label: "Next 4w", num: true, render: (r) => fmt.n(r.next_4w) },
            { key: "last_4w", label: "Last 4w", num: true, render: (r) => fmt.n(r.last_4w) }, { key: "next_12w", label: "Next 12w", num: true, render: (r) => fmt.n(r.next_12w) },
            { key: "model_wape", label: "Model error", num: true, render: (r) => fmt.pct(r.model_wape) },
            { key: "planner_wape", label: "Planner error", num: true, render: (r) => fmt.pct(r.planner_wape) },
            { key: "planner_bias", label: "Planner bias", num: true, render: (r) => <span className={Math.abs(r.planner_bias) > 0.1 ? "bad-t" : undefined}>{fmt.pct(r.planner_bias)}</span> },
            { key: "better", label: "Better", render: (r) => r.model_wape != null && r.planner_wape != null ? (r.model_wape < r.planner_wape ? <Status value="model" tone="ai" /> : <Status value="planner" tone="plain" />) : "–" }]} />}
        </Card>
        <Card title="Fastest movers" sub="last 13 weeks" flush>
          {!ov ? <Loading /> : <DataTable rows={ov.top_products} onRow={(r) => { setPid(r.product_id); set("product", r.product_id); }} cols={[
            { key: "product_id", label: "Product", render: (r) => <span className="mono">{r.product_id}</span> }, { key: "sku", label: "SKU" }, { key: "units_13w", label: "Units", num: true }]} />}
        </Card>
      </div>
    </div>
  );
}
