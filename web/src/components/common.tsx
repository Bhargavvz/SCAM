import { marked } from "marked";
import { useState } from "react";
import { api, MemoryDoc, Row } from "../api";

export function fmt(v: unknown): string {
  if (v === null || v === undefined || v === "") return "-";
  if (Array.isArray(v)) return v.length ? v.map(fmt).join(", ") : "-";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "number") return Number.isInteger(v) ? v.toLocaleString() : v.toFixed(2);
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

export function money(v?: number | null) {
  return v === null || v === undefined ? "-" : `$${Math.round(v).toLocaleString()}`;
}

export function DataTable({ rows, columns, rowClass }: { rows: Row[]; columns?: string[]; rowClass?: (r: Row) => string }) {
  if (!rows || rows.length === 0) return <div className="muted">none</div>;
  const cols = columns ?? Object.keys(rows[0]);
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>{cols.map((c) => <th key={c}>{c.replace(/_/g, " ")}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className={rowClass?.(r)}>
              {cols.map((c) => <td key={c}>{fmt(r[c])}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Panel({ title, children, right }: { title?: string; children: React.ReactNode; right?: React.ReactNode }) {
  return (
    <section className="panel">
      {(title || right) && (
        <div className="row" style={{ justifyContent: "space-between", marginBottom: 8 }}>
          {title && <h3 style={{ margin: 0 }}>{title}</h3>}
          {right}
        </div>
      )}
      {children}
    </section>
  );
}

export function Metric({ k, v, highlight }: { k: string; v: React.ReactNode; highlight?: boolean }) {
  return (
    <div className={`metric${highlight ? " highlight" : ""}`}>
      <div className="k">{k}</div>
      <div className="v">{v}</div>
    </div>
  );
}

export function Alerts({ errors, warnings, infos }: { errors?: string[]; warnings?: string[]; infos?: string[] }) {
  return (
    <>
      {(errors ?? []).map((e, i) => <div key={`e${i}`} className="alert error">🛡️ {e}</div>)}
      {(warnings ?? []).map((w, i) => <div key={`w${i}`} className="alert warn">⚠️ {w}</div>)}
      {(infos ?? []).map((m, i) => <div key={`i${i}`} className="alert info">{m}</div>)}
    </>
  );
}

export function Markdown({ text }: { text?: string | null }) {
  if (!text) return <div className="muted">-</div>;
  return <div className="markdown" dangerouslySetInnerHTML={{ __html: marked.parse(text, { async: false }) as string }} />;
}

export function Citations({ docs, records, unverifiable, asOf }: { docs: string[]; records: string[]; unverifiable?: string[]; asOf: string }) {
  const [open, setOpen] = useState<MemoryDoc | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const show = async (id: string) => {
    setErr(null);
    try {
      setOpen(await api.doc(id, asOf));
    } catch (e) {
      setErr(String((e as Error).message));
    }
  };
  return (
    <div>
      <div className="row" style={{ alignItems: "flex-start" }}>
        <div style={{ flex: 1 }}>
          <div className="muted">Cited memory documents (click to read)</div>
          {docs.length ? docs.map((d) => <span key={d} className="pill accent link" onClick={() => show(d)}>{d}</span>) : <span className="muted">-</span>}
        </div>
        <div style={{ flex: 1 }}>
          <div className="muted">Cited database records</div>
          {records.length ? records.map((r) => <span key={r} className="pill good">{r}</span>) : <span className="muted">-</span>}
        </div>
      </div>
      {unverifiable && unverifiable.length > 0 && (
        <div className="muted" style={{ marginTop: 6 }}>Unverifiable citations dropped: {unverifiable.join(", ")}</div>
      )}
      {err && <div className="alert warn" style={{ marginTop: 8 }}>{err}</div>}
      {open && (
        <div style={{ marginTop: 10 }}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <b>{open.doc_id} · {open.date} · {open.doc_type}</b>
            <button className="btn ghost" onClick={() => setOpen(null)}>Close</button>
          </div>
          <div className="muted">{open.context}</div>
          {open.superseded_by.length > 0 && (
            <div className="alert warn" style={{ marginTop: 6 }}>
              Superseded by {open.superseded_by.map((s) => `${s.doc_id} (${s.date})`).join(", ")}
            </div>
          )}
          <pre className="doc">{open.content}</pre>
        </div>
      )}
    </div>
  );
}

export function RunButton({ busy, onClick, label }: { busy: boolean; onClick: () => void; label: string }) {
  return (
    <button className="btn" disabled={busy} onClick={onClick}>
      {busy && <span className="spinner" />}
      {busy ? "Working..." : label}
    </button>
  );
}

export function useRunner<T>() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [data, setData] = useState<T | null>(null);
  const run = async (fn: () => Promise<T>) => {
    setBusy(true);
    setError(null);
    try {
      setData(await fn());
    } catch (e) {
      setError(String((e as Error).message));
    } finally {
      setBusy(false);
    }
  };
  return { busy, error, data, setData, run };
}
