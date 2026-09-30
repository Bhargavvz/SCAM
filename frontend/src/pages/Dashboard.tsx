import { AlertTriangle, ArrowRight, Boxes, Building2, ClipboardList, DollarSign, Factory, PackageCheck, RefreshCw, ShoppingCart, Sparkles, Truck, Undo2, Users, Warehouse } from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Bars, TrendArea } from "../components/charts";
import { Card, Delta, EntityLink, ErrorBox, Kpi, Linkify, Loading, Markdown, PageHead, Spinner, Status } from "../components/ui";
import { api, fmt, Row, useQuery } from "../lib/api";

const STAGE_ICON: Record<string, any> = { Suppliers: Building2, Procurement: ClipboardList, Inventory: Boxes, Production: Factory, Warehouse: Warehouse, Shipping: Truck, Customers: Users };

export function Briefing() {
  const [data, setData] = useState<Row | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const load = async () => {
    setBusy(true);
    setErr(null);
    try {
      setData(await api.post("/insights/briefing"));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Card title="Today's briefing" icon={<Sparkles size={15} className="ai-t" />} sub={data ? `${data.cached ? "generated" : "just generated"} · ${data.model?.split("/").pop()}` : "AI summary of the findings below"}
      actions={<button className="btn sm ai" disabled={busy} onClick={load}>{busy ? <Spinner /> : <Sparkles size={13} />}{data ? "Regenerate" : "Generate briefing"}</button>}>
      {err && <ErrorBox error={err} />}
      {!data && !busy && !err && <div className="muted">Meridian reads today's KPIs and detected findings and writes a short briefing for the operations team. Numbers come only from the data.</div>}
      {busy && !data && <Loading />}
      {data && (
        <>
          <Markdown text={data.text} />
          {data.ungrounded_numbers?.length > 0 && <div className="small warn-t">Check: {data.ungrounded_numbers.join(", ")} not found in the evidence.</div>}
        </>
      )}
    </Card>
  );
}

export function Dashboard() {
  const { data: d, error, loading, reload } = useQuery<Row>("/dashboard");
  useEffect(() => {
    window.addEventListener("meridian:changed", reload);
    return () => window.removeEventListener("meridian:changed", reload);
  }, [reload]);
  if (loading && !d) return <Loading />;
  if (error) return <ErrorBox error={error} />;
  const k = d!.kpis;
  return (
    <div>
      <PageHead title="Operations dashboard" sub={`End-to-end view of the supply chain on ${d!.today}.`}
        actions={<button className="btn" onClick={reload}><RefreshCw size={14} />Refresh</button>} />

      <div className="kpis">
        <Kpi label="Revenue · 30 days" value={fmt.money(k.revenue_30d)} icon={<DollarSign size={15} />} tone="good" to="/reports"
          sub={<><Delta now={k.revenue_30d} before={k.revenue_prev_30d} /> vs previous 30 days</>} />
        <Kpi label="Orders · 30 days" value={fmt.n(k.orders_30d)} icon={<ShoppingCart size={15} />} tone="info" to="/sales"
          sub={`${fmt.pct(k.otd_30d)} delivered on time`} />
        <Kpi label="Inventory value" value={fmt.money(k.inventory_value)} icon={<Boxes size={15} />} tone="info" to="/inventory"
          sub={`${fmt.n(k.below_min)} positions below min`} />
        <Kpi label="Open purchase orders" value={fmt.n(k.open_pos)} icon={<ClipboardList size={15} />} tone={k.late_pos ? "warn" : "good"} to="/purchasing"
          sub={`${fmt.n(k.late_pos)} overdue · ${fmt.n(k.stale_pos)} stale`} />
        <Kpi label="Shipments in transit" value={fmt.n(k.in_transit)} icon={<Truck size={15} />} tone={k.late_shipments ? "warn" : "good"} to="/logistics"
          sub={`${fmt.n(k.late_shipments)} past promised date`} />
        <Kpi label="Critical alerts" value={fmt.n(k.critical_alerts)} icon={<AlertTriangle size={15} />} tone={k.critical_alerts ? "bad" : "good"} to="/alerts?severity=critical"
          sub={`${fmt.n(k.open_returns)} open returns`} />
      </div>

      <section className="card" style={{ marginBottom: 14 }}>
        <div className="card-head"><h3>Supply chain flow</h3><span className="sub">live counts · issues in red</span></div>
        <div className="flow">
          {d!.flow.map((s: Row) => {
            const Icon = STAGE_ICON[s.stage] ?? PackageCheck;
            return (
              <Link key={s.stage} className="flow-step" to={s.link}>
                <div className="stage"><Icon size={13} />{s.stage}</div>
                <div className="big">{fmt.compact(s.primary)}</div>
                <div className="lbl">{s.label}</div>
                <div className={`issue ${s.issue ? "bad-t" : "good-t"}`}>{fmt.n(s.issue)} {s.issue_label}</div>
              </Link>
            );
          })}
        </div>
      </section>

      <div className="grid g-main">
        <Card title="Revenue" sub="monthly, last 13 months">
          <TrendArea data={d!.revenue_monthly} x="month" series={[{ key: "revenue", name: "Revenue", color: "#0e7490" }]} yFmt={fmt.money} />
        </Card>
        <Card title="Top findings" icon={<Sparkles size={15} className="ai-t" />} actions={<Link to="/insights" className="small">All insights <ArrowRight size={12} /></Link>} flush>
          {d!.insights.map((i: Row) => (
            <Link key={i.id} to={i.link} className="alert-row" style={{ color: "inherit", textDecoration: "none" }}>
              <span className={`sev ${i.severity === "medium" ? "warning" : i.severity === "low" ? "info" : "critical"}`} />
              <div><div style={{ fontWeight: 600 }}>{i.title}</div><div className="small muted">{i.module}</div></div>
            </Link>
          ))}
        </Card>
      </div>

      <div className="grid g-main">
        <Briefing />
        <Card title="Alerts" actions={<Link to="/alerts" className="small">View all <ArrowRight size={12} /></Link>} flush>
          {d!.alerts.length === 0 && <div className="empty">No open alerts.</div>}
          {d!.alerts.map((a: Row) => (
            <div key={a.alert_id} className="alert-row">
              <span className={`sev ${a.severity}`} />
              <div style={{ minWidth: 0 }}>
                <div style={{ fontWeight: 600 }}><Linkify text={a.title} /></div>
                <div className="small muted"><Linkify text={a.detail} /></div>
              </div>
            </div>
          ))}
        </Card>
      </div>

      <div className="grid g2">
        <Card title="Inventory value by warehouse">
          <Bars data={d!.inventory_by_warehouse} x="warehouse_id" series={[{ key: "value", name: "Value", color: "#2563eb" }]} yFmt={fmt.money} height={230} />
        </Card>
        <Card title="Recent activity" sub="every change made in Meridian" actions={<Link to="/system" className="small">Audit log <ArrowRight size={12} /></Link>} flush>
          {d!.activity.length === 0 && <div className="empty">No activity yet. Create a purchase order or a sales order to see it here.</div>}
          {d!.activity.map((a: Row, i: number) => (
            <div key={i} className="alert-row">
              <Status value={a.module} tone="plain" />
              <div style={{ minWidth: 0 }}><div><Linkify text={a.summary} /></div><div className="small muted">{fmt.ago(a.at)} · {a.actor}</div></div>
            </div>
          ))}
        </Card>
      </div>
    </div>
  );
}
