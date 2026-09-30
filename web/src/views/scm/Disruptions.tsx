import { useEffect, useState } from "react";
import { DecisionCard, Row } from "../../api";
import { Panel } from "../../components/common";
import { DecisionCardView } from "../../components/results";
import { Badge, ErrorBox, JobStatus, KV, Loading, MemoryPanel, PageHead, Pager, Seg, Severity, Table } from "../../components/scm";
import { EntityLink, go, Route, useAsOf, useData, useJob } from "../../nav";
import { AcceptResult, scm } from "../../scm";

export function DisruptionList({ route }: { route: Route }) {
  const { asOf } = useAsOf();
  const [status, setStatus] = useState<"open" | "recent" | "all">((route.query.get("status") as "open") ?? "open");
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  useEffect(() => setOffset(0), [status, q, asOf]);
  const { data, error, loading } = useData(() => scm.disruptions({ as_of: asOf, status, q, limit: 50, offset }), [asOf, status, q, offset]);
  return (
    <div>
      <PageHead title="Disruptions" sub="Supplier delays, quality holds, port congestion and other events detected up to the business date." />
      <div className="toolbar">
        <Seg value={status} onChange={setStatus} options={[["open", "Open"], ["recent", "Last 30 days"], ["all", "All"]]} />
        <input className="search" placeholder="Filter by title, event or supplier" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      <ErrorBox error={error} />
      {loading && !data ? <Loading /> : data && (
        <Panel>
          <Table rows={data.rows} onRow={(r) => go(`/disruptions/${r.event_id}`)} empty="No disruptions match." cols={[
            { key: "event_id", label: "event" },
            { key: "title", render: (r) => String(r.title ?? r.event_type) },
            { key: "event_type", label: "type" },
            { key: "severity", render: (r) => <Severity v={r.severity} /> },
            { key: "origin_entity_id", label: "origin" },
            { key: "detected_at", label: "detected" },
            { key: "state", render: (r) => r.decision_id ? <Badge tone="good">decided</Badge> : r.end_date ? <Badge>closed</Badge> : <Badge tone="bad">needs decision</Badge> },
          ]} />
          <Pager total={data.total} limit={data.limit} offset={data.offset} onChange={setOffset} />
        </Panel>
      )}
    </div>
  );
}

