import { useEffect, useState } from "react";
import { Row } from "../../api";
import { money, Panel } from "../../components/common";
import { Badge, ErrorBox, KV, Loading, MemoryPanel, PageHead, Pager, Seg, Table } from "../../components/scm";
import { EntityLink, go, Route, useAsOf, useData } from "../../nav";
import { scm } from "../../scm";

type Status = "open" | "slipped" | "overdue" | "all";

const slip = (r: Row) => {
  const d = Number(r.slip_days ?? 0);
  return d > 0 ? <Badge tone={d > 14 ? "bad" : "warn"}>+{d}d</Badge> : <span className="muted">on time</span>;
};

export function PoList({ route }: { route: Route }) {
  const { asOf } = useAsOf();
  const [status, setStatus] = useState<Status>((route.query.get("status") as Status) ?? "slipped");
  const [q, setQ] = useState(route.query.get("q") ?? "");
  const [offset, setOffset] = useState(0);
  useEffect(() => setOffset(0), [status, q, asOf]);
  const { data, error, loading } = useData(() => scm.pos({ as_of: asOf, status, q, limit: 50, offset }), [asOf, status, q, offset]);
  return (
    <div>
      <PageHead title="Purchase orders" sub="Raw-material orders with their promised and currently expected arrival." />
      <div className="toolbar">
        <Seg value={status} onChange={setStatus} options={[["slipped", "Slipped"], ["overdue", "Overdue"], ["open", "Open"], ["all", "All"]]} />
        <input className="search" placeholder="Order, supplier id or name" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      <ErrorBox error={error} />
      {loading && !data ? <Loading /> : data && (
        <Panel>
          <Table rows={data.rows} onRow={(r) => go(`/pos/${r.rm_purchase_order_id}`)} empty="No orders match." cols={[
            { key: "rm_purchase_order_id", label: "order" }, { key: "supplier_id", label: "supplier" }, "supplier_name", "plant_id",
            { key: "rm_id", label: "material" }, "ordered_at", "promised_at", "expected_at",
            { key: "slip", render: slip }, { key: "value", num: true, render: (r) => money(Number(r.value)) }, "status",
          ]} />
          <Pager total={data.total} limit={data.limit} offset={data.offset} onChange={setOffset} />
        </Panel>
      )}
    </div>
  );
}

const ACTIONS: [string, string][] = [
  ["cancel_po", "Cancel order"],
  ["expedite", "Expedite"],
  ["switch_supplier", "Switch supplier"],
  ["accept_delay", "Accept delay"],
  ["substitute_rm", "Use substitute"],
];

export function PoDetail({ id }: { id: string }) {
  const { asOf } = useAsOf();
  const { data, error, loading } = useData(() => scm.po(id, asOf), [id, asOf]);
  const [check, setCheck] = useState<Row | null>(null);
  const [checking, setChecking] = useState<string | null>(null);
  const [checkErr, setCheckErr] = useState<string | null>(null);
  useEffect(() => setCheck(null), [id, asOf]);

  const run = async (action: string) => {
    setChecking(action);
    setCheckErr(null);
    try {
      setCheck(await scm.checkAction(id, action, asOf));
    } catch (e) {
      setCheckErr(String((e as Error).message));
    } finally {
      setChecking(null);
    }
  };

  if (loading && !data) return <Loading />;
  if (error) return <ErrorBox error={error} />;
  const po = data!.po as Row;
  const sim = check?.simulation as Row | null | undefined;
  return (
    <div>
      <PageHead crumbs={[["Purchase orders", "#/pos"]]} title={<>Order <span className="mono">{id}</span></>}
        sub={<><EntityLink id={String(po.supplier_id)}>{String(po.supplier_name)}</EntityLink> → plant {String(po.plant_id)} · {String(po.status)}</>}
        right={slip(po)} />
      <div className="grid split">
        <div>
          <Panel title="Order">
            <KV items={[
              ["ordered", String(po.ordered_at)], ["promised", String(po.promised_at)], ["expected now", String(po.expected_at ?? "-")],
              ["received", String(po.received_at ?? "not yet")], ["type", String(po.order_type)], ["value", money(Number(po.value))],
            ]} />
            <Table rows={data!.lines as Row[]} cols={[{ key: "rm_id", label: "material" }, "rm_name", { key: "quantity_ordered", label: "ordered", num: true }, { key: "quantity_received", label: "received", num: true }, "uom", { key: "unit_cost", num: true }]} />
          </Panel>

          {po.status === "open" && (
            <section className="panel guard">
              <div className="panel-head"><h3>Change this order</h3><span className="muted small">every change is checked before it reaches the ERP</span></div>
              <div className="row">
                {ACTIONS.map(([a, l]) => (
                  <button key={a} className="btn ghost" disabled={!!checking} onClick={() => run(a)}>{checking === a && <span className="spinner dark" />}{l}</button>
                ))}
              </div>
              {checkErr && <div className="alert error"><span className="tag">Check</span>{checkErr}</div>}
              {check && (
                <div className={`verdict ${check.blocked ? "blocked" : "ok"}`}>
                  <div className="verdict-head">{check.blocked ? "Blocked by guardrail" : "Allowed"} — {String(check.action).replace(/_/g, " ")}</div>
                  {(check.breached_commitments as Row[]).map((c) => (
                    <div key={String(c.commitment_id)}>Would break <b className="mono">{String(c.commitment_id)}</b>: “{String(c.commitment_text)}” (due {String(c.due_date)})</div>
                  ))}
                  {sim && "stockout_days" in sim && (
                    <div className="muted">Simulated: arrival {String(sim.arrival ?? "-")}, {String(sim.stockout_days)} stockout days, cost {money(Number(sim.cost))}{sim.note ? ` — ${sim.note}` : ""}</div>
                  )}
                  {sim && "note" in sim && !("stockout_days" in sim) && <div className="muted">{String(sim.note)}</div>}
                  {(check.external_actions as string[]).map((m) => <div key={m} className="mock">{m}</div>)}
                </div>
              )}
            </section>
          )}

          <Panel title="Expected date revisions">
            <Table rows={data!.revisions as Row[]} empty="The supplier has not revised this order." cols={["revised_at", { key: "old_expected_at", label: "from" }, { key: "new_expected_at", label: "to" }, { key: "reason_code", label: "reason" }]} />
          </Panel>
          <Panel title="Production that depends on it">
            <Table rows={data!.dependent_runs as Row[]} empty="No upcoming runs consume this material at this plant." cols={[{ key: "production_run_id", label: "run" }, "product_id", "planned_start", { key: "planned_qty", num: true }]} />
          </Panel>
          {(data!.receipts as Row[]).length > 0 && (
            <Panel title="Receipts"><Table rows={data!.receipts as Row[]} cols={["movement_at", "movement_type", { key: "quantity_change", num: true }]} /></Panel>
          )}
        </div>
        <div>
          <MemoryPanel entityId={id} />
          <Panel title="Open commitments" right={<span className="muted small">to this supplier and plant</span>}>
            <Table rows={data!.open_commitments as Row[]} empty="No open commitments." cols={[
              { key: "commitment_id", label: "id" }, { key: "counterparty_id", label: "to" }, "due_date",
              { key: "commitment_text", label: "promise", render: (r) => <span className={r.is_volume_commitment ? "bad-t" : undefined}>{String(r.commitment_text)}</span> },
            ]} />
          </Panel>
          <Panel title="Disruptions involving this order">
            <Table rows={data!.events as Row[]} empty="None." cols={[{ key: "event_id", label: "event" }, "title", "detected_at"]} />
          </Panel>
        </div>
      </div>
    </div>
  );
}
