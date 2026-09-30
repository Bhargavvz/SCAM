import { PackageOpen, Undo2 } from "lucide-react";
import { useEffect, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { Bars, Donut } from "../components/charts";
import { Card, DataTable, EntityLink, ErrorBox, Kpi, KV, Loading, PageHead, Pager, Seg, Status, Steps, useAction } from "../components/ui";
import { api, fmt, Row, useDebounced, useQuery } from "../lib/api";

export function ReturnsPage() {
  const nav = useNavigate();
  const [params] = useSearchParams();
  const [status, setStatus] = useState("");
  const [reason, setReason] = useState("");
  const [q, setQ] = useState(params.get("q") ?? "");
  const [offset, setOffset] = useState(0);
  const dq = useDebounced(q);
  useEffect(() => setOffset(0), [status, reason, dq]);
  const { data: s } = useQuery<Row>("/returns/summary");
  const { data, error, loading } = useQuery<Row>("/returns", { status, reason, q: dq, limit: 50, offset });
  return (
    <div>
      <PageHead title="Returns" icon={<Undo2 size={20} />} sub="Return authorizations (RMA) from request to receipt, inspection and disposition. Start a return from a delivered sales order line." />
      {s && (
        <>
          <div className="kpis">
            <Kpi label="RMAs · 90 days" value={fmt.n(s.kpis.rmas_90d)} sub={`${fmt.n(s.kpis.units_90d)} units`} />
            <Kpi label="Refunds · 90 days" value={fmt.money(s.kpis.refunds_90d)} tone="warn" />
            <Kpi label="Open returns" value={fmt.n(s.kpis.open)} tone={s.kpis.open ? "info" : "good"} sub="authorized or awaiting disposition" />
          </div>
          <div className="grid g3">
            <Card title="Returns by month"><Bars data={s.monthly} x="month" series={[{ key: "rmas", name: "RMAs", color: "#db2777" }]} height={200} /></Card>
            <Card title="Reasons" sub="12 months"><Donut data={s.by_reason} nameKey="reason" valueKey="n" height={160} fmtValue={(v) => fmt.n(v)} /></Card>
            <Card title="Disposition" sub="12 months"><Donut data={s.by_disposition} nameKey="disposition" valueKey="n" height={160} fmtValue={(v) => fmt.n(v)} /></Card>
          </div>
        </>
      )}
      <Card flush>
        <div className="toolbar">
          <input className="input" style={{ width: 260 }} placeholder="RMA, product, order, customer" value={q} onChange={(e) => setQ(e.target.value)} />
          <Seg value={status} onChange={setStatus} options={[["", "All"], ["open", "Open"], ["authorized", "Authorized"], ["received", "Received"], ["closed", "Closed"]]} />
          <select className="input" value={reason} onChange={(e) => setReason(e.target.value)}><option value="">All reasons</option>{["defective", "damaged_in_transit", "wrong_item", "not_needed", "quality_issue"].map((r) => <option key={r} value={r}>{fmt.label(r)}</option>)}</select>
        </div>
        <ErrorBox error={error} />
        {loading && !data ? <Loading /> : (
          <DataTable rows={data!.rows} onRow={(r) => nav(`/returns/${r.rma_id}`)} cols={[
            { key: "rma_id", label: "RMA" }, { key: "sales_order_id", label: "Order" }, { key: "customer_name", label: "Customer" },
            { key: "product_id", label: "Product" }, { key: "brand_family", label: "Brand" }, { key: "qty", num: true },
            { key: "reason", render: (r) => fmt.label(r.reason) }, { key: "requested_at", label: "Requested" },
            { key: "disposition", render: (r) => r.status === "closed" ? fmt.label(r.disposition) : <span className="muted">pending</span> },
            { key: "refund_value", label: "Refund", num: true, render: (r) => fmt.moneyFull(r.refund_value) },
            { key: "status", render: (r) => <Status value={r.status} /> },
          ]} />
        )}
        <Pager page={data as any} onOffset={setOffset} />
      </Card>
    </div>
  );
}

export function ReturnDetail() {
  const { id = "" } = useParams();
  const { data: d, error, loading, reload } = useQuery<Row>(`/returns/${id}`);
  const { run, busy } = useAction();
  if (loading && !d) return <Loading />;
  if (error) return <ErrorBox error={error} />;
  const r = d!.rma;
  const dispose = async (disposition: string) => {
    await run("d", () => api.post(`/returns/${id}/disposition`, { disposition }), `${id} closed as ${disposition}`);
    reload();
  };
  return (
    <div>
      <PageHead crumbs={[["Returns", "/returns"]]} title={<>RMA <span className="mono">{id}</span> <Status value={r.status} /></>}
        sub={<><EntityLink id={r.customer_id}>{r.customer_name}</EntityLink> · order <EntityLink id={r.sales_order_id} /> · {fmt.label(r.reason)}</>}
        actions={r.status === "authorized" && <button className="btn primary" disabled={!!busy} onClick={async () => { await run("r", () => api.post(`/returns/${id}/receive`), "Return received — inspection task created"); reload(); }}><PackageOpen size={14} />Receive return</button>} />
      <Card><Steps steps={["authorized", "received", "closed"]} current={r.status} /></Card>
      <div style={{ height: 14 }} />
      <div className="grid g-main">
        <Card title="Return">
          <KV items={[["Product", <EntityLink id={r.product_id}>{`${r.product_id} · ${r.sku}`}</EntityLink>], ["Brand", r.brand_family], ["Quantity", r.qty], ["Warehouse", <EntityLink id={r.warehouse_id} />],
            ["Requested", r.requested_at], ["Received", r.received_at ?? "–"], ["Closed", r.closed_at ?? "–"], ["Refund value", fmt.moneyFull(r.refund_value)],
            ["Disposition", r.status === "closed" ? fmt.label(r.disposition) : "pending"], ["Replacement order", r.replacement_order_id ? <EntityLink id={r.replacement_order_id} /> : "–"]]} />
          {r.notes && <div className="note" style={{ marginTop: 12 }}>{r.notes}</div>}
        </Card>
        <Card title="Disposition">
          {r.status === "received" ? (
            <div className="stack">
              <div className="small muted">After inspection, choose what happens to the returned units.</div>
              <button className="btn" disabled={!!busy} onClick={() => dispose("restock")}>Restock — return {r.qty} units to {r.warehouse_id} inventory</button>
              <button className="btn" disabled={!!busy} onClick={() => dispose("replace")}>Replace — create a no-charge replacement order</button>
              <button className="btn" disabled={!!busy} onClick={() => dispose("refurbish")}>Refurbish — send to repair</button>
              <button className="btn danger" disabled={!!busy} onClick={() => dispose("scrap")}>Scrap — write off</button>
            </div>
          ) : r.status === "authorized" ? <div className="muted">Receive the goods first.</div> : <div className="muted">Closed as <b>{fmt.label(r.disposition)}</b>.</div>}
        </Card>
      </div>
      {d!.history.length > 0 && (
        <Card title="History"><ul className="timeline">{d!.history.map((h: Row, i: number) => <li key={i}><div>{h.summary}</div><div className="t">{fmt.ago(h.at)} · {h.actor}</div></li>)}</ul></Card>
      )}
    </div>
  );
}
