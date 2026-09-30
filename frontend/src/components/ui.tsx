import { marked } from "marked";
import { ChevronDown, ChevronLeft, ChevronRight, ChevronUp, Loader2, X } from "lucide-react";
import { createContext, ReactNode, useCallback, useContext, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { fmt, ID_PATTERN, Page, routeFor, Row } from "../lib/api";

// ---------------------------------------------------------------- links
export function EntityLink({ id, children }: { id: string; children?: ReactNode }) {
  const to = routeFor(id);
  return to ? <Link className="idlink" to={to} onClick={(e) => e.stopPropagation()}>{children ?? id}</Link> : <span className="mono">{children ?? id}</span>;
}

export function Linkify({ text }: { text: string }) {
  const parts = String(text ?? "").split(new RegExp(ID_PATTERN.source, "g"));
  return <>{parts.map((p, i) => (i % 2 ? <EntityLink key={i} id={p} /> : p))}</>;
}

// ---------------------------------------------------------------- layout
export function PageHead({ title, sub, actions, crumbs, icon }: {
  title: ReactNode; sub?: ReactNode; actions?: ReactNode; crumbs?: [string, string][]; icon?: ReactNode;
}) {
  return (
    <div className="page-head">
      <div>
        {crumbs && (
          <div className="crumbs">
            {crumbs.map(([l, to], i) => (
              <span key={to} className="row" style={{ gap: 6 }}>{i > 0 && <ChevronRight size={12} />}<Link to={to}>{l}</Link></span>
            ))}
          </div>
        )}
        <h1>{icon}{title}</h1>
        {sub && <p>{sub}</p>}
      </div>
      {actions && <div className="row">{actions}</div>}
    </div>
  );
}

export function Card({ title, sub, actions, children, flush, className, icon }: {
  title?: ReactNode; sub?: ReactNode; actions?: ReactNode; children: ReactNode; flush?: boolean; className?: string; icon?: ReactNode;
}) {
  return (
    <section className={`card ${className ?? ""}`}>
      {(title || actions) && (
        <div className="card-head">
          <h3>{icon}{title}{sub && <span className="sub">{sub}</span>}</h3>
          {actions && <div className="row">{actions}</div>}
        </div>
      )}
      <div className={`card-body ${flush ? "flush" : ""}`}>{children}</div>
    </section>
  );
}

export function Kpi({ label, value, sub, icon, tone, to }: {
  label: string; value: ReactNode; sub?: ReactNode; icon?: ReactNode; tone?: "good" | "bad" | "warn" | "info"; to?: string;
}) {
  const body = (
    <>
      <div className="kpi-top"><span>{label}</span>{icon && <span className="kpi-icon">{icon}</span>}</div>
      <div className="kpi-value">{value}</div>
      {sub && <div className="kpi-sub">{sub}</div>}
    </>
  );
  return to ? <Link className={`kpi ${tone ?? ""}`} to={to}>{body}</Link> : <div className={`kpi ${tone ?? ""}`}>{body}</div>;
}

export function Delta({ now, before, invert = false }: { now: number | null | undefined; before: number | null | undefined; invert?: boolean }) {
  if (!now || !before) return null;
  const d = now / before - 1;
  const up = d >= 0;
  const good = invert ? !up : up;
  return <span className={`delta ${good ? "up" : "down"}`}>{up ? "▲" : "▼"} {Math.abs(d * 100).toFixed(1)}%</span>;
}

// ---------------------------------------------------------------- status
const TONES: Record<string, string> = {
  delivered: "good", received: "good", completed: "good", closed: "good", ok: "good", active: "good", done: "good", fulfilled: "good", preferred: "good", on_time: "good", success: "good",
  open: "info", confirmed: "info", allocated: "info", in_transit: "info", shipped: "info", planned: "info", booked: "info", authorized: "info", acknowledged: "violet", approved: "info",
  picking: "violet", packed: "violet", in_progress: "violet", partial: "warn", low: "warn", warning: "warn", excess: "violet", draft: "plain", medium: "warn", putaway: "violet",
  overdue: "bad", late: "bad", cancelled: "plain", critical: "bad", out: "bad", negative: "bad", exception: "bad", at_risk: "bad", breached: "bad", high: "bad", failed: "bad",
  info: "info", resolved: "good", expired: "plain", future: "info", unrated: "plain",
};

export function Status({ value, tone }: { value: unknown; tone?: string }) {
  if (value === null || value === undefined || value === "") return <span className="muted">–</span>;
  const v = String(value);
  return <span className={`pill ${tone ?? TONES[v] ?? ""}`}>{fmt.label(v)}</span>;
}

export function Bar({ value, max = 1, tone }: { value: number; max?: number; tone?: "good" | "warn" | "bad" }) {
  const pct = Math.max(0, Math.min(100, (value / (max || 1)) * 100));
  return <div className={`bar ${tone ?? ""}`}><div style={{ width: `${pct}%` }} /></div>;
}

export function KV({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="kv">
      {items.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v === null || v === undefined || v === "" ? "–" : v}</dd></div>)}
    </dl>
  );
}

export function Steps({ steps, current }: { steps: string[]; current: string }) {
  const idx = steps.indexOf(current);
  return (
    <div className="steps">
      {steps.map((s, i) => <div key={s} className={`step ${i < idx || (i === idx && i === steps.length - 1) ? "done" : i === idx ? "current done" : ""}`}>{fmt.label(s)}</div>)}
    </div>
  );
}

// ---------------------------------------------------------------- controls
export function Seg<T extends string>({ value, options, onChange }: { value: T; options: [T, string][]; onChange: (v: T) => void }) {
  return <div className="seg">{options.map(([v, l]) => <button key={v} className={v === value ? "on" : ""} onClick={() => onChange(v)}>{l}</button>)}</div>;
}

