import { BarChart3, Download } from "lucide-react";
import { useState } from "react";
import { Bars, Donut, Lines, TrendArea } from "../components/charts";
import { Card, DataTable, ErrorBox, Loading, PageHead, Tabs } from "../components/ui";
import { fmt, Row, useQuery } from "../lib/api";

const csv = (report: string, section: string) => (
  <a className="btn sm" href={`/api/reports/${report}/${section}.csv`} download><Download size={13} />CSV</a>
);

export function ReportsPage() {
  const [tab, setTab] = useState<"inventory" | "sales" | "costs" | "suppliers">("sales");
  const { data: d, error, loading } = useQuery<Row>(`/reports/${tab}`);
  return (
    <div>
      <PageHead title="Reports" icon={<BarChart3 size={20} />} sub="Standard reports over the live database. Every table can be exported as CSV." />
      <Tabs value={tab} onChange={setTab} options={[["sales", "Sales"], ["inventory", "Inventory"], ["costs", "Costs"], ["suppliers", "Supplier performance"]]} />
      <ErrorBox error={error} />
      {loading || !d ? <Loading /> : (
        <>
          {tab === "sales" && (
            <>
              <div className="grid g-main">
                <Card title="Revenue by month" actions={csv("sales", "monthly")}><TrendArea data={d.monthly} x="month" series={[{ key: "revenue", name: "Revenue", color: "#0e7490" }]} yFmt={fmt.money} height={250} /></Card>
                <Card title="Revenue by segment" actions={csv("sales", "by_segment")}><Donut data={d.by_segment} nameKey="segment" valueKey="revenue" fmtValue={fmt.money} height={180} /></Card>
              </div>
              <Card title="By category" actions={csv("sales", "by_category")} flush>
                <DataTable rows={d.by_category} cols={[{ key: "category", render: (r) => fmt.label(r.category) }, { key: "units", num: true, render: (r) => fmt.n(r.units) },
                  { key: "revenue", num: true, render: (r) => fmt.money(r.revenue) }, { key: "gross_margin", label: "Gross margin", num: true, render: (r) => fmt.money(r.gross_margin) },
                  { key: "margin_pct", label: "Margin %", num: true, render: (r) => fmt.pct(r.gross_margin / r.revenue) }]} />
              </Card>
              <div style={{ height: 14 }} />
              <div className="grid g2">
                <Card title="Top customers" actions={csv("sales", "top_customers")} flush><DataTable rows={d.top_customers} cols={[{ key: "customer_id", label: "Customer" }, { key: "customer_name", label: "Name" }, { key: "orders", num: true }, { key: "revenue", num: true, render: (r) => fmt.money(r.revenue) }]} /></Card>
                <Card title="Top products" actions={csv("sales", "top_products")} flush><DataTable rows={d.top_products} cols={[{ key: "product_id", label: "Product" }, { key: "sku", label: "SKU" }, { key: "units", num: true }, { key: "revenue", num: true, render: (r) => fmt.money(r.revenue) }]} /></Card>
              </div>
            </>
          )}
          {tab === "inventory" && (
            <>
              <div className="grid g2">
                <Card title="Value by warehouse" actions={csv("inventory", "by_warehouse")}><Bars data={d.by_warehouse} x="warehouse_id" series={[{ key: "value", name: "Value", color: "#2563eb" }]} yFmt={fmt.money} height={240} /></Card>
                <Card title="Stock by weeks of cover" sub="value" actions={csv("inventory", "cover_bands")}><Donut data={d.cover_bands.map((b: Row) => ({ ...b, band: String(b.band).slice(2) }))} nameKey="band" valueKey="value" fmtValue={fmt.money} height={190} /></Card>
              </div>
              <div className="grid g2">
                <Card title="By category" actions={csv("inventory", "by_category")} flush><DataTable rows={d.by_category} cols={[{ key: "category", render: (r) => fmt.label(r.category) }, { key: "units", num: true, render: (r) => fmt.n(r.units) }, { key: "value", num: true, render: (r) => fmt.money(r.value) }, { key: "years_of_cover", label: "Years of cover", num: true }]} /></Card>
                <Card title="By ABC class" actions={csv("inventory", "by_abc")} flush><DataTable rows={d.by_abc} cols={[{ key: "abc_class", label: "Class" }, { key: "products", num: true }, { key: "value", num: true, render: (r) => fmt.money(r.value) }]} /></Card>
              </div>
            </>
          )}
          {tab === "costs" && (
            <>
              <Card title="Cost by month" actions={csv("costs", "monthly")}>
                <Bars data={d.monthly} x="month" stacked yFmt={fmt.money} height={280} series={[
                  { key: "purchasing_goods", name: "Goods purchasing", color: "#2563eb" }, { key: "purchasing_materials", name: "Material purchasing", color: "#0e7490" },
                  { key: "freight", name: "Freight", color: "#f59e0b" }, { key: "return_refunds", name: "Return refunds", color: "#dc2626" }]} />
              </Card>
              <div style={{ height: 14 }} />
              <Card title="Disruption response cost" sub="expected vs actual, last 12 months" actions={csv("costs", "decisions")} flush>
                <DataTable rows={d.decisions} cols={[{ key: "decision_type", label: "Response", render: (r) => fmt.label(r.decision_type) }, { key: "decisions", num: true },
                  { key: "expected_cost", label: "Expected", num: true, render: (r) => fmt.money(r.expected_cost) }, { key: "actual_cost", label: "Actual", num: true, render: (r) => fmt.money(r.actual_cost) },
                  { key: "ratio", label: "Actual / expected", num: true, render: (r) => <span className={r.actual_cost > r.expected_cost * 1.2 ? "bad-t" : undefined}>{(r.actual_cost / r.expected_cost).toFixed(2)}×</span> }]} />
              </Card>
            </>
          )}
          {tab === "suppliers" && (
            <>
              <div className="grid g2">
                <Card title="OTIF trend" sub="24 months" actions={csv("suppliers", "monthly")}><Lines data={d.monthly} x="month" yFmt={(v) => fmt.pct(v, 0)} height={230} series={[{ key: "otif", name: "OTIF", color: "#059669" }]} /></Card>
                <Card title="By country" actions={csv("suppliers", "by_country")}><Bars data={d.by_country} x="country_code" yFmt={(v) => fmt.pct(v, 0)} height={230} series={[{ key: "otif", name: "OTIF", color: "#0e7490" }]} /></Card>
              </div>
              <Card title="Supplier ranking" sub="last 12 months" actions={csv("suppliers", "ranking")} flush>
                <DataTable rows={d.ranking} cols={[{ key: "supplier_id", label: "Supplier" }, { key: "supplier_name", label: "Name" }, { key: "country_code", label: "Country" },
                  { key: "otif", label: "OTIF", num: true, render: (r) => fmt.pct(r.otif) }, { key: "fill_rate", label: "Fill rate", num: true, render: (r) => fmt.pct(r.fill_rate) },
                  { key: "avg_delay_days", label: "Avg delay", num: true }, { key: "quality_ppm", label: "PPM", num: true, render: (r) => fmt.n(r.quality_ppm) }, { key: "pos", label: "POs", num: true },
                  { key: "kept", label: "Promises kept", num: true, render: (r) => r.commitments_made ? `${r.commitments_kept}/${r.commitments_made}` : "–" }]} />
              </Card>
            </>
          )}
        </>
      )}
    </div>
  );
}
