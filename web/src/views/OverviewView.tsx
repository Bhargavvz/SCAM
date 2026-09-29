import { useEffect, useState } from "react";
import { api, Results } from "../api";
import { CellGrid, HBars, Ring, Tone } from "../components/charts";
import { Panel } from "../components/common";

const PIPELINE = [
  { k: "Report", d: "free-text disruption, dated" },
  { k: "Recall", d: "Hindsight memory: broad, entity, recent window" },
  { k: "Ground truth", d: "as-of SQL: commitments, decisions, events, scorecards" },
  { k: "Simulate", d: "deterministic cost / stockout for every option" },
  { k: "Decide", d: "LLM chooses; guardrails block commitment breaches" },
  { k: "Explain + log", d: "cited rationale; optional write-back to memory" },
];

const GUARANTEES = [
  ["No future leakage", "SQL runs on as-of views; memory hits dated after the question are dropped and counted."],
  ["Numbers are never invented", "Costs and stockout days come only from the simulator; prose is checked for stray figures."],
  ["Commitments first", "An action that breaks an open commitment is rejected before it can be recommended."],
  ["Every claim traceable", "Cited document and record ids are verified; anything unverifiable is shown as such."],
  ["Latest fact wins", "Superseded documents are flagged with the document that replaced them."],
  ["Learns from itself", "Logged decisions become precedents for the next disruption."],
];

const pct = (v: number | null | undefined) => (v === null || v === undefined ? "–" : `${Math.round(v * 100)}%`);

