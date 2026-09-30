import { BrainCircuit, CheckCircle2, Database, FileCheck2, ShieldCheck } from "lucide-react";
import { useState } from "react";
import { Card, DataTable, ErrorBox, Kpi, KV, Linkify, Loading, PageHead, Pager, Spinner, Status } from "../components/ui";
import { api, fmt, Row, useQuery } from "../lib/api";

export function SystemPage() {
  const { data: d, error, loading } = useQuery<Row>("/system/sync");
  const [offset, setOffset] = useState(0);
  const { data: log } = useQuery<Row>("/activity", { limit: 25, offset });
  const [check, setCheck] = useState<Row | null>(null);
  const [checking, setChecking] = useState(false);
  if (loading && !d) return <Loading />;
  if (error) return <ErrorBox error={error} />;
  const tables: Row[] = d!.tables;
  const diverged = tables.filter((t) => t.status === "DIVERGED").length;
  const changed = tables.filter((t) => t.status !== "identical").length;
  const m = d!.memory;
  return (
    <div>
      <PageHead title="Data & memory sync" icon={<Database size={20} />}
        sub="Meridian runs on a copy of the supply chain dataset. This page proves the copy still matches the source row for row, that every difference was made through the application and audited, and how the dataset maps to the memory corpus in Hindsight." />
      <div className="kpis">
        <Kpi label="Dataset tables" value={`${tables.length - diverged}/${tables.length}`} tone={diverged ? "bad" : "good"} icon={<ShieldCheck size={15} />}
          sub={diverged ? `${diverged} diverged` : changed ? `${changed} changed by audited app actions` : "byte-identical to source"} />
        <Kpi label="Sales reconciliation" value={d!.reconciliation?.units_shipped_on_sales_orders === d!.reconciliation?.dataset_fulfilled_units ? "Exact" : "Mismatch"}
          tone={d!.reconciliation?.units_shipped_on_sales_orders === d!.reconciliation?.dataset_fulfilled_units ? "good" : "bad"} icon={<FileCheck2 size={15} />}
          sub={`${fmt.n(d!.reconciliation?.units_shipped_on_sales_orders)} units = dataset fulfilled demand`} />
        <Kpi label="Memory corpus" value={fmt.n(m.corpus_docs)} icon={<BrainCircuit size={15} />} tone="info"
          sub={`${fmt.pct(m.record_refs_resolved / (m.record_refs || 1))} of ${fmt.n(m.record_refs)} record references resolve`} />
        <Kpi label="Retained in Hindsight" value={fmt.n(m.hindsight.retained)} tone={m.hindsight.failed ? "warn" : "good"} icon={<BrainCircuit size={15} />}
          sub={`${fmt.n(m.hindsight.failed)} documents failed to retain`} />
        <Kpi label="App changes" value={fmt.n(d!.app_activity.audit_entries)} icon={<CheckCircle2 size={15} />}
          sub={`${fmt.n(d!.app_activity.records_created)} records created · pending memory sync`} />
      </div>
      <div className="grid g-main">
        <Card title="Dataset tables" sub="current database vs untouched source" flush>
          <DataTable rows={tables} cols={[
            { key: "table", render: (r) => <span className="mono">{r.table}</span> },
            { key: "source_rows", label: "Source rows", num: true, render: (r) => fmt.n(r.source_rows) },
            { key: "current_rows", label: "Current rows", num: true, render: (r) => fmt.n(r.current_rows) },
            { key: "added_by_app", label: "Added", num: true, render: (r) => r.added_by_app ? <b>{fmt.n(r.added_by_app)}</b> : "–" },
            { key: "changed_by_app", label: "Changed", num: true, render: (r) => r.changed_by_app ? <b>{fmt.n(r.changed_by_app)}</b> : "–" },
            { key: "status", render: (r) => <Status value={r.status} tone={r.status === "identical" ? "good" : r.status === "DIVERGED" ? "bad" : "info"} /> },
          ]} />
        </Card>
        <div className="stack">
          <Card title="Source dataset">
            <KV items={[["Path", <span className="mono small" style={{ wordBreak: "break-all" }}>{d!.source.path}</span>], ["Built", d!.source.built_at], ["Business date", d!.business_date],
              ["SHA-256 at build", <span className="mono small" style={{ wordBreak: "break-all" }}>{d!.source.sha256_at_build?.slice(0, 24)}…</span>]]} />
            <div className="row" style={{ marginTop: 12 }}>
              <button className="btn sm" disabled={checking} onClick={async () => { setChecking(true); try { setCheck(await api.get("/system/source-check")); } finally { setChecking(false); } }}>{checking && <Spinner />}Verify source checksum</button>
              {check && <Status value={check.unchanged ? "unchanged" : "changed"} tone={check.unchanged ? "good" : "bad"} />}
            </div>
          </Card>
          <Card title="How derived data was built">
            <div className="stack small" style={{ gap: 8 }}>
              {Object.entries(d!.lineage ?? {}).map(([k, v]) => <div key={k}><b className="mono">{k}</b><div className="muted">{String(v)}</div></div>)}
            </div>
          </Card>
          <Card title="Memory (Hindsight)">
            <div className="small" style={{ lineHeight: 1.6 }}>{m.note}</div>
          </Card>
        </div>
      </div>
      <Card title="Audit log" sub="every change made through Meridian" flush>
        <DataTable rows={log?.rows ?? []} empty="No changes yet." cols={[
          { key: "at", label: "When", render: (r) => <span title={r.at}>{fmt.ago(r.at)}</span> }, { key: "actor" }, { key: "module", render: (r) => <Status value={r.module} tone="plain" /> },
          { key: "action" }, { key: "summary", wrap: true, render: (r) => <Linkify text={r.summary} /> }]} />
        <Pager page={log as any} onOffset={setOffset} />
      </Card>
    </div>
  );
}
