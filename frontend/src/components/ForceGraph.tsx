import { useMemo, useState } from "react";

type Node = { id: string; label: string; weight: number; color?: string };
type Edge = { source: string; target: string; weight: number };

/** Small dependency-free force layout (runs once per data set) rendered as SVG. */
export function ForceGraph({ nodes, edges, height = 460, onPick }: { nodes: Node[]; edges: Edge[]; height?: number; onPick?: (n: Node) => void }) {
  const [hover, setHover] = useState<string | null>(null);
  const W = 900, H = height;
  const layout = useMemo(() => {
    const idx = new Map(nodes.map((n, i) => [n.id, i]));
    const P = nodes.map((_, i) => ({ x: W / 2 + Math.cos(i * 2.39996) * (40 + i * 3), y: H / 2 + Math.sin(i * 2.39996) * (40 + i * 3), vx: 0, vy: 0 }));
    const E = edges.filter((e) => idx.has(e.source) && idx.has(e.target)).map((e) => ({ a: idx.get(e.source)!, b: idx.get(e.target)!, w: e.weight }));
    const maxW = Math.max(1, ...E.map((e) => e.w));
    for (let it = 0; it < 260; it++) {
      const alpha = 1 - it / 260;
      for (let i = 0; i < P.length; i++) {
        for (let j = i + 1; j < P.length; j++) {
          const dx = P[j].x - P[i].x, dy = P[j].y - P[i].y;
          const d2 = Math.max(dx * dx + dy * dy, 25);
          const f = (1600 / d2) * alpha;
          P[i].vx -= dx * f / 10; P[i].vy -= dy * f / 10; P[j].vx += dx * f / 10; P[j].vy += dy * f / 10;
        }
      }
      for (const e of E) {
        const a = P[e.a], b = P[e.b];
        const dx = b.x - a.x, dy = b.y - a.y, d = Math.sqrt(dx * dx + dy * dy) || 1;
        const k = ((d - 70) / d) * 0.04 * (0.4 + e.w / maxW) * alpha;
        a.vx += dx * k; a.vy += dy * k; b.vx -= dx * k; b.vy -= dy * k;
      }
      for (const p of P) {
        p.vx += (W / 2 - p.x) * 0.004 * alpha; p.vy += (H / 2 - p.y) * 0.004 * alpha;
        p.x += p.vx; p.y += p.vy; p.vx *= 0.6; p.vy *= 0.6;
        p.x = Math.max(20, Math.min(W - 20, p.x)); p.y = Math.max(16, Math.min(H - 16, p.y));
      }
    }
    return { P, E, maxW };
  }, [nodes, edges, H]);
  const maxN = Math.max(1, ...nodes.map((n) => n.weight));
  const near = new Set<number>();
  if (hover) {
    const hi = nodes.findIndex((n) => n.id === hover);
    near.add(hi);
    layout.E.forEach((e) => { if (e.a === hi) near.add(e.b); if (e.b === hi) near.add(e.a); });
  }
  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height: "auto", background: "var(--surface-2)", borderRadius: 8 }}>
      {layout.E.map((e, i) => {
        const a = layout.P[e.a], b = layout.P[e.b];
        const on = hover && (near.has(e.a) && near.has(e.b));
        return <line key={i} x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke={on ? "#7c3aed" : "var(--border-strong)"} strokeOpacity={hover && !on ? 0.15 : 0.7} strokeWidth={0.5 + 2.5 * (e.w / layout.maxW)} />;
      })}
      {nodes.map((n, i) => {
        const p = layout.P[i];
        const r = 4 + 14 * Math.sqrt(n.weight / maxN);
        const dim = hover && !near.has(i);
        return (
          <g key={n.id} transform={`translate(${p.x},${p.y})`} style={{ cursor: "pointer" }} opacity={dim ? 0.25 : 1}
            onMouseEnter={() => setHover(n.id)} onMouseLeave={() => setHover(null)} onClick={() => onPick?.(n)}>
            <circle r={r} fill={/^(SUP|RPO|PR|RM|EVT|DEC|CMT|W0|P0|IP)/.test(n.label) ? "#0e7490" : "#7c3aed"} fillOpacity={0.85} stroke="#fff" strokeWidth={1.2} />
            {(r > 8 || hover === n.id || near.has(i)) && <text y={-r - 3} textAnchor="middle" fontSize={10.5} fill="var(--text)" fontWeight={600}>{n.label.slice(0, 26)}</text>}
          </g>
        );
      })}
    </svg>
  );
}
