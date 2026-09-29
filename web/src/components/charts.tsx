// Small hand-built SVG/HTML charts in the same paper-and-ink style as the rest of the UI.
import { Fragment } from "react";

export type Tone = "good" | "bad" | "warn" | "ink" | "muted" | "accent";
const COLOR: Record<Tone, string> = {
  good: "#1c6b3f", bad: "#a52a2a", warn: "#c9962c", ink: "#16181d", muted: "#c8c4bb", accent: "#1d4ed8",
};

/** Big number with a thin progress ring. */
export function Ring({ value, total, label, sub, tone = "good" }: { value: number; total: number; label: string; sub?: string; tone?: Tone }) {
  const pct = total > 0 ? Math.max(0, Math.min(1, value / total)) : 0;
  const r = 30, c = 2 * Math.PI * r;
  return (
    <div className="ring">
      <svg viewBox="0 0 76 76" width="76" height="76" aria-hidden>
        <circle cx="38" cy="38" r={r} fill="none" stroke="#ebe8e1" strokeWidth="6" />
        <circle cx="38" cy="38" r={r} fill="none" stroke={COLOR[tone]} strokeWidth="6" strokeDasharray={`${pct * c} ${c}`}
          strokeLinecap="butt" transform="rotate(-90 38 38)" />
        <text x="38" y="43" textAnchor="middle" className="ring-pct">{Math.round(pct * 100)}%</text>
      </svg>
      <div>
        <div className="ring-value">{value}<span>/{total}</span></div>
        <div className="ring-label">{label}</div>
        {sub && <div className="muted">{sub}</div>}
      </div>
    </div>
  );
}

/** Horizontal bars. `log` spreads values that differ by orders of magnitude (e.g. $2 vs $31,826). */
export function HBars({ items, log = false, max, unit = "" }: {
  items: { label: string; value: number | null; display?: string; tone?: Tone; note?: string }[];
  log?: boolean; max?: number; unit?: string;
}) {
  const vals = items.map((i) => i.value ?? 0);
  const scale = (v: number) => (log ? Math.log10(1 + Math.max(0, v)) : Math.max(0, v));
  const top = max !== undefined ? scale(max) : Math.max(...vals.map(scale), 1e-9);
  return (
    <div className="hbars">
      {items.map((it) => (
        <Fragment key={it.label}>
          <div className="hbar-label" title={it.note}>{it.label}</div>
          <div className="hbar-track">
            {it.value === null ? (
              <span className="muted">not simulated</span>
            ) : (
              <div className="hbar-fill" style={{ width: `${Math.max(1.5, (scale(it.value) / top) * 100)}%`, background: COLOR[it.tone ?? "ink"] }} />
            )}
          </div>
          <div className="hbar-value">{it.value === null ? "" : it.display ?? `${it.value}${unit}`}</div>
        </Fragment>
      ))}
    </div>
  );
}

/** Dated events on a horizontal axis with a "today" marker. */
export function Timeline({ points, today }: { points: { date: string; label: string; tone: Tone; detail?: string }[]; today: string }) {
  const dates = [...points.map((p) => +new Date(p.date)), +new Date(today)].filter((d) => !Number.isNaN(d));
  if (points.length === 0 || dates.length === 0) return <div className="muted">no dated precedents</div>;
  const lo = Math.min(...dates), hi = Math.max(...dates);
  const x = (d: string) => (hi === lo ? 50 : 4 + ((+new Date(d) - lo) / (hi - lo)) * 92);
  return (
    <div className="timeline">
      <div className="tl-axis" />
      {points.map((p, i) => (
        <div key={i} className="tl-point" style={{ left: `${x(p.date)}%` }} title={`${p.label} · ${p.date}${p.detail ? " · " + p.detail : ""}`}>
          <span className="tl-dot" style={{ background: COLOR[p.tone] }} />
          <span className={`tl-text ${i % 2 ? "down" : "up"}`}>{p.label}<br /><span className="muted">{p.date.slice(0, 10)}</span></span>
        </div>
      ))}
      <div className="tl-today" style={{ left: `${x(today)}%` }}><span>as of {today}</span></div>
    </div>
  );
}

/** Where the time went in one agent run (the trace). */
export function StepBars({ steps }: { steps: { name: string; kind: string; ms: number }[] }) {
  const total = steps.reduce((a, s) => a + s.ms, 0) || 1;
  const tone: Record<string, Tone> = { step: "ink", tool: "accent", llm: "warn" };
  return (
    <div>
      <div className="stack">
        {steps.map((s, i) => (
          <div key={i} className="stack-seg" style={{ width: `${(s.ms / total) * 100}%`, background: COLOR[tone[s.kind] ?? "muted"] }}
            title={`${s.kind} · ${s.name} · ${(s.ms / 1000).toFixed(1)}s`} />
        ))}
      </div>
      <div className="legend">
        <span><i style={{ background: COLOR.ink }} />pipeline step</span>
        <span><i style={{ background: COLOR.accent }} />tool call</span>
        <span><i style={{ background: COLOR.warn }} />model call</span>
        <span className="muted">total {(total / 1000).toFixed(1)}s · {steps.length} steps</span>
      </div>
    </div>
  );
}

/** One cell per item, coloured by outcome - e.g. 14 holdout scenarios or 12 patterns. */
export function CellGrid({ cells }: { cells: { id: string; tone: Tone; mark?: string; title?: string }[] }) {
  return (
    <div className="cells">
      {cells.map((c) => (
        <div key={c.id} className={`cell ${c.tone}`} title={c.title}>
          <div className="cell-id">{c.id}</div>
          <div className="cell-mark">{c.mark ?? ""}</div>
        </div>
      ))}
    </div>
  );
}
