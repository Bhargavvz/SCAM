import { ArrowRight, RefreshCw, Sparkles, Wand2 } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { EntityLink, ErrorBox, Loading, Markdown, PageHead, Seg, Spinner, Status } from "../components/ui";
import { api, fmt, Row, useQuery } from "../lib/api";
import { Briefing } from "./Dashboard";
import { MemoryHit } from "../components/Memory";

function InsightCard({ i }: { i: Row }) {
  const [exp, setExp] = useState<Row | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const explain = async () => {
    setBusy(true);
    setErr(null);
    try {
      setExp(await api.post(`/insights/${encodeURIComponent(i.id)}/explain`));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const metrics = Object.entries(i.metrics ?? {}).filter(([, v]) => typeof v !== "object").slice(0, 4);
  return (
    <div className={`insight ${i.severity}`}>
      <div className="row between">
        <div className="row"><Status value={i.severity} /><span className="pill plain">{i.module}</span></div>
        <Link to={i.link} className="small">Open <ArrowRight size={12} /></Link>
      </div>
      <h4>{i.title}</h4>
      <div className="small" style={{ color: "var(--text-2)", lineHeight: 1.55 }}>{i.summary}</div>
      {metrics.length > 0 && (
        <div className="kv" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(120px, 1fr))" }}>
          {metrics.map(([k, v]) => (
            <div key={k}><dt>{fmt.label(k)}</dt><dd>{typeof v === "number" ? (Math.abs(v) < 1 && v !== 0 ? fmt.pct(v) : fmt.compact(v)) : String(v)}</dd></div>
          ))}
        </div>
      )}
      {i.entities?.length > 0 && (
        <div className="row small">{i.entities.slice(0, 6).map((e: Row) => <span key={e.id} className="pill plain"><EntityLink id={e.id}>{e.label}</EntityLink></span>)}</div>
      )}
      <div className="rec"><b>Recommended:</b> {i.recommendation}</div>
      {exp ? (
        <div className="ai-box">
          <h5><Sparkles size={12} />AI analysis</h5>
          <Markdown text={exp.text} />
          {exp.ungrounded_numbers?.length > 0 && <div className="small warn-t">Not in evidence: {exp.ungrounded_numbers.join(", ")}</div>}
          {exp.memories?.length > 0 && (
            <details style={{ marginTop: 8 }}><summary className="small" style={{ cursor: "pointer" }}>Recalled from memory ({exp.memories.length})</summary>
              {exp.memories.map((m: Row, k: number) => <MemoryHit key={k} h={m} compact />)}</details>
          )}
        </div>
      ) : (
        <div><button className="btn sm ai" disabled={busy} onClick={explain}>{busy ? <Spinner /> : <Wand2 size={13} />}Explain & plan actions</button></div>
      )}
      {err && <ErrorBox error={err} />}
    </div>
  );
}

export function InsightsPage() {
  const [module, setModule] = useState("all");
  const [refresh, setRefresh] = useState(0);
  const { data, error, loading } = useQuery<Row>("/insights", refresh ? { refresh: true, t: refresh } : undefined);
  const list: Row[] = (data?.insights ?? []).filter((i: Row) => module === "all" || i.module === module);
  const modules = Array.from(new Set<string>((data?.insights ?? []).map((i: Row) => i.module)));
  return (
    <div>
      <PageHead title="AI Insights" icon={<Sparkles size={20} className="ai-t" />}
        sub="Detectors scan every module for changes that matter — measured from the data, with the records involved. AI turns each finding into an explanation and an action plan, using only those numbers."
        actions={<button className="btn" onClick={() => setRefresh(Date.now())}><RefreshCw size={14} />Re-scan</button>} />
      <div style={{ marginBottom: 14 }}><Briefing /></div>
      <div className="row between" style={{ marginBottom: 12 }}>
        <Seg value={module} onChange={setModule} options={[["all", `All (${data?.insights?.length ?? 0})`], ...modules.map((m) => [m, fmt.label(m)] as [string, string])]} />
        {data && <span className="small muted">Scanned {fmt.ago(data.generated_at)} in {data.ms} ms</span>}
      </div>
      <ErrorBox error={error} />
      {loading && !data ? <Loading /> : (
        <div className="grid g2">{list.map((i) => <InsightCard key={i.id} i={i} />)}</div>
      )}
    </div>
  );
}
