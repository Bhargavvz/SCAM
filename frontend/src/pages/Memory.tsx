import { ArrowRight, BookOpen, BrainCircuit, CloudUpload, Database, Download, FolderPlus, GitFork, Library, ListChecks, Pencil, Play, Plus, RefreshCw, RotateCcw, Search, Settings2, Sparkles, Trash2, Wand2, XCircle } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Bars, Donut, TrendArea } from "../components/charts";
import { ForceGraph } from "../components/ForceGraph";
import { MemoryHit, TYPE_LABEL } from "../components/Memory";
import { Card, DataTable, Drawer, EntityLink, ErrorBox, Kpi, KV, Linkify, Loading, Markdown, PageHead, Pager, Seg, Spinner, Status, Tabs, useAction } from "../components/ui";
import { api, fmt, Job, Row, useDebounced, useJob, useQuery } from "../lib/api";

// ======================================================================================= hub
export function MemoryHub() {
  const { data: s, error, reload } = useQuery<Row>("/memory/status");
  const [cov, setCov] = useState<Row | null>(null);
  const [covBusy, setCovBusy] = useState(false);
  const [obStatus, setObStatus] = useState("");
  const [obOffset, setObOffset] = useState(0);
  const [opStatus, setOpStatus] = useState("");
  const ob = useQuery<Row>("/memory/outbox", { status: obStatus, limit: 15, offset: obOffset });
  const ops = useQuery<Row>("/memory/operations", { status: opStatus, limit: 15 });
  const { data: ts } = useQuery<Row>("/memory/timeseries", { period: "30d", time_field: "created_at" });
  const { run, busy } = useAction();
  useEffect(() => {
    const t = setInterval(() => { reload(); ob.reload(); }, 8000);
    return () => clearInterval(t);
  }, [reload, ob.reload]);
  const loadCov = async () => {
    setCovBusy(true);
    try { setCov(await api.get("/memory/coverage")); } finally { setCovBusy(false); }
  };
  useEffect(() => { loadCov(); }, []);
  if (error) return <ErrorBox error={error} />;
  if (!s) return <Loading />;
  const st = s.stats ?? {};
  const k = (kind: string, status: string) => s.outbox.by_kind?.[kind]?.[status] ?? 0;
  const types = Object.entries(st.nodes_by_fact_type ?? {}).map(([t, n]) => ({ type: TYPE_LABEL[t] ?? t, n }));
  const links = Object.entries(st.links_by_link_type ?? {}).map(([t, n]) => ({ type: t, n }));
  const w = s.outbox.worker;
  return (
    <div>
      <PageHead title="Memory" icon={<BrainCircuit size={20} className="ai-t" />}
        sub="Meridian's long-term memory runs on Hindsight. Every change made in the system is retained through a transactional outbox, so the memory bank stays in step with the database — and every module reads from it."
        actions={<>
          <Link className="btn" to="/memory/explore"><Search size={14} />Explore</Link>
          <button className="btn" disabled={!!busy} onClick={async () => { await run("kick", () => api.post("/memory/sync/kick"), "Sync worker woken"); reload(); }}><RefreshCw size={14} />Sync now</button>
        </>} />
      <div className="kpis">
        <Kpi label="Memory bank" value={<span style={{ fontSize: 17 }}>{s.bank_id}</span>} tone={s.health.ok ? "good" : "bad"} icon={<Database size={15} />}
          sub={s.health.ok ? `Hindsight ${s.health.version} · ${s.health.latency_ms} ms` : s.health.error} />
        <Kpi label="Memory units" value={fmt.n(st.total_nodes)} icon={<BrainCircuit size={15} />} tone="info" sub={`${fmt.n(st.total_documents)} documents`} />
        <Kpi label="Graph links" value={fmt.compact(st.total_links)} icon={<GitFork size={15} />} sub="temporal, semantic, entity, causal" />
        <Kpi label="Changes retained" value={`${fmt.n(s.events_synced)} / ${fmt.n(s.audit_entries)}`} icon={<CloudUpload size={15} />}
          tone={s.events_synced === s.audit_entries ? "good" : "warn"} sub="Meridian events in memory" />
        <Kpi label="Sync worker" value={<span style={{ fontSize: 17 }}>{w.running ? "running" : "stopped"}</span>} tone={w.running && !w.last_error ? "good" : "warn"}
          icon={<RefreshCw size={15} />} sub={w.last_error ? w.last_error.slice(0, 60) : w.last_cycle ? `last cycle ${fmt.ago(w.last_cycle)}` : "waiting"} />
      </div>

      <Card title="Sync pipeline" sub="database → outbox → Hindsight operation → memory">
        <div className="pipeline-row">
          <div className="pipe"><div className="pipe-h">Meridian changes</div><div className="pipe-n">{fmt.n(s.audit_entries)}</div><div className="small muted">audited events</div></div>
          <ArrowRight className="muted" />
          <div className="pipe"><div className="pipe-h">Queued</div><div className="pipe-n">{fmt.n(k("event", "queued") + k("corpus", "queued") + k("note", "queued"))}</div><div className="small muted">waiting to send</div></div>
          <ArrowRight className="muted" />
          <div className="pipe"><div className="pipe-h">Processing</div><div className="pipe-n">{fmt.n(k("event", "submitted") + k("corpus", "submitted") + k("note", "submitted"))}</div><div className="small muted">async retain in Hindsight</div></div>
          <ArrowRight className="muted" />
          <div className="pipe good"><div className="pipe-h">In memory</div><div className="pipe-n">{fmt.n(k("event", "synced") + k("corpus", "synced") + k("note", "synced"))}</div><div className="small muted">extracted & linked</div></div>
          <div className="pipe bad"><div className="pipe-h">Retrying / failed</div><div className="pipe-n">{fmt.n(k("event", "failed") + k("corpus", "failed") + k("event", "dead") + k("corpus", "dead"))}</div><div className="small muted">backoff, up to 6 tries</div></div>
        </div>
      </Card>
      <div style={{ height: 14 }} />

      <div className="grid g3">
        <Card title="Dataset corpus coverage" actions={<button className="btn sm ghost" disabled={covBusy} onClick={loadCov}>{covBusy ? <Spinner /> : <RefreshCw size={12} />}</button>}>
          {!cov ? <Loading /> : (
            <div className="stack">
              <div className="kpi-value">{fmt.n(cov.corpus_in_bank)}<span className="small muted"> / {fmt.n(cov.corpus_total)} documents</span></div>
              <div className="bar good"><div style={{ width: `${(cov.corpus_in_bank / cov.corpus_total) * 100}%` }} /></div>
              <div className="small muted">{fmt.n(cov.corpus_missing)} dataset memories never reached the bank during the original ingest. Backfill sends them through the outbox with their original dates.</div>
              <div className="small muted">Meridian events in bank: {fmt.n(cov.events_in_bank)} / {fmt.n(cov.events_total)}</div>
              <button className="btn primary" disabled={!!busy || cov.corpus_missing === 0} onClick={async () => {
                await run("bf", () => api.post("/memory/backfill"), (r: Row) => `Queued ${r.queued} memories for backfill`); reload(); ob.reload();
              }}><CloudUpload size={14} />Backfill {fmt.n(cov.corpus_missing)} missing</button>
            </div>
          )}
        </Card>
        <Card title="What memory holds" sub="by fact type">
          <Donut data={types} nameKey="type" valueKey="n" height={160} fmtValue={(v) => fmt.n(v)} />
        </Card>
        <Card title="How memories connect" sub="links by type">
          <Bars data={links} x="type" horizontal height={180} series={[{ key: "n", name: "Links", color: "#7c3aed" }]} />
        </Card>
      </div>

      <div className="grid g-main">
        <Card title="Outbox" flush actions={<Seg value={obStatus} onChange={(v) => { setObStatus(v); setObOffset(0); }} options={[["", "All"], ["queued", "Queued"], ["submitted", "Processing"], ["synced", "Synced"], ["failed", "Failed"]]} />}>
          <DataTable rows={ob.data?.rows ?? []} empty="Nothing in the outbox yet — make a change anywhere in Meridian." cols={[
            { key: "document_id", label: "Document", render: (r) => <span className="mono">{r.document_id}</span> },
            { key: "kind", render: (r) => <Status value={r.kind} tone="plain" /> },
            { key: "content", wrap: true, render: (r) => <span className="small"><Linkify text={r.content.slice(0, 160)} /></span> },
            { key: "status", render: (r) => <Status value={r.status === "submitted" ? "processing" : r.status} tone={r.status === "synced" ? "good" : r.status === "submitted" ? "violet" : r.status === "queued" ? "info" : "bad"} /> },
            { key: "attempts", num: true },
            { key: "act", label: "", render: (r) => ["failed", "dead"].includes(r.status) ? <button className="btn sm" onClick={async () => { await api.post(`/memory/outbox/${r.outbox_id}/retry`); ob.reload(); }}><RotateCcw size={12} />Retry</button> : r.last_error ? <span className="small bad-t" title={r.last_error}>error</span> : null },
          ]} />
          <Pager page={ob.data} onOffset={setObOffset} />
        </Card>
        <Card title="Hindsight operations" flush actions={<Seg value={opStatus} onChange={setOpStatus} options={[["", "All"], ["pending", "Pending"], ["processing", "Running"], ["completed", "Done"], ["failed", "Failed"]]} />}>
          <ErrorBox error={ops.error} />
          <DataTable rows={ops.data?.operations ?? []} empty="No operations." cols={[
            { key: "task_type", label: "Type", render: (r) => fmt.label(r.task_type) },
            { key: "document_id", label: "Doc", render: (r) => <span className="mono small">{r.document_id ?? r.mental_model_id ?? "–"}</span> },
            { key: "created_at", label: "Created", render: (r) => fmt.ago(r.created_at) },
            { key: "status", render: (r) => <Status value={r.status} tone={r.status === "completed" ? "good" : r.status === "failed" ? "bad" : "violet"} /> },
            { key: "act", label: "", render: (r) => r.status === "failed" ? <button className="btn sm" onClick={async () => { await api.post(`/memory/operations/${r.id}/retry`); ops.reload(); }}>Retry</button>
              : ["pending", "processing"].includes(r.status) ? <button className="btn sm ghost" onClick={async () => { if (confirm("Cancel this operation?")) { await api.post(`/memory/operations/${r.id}/cancel`); ops.reload(); } }}><XCircle size={12} /></button> : null },
          ]} />
        </Card>
      </div>
      <div className="grid g2">
        <Card title="Memories added" sub="last 30 days, by type">
          {ts?.buckets ? <TrendArea data={ts.buckets} x="time" height={200} yFmt={(v) => fmt.n(v)} series={[{ key: "world", name: "Facts", color: "#2563eb" }, { key: "experience", name: "Experiences", color: "#7c3aed" }, { key: "observation", name: "Observations", color: "#0e7490" }]} /> : <Loading />}
        </Card>
        <Card title="Memory layer setup" sub="directives, mental models and playbooks created in the bank"
          actions={<button className="btn sm" disabled={!!busy} onClick={async () => { await run("bs", () => api.post("/memory/bootstrap"), (r: Row) => `Setup ${r.status}: ${r.created.length} created`); reload(); }}><Play size={12} />Run setup</button>}>
          <KV items={[["Status", <Status value={s.bootstrap.status} />], ["Finished", s.bootstrap.finished_at ? fmt.ago(s.bootstrap.finished_at) : "–"], ["Created this run", s.bootstrap.created.length]]} />
          {s.bootstrap.created.length > 0 && <ul className="mem-list small">{s.bootstrap.created.slice(0, 8).map((c: string) => <li key={c}>{c}</li>)}</ul>}
          {s.bootstrap.errors.length > 0 && <ErrorBox error={s.bootstrap.errors.join("; ")} />}
          <div className="row" style={{ marginTop: 10 }}>
            <Link className="btn sm" to="/memory/models"><Library size={12} />Mental models</Link>
            <Link className="btn sm" to="/memory/knowledge"><BookOpen size={12} />Knowledge base</Link>
            <Link className="btn sm" to="/memory/settings"><Settings2 size={12} />Directives & settings</Link>
          </div>
        </Card>
      </div>
    </div>
  );
}

