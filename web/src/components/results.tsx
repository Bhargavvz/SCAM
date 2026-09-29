import { CapabilityAnswer, DecisionCard, QAResult, Row } from "../api";
import { HBars, StepBars, Timeline, Tone } from "./charts";
import { Alerts, Citations, DataTable, Markdown, Metric, Panel, fmt, money } from "./common";

export function DecisionCardView({ card }: { card: DecisionCard }) {
  const rec = card.recommended_action;
  const best = card.simulator_best_action;
  const options: Row[] = card.options.map((o) => ({
    "": (o.action === rec ? "★ " : "") + (o.action === best ? "▲" : ""),
    action: o.action,
    feasible: o.feasible,
    arrival: o.arrival,
    stockout_days: o.stockout_days,
    cost: money(o.cost),
    risk: o.risk,
    score: money(o.score),
    service_impact_units: o.service_impact_units,
    breaches: o.breaches_commitments,
    note: o.note,
  }));
  const warnings = [...card.warnings];
  if (rec && best && rec !== best) warnings.unshift(`The agent deviates from the simulator's lowest risk-adjusted option (${best}); see rationale.`);
  return (
    <div>
      <div className="metrics">
        <Metric k="Recommendation" v={rec ?? "none"} highlight={!!rec} />
        <Metric k="Simulator best" v={best ?? "-"} />
        <Metric k="Confidence" v={card.confidence} />
        <Metric k="Time · model calls" v={`${card.latency_s}s · ${card.usage?.calls ?? 0}`} />
      </div>
      <Alerts errors={card.guardrail_events} warnings={warnings} infos={card.situation_summary ? [card.situation_summary] : []} />

      <div className="grid cols-2">
        <Panel title="Risk-adjusted cost by option" right={<span className="muted">log scale · lower is better</span>}>
          <HBars log items={card.options.filter((o) => o.supported !== false).map((o) => ({
            label: o.action.replace(/_/g, " "),
            value: o.feasible ? o.score ?? null : null,
            display: o.feasible ? `${money(o.score)} · ${o.stockout_days}d` : "",
            tone: (!o.feasible ? "muted" : o.breaches_commitments?.length ? "bad" : o.action === rec ? "good" : "ink") as Tone,
            note: o.note,
          }))} />
          <div className="legend">
            <span><i style={{ background: "#1c6b3f" }} />recommended</span>
            <span><i style={{ background: "#16181d" }} />alternative</span>
            <span><i style={{ background: "#a52a2a" }} />breaks a commitment</span>
            <span className="muted">label: cost · stockout days</span>
          </div>
        </Panel>
        <Panel title="Precedents over time" right={<span className="muted">green worked · red failed · grey pending</span>}>
          <Timeline today={card.as_of} points={card.precedents.filter((pr) => pr.decided_at).map((pr) => ({
            date: String(pr.decided_at),
            label: `${String(pr.decision_type).replace(/_/g, " ")}${pr.applies_today ? "" : " ✗"}`,
            tone: (pr.outcome_label === "success" ? "good" : pr.outcome_label === "failed" ? "bad" : pr.outcome_label === "partial" ? "warn" : "muted") as Tone,
            detail: `${pr.decision_id} · ${pr.outcome_label ?? "outcome unknown"} · ${pr.note}`,
          }))} />
          <div className="muted">✗ = the same action would not be the best choice today</div>
        </Panel>
      </div>

      <Panel title="Options (simulated)" right={<span className="muted">★ recommended · ▲ lowest risk-adjusted score</span>}>
        <DataTable rows={options} rowClass={(r) => (String(r[""]).includes("★") ? "rec" : r.feasible ? "" : "dim")} />
      </Panel>

      <div className="grid cols-2">
        <Panel title="Precedents">
          <DataTable
            rows={card.precedents}
            columns={["decision_id", "decided_at", "decision_type", "outcome_label", "today_rank", "applies_today", "source", "note"]}
            rowClass={(r) => (r.applies_today ? "" : "dim")}
          />
        </Panel>
        <Panel title="Open commitments">
          <DataTable
            rows={card.open_commitments}
            columns={["commitment_id", "counterparty_id", "due_date", "commitment_text", "affected_by"]}
            rowClass={(r) => (Array.isArray(r.affected_by) && r.affected_by.length ? "dim" : "")}
          />
        </Panel>
      </div>

      <Panel title="Rationale">
        <Markdown text={card.rationale} />
        {card.ungrounded_claims.length > 0 && <div className="alert warn">Ungrounded claims: {card.ungrounded_claims.join("; ")}</div>}
        <div style={{ marginTop: 12 }}>
          <Citations docs={card.cited_doc_ids} records={card.cited_record_ids} unverifiable={card.unverifiable_citations} asOf={card.as_of} />
        </div>
      </Panel>

      {(card.writeback || card.mock_actions.length > 0) && (
        <Panel title="Logged response">
          {card.writeback && <div className="alert ok"><span className="tag">Logged</span>{fmt(card.writeback.decision_id)} + {fmt(card.writeback.commitment_id)}: {fmt(card.writeback.commitment_text)} · retain: {fmt(card.writeback.retain_status)}</div>}
          {card.mock_actions.map((m) => <div key={m} className="muted">{m}</div>)}
        </Panel>
      )}

      <Panel>
        <div className="muted">
          as_of {card.as_of} · memory hits {card.memory_hits} · dropped as future {card.dropped_future_hits} · reflect {card.reflect_mode} · tokens in/out {card.usage?.input_tokens ?? "-"}/{card.usage?.output_tokens ?? "-"} · cost {card.usage?.cost_usd == null ? "unknown" : `$${card.usage.cost_usd}`}
        </div>
        {card.reflection && (
          <details style={{ marginTop: 10 }}>
            <summary>Memory reflection</summary>
            <Markdown text={card.reflection} />
          </details>
        )}
        {card.trace.length > 0 && (
          <div style={{ marginTop: 12 }}>
            <div className="label">Where the time went</div>
            <StepBars steps={card.trace.map((s) => ({ name: String(s.name), kind: String(s.kind), ms: Number(s.ms) || 0 }))} />
          </div>
        )}
        {card.trace.length > 0 && (
          <details style={{ marginTop: 10 }}>
            <summary>Full trace ({card.trace.length} steps)</summary>
            <DataTable rows={card.trace} columns={["kind", "name", "ms", "input", "output"]} />
          </details>
        )}
      </Panel>
    </div>
  );
}

