import { useEffect, useState } from "react";
import { api, CapabilityAnswer, DemoSpec } from "../api";
import { Panel, RunButton, useRunner } from "../components/common";
import { CapabilityView } from "../components/results";

export function AskView({ defaultAsOf }: { defaultAsOf: string }) {
  const [examples, setExamples] = useState<DemoSpec[]>([]);
  const [question, setQuestion] = useState("");
  const [asOf, setAsOf] = useState(defaultAsOf);
  const { busy, error, data, run } = useRunner<CapabilityAnswer>();

  useEffect(() => {
    api.demoSpecs().then((specs) => {
      const asks = specs.filter((s) => s.kind === "ask");
      setExamples(asks);
      if (asks[0]?.question) setQuestion(asks[0].question);
    }).catch(() => setExamples([]));
  }, []);

  useEffect(() => setAsOf(defaultAsOf), [defaultAsOf]);

  return (
    <div>
      <h2>Ask the memory</h2>
      <p className="lead">Any supply-chain question is routed to one of seven capabilities: supplier reliability, commitments, decision precedent, exceptions, quality root cause, negotiation, decision-outcome learning.</p>
      <Panel title="Examples">
        <div className="list">
          {examples.map((e) => (
            <button key={e.id} className={question === e.question ? "on" : ""} onClick={() => setQuestion(e.question ?? "")}>
              <b>{e.title.split(":")[0]}</b> — {e.question}
            </button>
          ))}
        </div>
      </Panel>
      <Panel>
        <label>Question<textarea value={question} onChange={(e) => setQuestion(e.target.value)} /></label>
        <div className="row" style={{ marginTop: 10, justifyContent: "space-between" }}>
          <label style={{ width: 200 }}>As of<input value={asOf} onChange={(e) => setAsOf(e.target.value)} /></label>
          <RunButton busy={busy} onClick={() => run(() => api.ask(question, asOf || undefined))} label="Ask" />
        </div>
      </Panel>
      {error && <div className="alert error">{error}</div>}
      {data && <CapabilityView ans={data} />}
    </div>
  );
}
