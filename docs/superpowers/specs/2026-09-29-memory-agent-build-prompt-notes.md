# Supply Chain Memory Agent build prompt - merge notes (2026-09-29)

The user supplied a second build prompt ("Supply Chain Memory Agent - Build Prompt"). It covers:
- Hindsight setup
- bank mission, directives and disposition
- ingestion
- 12 Mental Models
- a 7-capability agent
- an eval strategy
- 7 demo queries
- success criteria

Decision on 2026-09-29:
- **Merge it into the existing plan.** The first brief, `2026-09-29-sc-decision-agent-brief.md`, stays the primary spec.
- **Create the Mental Models behind a flag**, so pattern detection is reported both with and without them.

## Requirements taken from the prompt

- **Local Hindsight.** Docker `ghcr.io/vectorize-io/hindsight` (API :8888, Control Plane UI :9999) with a persistent volume. The server needs its own LLM key (`HINDSIGHT_API_LLM_PROVIDER` / `HINDSIGHT_API_LLM_API_KEY`, OpenAI or Groq recommended). Set a stable `HINDSIGHT_API_WORKER_ID`.
- **One bank, `supply-chain-memory`**, with:
  - the mission below, verbatim;
  - 8 directives;
  - four disposition traits: cautious about supplier switches (check commitments first), data-driven (quantify cost and stockout days), precedent-aware, proactive (flag risks unasked).
- **Ingestion** with `dataset/code/hindsight_loader.py`: `split=memory` only, chronological order, batch 25, concurrency 5, resumable log. About 27% of docs are noise and about 10% supersede earlier ones; let Hindsight handle both.
- **12 Mental Models**, one per planted pattern P01-P12.
- **Agent:**
  - an interactive CLI;
  - a router for 7 capabilities plus "general": supplier reliability, commitments, decision precedent, exceptions, quality root cause, negotiation, decision outcome learning;
  - capability handlers built on reflect (budgets mid or high, `response_schema` for precedent and outcome-learning);
  - a response formatter with citations, confidence and actions.
- **Eval:**
  - `recall(budget="mid")` for single-hop fact recall, `reflect(budget="high")` for everything else;
  - pass/fail against gold;
  - scores by type and by hop_count.
- **7 demo queries**, one per capability.
- **Success criteria:**
  - all memory docs retained;
  - 12 Mental Models created;
  - at least 3 of 7 demos answered with evidence;
  - at least 60% on single-hop fact recall;
  - meaningful multi-hop answers;
  - at least 8 of 12 patterns identified when the entity is queried;
  - citations in every response;
  - commitments always checked before recommending a supplier switch or PO cancellation.

### Mission (verbatim)

> Institutional supply-chain memory for a multi-plant consumer-goods manufacturer. This bank stores the complete operational history: suppliers, raw materials, plants, distribution centers, purchase orders, disruption events, negotiations, commitments, and past decisions with their outcomes. Use it to recall specific historical facts with evidence, connect causally related events across the supply chain, track open commitments and obligations, evaluate consequences of candidate actions by referencing precedents, and recommend responses grounded in what worked before.

### Directives (verbatim)

1. Cite the evidence (document dates and record IDs such as RPO, PO, EVT, DEC, CMT) behind every claim and recommendation.
2. Always list open commitments that a recommended action could affect.
3. Never recommend cancelling a purchase order without first checking open commitments with that supplier.
4. Prefer the most recent document when facts conflict; say which earlier statement was superseded.
5. When citing a precedent, state how today's conditions differ from the precedent's conditions.
6. When a supplier has a known pattern (seasonal delays, size-dependent reliability, etc.), flag it explicitly.
7. For decision recommendations, always show: predicted outcome, confidence level, and what happened last time.
8. Distinguish between facts (from documents) and observations (consolidated patterns) in responses.

### The 7 demo queries (verbatim)

1. What is SUP0247's delivery track record in November and December? Should we trust their current promise for a November delivery?
2. What open commitments do we have with SUP0091? If we switch suppliers, what obligations would we breach?
3. Our primary RM supplier just had a 35% supply reduction. We're deciding between prioritizing high-margin products vs. switching to the backup supplier. What happened last time we faced this?
4. Have we ever accepted partial shipments from suppliers? How many times has this exception been granted this quarter, and should we escalate for a policy review?
5. We're seeing corrosion on steel components from a supplier that had humidity issues before. Are their corrective controls still in place, or has the problem recurred?
6. We need to renegotiate with SUP0179 whose lead times keep getting longer. What negotiation strategies have worked with them before?
7. How accurate have our past 'switch to cheaper supplier' decisions been? Should we trust our cost-savings projections?

## Where the prompt and the dataset disagree (the dataset wins; checked 2026-09-29)

| Prompt says | Dataset / Hindsight has | Plan uses |
|---|---|---|
| eval types fact_recall, multi_hop, temporal, pattern, precedent, commitment_check, decision_outcome, negotiation_history | fact_recall 34, causal_why 34, temporal 31, commitment_tracking 31, multi_hop 29, consequence_evaluation 29, precedent_analogy 29, contradiction_supersession 24 | the real types; scores by type and by hop_count |
| ~1,800 memory-split docs | 2,201 memory + 299 holdout | 2,201 |
| demos 3, 4, 5 and 7 (35% supply reduction, partial-shipment exceptions, corrosion/humidity, cheaper supplier + customer churn) | 0 corpus mentions of corrosion, humidity, defect, churn, cheaper, high-margin or partial shipment | the queries run as written; a correct answer uses what does exist, or says plainly that there is no record |
| mission text | differs from `hindsight_loader.py` MISSION | the prompt's mission (+ traits), set by `agent/bank_setup.py` |
| four disposition traits (text) | Hindsight disposition is 3 numeric 1-5 scales (skepticism, literalism, empathy) | skepticism 4, literalism 4, empathy 2, plus the four traits written into `reflect_mission` |
| Mental Models "encode" the pattern statements | a mental model is defined by a `source_query`; Hindsight synthesizes its content from the bank | one standing question per pattern that names the entity and the behaviour but no effect sizes, tagged `planted-pattern` |
| reflect for everything | reflect has no date filter; mental models summarize the whole bank | native reflect / mental models only where the as-of guard allows it; otherwise local synthesis over as-of-filtered recall |
| loader writes `retention_log.jsonl` next to the corpus | that would write into `dataset/` | loader runs with `--log runtime/retention_log.jsonl` |
