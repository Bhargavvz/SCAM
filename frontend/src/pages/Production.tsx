import { AdviceBox, MemoryPanel } from "../components/Memory";
import { CalendarClock, CalendarPlus, Factory, Play, CheckCircle2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Bars, PALETTE } from "../components/charts";
import { Bar, Card, DataTable, Drawer, EntityLink, ErrorBox, Kpi, KV, Loading, PageHead, Pager, Seg, Status, useAction } from "../components/ui";
import { api, fmt, Row, useQuery } from "../lib/api";

function ScheduleRun({ open, onClose }: { open: boolean; onClose: (id?: string) => void }) {
  const { data: lk } = useQuery<Row>("/lookup");
  const [pid, setPid] = useState("");
  const [plant, setPlant] = useState("");
  const [date, setDate] = useState("");
  const [qty, setQty] = useState("100");
  const { run, busy } = useAction();
  return (
    <Drawer open={open} onClose={() => onClose()} title="Schedule production run"
      footer={<><button className="btn" onClick={() => onClose()}>Cancel</button><button className="btn primary" disabled={!!busy || !pid || !plant || !date || !qty} onClick={async () => {
        const r = await run("new", () => api.post<Row>("/production/runs", { product_id: pid, plant_id: plant, planned_start: date, planned_qty: Number(qty) }), (x: Row) => `${x.run.production_run_id} scheduled`);
        if (r) onClose(r.run.production_run_id);
      }}>Schedule</button></>}>
      <label className="field">Product id (made in-house)<input className="input mono" value={pid} placeholder="IP00001" onChange={(e) => setPid(e.target.value.toUpperCase())} /></label>
      <label className="field">Plant<select className="input" value={plant} onChange={(e) => setPlant(e.target.value)}><option value="">Select…</option>{lk?.plants.map((p: Row) => <option key={p.plant_id} value={p.plant_id}>{p.plant_id} · {p.plant_name}</option>)}</select></label>
      <label className="field">Planned start<input className="input" type="date" min={lk?.today} value={date} onChange={(e) => setDate(e.target.value)} /></label>
      <label className="field">Quantity<input className="input" type="number" min={1} value={qty} onChange={(e) => setQty(e.target.value)} /></label>
      <div className="note">The product needs a bill of materials in effect. After scheduling, the run page checks every material against plant stock.</div>
    </Drawer>
  );
}

export function ProductionPage() {
  const nav = useNavigate();
  const [sched, setSched] = useState(false);
  const [plant, setPlant] = useState("");
  const [status, setStatus] = useState("");
  const [offset, setOffset] = useState(0);
  useEffect(() => setOffset(0), [plant, status]);
  const { data: o, error } = useQuery<Row>("/production/overview");
  const runs = useQuery<Row>("/production/runs", { plant_id: plant, status, limit: 50, offset });
  const load = useMemo(() => {
    if (!o) return { rows: [], plants: [] as string[] };
    const by: Record<string, Row> = {};
    o.weekly_load.forEach((r: Row) => { by[r.week] = { ...(by[r.week] ?? { week: r.week }), [r.plant_id]: r.planned }; });
    return { rows: Object.values(by), plants: o.plants.map((p: Row) => p.plant_id) };
  }, [o]);
  return (
    <div>
      <PageHead title="Production planning" icon={<Factory size={20} />} sub="Manufacturing schedule by plant, material availability against the bill of materials, and delay causes."
        actions={<button className="btn primary" onClick={() => setSched(true)}><CalendarPlus size={14} />Schedule run</button>} />
      <ErrorBox error={error} />
      {o && (
        <>
          <div className="kpis">
            {o.plants.map((p: Row) => (
              <button key={p.plant_id} className={`kpi ${plant === p.plant_id ? "info" : p.materials_below_safety ? "warn" : ""}`} style={{ textAlign: "left", cursor: "pointer" }} onClick={() => setPlant(plant === p.plant_id ? "" : p.plant_id)}>
                <div className="kpi-top"><span>{p.plant_name}</span><span className="mono muted">{p.plant_id}</span></div>
                <div className="kpi-value">{fmt.n(p.runs_30d)} <span className="small muted">runs · 30d</span></div>
                <div className="kpi-sub">{fmt.pct(p.late_share_90d, 0)} started late · {fmt.n(p.materials_below_safety)} materials below safety</div>
              </button>
            ))}
          </div>
          <div className="grid g-main">
            <Card title="Planned output by week" sub="units, all plants">
              <Bars data={load.rows} x="week" stacked height={240} series={load.plants.map((p: string, i: number) => ({ key: p, name: p, color: PALETTE[i] }))} />
            </Card>
            <Card title="Why runs started late" sub="last 90 days">
              <Bars data={o.delay_reasons.filter((r: Row) => r.reason !== "on_time")} x="reason" horizontal height={240} series={[{ key: "runs", name: "Runs", color: "#dc2626" }]} />
            </Card>
          </div>
          <Card title="Raw materials below safety stock" sub={`snapshot ${o.snapshot_date}`} flush>
            <DataTable rows={o.shortages} empty="All materials are above safety stock." cols={[
              { key: "plant_id", label: "Plant" }, { key: "rm_id", label: "Material", render: (r) => <span className="mono">{r.rm_id}</span> }, { key: "rm_name", label: "Name" },
              { key: "criticality", render: (r) => <Status value={r.criticality} tone={r.criticality === "high" ? "bad" : "plain"} /> },
              { key: "on_hand", label: "On hand", num: true }, { key: "safety_stock", label: "Safety", num: true },
              { key: "gap", label: "", render: (r) => <Bar value={r.on_hand} max={r.safety_stock} tone="bad" /> }, { key: "days_of_cover", label: "Cover (days)", num: true }]} />
          </Card>
          <div style={{ height: 14 }} />
        </>
      )}
      <Card title="Production runs" sub="30 days either side of today" flush>
        <div className="toolbar">
          <Seg value={status} onChange={setStatus} options={[["", "All"], ["planned", "Planned"], ["overdue", "Overdue start"], ["in_progress", "In progress"], ["completed", "Completed"]]} />
          {plant && <button className="btn sm ghost" onClick={() => setPlant("")}>Plant {plant} ✕</button>}
        </div>
        {runs.loading && !runs.data ? <Loading /> : (
          <DataTable rows={runs.data?.rows ?? []} onRow={(r) => nav(`/production/${r.production_run_id}`)} cols={[
            { key: "production_run_id", label: "Run" }, { key: "plant_id", label: "Plant" }, { key: "product_id", label: "Product" }, { key: "sku", label: "SKU" },
            { key: "planned_start", label: "Planned" }, { key: "actual_start", label: "Started" }, { key: "planned_qty", label: "Planned qty", num: true },
            { key: "produced_qty", label: "Produced", num: true }, { key: "delay_days", label: "Delay", num: true, render: (r) => r.delay_days > 0 ? <b className="warn-t">{r.delay_days} d</b> : "–" },
            { key: "delay_reason_code", label: "Reason", render: (r) => fmt.label(r.delay_reason_code ?? "") || "–" }, { key: "status", render: (r) => <Status value={r.status} /> }]} />
        )}
        <Pager page={runs.data as any} onOffset={setOffset} />
      </Card>
      <ScheduleRun open={sched} onClose={(id) => { setSched(false); if (id) nav(`/production/${id}`); }} />
    </div>
  );
}

