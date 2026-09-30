import {
  Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, ComposedChart, Legend, Line, LineChart, Pie, PieChart,
  ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { fmt, Row } from "../lib/api";

export const PALETTE = ["#0e7490", "#2563eb", "#7c3aed", "#059669", "#f59e0b", "#dc2626", "#64748b", "#db2777", "#0891b2", "#65a30d"];
const grid = <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />;
const tip = { contentStyle: { borderRadius: 8, border: "1px solid var(--border)", background: "var(--surface)", fontSize: 12 } };

type Series = { key: string; name?: string; color?: string; dashed?: boolean };

export function TrendArea({ data, x, series, height = 240, yFmt = fmt.compact }: {
  data: Row[]; x: string; series: Series[]; height?: number; yFmt?: (v: number) => string;
}) {
  return (
    <ResponsiveContainer width="100%" height={height}>
      <AreaChart data={data} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
        <defs>
          {series.map((s, i) => (
            <linearGradient key={s.key} id={`g-${s.key}`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={s.color ?? PALETTE[i]} stopOpacity={0.28} />
              <stop offset="100%" stopColor={s.color ?? PALETTE[i]} stopOpacity={0} />
            </linearGradient>
          ))}
        </defs>
        {grid}
        <XAxis dataKey={x} tickLine={false} axisLine={false} minTickGap={24} tickFormatter={(v) => String(v).slice(0, 7)} />
        <YAxis tickLine={false} axisLine={false} width={52} tickFormatter={yFmt} />
        <Tooltip {...tip} formatter={(v: number) => yFmt(v)} />
        {series.length > 1 && <Legend iconType="circle" wrapperStyle={{ fontSize: 12 }} />}
        {series.map((s, i) => (
          <Area key={s.key} type="monotone" dataKey={s.key} name={s.name ?? s.key} stroke={s.color ?? PALETTE[i]} strokeWidth={2}
            fill={`url(#g-${s.key})`} strokeDasharray={s.dashed ? "5 4" : undefined} />
        ))}
      </AreaChart>
    </ResponsiveContainer>
  );
}

export function Lines({ data, x, series, height = 240, yFmt = fmt.compact, yDomain }: {
  data: Row[]; x: string; series: Series[]; height?: number; yFmt?: (v: number) => string; yDomain?: [number, number];
}) {
  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart data={data} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
        {grid}
        <XAxis dataKey={x} tickLine={false} axisLine={false} minTickGap={24} tickFormatter={(v) => String(v).slice(0, 7)} />
        <YAxis tickLine={false} axisLine={false} width={52} tickFormatter={yFmt} domain={yDomain ?? ["auto", "auto"]} />
        <Tooltip {...tip} formatter={(v: number) => yFmt(v)} />
        {series.length > 1 && <Legend iconType="circle" wrapperStyle={{ fontSize: 12 }} />}
        {series.map((s, i) => (
          <Line key={s.key} type="monotone" dataKey={s.key} name={s.name ?? s.key} stroke={s.color ?? PALETTE[i]} strokeWidth={2}
            dot={false} strokeDasharray={s.dashed ? "5 4" : undefined} connectNulls />
        ))}
      </LineChart>
    </ResponsiveContainer>
  );
}

export function Bars({ data, x, series, height = 240, yFmt = fmt.compact, stacked, horizontal, colorBy }: {
  data: Row[]; x: string; series: Series[]; height?: number; yFmt?: (v: number) => string; stacked?: boolean; horizontal?: boolean;
  colorBy?: (r: Row) => string;
}) {
  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart data={data} layout={horizontal ? "vertical" : "horizontal"} margin={{ top: 8, right: 12, left: horizontal ? 8 : 0, bottom: 0 }}>
        {grid}
        {horizontal ? (
          <>
            <XAxis type="number" tickLine={false} axisLine={false} tickFormatter={yFmt} />
            <YAxis type="category" dataKey={x} tickLine={false} axisLine={false} width={110} tickFormatter={(v) => fmt.label(v)} />
          </>
        ) : (
          <>
            <XAxis dataKey={x} tickLine={false} axisLine={false} minTickGap={8} tickFormatter={(v) => fmt.label(String(v)).slice(0, 12)} />
            <YAxis tickLine={false} axisLine={false} width={52} tickFormatter={yFmt} />
          </>
        )}
        <Tooltip {...tip} formatter={(v: number) => yFmt(v)} cursor={{ fill: "var(--surface-2)" }} />
        {series.length > 1 && <Legend iconType="circle" wrapperStyle={{ fontSize: 12 }} />}
        {series.map((s, i) => (
          <Bar key={s.key} dataKey={s.key} name={s.name ?? s.key} fill={s.color ?? PALETTE[i]} radius={stacked ? 0 : [4, 4, 0, 0]}
            stackId={stacked ? "a" : undefined} maxBarSize={42}>
            {colorBy && data.map((r, j) => <Cell key={j} fill={colorBy(r)} />)}
          </Bar>
        ))}
      </BarChart>
    </ResponsiveContainer>
  );
}