// ======================================================================================= explorer
const SCHEMA_OPTS: [string, string][] = [["", "Free text"], ["brief", "Brief"], ["advice", "Advice"], ["precedent", "Precedent"]];

export function MemoryExplorer() {
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") ?? "recall";
  return (
    <div>
      <PageHead crumbs={[["Memory", "/memory"]]} title="Memory explorer" icon={<Search size={20} />}
        sub="Query the memory bank directly: recall ranked memories, reflect to reason over them, and browse memories, documents and the entity graph." />
      <Tabs value={tab} onChange={(t) => setParams({ tab: t })} options={[["recall", "Recall"], ["reflect", "Reflect"], ["memories", "Memories"], ["documents", "Documents"], ["graph", "Entity graph"]]} />
      {tab === "recall" && <RecallTab />}
      {tab === "reflect" && <ReflectTab />}
      {tab === "memories" && <MemoriesTab />}
      {tab === "documents" && <DocumentsTab />}
      {tab === "graph" && <GraphTab />}
    </div>
  );
}

function RecallTab() {
  const [q, setQ] = useState("Which suppliers slip in the fourth quarter?");
  const [types, setTypes] = useState<string[]>([]);
  const [budget, setBudget] = useState("mid");
  const [asOf, setAsOf] = useState("2025-12-31");
  const [windowDays, setWindowDays] = useState("");
  const [tag, setTag] = useState("");
  const [prefer, setPrefer] = useState(false);
  const [res, setRes] = useState<Row | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const go = async () => {
    setBusy(true); setErr(null);
    try {
      setRes(await api.post("/memory/recall", { query: q, types: types.length ? types : undefined, budget, as_of: asOf || undefined,
        window_days: windowDays ? Number(windowDays) : undefined, tags: tag ? [tag] : undefined, prefer_observations: prefer }));
    } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  };
  return (
    <div className="grid g-side">
      <Card title="Recall">
        <div className="stack">
          <textarea className="input" value={q} onChange={(e) => setQ(e.target.value)} />
          <div className="field">Memory types
            <div className="row">{["world", "experience", "observation"].map((t) => (
              <label key={t} className="row small"><input type="checkbox" checked={types.includes(t)} onChange={(e) => setTypes(e.target.checked ? [...types, t] : types.filter((x) => x !== t))} />{TYPE_LABEL[t]}s</label>
            ))}</div>
          </div>
          <div className="grid g2" style={{ marginBottom: 0 }}>
            <label className="field">Budget<select className="input" value={budget} onChange={(e) => setBudget(e.target.value)}><option>low</option><option>mid</option><option>high</option></select></label>
            <label className="field">As of<input className="input" type="date" value={asOf} onChange={(e) => setAsOf(e.target.value)} /></label>
            <label className="field">Window (days back)<input className="input" type="number" placeholder="any" value={windowDays} onChange={(e) => setWindowDays(e.target.value)} /></label>
            <label className="field">Tag filter<input className="input" placeholder="e.g. source:meridian" value={tag} onChange={(e) => setTag(e.target.value)} /></label>
          </div>
          <label className="row small"><input type="checkbox" checked={prefer} onChange={(e) => setPrefer(e.target.checked)} />Prefer consolidated observations</label>
          <button className="btn primary" disabled={busy || q.length < 2} onClick={go}>{busy ? <Spinner /> : <Search size={14} />}Recall</button>
          <ErrorBox error={err} />
        </div>
      </Card>
      <Card title={res ? `${res.results.length} memories` : "Results"} sub={res ? `${res.latency_ms} ms` : undefined}>
        {!res ? <div className="muted">Recall ranks memories by meaning, keywords, time and the entity graph. Dates after “as of” are never returned.</div> : (
          <>
            {res.entities?.length > 0 && <div className="row small" style={{ marginBottom: 10 }}>{res.entities.slice(0, 14).map((e: Row) => <span key={e.name} className="pill plain">{e.name}{e.observations.length ? ` · ${e.observations.length} obs` : ""}</span>)}</div>}
            <div className="mem-scroll tall">{res.results.map((h: Row) => <MemoryHit key={h.id} h={h} />)}</div>
          </>
        )}
      </Card>
    </div>
  );
}