export function Tabs<T extends string>({ value, options, onChange }: { value: T; options: [T, ReactNode][]; onChange: (v: T) => void }) {
  return <div className="tabs">{options.map(([v, l]) => <button key={v} className={v === value ? "on" : ""} onClick={() => onChange(v)}>{l}</button>)}</div>;
}

export function Spinner({ size = 14 }: { size?: number }) {
  return <Loader2 size={size} className="spin" />;
}

export function Loading() {
  return <div className="skeleton"><div /><div /><div /></div>;
}

export function ErrorBox({ error }: { error: string | null | undefined }) {
  return error ? <div className="error-box">{error}</div> : null;
}

export function Markdown({ text }: { text: string }) {
  return <div className="prose" dangerouslySetInnerHTML={{ __html: marked.parse(text ?? "", { async: false }) as string }} />;
}

// ---------------------------------------------------------------- table
export type Col<R = Row> = {
  key: string; label?: ReactNode; render?: (r: R) => ReactNode; num?: boolean; sort?: boolean; wrap?: boolean; width?: number;
};

export function DataTable<R extends Row = Row>({ rows, cols, onRow, empty = "No records.", sort, onSort, rowKey }: {
  rows: R[]; cols: Col<R>[]; onRow?: (r: R) => void; empty?: string; sort?: string; onSort?: (s: string) => void; rowKey?: (r: R) => string;
}) {
  const [skey, sdir] = (sort ?? "").split(":");
  if (!rows?.length) return <div className="empty">{empty}</div>;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            {cols.map((c) => (
              <th key={c.key} className={`${c.num ? "num" : ""} ${c.sort && onSort ? "sortable" : ""}`} style={c.width ? { width: c.width } : undefined}
                onClick={c.sort && onSort ? () => onSort(`${c.key}:${skey === c.key && sdir !== "desc" ? "desc" : "asc"}`) : undefined}>
                <span className="row" style={{ gap: 3, display: "inline-flex", justifyContent: c.num ? "flex-end" : undefined }}>
                  {c.label ?? fmt.label(c.key)}
                  {skey === c.key && (sdir === "desc" ? <ChevronDown size={12} /> : <ChevronUp size={12} />)}
                </span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={rowKey ? rowKey(r) : i} className={onRow ? "clickable" : undefined} onClick={onRow ? () => onRow(r) : undefined}>
              {cols.map((c) => {
                const v = r[c.key];
                const content = c.render ? c.render(r) : typeof v === "string" && routeFor(v) ? <EntityLink id={v} /> :
                  typeof v === "number" ? fmt.n(v, 2) : v === null || v === undefined || v === "" ? <span className="muted">–</span> : String(v);
                return <td key={c.key} className={`${c.num ? "num" : ""} ${c.wrap ? "wrap" : ""}`}>{content}</td>;
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Pager({ page, onOffset }: { page: Page | Row | null | undefined; onOffset: (o: number) => void }) {
  if (!page) return null;
  const { total, limit, offset } = page;
  return (
    <div className="pager">
      <span>{total === 0 ? "0 records" : `${(offset + 1).toLocaleString()}–${Math.min(total, offset + limit).toLocaleString()} of ${total.toLocaleString()}`}</span>
      <div className="row">
        <button className="btn sm" disabled={offset === 0} onClick={() => onOffset(Math.max(0, offset - limit))}><ChevronLeft size={14} />Prev</button>
        <button className="btn sm" disabled={offset + limit >= total} onClick={() => onOffset(offset + limit)}>Next<ChevronRight size={14} /></button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- drawer
export function Drawer({ open, title, onClose, children, footer, wide }: {
  open: boolean; title: ReactNode; onClose: () => void; children: ReactNode; footer?: ReactNode; wide?: boolean;
}) {
  useEffect(() => {
    if (!open) return;
    const k = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <>
      <div className="drawer-bg" onClick={onClose} />
      <aside className={`drawer ${wide ? "wide" : ""}`} role="dialog">
        <div className="drawer-head"><h3>{title}</h3><button className="iconbtn" onClick={onClose} aria-label="Close"><X size={16} /></button></div>
        <div className="drawer-body">{children}</div>
        {footer && <div className="drawer-foot">{footer}</div>}
      </aside>
    </>
  );
}

// ---------------------------------------------------------------- toasts
type Toast = { id: number; text: string; kind: "ok" | "error" };
const ToastCtx = createContext<(text: string, kind?: "ok" | "error") => void>(() => undefined);
export const useToast = () => useContext(ToastCtx);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = useCallback((text: string, kind: "ok" | "error" = "ok") => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, text, kind }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), kind === "error" ? 7000 : 4000);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toasts">{toasts.map((t) => <div key={t.id} className={`toast ${t.kind === "error" ? "error" : ""}`}>{t.text}</div>)}</div>
    </ToastCtx.Provider>
  );
}

/** Run a mutation, toast the result, and return it. */
export function useAction() {
  const toast = useToast();
  const [busy, setBusy] = useState<string | null>(null);
  const run = async <T,>(key: string, fn: () => Promise<T>, ok?: string | ((r: T) => string)): Promise<T | undefined> => {
    setBusy(key);
    try {
      const r = await fn();
      if (ok) toast(typeof ok === "function" ? ok(r) : ok);
      window.dispatchEvent(new Event("meridian:changed"));
      return r;
    } catch (e) {
      toast((e as Error).message, "error");
      return undefined;
    } finally {
      setBusy(null);
    }
  };
  return { run, busy };
}

export function useGo() {
  const nav = useNavigate();
  return (id: string) => {
    const to = routeFor(id);
    if (to) nav(to);
  };
}
