import { useEffect, useState } from "react";
import { api, DecisionCard, Scenario } from "../api";
import { Panel, RunButton, useRunner } from "../components/common";
import { DecisionCardView } from "../components/results";

const FREE_TEXT = {
  report:
    "2025-01-13 - Supplier 169 (SUP0169) just moved RPO016183 (Elastic Film Laminate, RM0100) for Eastfield Plant (P03) to 2025-03-10 citing weather force majeure. Run PR0016522 is planned 2025-02-17 and needs this material. The buyer proposes cancelling RPO016183 and re-buying elsewhere. What should we do?",
  as_of: "2025-01-13",
  rpo: "RPO016183",
  runs: "PR0016522",
  rm: "RM0100",
};

export function DecideView() {
  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [mode, setMode] = useState<"scenario" | "free">("scenario");
  const [sid, setSid] = useState("HS05");
  const [report, setReport] = useState("");
  const [asOf, setAsOf] = useState("");
  const [rpo, setRpo] = useState("");
  const [runs, setRuns] = useState("");
  const [rm, setRm] = useState("");
  const [writeback, setWriteback] = useState(false);
  const { busy, error, data, run } = useRunner<DecisionCard>();

  useEffect(() => {
    api.scenarios().then(setScenarios).catch(() => setScenarios([]));
  }, []);

  useEffect(() => {
    if (mode === "free") {
      setReport(FREE_TEXT.report); setAsOf(FREE_TEXT.as_of); setRpo(FREE_TEXT.rpo); setRuns(FREE_TEXT.runs); setRm(FREE_TEXT.rm);
      return;
    }
    const sc = scenarios.find((s) => s.id === sid);
    if (sc) {
      setReport(sc.report); setAsOf(sc.day0); setRpo(sc.context.rpo_id); setRuns(sc.context.run_ids.join(",")); setRm(sc.context.rm_id);
    }
  }, [mode, sid, scenarios]);

  const current = scenarios.find((s) => s.id === sid);
  const decide = () =>
    run(() =>
      api.decide({
        report,
        as_of: asOf || undefined,
        rpo_id: rpo || undefined,
        run_ids: runs.split(",").map((r) => r.trim()).filter(Boolean),
        rm_id: rm || undefined,
        writeback,
      }),
    );

  return (
    <div>
      <div className="page-head">
        <h2>Disruption</h2>
        <p className="lead">Report a supply disruption. The agent checks what happened last time, what is still promised, and what each response would cost.</p>
      </div>
      <Panel>
        <div className="row" style={{ marginBottom: 12 }}>
          <div className="seg">
            <button className={mode === "scenario" ? "on" : ""} onClick={() => setMode("scenario")}>Holdout case</button>
            <button className={mode === "free" ? "on" : ""} onClick={() => setMode("free")}>Own report</button>
          </div>
          {mode === "scenario" && (
            <select value={sid} onChange={(e) => setSid(e.target.value)}>
              {scenarios.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.id}  {s.day0}  {s.supplier_id} / {s.rm_id}{s.trap ? "  (trap)" : ""}
                </option>
              ))}
            </select>
          )}
        </div>
        {mode === "scenario" && current?.trap && (
          <div className="alert warn">
            <span className="tag">Trap</span>The closest precedent, {current.trap.its_decision_id} ({current.trap.its_action.replace(/_/g, " ")}, {current.trap.its_outcome}), no longer fits: {current.trap.why_misleading}
          </div>
        )}
        <label>
          Report
          <textarea value={report} onChange={(e) => setReport(e.target.value)} />
        </label>
        <div className="grid cols-4" style={{ marginTop: 10 }}>
          <label>As of<input className="mono" value={asOf} onChange={(e) => setAsOf(e.target.value)} placeholder="YYYY-MM-DD" /></label>
          <label>Purchase order<input className="mono" value={rpo} onChange={(e) => setRpo(e.target.value)} /></label>
          <label>Affected runs<input className="mono" value={runs} onChange={(e) => setRuns(e.target.value)} /></label>
          <label>Raw material<input className="mono" value={rm} onChange={(e) => setRm(e.target.value)} /></label>
        </div>
        <div className="row" style={{ marginTop: 14, justifyContent: "space-between" }}>
          <label className="check">
            <input type="checkbox" checked={writeback} onChange={(e) => setWriteback(e.target.checked)} />
            Log the decision and remember it for next time
          </label>
          <RunButton busy={busy} onClick={decide} label="Decide" />
        </div>
      </Panel>
      {error && <div className="alert error"><span className="tag">Error</span>{error}</div>}
      {data && <DecisionCardView card={data} />}
    </div>
  );
}