export function OverviewView({ onOpen }: { onOpen: (tab: "decide" | "demo" | "results") => void }) {
  const [data, setData] = useState<Results | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    api.results().then(setData).catch((e) => setErr(String((e as Error).message)));
  }, []);

  const h = data?.scorecard?.holdout ?? null;
  const q = data?.scorecard?.questions ?? data?.scorecard?.questions_hindsight_mode ?? null;
  const p = data?.scorecard?.patterns ?? null;
  const cov = data?.coverage;
  const rows = h?.per_scenario ?? [];
  const traps = rows.filter((r) => r.trap);
  const commitmentsFull = rows.filter((r) => r.commitments_recall === 1).length;

  return (
    <div>
      <section className="hero">
        <div>
          <div className="eyebrow">Supply chain memory</div>
          <h1>Decisions that remember what happened last time.</h1>
          <p>
            When a supplier slips, the agent recalls the history, checks what was promised, simulates every response and
            recommends one it can back with evidence — and it refuses the tempting precedent when conditions have changed.
          </p>
          <div className="row">
            <button className="btn" onClick={() => onOpen("decide")}>Try a disruption</button>
            <button className="btn ghost" onClick={() => onOpen("demo")}>Run the walkthrough</button>
          </div>
        </div>
        {h && (
          <div className="hero-stat">
            <div className="big">{rows.filter((r) => r.correct).length}<span>/{h.n}</span></div>
            <div>held-out disruptions decided correctly</div>
            <div className="muted">scenarios the agent never saw in memory · gold answers from the dataset</div>
          </div>
        )}
      </section>

      {err && <div className="alert error"><span className="tag">Error</span>{err}</div>}

      {h ? (
        <div className="rings">
          <Ring value={rows.filter((r) => r.correct).length} total={h.n} label="Correct decisions" sub="best action vs gold" />
          <Ring value={traps.filter((r) => r.trap_pass).length} total={traps.length} label="Traps avoided" sub="misleading precedent rejected" />
          <Ring value={h.simulator_parity_ok} total={h.n} label="Simulator parity" sub="exact match with the generator" tone="ink" />
          <Ring value={commitmentsFull} total={h.n} label="Commitments surfaced" sub="all open obligations flagged" tone="accent" />
        </div>
      ) : (
        <div className="alert info">No evaluation results yet — run scripts/run_live_evals.sh to fill this page.</div>
      )}

      <div className="grid cols-2">
        <Panel title="Held-out scenarios" right={<span className="muted">green = correct · outlined = trap case</span>}>
          <CellGrid cells={rows.map((r) => ({
            id: r.scenario_id,
            tone: (r.correct ? "good" : "bad") as Tone,
            mark: `${r.correct ? "✓" : "✗"}${r.trap ? " trap" : ""}`,
            title: `chose ${r.chosen} · gold ${r.gold}${r.trap ? ` · trap ${r.trap_pass ? "avoided" : "missed"}` : ""}`,
          })).map((c, i) => ({ ...c, tone: (rows[i].trap ? (rows[i].trap_pass ? "accent" : "bad") : c.tone) as Tone }))} />
          {h && (
            <div className="kv">
              <div><span>precedent recall</span><b>{pct(h.precedent_recall)}</b></div>
              <div><span>avg decision time</span><b>{h.latency.mean_s ?? "–"} s</b></div>
              <div><span>tokens per decision</span><b>{Math.round((h.tokens.mean_input ?? 0) + (h.tokens.mean_output ?? 0)).toLocaleString()}</b></div>
              <div><span>errors</span><b>{h.errors}</b></div>
            </div>
          )}
        </Panel>

        <Panel title="How a decision is made">
          <div className="pipeline">
            {PIPELINE.map((s, i) => (
              <div key={s.k} className="pipe-step">
                <div className="pipe-n">{i + 1}</div>
                <div><b>{s.k}</b><div className="muted">{s.d}</div></div>
              </div>
            ))}
          </div>
        </Panel>
      </div>

      <div className="grid cols-2">
        <Panel title="History questions by type" right={q && <span className="muted">{q.n} questions · as-of enforced</span>}>
          {q ? (
            <>
              <HBars max={1} items={Object.entries(q.by_type).map(([t, v]) => ({
                label: t.replace(/_/g, " "), value: v.accuracy, display: `${pct(v.accuracy)} (${v.n})`,
                tone: (v.accuracy ?? 0) >= 0.6 ? "good" : (v.accuracy ?? 0) >= 0.3 ? "warn" : "bad",
              }))} />
              <div className="kv">
                <div><span>overall</span><b>{pct(q.accuracy)}</b></div>
                <div><span>doc citation recall</span><b>{pct(q.citations.doc_recall)}</b></div>
                <div><span>future-dated citations</span><b>{q.future_doc_citations}</b></div>
              </div>
            </>
          ) : (
            <div className="muted">Not run yet: .venv/bin/python -m eval.run_eval_questions --per-type 5</div>
          )}
        </Panel>

        <Panel title="Planted patterns surfaced" right={p && <span className="muted">{p.detected_without_mental_models}/12 from raw memory · {p.detected_with_mental_models}/12 with mental models</span>}>
          {p ? (
            <CellGrid cells={Object.entries(p.per_pattern).map(([id, v]) => ({
              id,
              tone: (v.on || v.off ? "good" : "muted") as Tone,
              mark: v.on && v.off ? "both" : v.off ? "memory" : v.on ? "model" : "–",
              title: `${id}: raw memory ${v.off ? "found" : "missed"}${v.on !== undefined ? ` · with mental models ${v.on ? "found" : "missed"}` : ""}`,
            }))} />
          ) : (
            <div className="muted">Not run yet: .venv/bin/python -m eval.run_pattern_probe</div>
          )}
          <div className="muted" style={{ marginTop: 8 }}>Recurring behaviours hidden in the data (seasonal slips, size-dependent reliability, lead-time creep…), asked about without naming the behaviour.</div>
        </Panel>
      </div>

      <Panel title="What the agent guarantees">
        <div className="guarantees">
          {GUARANTEES.map(([t, d]) => (
            <div key={t}><b>{t}</b><div className="muted">{d}</div></div>
          ))}
        </div>
      </Panel>

      {cov && (
        <Panel title="What it runs on" right={<button className="linkbtn" onClick={() => onOpen("results")}>full scorecard</button>}>
          <div className="figures">
            <div><b>{cov.memory_docs_stored.toLocaleString()}</b><span>of {cov.memory_docs_total.toLocaleString()} memory documents</span></div>
            <div><b>{cov.disruption_events.toLocaleString()}</b><span>disruption events</span></div>
            <div><b>{cov.decisions.toLocaleString()}</b><span>past decisions with outcomes</span></div>
            <div><b>{cov.commitments.toLocaleString()}</b><span>commitments</span></div>
            <div><b>{cov.rm_purchase_orders.toLocaleString()}</b><span>raw-material POs</span></div>
            <div><b>{cov.eval_questions}</b><span>graded history questions</span></div>
          </div>
        </Panel>
      )}
    </div>
  );
}
