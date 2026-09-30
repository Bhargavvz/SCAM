import { Database, Send, Sparkles } from "lucide-react";
import { useState } from "react";
import { api, Row } from "../lib/api";
import { Drawer, Markdown, Spinner } from "./ui";

const EXAMPLES = [
  "Which 5 suppliers have the most overdue purchase orders?",
  "What were our top 10 products by revenue last month?",
  "Which warehouse shipped the most units in December 2025?",
  "How many returns did we get per reason in the last 90 days?",
  "Which carrier has the worst on-time rate this quarter?",
  "Show products below minimum stock at W003 with their on-order quantity.",
  "Why does SUP0247 keep slipping, and what did we do about it last time?",
  "What lessons have we learned about expediting by air?",
];

type Turn = { q: string; a?: Row; error?: string };

export function Copilot({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [q, setQ] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);
  const ask = async (text = q) => {
    if (!text.trim() || busy) return;
    setQ("");
    setBusy(true);
    setTurns((t) => [...t, { q: text }]);
    try {
      const a = await api.post<Row>("/insights/ask", { question: text });
      setTurns((t) => t.map((x, i) => (i === t.length - 1 ? { ...x, a } : x)));
    } catch (e) {
      setTurns((t) => t.map((x, i) => (i === t.length - 1 ? { ...x, error: (e as Error).message } : x)));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Drawer open={open} onClose={onClose} wide title={<span className="row"><Sparkles size={16} className="ai-t" />Ask Meridian</span>}
      footer={
        <div className="row" style={{ width: "100%", flexWrap: "nowrap" }}>
          <input className="input grow" placeholder="Ask anything about your supply chain…" value={q} onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && ask()} autoFocus />
          <button className="btn ai" disabled={busy || !q.trim()} onClick={() => ask()}>{busy ? <Spinner /> : <Send size={14} />}Ask</button>
        </div>
      }>
      {turns.length === 0 && (
        <div className="stack">
          <div className="note">
            Meridian answers by writing read-only SQL against the live operational database and by searching the organisation's long-term memory in Hindsight (notes, emails, past decisions and their outcomes), then explains the result.
            Every query it ran is shown under the answer, and figures that do not appear in the query results are flagged.
          </div>
          <div className="chips">{EXAMPLES.map((e) => <button key={e} className="chip" onClick={() => ask(e)}>{e}</button>)}</div>
        </div>
      )}
      {turns.map((t, i) => (
        <div key={i} className="stack" style={{ gap: 8 }}>
          <div style={{ alignSelf: "flex-end", background: "var(--accent-soft)", padding: "8px 12px", borderRadius: 10, maxWidth: "85%" }}>{t.q}</div>
          {!t.a && !t.error && <div className="row muted"><Spinner />Querying the database…</div>}
          {t.error && <div className="error-box">{t.error}</div>}
          {t.a && (
            <div className="ai-box">
              <Markdown text={t.a.answer} />
              {t.a.ungrounded_numbers?.length > 0 && (
                <div className="small warn-t" style={{ marginTop: 6 }}>Not found in query results: {t.a.ungrounded_numbers.join(", ")}</div>
              )}
              <details style={{ marginTop: 8 }}>
                <summary className="small muted" style={{ cursor: "pointer" }}><Database size={12} /> {t.a.queries.length} queries · {t.a.latency_s}s · {t.a.model}</summary>
                {t.a.queries.map((qq: Row, j: number) => (
                  <pre key={j} className="mono" style={{ whiteSpace: "pre-wrap", background: "var(--surface)", padding: 8, borderRadius: 6, border: "1px solid var(--border)" }}>
                    {qq.tool === "memory" ? "🧠 " : ""}{qq.sql}{"\n"}→ {qq.error ? `error: ${qq.error}` : `${qq.rows} ${qq.tool === "memory" ? "memories" : "rows"}`}
                  </pre>
                ))}
              </details>
            </div>
          )}
        </div>
      ))}
    </Drawer>
  );
}
