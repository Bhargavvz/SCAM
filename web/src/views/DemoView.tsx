import { useEffect, useState } from "react";
import { api, Check, DemoResult, DemoSpec } from "../api";
import { DataTable, Panel, RunButton, fmt, useRunner } from "../components/common";
import { AnswerView, CapabilityView, DecisionCardView } from "../components/results";

type Outcome = { spec: DemoSpec; result: DemoResult; checks: Check[] };

export function DemoView() {
  const [specs, setSpecs] = useState<DemoSpec[]>([]);
  const [selected, setSelected] = useState<string>("D1");
  const [outcomes, setOutcomes] = useState<Record<string, Outcome>>({});
  const { busy, error, run } = useRunner<Outcome>();

  useEffect(() => {
    api.demoSpecs().then(setSpecs).catch(() => setSpecs([]));
  }, []);

  const spec = specs.find((s) => s.id === selected);
  const outcome = outcomes[selected];
  const runOne = () =>
    run(async () => {
      const o = await api.demoRun(selected);
      setOutcomes((prev) => ({ ...prev, [selected]: o }));
      return o;
    });

  return (
    <div>
      <h2>Demo walkthrough</h2>
      <p className="lead">Scripted scenarios — routine decision, trap, commitment conflict, supersession, closing the loop, and one question per capability — each checked against its expected behaviour.</p>
      <div className="grid" style={{ gridTemplateColumns: "340px 1fr", alignItems: "start" }}>
        <Panel title="Scenarios">
          <div className="list">
            {specs.map((s) => {
              const o = outcomes[s.id];
              const passed = o ? o.checks.filter((c) => c.ok).length : 0;
              return (
                <button key={s.id} className={s.id === selected ? "on" : ""} onClick={() => setSelected(s.id)}>
                  <b>{s.id}</b> {s.title}
                  {o && (
                    <span className={`pill ${passed === o.checks.length ? "good" : "bad"}`} style={{ marginLeft: 6 }}>
                      {passed}/{o.checks.length}
                    </span>
                  )}
                </button>
              );
            })}
          </div>
        </Panel>
        <div>
          {spec && (
            <Panel title={`${spec.id} · ${spec.title}`} right={<RunButton busy={busy} onClick={runOne} label="Run scenario" />}>
              <div className="muted">Expected</div>
              <div>
                {Object.entries(spec.expect).map(([k, v]) => (
                  <span key={k} className="pill">{k}: {fmt(v)}</span>
                ))}
              </div>
            </Panel>
          )}
          {error && <div className="alert error">{error}</div>}
          {outcome && (
            <>
              <Panel title="Expected vs actual">
                <DataTable
                  rows={outcome.checks.map((c) => ({ check: c.check, expected: c.expected, actual: c.actual, result: c.ok ? "PASS" : "FAIL" }))}
                  rowClass={(r) => (r.result === "PASS" ? "rec" : "")}
                />
              </Panel>
              {outcome.result.error && <div className="alert error">{outcome.result.error}</div>}
              {outcome.result.card && <DecisionCardView card={outcome.result.card} />}
              {outcome.result.cards?.map((c, i) => (
                <div key={i}>
                  <h3>Step {i + 1} · as of {c.as_of}</h3>
                  <DecisionCardView card={c} />
                </div>
              ))}
              {outcome.result.answer && <AnswerView res={outcome.result.answer} />}
              {outcome.result.capability && <CapabilityView ans={outcome.result.capability} />}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