function ReflectTab() {
  const [q, setQ] = useState("What should a buyer know before placing an order with SUP0247?");
  const [schema, setSchema] = useState("");
  const [budget, setBudget] = useState("mid");
  const [noMM, setNoMM] = useState(false);
  const j = useJob<Row>();
  const r = j.result;
  return (
    <div className="grid g-side">
      <Card title="Reflect">
        <div className="stack">
          <textarea className="input" value={q} onChange={(e) => setQ(e.target.value)} />
          <label className="field">Structured output<Seg value={schema} onChange={setSchema} options={SCHEMA_OPTS} /></label>
          <label className="field">Budget<select className="input" value={budget} onChange={(e) => setBudget(e.target.value)}><option>low</option><option>mid</option><option>high</option></select></label>
          <label className="row small"><input type="checkbox" checked={noMM} onChange={(e) => setNoMM(e.target.checked)} />Exclude mental models (raw memories only)</label>
          <button className="btn ai" disabled={j.running || q.length < 2} onClick={() => j.start(() => api.post<Job>("/memory/reflect", { query: q, schema_name: schema || undefined, budget, exclude_mental_models: noMM }))}>
            {j.running ? <Spinner /> : <Wand2 size={14} />}Reflect</button>
          {j.running && <div className="small muted">Hindsight is searching memories, applying directives and mental models, and reasoning… ({j.job?.elapsed_s}s)</div>}
          <ErrorBox error={j.error} />
        </div>
      </Card>
      <Card title="Answer" sub={r ? `${r.memory_count} memories · ${r.mental_models.length} mental models · ${r.directives.length} directives · ${(r.latency_ms / 1000).toFixed(1)}s` : undefined}>
        {!r ? <div className="muted">Reflect reasons over the bank with its mission, directives and mental models, and can return structured JSON.</div> : (
          <div className="stack">
            {r.structured ? <pre className="json">{JSON.stringify(r.structured, null, 2)}</pre> : <Markdown text={r.text} />}
            {r.directives.length > 0 && <div className="row small">{r.directives.map((d: Row) => <span key={d.id ?? d.name} className="pill violet plain">{d.name}</span>)}</div>}
            {r.mental_models.length > 0 && <details><summary className="small" style={{ cursor: "pointer" }}>Mental models used ({r.mental_models.length})</summary>{r.mental_models.map((m: Row) => <div key={m.id} className="note small" style={{ marginTop: 6 }}>{m.text?.slice(0, 500)}</div>)}</details>}
            <details open><summary className="small" style={{ cursor: "pointer" }}>Based on {r.memory_count} memories</summary><div className="mem-scroll">{r.memories.map((h: Row, i: number) => <MemoryHit key={i} h={h} compact />)}</div></details>
          </div>
        )}
      </Card>
    </div>
  );
}

