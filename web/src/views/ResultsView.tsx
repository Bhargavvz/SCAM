import { useEffect, useState } from "react";
import { api, DecisionCard, Results } from "../api";
import { Markdown, Panel } from "../components/common";
import { DecisionCardView } from "../components/results";

export function ResultsView() {
  const [data, setData] = useState<Results | null>(null);
  const [card, setCard] = useState<DecisionCard | null>(null);
  const [pick, setPick] = useState("");

  const load = () => api.results().then(setData).catch(() => setData(null));
  useEffect(() => { load(); }, []);
  useEffect(() => {
    if (pick) api.resultCard(pick).then(setCard).catch(() => setCard(null));
  }, [pick]);

  return (
    <div>
      <div className="page-head">
        <h2>Results</h2>
        <p className="lead">Latest evaluation run, walkthrough report and saved decision cards.</p>
      </div>
      <Panel title="Scorecard" right={<button className="btn ghost" onClick={load}>Refresh</button>}>
        {data?.scorecard_md ? <Markdown text={data.scorecard_md} /> : <div className="muted">Not generated yet — run scripts/run_live_evals.sh</div>}
      </Panel>
      <Panel title="Walkthrough report">
        {data?.demo_report_md ? <Markdown text={data.demo_report_md} /> : <div className="muted">Not generated yet — run .venv/bin/python -m demo.run_demo</div>}
      </Panel>
      {data && data.cards.length > 0 && (
        <Panel title="Saved decision cards" right={
          <select value={pick} onChange={(e) => setPick(e.target.value)}>
            <option value="">choose a scenario…</option>
            {data.cards.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        }>
          {card ? <DecisionCardView card={card} /> : <div className="muted">Pick a card to view it.</div>}
        </Panel>
      )}
    </div>
  );
}
