import { useState } from "react";
import { api, QAResult } from "../api";
import { Panel, RunButton, useRunner } from "../components/common";
import { AnswerView } from "../components/results";

const EXAMPLES = [
  { q: "What is the latest ETA for RPO003179?", asOf: "2023-06-11", note: "supersession: the later expedite ETA must win" },
  { q: "What is the latest ETA for RPO003179?", asOf: "2023-06-03", note: "same question before the expedite was known" },
  { q: "Why could production run PR0002426 at Northgate Plant not start on 2023-05-13?", asOf: "2023-05-24", note: "causal chain" },
  { q: "How did decision DEC00021 (reallocate stock, decided 2023-04-14) turn out against its estimate?", asOf: "2023-05-15", note: "consequence evaluation" },
];

export function QuestionView() {
  const [question, setQuestion] = useState(EXAMPLES[0].q);
  const [asOf, setAsOf] = useState(EXAMPLES[0].asOf);
  const { busy, error, data, run } = useRunner<QAResult>();
  return (
    <div>
      <div className="page-head">
        <h2>History</h2>
        <p className="lead">Ask what was known on a given date. Anything recorded after that date is hidden from the agent.</p>
      </div>
      <Panel title="Examples">
        <div className="list">
          {EXAMPLES.map((e, i) => (
            <button key={i} className={question === e.q && asOf === e.asOf ? "on" : ""} onClick={() => { setQuestion(e.q); setAsOf(e.asOf); }}>
              <span className="key">{e.asOf}</span>{e.q} <span className="muted">— {e.note}</span>
            </button>
          ))}
        </div>
      </Panel>
      <Panel>
        <label>Question<textarea value={question} onChange={(e) => setQuestion(e.target.value)} /></label>
        <div className="row" style={{ marginTop: 10, justifyContent: "space-between" }}>
          <label style={{ width: 180 }}>As of<input className="mono" value={asOf} onChange={(e) => setAsOf(e.target.value)} /></label>
          <RunButton busy={busy} onClick={() => run(() => api.question(question, asOf))} label="Answer" />
        </div>
      </Panel>
      {error && <div className="alert error"><span className="tag">Error</span>{error}</div>}
      {data && <AnswerView res={data} />}
    </div>
  );
}