function MemoriesTab() {
  const [type, setType] = useState("");
  const [search, setSearch] = useState("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [offset, setOffset] = useState(0);
  const ds = useDebounced(search, 400);
  useEffect(() => setOffset(0), [type, ds, start, end]);
  const { data, error, loading } = useQuery<Row>("/memory/memories", { type, search: ds, start, end, limit: 40, offset });
  return (
    <Card flush>
      <div className="toolbar">
        <Seg value={type} onChange={setType} options={[["", "All"], ["world", "Facts"], ["experience", "Experiences"], ["observation", "Observations"]]} />
        <input className="input" style={{ width: 240 }} placeholder="Search text" value={search} onChange={(e) => setSearch(e.target.value)} />
        <input className="input" type="date" value={start} onChange={(e) => setStart(e.target.value)} title="from" />
        <input className="input" type="date" value={end} onChange={(e) => setEnd(e.target.value)} title="to" />
      </div>
      <ErrorBox error={error} />
      {loading && !data ? <Loading /> : (
        <DataTable rows={data?.items ?? []} cols={[
          { key: "fact_type", label: "Type", render: (r) => <Status value={TYPE_LABEL[r.fact_type] ?? r.fact_type} tone={r.fact_type === "observation" ? "violet" : r.fact_type === "experience" ? "ai" : "info"} /> },
          { key: "when", label: "When", render: (r) => (r.occurred_start || r.mentioned_at || "").slice(0, 10) || "–" },
          { key: "text", wrap: true, render: (r) => <Linkify text={r.text} /> },
          { key: "document_id", label: "Document", render: (r) => <Link className="mono small" to={`/memory/explore?tab=documents&doc=${r.document_id}`}>{r.document_id}</Link> },
          { key: "proof_count", label: "Proof", num: true },
        ]} />
      )}
      <Pager page={data ? { total: data.total, limit: data.limit, offset: data.offset, rows: [] } : null} onOffset={setOffset} />
    </Card>
  );
}

function DocumentsTab() {
  const [params, setParams] = useSearchParams();
  const [q, setQ] = useState("");
  const [tag, setTag] = useState("");
  const [offset, setOffset] = useState(0);
  const dq = useDebounced(q, 350);
  const docId = params.get("doc");
  const { data, error, loading } = useQuery<Row>("/memory/documents", { q: dq, tag, limit: 30, offset });
  const doc = useQuery<Row>(docId ? `/memory/documents/${docId}` : null);
  return (
    <div className="grid g-side">
      <Card flush>
        <div className="toolbar">
          <input className="input grow" placeholder="Document id (DOC…, MER-…)" value={q} onChange={(e) => { setQ(e.target.value); setOffset(0); }} />
          <select className="input" value={tag} onChange={(e) => setTag(e.target.value)}><option value="">All</option><option value="source:meridian">Meridian events</option><option value="source:dataset">Backfilled corpus</option></select>
        </div>
        <ErrorBox error={error} />
        {loading && !data ? <Loading /> : (
          <DataTable rows={data?.items ?? []} onRow={(r) => setParams({ tab: "documents", doc: r.id })} cols={[
            { key: "id", label: "Document", render: (r) => <span className="mono">{r.id}</span> },
            { key: "memory_unit_count", label: "Units", num: true },
            { key: "created_at", label: "Added", render: (r) => fmt.ago(r.created_at) },
          ]} />
        )}
        <Pager page={data ? { total: data.total, limit: data.limit, offset: data.offset, rows: [] } : null} onOffset={setOffset} />
      </Card>
      <Card title={docId ?? "Document"}>
        {!docId ? <div className="muted">Pick a document to see its original text, the memory units extracted from it and the links between them.</div> :
          doc.loading ? <Loading /> : doc.error ? <ErrorBox error={doc.error} /> : (
            <div className="stack">
              <KV items={[["Context", doc.data!.document.retain_params?.context ?? doc.data!.corpus?.context], ["Event date", (doc.data!.document.retain_params?.event_date ?? "").slice(0, 10)],
                ["Units", doc.data!.document.memory_unit_count], ["Tags", (doc.data!.document.tags ?? []).slice(0, 4).join(", ") || "–"]]} />
              {doc.data!.corpus?.records?.length > 0 && <div className="row small">{doc.data!.corpus.records.map((r: string) => <span key={r} className="pill plain"><EntityLink id={r} /></span>)}</div>}
              <pre className="doc-text">{doc.data!.document.original_text}</pre>
              <div className="mem-label">Extracted memory units</div>
              {(doc.data!.graph?.table_rows ?? []).slice(0, 20).map((u: Row, i: number) => (
                <div key={i} className="mem-hit"><div className="row" style={{ gap: 6 }}><Status value={TYPE_LABEL[u.fact_type] ?? u.fact_type ?? "unit"} tone="info" /><span className="small muted">{(u.occurred_start ?? u.date ?? "").slice(0, 10)}</span></div><div className="mem-text"><Linkify text={u.text ?? u.content ?? JSON.stringify(u).slice(0, 200)} /></div></div>
              ))}
            </div>
          )}
      </Card>
    </div>
  );
}

function GraphTab() {
  const [limit, setLimit] = useState("120");
  const [minCount, setMinCount] = useState("3");
  const { data, error, loading } = useQuery<Row>("/memory/entity-graph", { limit, min_count: minCount });
  const [pick, setPick] = useState<Row | null>(null);
  const graph = useMemo(() => {
    if (!data) return { nodes: [], edges: [] };
    return {
      nodes: (data.nodes ?? []).map((n: Row) => ({ id: n.data.id, label: n.data.label, weight: n.data.mention_count ?? 1 })),
      edges: (data.edges ?? []).map((e: Row) => ({ source: e.data.source, target: e.data.target, weight: e.data.weight ?? 1 })),
    };
  }, [data]);
  const [pickRecall, setPickRecall] = useState<Row | null>(null);
  useEffect(() => {
    if (!pick) return;
    setPickRecall(null);
    api.post("/memory/recall", { query: pick.label, as_of: "2025-12-31", budget: "low", max_tokens: 1200 }).then(setPickRecall).catch(() => setPickRecall({ results: [] }));
  }, [pick]);
  return (
    <div className="grid g-main">
      <Card title="Entity co-occurrence graph" sub={data ? `${data.total_entities} entities · ${fmt.compact(data.total_edges)} edges in the bank` : undefined}
        actions={<><select className="input" value={limit} onChange={(e) => setLimit(e.target.value)}><option value="60">60 edges</option><option value="120">120 edges</option><option value="250">250 edges</option></select>
          <select className="input" value={minCount} onChange={(e) => setMinCount(e.target.value)}><option value="2">≥2 co-mentions</option><option value="3">≥3</option><option value="10">≥10</option></select></>}>
        <ErrorBox error={error} />
        {loading && !data ? <Loading /> : <ForceGraph nodes={graph.nodes} edges={graph.edges} onPick={setPick} />}
        <div className="small muted" style={{ marginTop: 6 }}>Entities Hindsight extracted from memories; edges connect entities mentioned together. Hover to highlight neighbours, click to recall.</div>
      </Card>
      <Card title={pick ? pick.label : "Entity"} sub={pick ? `${pick.weight} mentions` : undefined}>
        {!pick ? <div className="muted">Click a node.</div> : !pickRecall ? <Loading /> : (
          <div className="mem-scroll tall">{pickRecall.results.slice(0, 20).map((h: Row) => <MemoryHit key={h.id} h={h} compact />)}</div>
        )}
      </Card>
    </div>
  );
}

// ======================================================================================= mental models
export function MentalModels() {
  const { data, error, loading, reload } = useQuery<Row[]>("/memory/mental-models");
  const [sel, setSel] = useState<string | null>(null);
  const [edit, setEdit] = useState<Row | null>(null);
  const { run, busy } = useAction();
  const detail = useQuery<Row>(sel ? `/memory/mental-models/${sel}` : null);
  const history = useQuery<Row>(sel ? `/memory/mental-models/${sel}/history` : null);
  const dry = useJob<Row>();
  const list = Array.isArray(data) ? data : [];
  const m = detail.data;
  return (
    <div>
      <PageHead crumbs={[["Memory", "/memory"]]} title="Mental models" icon={<Library size={20} />}
        sub="Standing summaries Hindsight keeps up to date from the memory bank — supplier reliability, disruption playbooks, carrier performance and more. Reflect uses them, and they refresh on a schedule or on demand."
        actions={<button className="btn primary" onClick={() => setEdit({ name: "", source_query: "", refresh_cron: "0 5 * * *" })}><Plus size={14} />New mental model</button>} />
      <ErrorBox error={error} />
      <div className="grid g-side">
        <Card flush title={`${list.length} models`}>
          {loading && !data ? <Loading /> : list.map((x) => (
            <button key={x.id} className={`list-item ${sel === x.id ? "sel" : ""}`} onClick={() => { setSel(x.id); dry.reset(); }}>
              <div className="row between"><b>{x.name}</b>{x.is_stale ? <Status value="stale" tone="warn" /> : x.content ? <Status value="fresh" tone="good" /> : <Status value="building" tone="violet" />}</div>
              <div className="small muted">{x.last_refreshed_at ? `refreshed ${fmt.ago(x.last_refreshed_at)}` : "not refreshed yet"} · {(x.tags ?? []).join(", ")}</div>
            </button>
          ))}
          {!loading && list.length === 0 && <div className="empty">No mental models yet — Memory → Run setup creates twelve.</div>}
        </Card>
        <Card title={m?.name ?? "Select a model"} actions={m && <>
          <button className="btn sm" disabled={!!busy} onClick={async () => { await run("r", () => api.post(`/memory/mental-models/${m.id}/refresh`), "Refresh started"); reload(); }}><RefreshCw size={12} />Refresh</button>
          <button className="btn sm" disabled={dry.running} onClick={() => dry.start(() => api.post<Job>(`/memory/mental-models/${m.id}/dry-run`))}>{dry.running ? <Spinner /> : <Play size={12} />}Dry run</button>
          <button className="btn sm" onClick={() => setEdit({ id: m.id, name: m.name, source_query: m.source_query, refresh_cron: m.trigger?.refresh_cron })}><Pencil size={12} /></button>
          <button className="btn sm danger" onClick={async () => { if (confirm(`Delete mental model "${m.name}"?`)) { await run("d", () => api.delete(`/memory/mental-models/${m.id}`), "Deleted"); setSel(null); reload(); } }}><Trash2 size={12} /></button>
        </>}>
          {!sel ? <div className="muted">Pick a model to read its current content, its source query, refresh history, or preview what a refresh would produce.</div> : detail.loading ? <Loading /> : m && (
            <div className="stack">
              <div className="note small"><b>Source query:</b> {m.source_query}</div>
              <KV items={[["Refreshed", m.last_refreshed_at ? fmt.ago(m.last_refreshed_at) : "never"], ["Schedule", m.trigger?.refresh_cron ?? "manual"], ["Last memory seen", (m.last_memory_seen_at ?? "").slice(0, 10) || "–"], ["Stale", m.is_stale ? "yes" : "no"]]} />
              {dry.result ? (
                <div className="ai-box"><h5><Play size={12} />Dry-run preview (not saved)</h5><Markdown text={dry.result.content ?? dry.result.text ?? JSON.stringify(dry.result).slice(0, 3000)} /></div>
              ) : null}
              <ErrorBox error={dry.error} />
              {m.content ? <Markdown text={m.content} /> : <div className="muted">No content yet — the first refresh is running in Hindsight.</div>}
              {Array.isArray(history.data?.items ?? history.data) && (history.data?.items ?? history.data).length > 0 && (
                <details><summary className="small" style={{ cursor: "pointer" }}>History ({(history.data?.items ?? history.data).length} versions)</summary>
                  {(history.data?.items ?? history.data).slice(0, 10).map((h: Row, i: number) => <div key={i} className="small muted" style={{ marginTop: 4 }}>{fmt.ago(h.created_at ?? h.refreshed_at ?? "")} · {(h.content ?? "").slice(0, 160)}</div>)}</details>
              )}
            </div>
          )}
        </Card>
      </div>
      <Drawer open={!!edit} onClose={() => setEdit(null)} title={edit?.id ? "Edit mental model" : "New mental model"}
        footer={<><button className="btn" onClick={() => setEdit(null)}>Cancel</button><button className="btn primary" disabled={!!busy || !edit?.name || !edit?.source_query} onClick={async () => {
          const body = { name: edit!.name, source_query: edit!.source_query, refresh_cron: edit!.refresh_cron || undefined, tags: ["meridian"] };
          const r = await run("s", () => (edit!.id ? api.patch(`/memory/mental-models/${edit!.id}`, body) : api.post("/memory/mental-models", body)), "Saved");
          if (r) { setEdit(null); reload(); }
        }}>Save</button></>}>
        {edit && <>
          <label className="field">Name<input className="input" value={edit.name} onChange={(e) => setEdit({ ...edit, name: e.target.value })} /></label>
          <label className="field">Source query — the question the model keeps answering<textarea className="input" value={edit.source_query} onChange={(e) => setEdit({ ...edit, source_query: e.target.value })} /></label>
          <label className="field">Refresh schedule (cron, UTC)<input className="input mono" value={edit.refresh_cron ?? ""} onChange={(e) => setEdit({ ...edit, refresh_cron: e.target.value })} placeholder="0 5 * * *" /></label>
        </>}
      </Drawer>
    </div>
  );
}

// ======================================================================================= knowledge base
export function KnowledgeBase() {
  const { data, error, loading, reload } = useQuery<Row>("/memory/kb");
  const [sel, setSel] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<Row[] | null>(null);
  const [create, setCreate] = useState<Row | null>(null);
  const { run, busy } = useAction();
  const pageQ = useQuery<Row>(sel ? `/memory/kb/pages/${sel}` : null);
  const Node = ({ n, depth }: { n: Row; depth: number }) => (
    <>
      <button className={`list-item ${sel === n.id ? "sel" : ""}`} style={{ paddingLeft: 14 + depth * 16 }} onClick={() => n.kind !== "folder" && setSel(n.id)}>
        <div className="row">{n.kind === "folder" ? <FolderPlus size={13} className="muted" /> : <BookOpen size={13} className="ai-t" />}<b>{n.name}</b></div>
      </button>
      {(n.children ?? []).map((c: Row) => <Node key={c.id} n={c} depth={depth + 1} />)}
    </>
  );
  const roots: Row[] = data?.roots ?? [];
  const firstFolder = roots.find((r) => r.kind === "folder");
  return (
    <div>
      <PageHead crumbs={[["Memory", "/memory"]]} title="Knowledge base" icon={<BookOpen size={20} />}
        sub="Living playbooks written by Hindsight from the memory bank and refreshed automatically — the institutional knowledge a new planner would need."
        actions={<><button className="btn" onClick={() => setCreate({ folder: true, name: "" })}><FolderPlus size={14} />Folder</button>
          <button className="btn primary" onClick={() => setCreate({ folder: false, name: "", source_query: "", parent_id: firstFolder?.id })}><Plus size={14} />New page</button></>} />
      <ErrorBox error={error} />
      <div className="grid g-side">
        <div className="stack">
          <Card flush>
            <div className="toolbar">
              <input className="input grow" placeholder="Search the knowledge base" value={q} onChange={(e) => setQ(e.target.value)}
                onKeyDown={async (e) => { if (e.key === "Enter" && q) { const r = await api.get<Row>("/memory/kb/search", { q }); setHits(r.results ?? r.items ?? []); } }} />
            </div>
            {hits && <div>{hits.length === 0 ? <div className="empty">No matches.</div> : hits.map((h: Row) => <button key={h.id ?? h.page_id} className="list-item" onClick={() => setSel(h.id ?? h.page_id)}><b>{h.name}</b><div className="small muted">{(h.snippet ?? h.excerpt ?? "").slice(0, 120)}</div></button>)}</div>}
          </Card>
          <Card flush title="Pages">{loading && !data ? <Loading /> : roots.length === 0 ? <div className="empty">Empty — Memory → Run setup creates the playbooks.</div> : roots.map((r) => <Node key={r.id} n={r} depth={0} />)}</Card>
        </div>
        <Card title={pageQ.data?.name ?? "Page"} actions={pageQ.data && <button className="btn sm danger" onClick={async () => { if (confirm("Delete this page?")) { await run("d", () => api.delete(`/memory/kb/${sel}`), "Deleted"); setSel(null); reload(); } }}><Trash2 size={12} /></button>}>
          {!sel ? <div className="muted">Pick a page.</div> : pageQ.loading ? <Loading /> : pageQ.error ? <ErrorBox error={pageQ.error} /> :
            pageQ.data?.markdown || pageQ.data?.body ? <Markdown text={pageQ.data.markdown ?? pageQ.data.body} /> : <div className="muted">Hindsight is still writing this page. It refreshes automatically.</div>}
        </Card>
      </div>
      <Drawer open={!!create} onClose={() => setCreate(null)} title={create?.folder ? "New folder" : "New knowledge page"}
        footer={<><button className="btn" onClick={() => setCreate(null)}>Cancel</button><button className="btn primary" disabled={!!busy || !create?.name || (!create?.folder && !create?.source_query)} onClick={async () => {
          const r = await run("c", () => api.post("/memory/kb", { name: create!.name, source_query: create!.folder ? undefined : create!.source_query, parent_id: create!.parent_id, refresh_cron: create!.folder ? undefined : "0 6 * * *" }), "Created");
          if (r) { setCreate(null); reload(); }
        }}>Create</button></>}>
        {create && <>
          <label className="field">Name<input className="input" value={create.name} onChange={(e) => setCreate({ ...create, name: e.target.value })} /></label>
          {!create.folder && <label className="field">What should this page explain?<textarea className="input" value={create.source_query} onChange={(e) => setCreate({ ...create, source_query: e.target.value })} placeholder="Write a playbook for…" /></label>}
          <label className="field">Folder<select className="input" value={create.parent_id ?? ""} onChange={(e) => setCreate({ ...create, parent_id: e.target.value || undefined })}><option value="">(root)</option>{roots.filter((r) => r.kind === "folder").map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}</select></label>
        </>}
      </Drawer>
    </div>
  );
}

// ======================================================================================= settings
export function MemorySettings() {
  const dirs = useQuery<Row[]>("/memory/directives");
  const cfg = useQuery<Row>("/memory/config");
  const [edit, setEdit] = useState<Row | null>(null);
  const [draft, setDraft] = useState<Row>({});
  const { run, busy } = useAction();
  useEffect(() => { if (cfg.data) setDraft(cfg.data.editable); }, [cfg.data]);
  const list = Array.isArray(dirs.data) ? dirs.data : [];
  const bool = (k: string, label: string) => (
    <label className="row small" key={k}><input type="checkbox" checked={!!draft[k]} onChange={(e) => setDraft({ ...draft, [k]: e.target.checked })} />{label}</label>
  );
  return (
    <div>
      <PageHead crumbs={[["Memory", "/memory"]]} title="Directives & bank settings" icon={<Settings2 size={20} />}
        sub="Directives are rules Hindsight applies to every reflect answer. Bank settings control what gets extracted, how memories consolidate into observations, and how retrieval works."
        actions={<a className="btn" href="/api/memory/export" download><Download size={14} />Export bank</a>} />
      <div className="grid g2">
        <Card title="Directives" flush actions={<button className="btn sm primary" onClick={() => setEdit({ name: "", content: "", priority: 0 })}><Plus size={12} />Add</button>}>
          <ErrorBox error={dirs.error} />
          {list.map((d) => (
            <div key={d.id} className="alert-row">
              <input type="checkbox" checked={d.is_active} title="active" onChange={async () => { await api.patch(`/memory/directives/${d.id}`, { is_active: !d.is_active }); dirs.reload(); }} />
              <div className="grow"><b>{d.name}</b> <span className="small muted">priority {d.priority}</span><div className="small muted">{d.content}</div></div>
              <button className="btn sm ghost" onClick={() => setEdit(d)}><Pencil size={12} /></button>
              <button className="btn sm ghost" onClick={async () => { if (confirm(`Delete directive "${d.name}"?`)) { await api.delete(`/memory/directives/${d.id}`); dirs.reload(); } }}><Trash2 size={12} /></button>
            </div>
          ))}
        </Card>
        <Card title="Bank configuration" actions={<button className="btn sm primary" disabled={!!busy} onClick={async () => {
          const changes = Object.fromEntries(Object.entries(draft).filter(([k, v]) => v !== cfg.data?.editable?.[k]));
          if (!Object.keys(changes).length) return;
          await run("cfg", () => api.patch("/memory/config", changes), "Bank settings saved"); cfg.reload();
        }}>Save</button>}>
          {cfg.loading ? <Loading /> : cfg.error ? <ErrorBox error={cfg.error} /> : (
            <div className="stack">
              <label className="field">Reflect mission<textarea className="input" value={draft.reflect_mission ?? ""} onChange={(e) => setDraft({ ...draft, reflect_mission: e.target.value })} /></label>
              <label className="field">Retain mission (what to extract)<textarea className="input" value={draft.retain_mission ?? ""} onChange={(e) => setDraft({ ...draft, retain_mission: e.target.value })} /></label>
              <label className="field">Observations mission<textarea className="input" value={draft.observations_mission ?? ""} onChange={(e) => setDraft({ ...draft, observations_mission: e.target.value })} /></label>
              <div className="grid g2" style={{ marginBottom: 0 }}>
                {bool("enable_observations", "Consolidate observations")}{bool("enable_auto_consolidation", "Auto consolidation")}
                {bool("enable_temporal_retrieval", "Temporal retrieval")}{bool("enable_graph_retrieval", "Graph retrieval")}
                {bool("enable_text_search", "Keyword search")}{bool("enable_reranking", "Reranking")}
              </div>
              <div className="grid g3" style={{ marginBottom: 0 }}>
                {["disposition_skepticism", "disposition_literalism", "disposition_empathy"].map((k) => (
                  <label key={k} className="field">{fmt.label(k.replace("disposition_", ""))} (1-5)<input className="input" type="number" min={1} max={5} value={draft[k] ?? ""} onChange={(e) => setDraft({ ...draft, [k]: e.target.value ? Number(e.target.value) : null })} /></label>
                ))}
              </div>
              <details><summary className="small muted" style={{ cursor: "pointer" }}>All settings (read-only)</summary><pre className="json">{JSON.stringify(cfg.data?.all, null, 2)}</pre></details>
            </div>
          )}
        </Card>
      </div>
      <Drawer open={!!edit} onClose={() => setEdit(null)} title={edit?.id ? "Edit directive" : "New directive"}
        footer={<><button className="btn" onClick={() => setEdit(null)}>Cancel</button><button className="btn primary" disabled={!!busy || !edit?.name || !edit?.content} onClick={async () => {
          const body = { name: edit!.name, content: edit!.content, priority: Number(edit!.priority ?? 0) };
          const r = await run("d", () => (edit!.id ? api.patch(`/memory/directives/${edit!.id}`, body) : api.post("/memory/directives", body)), "Directive saved");
          if (r) { setEdit(null); dirs.reload(); }
        }}>Save</button></>}>
        {edit && <>
          <label className="field">Name<input className="input" value={edit.name} onChange={(e) => setEdit({ ...edit, name: e.target.value })} /></label>
          <label className="field">Rule<textarea className="input" value={edit.content} onChange={(e) => setEdit({ ...edit, content: e.target.value })} /></label>
          <label className="field">Priority<input className="input" type="number" value={edit.priority ?? 0} onChange={(e) => setEdit({ ...edit, priority: e.target.value })} /></label>
        </>}
      </Drawer>
    </div>
  );
}
