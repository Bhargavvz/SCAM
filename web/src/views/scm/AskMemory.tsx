import { useState } from "react";
import { CapabilityAnswer, QAResult } from "../../api";
import { Panel } from "../../components/common";
import { AnswerView, CapabilityView } from "../../components/results";
import { JobStatus, PageHead, Seg } from "../../components/scm";
import { useAsOf, useJob } from "../../nav";
import { AskResult, scm } from "../../scm";

const SUGGESTIONS = [
  "Which suppliers slip most in the fourth quarter, and is it getting worse?",
  "What did we promise SUP0247 and is any of it still open?",
  "Last time a resin supplier was late, did expediting pay off?",
  "Which of our responses to delays usually cost more than we expected?",
  "Is there a supplier whose lead time has been creeping up?",
  "What should I watch for before cancelling an order with a volume commitment?",
];

export function AskMemory() {
  const { asOf } = useAsOf();
  const [q, setQ] = useState("");
  const [mode, setMode] = useState<"ask" | "history">("ask");
  const j = useJob<AskResult>();
  const res = j.job?.status === "done" ? j.job.result : null;
  const submit = (text = q) => {
    if (!text.trim()) return;
    setQ(text);
    j.start(() => scm.askAgent(text.trim(), asOf, mode));
  };
  return (
    <div>
      <PageHead title="Ask memory" sub={`Ask anything about the supply chain's history. Answers use only what was known on ${asOf}, and every claim is traced to a memory document or database record.`} />
      <Panel>
        <textarea placeholder="e.g. Why do orders from SUP0112 keep slipping in November?" value={q} onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit(); }} />
        <div className="row" style={{ marginTop: 10, justifyContent: "space-between" }}>
          <Seg value={mode} onChange={setMode} options={[["ask", "Analyst (7 capabilities)"], ["history", "Fact lookup"]]} />
          <div className="row">
            <JobStatus job={j.job} label="recalling and reflecting" />
            <button className="btn" disabled={j.running || !q.trim()} onClick={() => submit()}>{j.running && <span className="spinner" />}Ask</button>
          </div>
        </div>
        {!res && !j.running && (
          <div className="chips">{SUGGESTIONS.map((s) => <button key={s} className="chip" onClick={() => submit(s)}>{s}</button>)}</div>
        )}
      </Panel>
      {j.error && <div className="alert error"><span className="tag">Agent</span>{j.error}</div>}
      {j.running && <Panel><div className="skeleton"><div /><div /><div /></div></Panel>}
      {res && res.mode === "ask" && <CapabilityView ans={res as unknown as CapabilityAnswer} />}
      {res && res.mode === "history" && <AnswerView res={res as unknown as QAResult} />}
    </div>
  );
}