export function Donut({ data, nameKey, valueKey, height = 220, fmtValue = fmt.compact }: {
  data: Row[]; nameKey: string; valueKey: string; height?: number; fmtValue?: (v: number) => string;
}) {
  const total = data.reduce((s, r) => s + Number(r[valueKey] ?? 0), 0);
  return (
    <div className="row" style={{ alignItems: "center", flexWrap: "nowrap" }}>
      <div style={{ width: height, height }}>
        <ResponsiveContainer>
          <PieChart>
            <Pie data={data} dataKey={valueKey} nameKey={nameKey} innerRadius="62%" outerRadius="95%" paddingAngle={1.5} stroke="none">
              {data.map((_, i) => <Cell key={i} fill={PALETTE[i % PALETTE.length]} />)}
            </Pie>
            <Tooltip {...tip} formatter={(v: number) => fmtValue(v)} />
          </PieChart>
        </ResponsiveContainer>
      </div>
      <div className="stack" style={{ gap: 6, flex: 1 }}>
        {data.map((r, i) => (
          <div key={String(r[nameKey])} className="row between small">
            <span><i style={{ display: "inline-block", width: 9, height: 9, borderRadius: 3, background: PALETTE[i % PALETTE.length], marginRight: 7 }} />{fmt.label(r[nameKey])}</span>
            <span className="muted">{fmtValue(Number(r[valueKey]))} · {total ? ((Number(r[valueKey]) / total) * 100).toFixed(0) : 0}%</span>
          </div>
        ))}
      </div>
    </div>
  );
}

/** History (actual + planner) and forecast with an 80% band. */
export function ForecastChart({ history, forecast, height = 300 }: { history: Row[]; forecast: Row[]; height?: number }) {
  const data = [
    ...history.map((h) => ({ week: h.week, actual: h.actual, planner: h.planner })),
    ...forecast.map((f, i) => ({ week: f.week, forecast: f.forecast, band: [f.low, f.high], ...(i === 0 && history.length ? {} : {}) })),
  ];
  if (history.length) {
    const last = history[history.length - 1];
    const idx = history.length - 1;
    (data[idx] as Row).forecast = last.actual;
    (data[idx] as Row).band = [last.actual, last.actual];
  }
  return (
    <ResponsiveContainer width="100%" height={height}>
      <ComposedChart data={data} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
        {grid}
        <XAxis dataKey="week" tickLine={false} axisLine={false} minTickGap={30} tickFormatter={(v) => String(v).slice(0, 7)} />
        <YAxis tickLine={false} axisLine={false} width={52} tickFormatter={fmt.compact} />
        <Tooltip {...tip} formatter={(v: number | number[]) => (Array.isArray(v) ? `${fmt.n(v[0])} – ${fmt.n(v[1])}` : fmt.n(v, 1))} />
        <Legend iconType="circle" wrapperStyle={{ fontSize: 12 }} />
        <Area dataKey="band" name="80% range" stroke="none" fill="#7c3aed" fillOpacity={0.12} />
        <Line dataKey="actual" name="Actual demand" stroke="#0f172a" strokeWidth={1.8} dot={false} />
        <Line dataKey="planner" name="Planner forecast" stroke="#94a3b8" strokeWidth={1.4} strokeDasharray="4 3" dot={false} />
        <Line dataKey="forecast" name="Model forecast" stroke="#7c3aed" strokeWidth={2.2} dot={false} />
        {history.length > 0 && <ReferenceLine x={history[history.length - 1].week} stroke="var(--border-strong)" strokeDasharray="3 3" label={{ value: "today", fontSize: 10, fill: "var(--muted)", position: "insideTopRight" }} />}
      </ComposedChart>
    </ResponsiveContainer>
  );
}

export function Spark({ data, k, color = "#0e7490", height = 36 }: { data: Row[]; k: string; color?: string; height?: number }) {
  return (
    <ResponsiveContainer width="100%" height={height}>
      <AreaChart data={data} margin={{ top: 2, right: 0, left: 0, bottom: 0 }}>
        <Area type="monotone" dataKey={k} stroke={color} strokeWidth={1.6} fill={color} fillOpacity={0.12} dot={false} isAnimationActive={false} />
      </AreaChart>
    </ResponsiveContainer>
  );
}