export function DisruptionDetail({ id }: { id: string }) {
  const { asOf } = useAsOf();
  const { data, error, loading, reload } = useData(() => scm.disruption(id, asOf), [id, asOf]);
  const j = useJob<DecisionCard>();
  const [accepting, setAccepting] = useState(false);
  const [accepted, setAccepted] = useState<AcceptResult | null>(null);
  const [acceptErr, setAcceptErr] = useState<string | null>(null);

  // A resolution computed earlier for this event and date is shown immediately.
  useEffect(() => {
    j.reset();
    setAccepted(null);
    setAcceptErr(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, asOf]);

  const ev = data?.event as Row | undefined;
  const ctx = data?.context as Row | null | undefined;
  const cached = (data?.resolution as Row | null)?.result as DecisionCard | undefined;
  const card: DecisionCard | undefined = accepted?.card ?? (j.job?.status === "done" ? (j.job.result as DecisionCard) : cached);
  const writeback = card?.writeback;

  const accept = async () => {
    setAccepting(true);
    setAcceptErr(null);
    try {
      setAccepted(await scm.accept(id, asOf));
      reload();
    } catch (e) {
      setAcceptErr(String((e as Error).message));
    } finally {
      setAccepting(false);
    }
  };

  if (loading && !data) return <Loading />;
  if (error) return <ErrorBox error={error} />;
  if (!ev) return null;
  const decisions = (data!.decisions as Row[]) ?? [];

  return (
    <div>
      <PageHead crumbs={[["Disruptions", "#/disruptions"]]}
        title={<>{String(ev.title ?? ev.event_type)} <span className="muted mono small">{id}</span></>}
        sub={<>Detected {String(ev.detected_at)} · {ev.end_date ? `closed ${ev.end_date}` : <b className="bad-t">open</b>} · origin <EntityLink id={String(ev.origin_entity_id)} /></>}
        right={<Severity v={ev.severity} />} />

      <div className="grid split">
        <div>
          <section className="panel agent">
            <div className="panel-head">
              <h3>Decision agent</h3>
              <div className="row">
                <JobStatus job={j.job} label="recalling memory, simulating options, checking commitments" />
                {ctx && !j.running && !writeback && (
                  <button className="btn" onClick={() => j.start(() => scm.resolve(id, asOf, !!card))}>
                    {card ? "Re-run analysis" : "Resolve with agent"}
                  </button>
                )}
              </div>
            </div>
            {!ctx && (
              <div className="muted">
                This event has no slipped purchase order with dependent production runs as of {asOf}, so there is nothing to simulate. Use the memory panel for its history.
              </div>
            )}
            {ctx && !card && !j.running && (
              <div className="muted">
                The agent will recall what happened in similar disruptions, check open commitments to <EntityLink id={String(ctx.supplier_id)} />, simulate every response for <EntityLink id={String(ctx.rpo_id)} /> against runs {(ctx.run_ids as string[]).map((r) => <EntityLink key={r} id={r} />)} and recommend one it can back with evidence.
              </div>
            )}
            {j.running && <div className="skeleton"><div /><div /><div /><div /></div>}
            {j.error && <div className="alert error"><span className="tag">Agent</span>{j.error}</div>}
            {card && (
              <>
                {card.recommended_action && (
                  <div className={`accept-bar ${writeback ? "done" : ""}`}>
                    {writeback ? (
                      <div>
                        <b>Accepted.</b> Logged as <EntityLink id={String(writeback.decision_id)} /> with commitment <span className="mono">{String(writeback.commitment_id)}</span>; memory write: {String(writeback.retain_status ?? "-")}.
                        {(accepted?.mock_actions ?? card.mock_actions).map((m) => <div key={m} className="mock">{m}</div>)}
                      </div>
                    ) : (
                      <>
                        <div>Recommendation: <b>{card.recommended_action.replace(/_/g, " ")}</b> · confidence {card.confidence}. Accepting records the decision and the commitment it creates, and writes it to memory so the next disruption can learn from it.</div>
                        <button className="btn" disabled={accepting} onClick={accept}>{accepting && <span className="spinner" />}Accept and log</button>
                      </>
                    )}
                  </div>
                )}
                {acceptErr && <div className="alert error"><span className="tag">Accept</span>{acceptErr}</div>}
                <DecisionCardView card={card} />
              </>
            )}
          </section>
        </div>

        <div>
          <Panel title="Event">
            <KV items={[
              ["type", String(ev.event_type)],
              ["root cause", String(ev.root_cause_code ?? "-")],
              ["started", String(ev.start_date ?? "-")],
              ["detail", String(ev.root_cause_text ?? "not yet known")],
            ]} />
          </Panel>
          {ctx && (
            <Panel title="Affected">
              <KV items={[
                ["purchase order", String(ctx.rpo_id)],
                ["supplier", String(ctx.supplier_id)],
                ["material", String(ctx.rm_id)],
                ["plant", String(ctx.plant_id)],
              ]} />
              <Table rows={(data!.affected_runs as Row[]) ?? []} cols={[{ key: "production_run_id", label: "run" }, { key: "sku" }, "planned_start", { key: "planned_qty", label: "qty", num: true }]} />
            </Panel>
          )}
          <MemoryPanel entityId={ctx ? String(ctx.supplier_id) : String(ev.origin_entity_id)} />
          {(data!.impacts as Row[]).length > 0 && (
            <Panel title="Measured impact"><Table rows={data!.impacts as Row[]} cols={["entity_id", "impact_metric", { key: "impact_value", label: "value", num: true }, "unit"]} /></Panel>
          )}
          <Panel title="Related events">
            <Table rows={data!.links as Row[]} empty="No linked events." cols={[{ key: "other_id", label: "event" }, "link_type", { key: "other_title", label: "title" }, { key: "other_detected", label: "detected" }]} />
          </Panel>
          {decisions.length > 0 && (
            <Panel title="Decisions on this event">
              <Table rows={decisions} cols={["decision_id", "decided_at", "decision_type", "outcome_label", "source"]} />
            </Panel>
          )}
        </div>
      </div>
    </div>
  );
}