export function CapabilityView({ ans }: { ans: CapabilityAnswer }) {
  const checked = ans.commitments_checked === null ? "n/a" : ans.commitments_checked ? "checked" : "not checked";
  return (
    <div>
      <div className="metrics">
        <Metric k="Routed to" v={ans.capability.replace(/_/g, " ")} />
        <Metric k="Reflect" v={`${ans.reflect_mode ?? "-"} · ${ans.budget}`} />
        <Metric k="Confidence" v={ans.confidence} />
        <Metric k="Commitments" v={checked} />
      </div>
      <Alerts warnings={ans.warnings} />
      <Panel title="Answer">
        <Markdown text={ans.answer} />
        {ans.structured && (
          <details style={{ marginTop: 10 }}>
            <summary>Structured output</summary>
            <pre className="doc">{JSON.stringify(ans.structured, null, 2)}</pre>
          </details>
        )}
        <div style={{ marginTop: 12 }}>
          <Citations docs={ans.cited_doc_ids} records={ans.cited_record_ids} unverifiable={ans.unverifiable_citations} asOf={ans.as_of} />
        </div>
      </Panel>
      {Object.entries(ans.ground_truth).map(([name, rows]) => (
        <Panel key={name} title={name.replace(/_/g, " ")} right={<span className="muted">database, as of {ans.as_of}</span>}>
          <DataTable rows={rows} />
        </Panel>
      ))}
      <div className="muted">latency {ans.latency_s}s</div>
    </div>
  );
}

export function AnswerView({ res }: { res: QAResult }) {
  return (
    <Panel title="Answer">
      <Markdown text={res.answer} />
      <div style={{ marginTop: 12 }}>
        <Citations docs={res.cited_doc_ids} records={res.cited_record_ids} unverifiable={res.unverifiable_citations} asOf={res.as_of} />
      </div>
      <div className="muted" style={{ marginTop: 8 }}>
        as_of {res.as_of} · confidence {res.confidence} · memory hits {res.memory_hits} · {res.latency_s}s
      </div>
    </Panel>
  );
}
