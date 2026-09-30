import { Bell, Check, CheckCheck, RefreshCw, RotateCcw } from "lucide-react";
import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Card, ErrorBox, Linkify, Loading, PageHead, Pager, Seg, Status, useAction } from "../components/ui";
import { api, fmt, Row, useDebounced, useQuery } from "../lib/api";

export function AlertsPage() {
  const [params] = useSearchParams();
  const [status, setStatus] = useState("open");
  const [severity, setSeverity] = useState(params.get("severity") ?? "");
  const [module, setModule] = useState("");
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const dq = useDebounced(q);
  const { data, error, loading, reload } = useQuery<Row>("/alerts", { status, severity, module, q: dq, limit: 50, offset });
  const { run, busy } = useAction();
  const act = async (id: number, action: string) => {
    await run(`${id}${action}`, () => api.post(`/alerts/${id}`, { action }), `Alert ${action}d`);
    reload();
  };
  const sum = data?.summary;
  return (
    <div>
      <PageHead title="Alerts & notifications" icon={<Bell size={20} />}
        sub="Raised automatically from live data — low stock, overdue orders, late shipments, supplier drops, material shortages, disruptions. Alerts resolve themselves when the condition clears."
        actions={<button className="btn" disabled={!!busy} onClick={async () => { await run("refresh", () => api.post("/alerts-refresh"), (r: Row) => `Re-evaluated: ${r.new} new, ${r.resolved} resolved`); reload(); }}><RefreshCw size={14} />Re-evaluate</button>} />
      {sum && (
        <div className="kpis">
          {(["critical", "warning", "info"] as const).map((s) => (
            <button key={s} className={`kpi ${s === "critical" ? "bad" : s === "warning" ? "warn" : "info"}`} style={{ textAlign: "left", cursor: "pointer" }} onClick={() => { setSeverity(severity === s ? "" : s); setOffset(0); }}>
              <div className="kpi-top"><span>{fmt.label(s)}</span><span className="kpi-icon"><Bell size={14} /></span></div>
              <div className="kpi-value">{fmt.n(sum.by_severity[s] ?? 0)}</div>
              <div className="kpi-sub">{severity === s ? "filtered — click to clear" : "open"}</div>
            </button>
          ))}
          {sum.by_module.slice(0, 3).map((m: Row) => (
            <button key={m.module} className="kpi" style={{ textAlign: "left", cursor: "pointer" }} onClick={() => { setModule(module === m.module ? "" : m.module); setOffset(0); }}>
              <div className="kpi-top"><span>{fmt.label(m.module)}</span></div>
              <div className="kpi-value">{fmt.n(m.n)}</div>
              <div className="kpi-sub">{m.critical} critical{module === m.module ? " · filtered" : ""}</div>
            </button>
          ))}
        </div>
      )}
      <Card flush>
        <div className="toolbar">
          <Seg value={status} onChange={(v) => { setStatus(v); setOffset(0); }} options={[["open", "Open"], ["acknowledged", "Acknowledged"], ["resolved", "Resolved"], ["all", "All"]]} />
          <input className="input" style={{ width: 260 }} placeholder="Search alerts" value={q} onChange={(e) => { setQ(e.target.value); setOffset(0); }} />
          {(severity || module) && <button className="btn sm ghost" onClick={() => { setSeverity(""); setModule(""); }}>Clear filters</button>}
        </div>
        <ErrorBox error={error} />
        {loading && !data ? <Loading /> : data!.rows.length === 0 ? <div className="empty">Nothing here.</div> : data!.rows.map((a: Row) => (
          <div key={a.alert_id} className="alert-row">
            <span className={`sev ${a.severity}`} />
            <div className="grow" style={{ minWidth: 0 }}>
              <div className="row"><b><Linkify text={a.title} /></b><Status value={a.status} /></div>
              <div className="small muted"><Linkify text={a.detail} /></div>
              <div className="small muted">{fmt.label(a.module)} · raised {fmt.ago(a.created_at)}{a.acknowledged_at ? ` · acknowledged ${fmt.ago(a.acknowledged_at)}` : ""}{a.resolved_at ? ` · resolved ${fmt.ago(a.resolved_at)}` : ""}</div>
            </div>
            <div className="row" style={{ flexWrap: "nowrap" }}>
              {a.status === "open" && <button className="btn sm" disabled={!!busy} onClick={() => act(a.alert_id, "acknowledge")}><Check size={13} />Acknowledge</button>}
              {a.status !== "resolved" && <button className="btn sm" disabled={!!busy} onClick={() => act(a.alert_id, "resolve")}><CheckCheck size={13} />Resolve</button>}
              {a.status === "resolved" && <button className="btn sm" disabled={!!busy} onClick={() => act(a.alert_id, "reopen")}><RotateCcw size={13} />Reopen</button>}
            </div>
          </div>
        ))}
        <Pager page={data} onOffset={setOffset} />
      </Card>
    </div>
  );
}
