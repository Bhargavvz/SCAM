// Building blocks shared by the supply chain pages.
import { useEffect } from "react";
import { CapabilityAnswer, Row } from "../api";
import { EntityLink, Linkify, useAsOf, useJob } from "../nav";
import { Job, scm } from "../scm";
import { fmt, Markdown } from "./common";

const ID = /^(?:EVT|RPO|SUP|RM|DECL|DEC|PR|CMTL|CMT|PL)\d{2,}$/;

export type Col = { key: string; label?: string; render?: (r: Row) => React.ReactNode; num?: boolean; width?: number };

/** Table where record ids become links and columns can render custom cells. */
export function Table({ rows, cols, empty = "Nothing to show.", rowClass, onRow }: {
  rows: Row[]; cols: (Col | string)[]; empty?: string; rowClass?: (r: Row) => string | undefined; onRow?: (r: Row) => void;
}) {
  if (!rows?.length) return <div className="empty">{empty}</div>;
  const cs: Col[] = cols.map((c) => (typeof c === "string" ? { key: c } : c));
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>{cs.map((c) => <th key={c.key} className={c.num ? "num" : undefined} style={c.width ? { width: c.width } : undefined}>{c.label ?? c.key.replace(/_/g, " ")}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className={[rowClass?.(r), onRow ? "clickable" : ""].filter(Boolean).join(" ") || undefined}
              onClick={onRow ? () => onRow(r) : undefined}>
              {cs.map((c) => {
                if (c.render) return <td key={c.key} className={c.num ? "num" : undefined}>{c.render(r)}</td>;
                const v = r[c.key];
                const s = fmt(v);
                return (
                  <td key={c.key} className={c.num || typeof v === "number" ? "num" : undefined}>
                    {typeof v === "string" && ID.test(v) ? <EntityLink id={v} /> : s}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Pager({ total, limit, offset, onChange }: { total: number; limit: number; offset: number; onChange: (o: number) => void }) {
  if (total <= limit) return <div className="pager muted">{total.toLocaleString()} rows</div>;
  return (
    <div className="pager">
      <span className="muted">{(offset + 1).toLocaleString()}–{Math.min(total, offset + limit).toLocaleString()} of {total.toLocaleString()}</span>
      <button className="btn ghost sm" disabled={offset === 0} onClick={() => onChange(Math.max(0, offset - limit))}>Previous</button>
      <button className="btn ghost sm" disabled={offset + limit >= total} onClick={() => onChange(offset + limit)}>Next</button>
    </div>
  );
}

export function Seg<T extends string>({ value, options, onChange }: { value: T; options: [T, string][]; onChange: (v: T) => void }) {
  return (
    <div className="seg">
      {options.map(([v, l]) => <button key={v} className={v === value ? "on" : ""} onClick={() => onChange(v)}>{l}</button>)}
    </div>
  );
}

export function PageHead({ title, sub, right, crumbs }: { title: React.ReactNode; sub?: React.ReactNode; right?: React.ReactNode; crumbs?: [string, string][] }) {
  return (
    <div className="page-head">
      <div>
        {crumbs && (
          <div className="crumbs">{crumbs.map(([l, h], i) => <span key={h}>{i > 0 && " / "}<a href={h}>{l}</a></span>)}</div>
        )}
        <h2>{title}</h2>
        {sub && <p className="lead">{sub}</p>}
      </div>
      {right}
    </div>
  );
}

export function Kpi({ label, value, sub, tone, href }: { label: string; value: React.ReactNode; sub?: React.ReactNode; tone?: "bad" | "warn" | "good"; href?: string }) {
  const body = (
    <>
      <div className="kpi-label">{label}</div>
      <div className={`kpi-value ${tone ?? ""}`}>{value}</div>
      {sub && <div className="kpi-sub">{sub}</div>}
    </>
  );
  return href ? <a className="kpi" href={href}>{body}</a> : <div className="kpi">{body}</div>;
}

export function Loading({ what = "Loading" }: { what?: string }) {
  return <div className="loading"><span className="spinner dark" />{what}…</div>;
}

export function ErrorBox({ error }: { error: string | null }) {
  return error ? <div className="alert error"><span className="tag">Error</span>{error}</div> : null;
}

export function Badge({ children, tone = "muted" }: { children: React.ReactNode; tone?: "bad" | "warn" | "good" | "muted" | "accent" | "ink" }) {
  return <span className={`badge ${tone}`}>{children}</span>;
}

export function Severity({ v }: { v: unknown }) {
  if (v === null || v === undefined) return <span className="muted small" title="assessed when the event closes">pending</span>;
  const n = Number(v);
  return <span className={`sev s${Math.max(1, Math.min(5, n))}`} title={`severity ${n}`}>{"■".repeat(n)}<i>{"■".repeat(Math.max(0, 5 - n))}</i></span>;
}

export function KV({ items }: { items: [string, React.ReactNode][] }) {
  return (
    <dl className="kvlist">
      {items.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{typeof v === "string" ? <Linkify text={v} /> : v ?? "-"}</dd></div>)}
    </dl>
  );
}

// ---------------------------------------------------------------- line chart
export type Series = { name: string; color: string; points: { x: string; y: number | null }[]; dashed?: boolean };

export function LineChart({ series, height = 180, yFmt = (v: number) => v.toLocaleString(), marker }: {
  series: Series[]; height?: number; yFmt?: (v: number) => string; marker?: string;
}) {
  const xs = Array.from(new Set(series.flatMap((s) => s.points.map((p) => p.x)))).sort();
  const ys = series.flatMap((s) => s.points.map((p) => p.y)).filter((y): y is number => y !== null && !Number.isNaN(y));
  if (xs.length < 2 || ys.length === 0) return <div className="empty">Not enough history for a trend.</div>;
  const W = 560, H = height, L = 44, R = 10, T = 10, B = 22;
  let lo = Math.min(...ys), hi = Math.max(...ys);
  if (lo === hi) { lo -= 1; hi += 1; }
  const pad = (hi - lo) * 0.08;
  lo = lo >= 0 && lo - pad < 0 ? 0 : lo - pad;
  hi += pad;
  const x = (v: string) => L + (xs.indexOf(v) / (xs.length - 1)) * (W - L - R);
  const y = (v: number) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
  const ticks = [0, 0.5, 1].map((f) => lo + f * (hi - lo));
  const every = Math.max(1, Math.ceil(xs.length / 8));
  const mx = marker ? xs.findIndex((v) => v >= marker) : -1;
  return (
    <div className="linechart">
      <svg viewBox={`0 0 ${W} ${H}`} role="img">
        {ticks.map((t) => (
          <g key={t}>
            <line x1={L} x2={W - R} y1={y(t)} y2={y(t)} className="grid" />
            <text x={L - 6} y={y(t) + 4} textAnchor="end" className="axis">{yFmt(t)}</text>
          </g>
        ))}
        {xs.map((v, i) => (i % every === 0 ? <text key={v} x={x(v)} y={H - 6} textAnchor="middle" className="axis">{v.slice(0, 7)}</text> : null))}
        {mx >= 0 && <line x1={x(xs[mx])} x2={x(xs[mx])} y1={T} y2={H - B} className="marker" />}
        {series.map((s) => {
          const d = s.points.filter((p) => p.y !== null).map((p, i) => `${i ? "L" : "M"}${x(p.x).toFixed(1)},${y(p.y as number).toFixed(1)}`).join("");
          return <path key={s.name} d={d} fill="none" stroke={s.color} strokeWidth={1.8} strokeDasharray={s.dashed ? "4 3" : undefined} vectorEffect="non-scaling-stroke" />;
        })}
      </svg>
      {series.length > 1 && (
        <div className="legend">{series.map((s) => <span key={s.name}><i style={{ background: s.color }} />{s.name}</span>)}</div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- agent status
export function JobStatus({ job, label }: { job: Job | null; label: string }) {
  if (!job) return null;
  if (job.status === "done")
    return <span className="muted small">{job.cached ? `from memory cache · computed ${new Date(job.finished! * 1000).toLocaleString()}` : `finished in ${job.elapsed_s}s`}</span>;
  if (job.status === "error") return null;
  return <span className="running"><span className="spinner dark" />{label} · {job.elapsed_s}s</span>;
}

/** "What does memory say about this?" — an entity brief from Hindsight plus database ground truth. */
export function MemoryPanel({ entityId, autoload = false }: { entityId: string; autoload?: boolean }) {
  const { asOf } = useAsOf();
  const j = useJob<CapabilityAnswer>();
  useEffect(() => {
    j.reset();
    if (autoload) j.start(() => scm.brief(entityId, asOf));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entityId, asOf]);
  const ans = j.job?.status === "done" ? j.job.result : null;
  return (
    <section className="panel memory">
      <div className="panel-head">
        <h3><span className="mem-dot" />Institutional memory</h3>
        <div className="row">
          <JobStatus job={j.job} label="recalling and reflecting" />
          {!j.running && (
            <button className="btn ghost sm" onClick={() => j.start(() => scm.brief(entityId, asOf, !!ans))}>
              {ans ? "Refresh" : "Ask memory"}
            </button>
          )}
        </div>
      </div>
      {j.error && <div className="alert error"><span className="tag">Memory</span>{j.error}</div>}
      {!j.job && !j.error && (
        <div className="muted">What happened with {entityId} before {asOf}: past disruptions, how decisions turned out, what was promised, what to watch for. Recalled from Hindsight, dated no later than the business date.</div>
      )}
      {j.running && <div className="skeleton"><div /><div /><div /></div>}
      {ans && (
        <>
          <Markdown text={ans.answer} />
          <div className="cites">
            {ans.cited_record_ids.slice(0, 16).map((id) => <EntityLink key={id} id={id} />)}
            {ans.cited_doc_ids.length > 0 && <span className="muted small">{ans.cited_doc_ids.length} memory documents cited</span>}
            <Badge tone="accent">confidence {ans.confidence}</Badge>
            {ans.warnings.map((w) => <Badge key={w} tone="warn">{w}</Badge>)}
          </div>
        </>
      )}
    </section>
  );
}