export function RunDetail() {
  const { id = "" } = useParams();
  const { data: d, error, loading, reload } = useQuery<Row>(`/production/runs/${id}`);
  const [date, setDate] = useState("");
  const { run, busy } = useAction();
  if (loading && !d) return <Loading />;
  if (error) return <ErrorBox error={error} />;
  const r = d!.run;
  const short = d!.materials.filter((m: Row) => m.status === "short");
  const act = async (body: Row, msg: string) => {
    await run("act", () => api.post(`/production/runs/${id}`, body), msg);
    reload();
  };
  return (
    <div>
      <PageHead crumbs={[["Production", "/production"]]} title={<>Run <span className="mono">{id}</span></>}
        sub={<><EntityLink id={r.product_id}>{`${r.product_id} · ${r.sku}`}</EntityLink> at plant {r.plant_id}</>}
        actions={<>
          {!r.actual_start && <><input className="input" type="date" value={date} onChange={(e) => setDate(e.target.value)} />
            <button className="btn" disabled={!!busy || !date} onClick={() => act({ action: "reschedule", planned_start: date }, "Run rescheduled")}><CalendarClock size={14} />Reschedule</button>
            <button className="btn primary" disabled={!!busy} onClick={() => act({ action: "start" }, "Run started")}><Play size={14} />Start run</button></>}
          {r.actual_start && !r.produced_qty && <button className="btn primary" disabled={!!busy} onClick={() => act({ action: "complete" }, "Run completed")}><CheckCircle2 size={14} />Complete</button>}
        </>} />
      <div className="kpis">
        <Kpi label="Planned start" value={r.planned_start} sub={r.actual_start ? `started ${r.actual_start}` : "not started"} />
        <Kpi label="Planned quantity" value={fmt.n(r.planned_qty)} sub={r.produced_qty ? `${fmt.n(r.produced_qty)} produced` : undefined} />
        <Kpi label="Delay" value={r.delay_days ? `${r.delay_days} days` : "–"} tone={r.delay_days ? "warn" : "good"} sub={fmt.label(r.delay_reason_code ?? "")} />
        <Kpi label="Material check" value={short.length ? `${short.length} short` : "All available"} tone={short.length ? "bad" : "good"} sub={`snapshot ${d!.snapshot_date}`} />
      </div>
      <div style={{ marginBottom: 14 }}><MemoryPanel entityId={r.product_id} title={`Memory: ${r.product_id} production`} /></div>
      <Card title="Material requirements" sub="bill of materials × planned quantity vs plant stock" flush>
        <DataTable rows={d!.materials} empty="No bill of materials in effect." cols={[
          { key: "rm_id", label: "Material", render: (x) => <span className="mono">{x.rm_id}</span> }, { key: "rm_name", label: "Name" },
          { key: "qty_per_unit", label: "Per unit", num: true }, { key: "scrap_pct", label: "Scrap %", num: true },
          { key: "required", label: "Required", num: true, render: (x) => `${fmt.n(x.required, 1)} ${x.uom}` },
          { key: "on_hand", label: "On hand", num: true, render: (x) => fmt.n(x.on_hand, 1) },
          { key: "cov", label: "", render: (x) => <Bar value={x.on_hand ?? 0} max={x.required} tone={x.status === "ok" ? "good" : "bad"} /> },
          { key: "status", render: (x) => <Status value={x.status} tone={x.status === "ok" ? "good" : "bad"} /> }]} />
      </Card>
      {d!.history.length > 0 && <Card title="History"><ul className="timeline">{d!.history.map((h: Row, i: number) => <li key={i}><div>{h.summary}</div><div className="t">{fmt.ago(h.at)}</div></li>)}</ul></Card>}
      <div style={{ marginTop: 14 }}><KV items={[["Category", fmt.label(r.category)], ["Linked PO line", r.purchase_order_line_id ?? "–"]]} /></div>
    </div>
  );
}
