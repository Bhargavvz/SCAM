# How Hindsight Helped My Agent Catch a Contract Breach

The most dangerous recommendation my supply chain agent ever made looked completely reasonable: cancel a late purchase order and re-buy the material from someone else. It would have cost eight dollars in fees and broken a twelve-month volume commitment we had signed with that supplier three weeks earlier.

The agent didn't get to make that recommendation. This article is about why. The short version is that memory told the agent the promise existed, and code made sure the promise mattered.

## What the system does

I build decision support for a multi-plant consumer-goods manufacturer: five plants, a few hundred suppliers, about a hundred raw materials, and three years of operational history. When a raw-material purchase order slips, a planner has to decide quickly whether to accept the delay, expedite, switch suppliers, move stock between plants, substitute a material, or cancel and re-buy.

The hard part is not listing the options. It's remembering everything around them:
- The supplier that always slips in November.
- The transfer out of one plant that left it short last time.
- The commitment someone made in a negotiation six months ago.

That kind of history lives in emails, call notes, exception tickets and post-mortems. Nobody reads all of them before making a call on a Tuesday afternoon.

So the agent has two sources of truth, and I keep them deliberately separate:

1. **Narrative memory in [Hindsight](https://github.com/vectorize-io/hindsight).** Every note, memo, escalation and post-mortem is retained into one memory bank, in chronological order, with its original timestamp and document ID. Hindsight extracts facts and entities, links them into a graph, and consolidates recurring patterns into observations. The agent uses `recall` to find what happened and `reflect` to synthesise it.
2. **Structured state in SQLite.** Purchase orders, revisions, production runs, decisions with their measured outcomes, commitments with due dates and penalties, and supplier scorecards.

Around those two stores sit three more pieces:
- A deterministic simulator that prices every candidate action.
- A small LLM tool loop that picks one.
- A set of guardrails that the LLM cannot talk its way past.

The output is a decision card: simulated options, precedents, open commitments, a cited rationale, and optionally a logged decision that gets written back into memory.

This split is the most important design decision in the project. If you have read [what agent memory actually is](https://vectorize.io/what-is-agent-memory) and how it differs from retrieval over a document pile, this will feel familiar. Memory is where the *why* lives. The database is where the *what is true right now* lives. A good agent needs both, and it needs to know which is which.

## The failure that shaped the design

The case that changed my thinking came from a real slice of the history.

On 13 January, Supplier 169 pushed a lot of elastic film laminate (`RPO016183`) for our Eastfield plant out to 10 March, citing weather. A production run needing that material was planned for mid-February. The buyer's instinct, written straight into the report, was: *cancel it and re-buy elsewhere.*

On paper that's fine. The simulator says cancelling and spot-buying from an alternate qualified source lands the material in time, with no stockout, for single-digit dollars. Waiting for the supplier's date means 21 days of blocked production.

What the report didn't mention: on 26 December, three weeks earlier, we had committed to Supplier 169 that *"we order at least 44,060 m of Elastic Film Laminate in the 12 months from 2024-12-30."* That's commitment `CMT00560`. Cancelling open orders with them is exactly how you end up breaching it.

That fact sits in two places. There's a commitment-confirmation note in memory, and a row in the commitments table. The question was how to make sure it actually stopped the wrong action, instead of showing up as a footnote in an otherwise confident paragraph.

## Directives are policy; code is enforcement

Hindsight lets you give a memory bank a mission and a set of directives that shape every `reflect` call. The [Hindsight memory bank configuration docs](https://hindsight.vectorize.io/) cover the options. Ours live in `agent/bank_setup.py`, and a few of them are about exactly this situation:

```python
DIRECTIVES = [
    ("Cite evidence", "Cite the evidence (document dates and record IDs such as RPO, PO, EVT, DEC, CMT) behind every "
                      "claim and recommendation."),
    ("List affected commitments", "Always list open commitments that a recommended action could affect."),
    ("Check commitments before cancelling", "Never recommend cancelling a purchase order without first checking open "
                                            "commitments with that supplier."),
    # ...
]
```

These directives matter. With them in place, Hindsight's reflections consistently surface commitments and cite the notes they came from, which is half the battle. When the agent asks memory *"what happened before with SUP0169 and RM0100, and what is still open or promised?"*, the volume commitment comes back with its document and date.

But a directive is a request to a language model. I wanted the breach to be impossible to recommend, not unlikely. So the same rule exists three times: as a directive in memory, as a fact the simulator computes, and as a check in code that runs before any recommendation is accepted.

## Knowing a commitment was open *on that day*

The first trap was time. The commitments table stores each commitment's *final* status. By the end of the year `CMT00560` is marked `breached`. If the agent reads that on 13 January, it's reading the future.

It's the same problem that forced me to be careful with memory. I enforce an as-of rule everywhere: the agent may only see what was known on the report's date. On the database side, every query goes through TEMP views that shadow the real tables and mask anything resolved later:

```python
"commitments": f"""
    SELECT commitment_id, decision_id, negotiation_id, counterparty_type, counterparty_id, made_by_role,
           made_at, commitment_text, quantity, due_date, penalty_or_credit,
           CASE WHEN {resolved} <= {A} THEN status ELSE 'open' END AS status,
           CASE WHEN {resolved} <= {A} THEN resolved_at END AS resolved_at,
           ...
    FROM main.commitments WHERE made_at <= {A}
```

Queried as of 13 January, `CMT00560` is `open`. Queried as of 31 December, it's `breached`. Everything else in the agent sees whichever answer matches the report's date.

Memory gets the same treatment. Every recall hit from Hindsight carries a `document_id` and a `mentioned_at` timestamp. The client resolves each hit back to its source document and drops anything dated after the question. Because we retained documents chronologically with their original timestamps, Hindsight's temporal reasoning and the supersession between documents line up with this rule instead of fighting it. When a later note replaces an earlier ETA, the agent is told which document won.

## The simulator knows about promises

The simulator is a deterministic port of the cost model behind our historical decisions. Given the same database and the same purchase order, it always returns the same numbers. The LLM never produces a cost or a stockout figure.

Each option is priced, risk-weighted and scored. For cancellation, the simulator also asks the question the buyer didn't:

```python
def _cancel(con, st: ScenarioState, rules, switch: dict, d_acc: int) -> dict:
    d0 = st.day0.isoformat()
    rows = con.execute("""SELECT commitment_id, commitment_text FROM commitments
                          WHERE made_at <= ? AND (resolved_at IS NULL OR resolved_at > ?) AND counterparty_id IN (?, ?)
                          ORDER BY commitment_id""", (d0, d0, st.supplier_id, st.plant_id)).fetchall()
    vol = [r["commitment_id"] for r in rows if "We order at least" in (r["commitment_text"] or "")]
    ...
    return _opt("cancel_po", ..., risk="high" if vol else "medium",
                breaches_commitments=vol, evidence_record_ids=[st.rpo_id, *vol])
```

For `RPO016183` on 13 January, `cancel_po` comes back as a perfectly feasible option: arrives in time, zero stockout days, eight dollars. It is also marked high risk, with `breaches_commitments: ["CMT00560"]`. A breach is a property of the option, not a sentence in a rationale.

## The guardrail sits between the model and the card

The LLM runs a short tool loop. It can recall from Hindsight, ask for a reflection, read a document, run read-only SQL, or re-run the simulator. It finishes by calling `submit_recommendation` with a structured payload. That call is where enforcement happens:

```python
action = u.input["recommended_action"]
violations = action_violations(action, card.options)
if violations and rejections < MAX_REJECTIONS:
    rejections += 1
    card.guardrail_events.append(f"Rejected {action}: {violations[0]}")
    results.append(self._tool_result(
        u, "REJECTED by guardrail: " + "; ".join(violations) + ". Choose another action.", error=True))
elif violations:
    safe = best_option(card.options)
    card.guardrail_events.append(f"Override: {action} still violates ({violations[0]}); using "
                                 f"{safe['action']} (lowest risk-adjusted score without violations)")
```

`action_violations` checks four things:
- Is the action in the decision taxonomy?
- Was it simulated?
- Is it feasible?
- Does it breach an open commitment?

A violation goes back to the model as a tool error with the commitment ID in it, and the model gets another turn. If it insists twice, the code overrides it with the best option that breaks nothing. Both the rejection and the override appear on the decision card. Nothing is hidden.

There's also a pre-check. If the report itself proposes an action ("the buyer proposes cancelling…"), the agent checks that proposal against the simulated options before the model sees anything, and flags it up front.

## What actually happens on this case

Here's the Eastfield case, end to end:

- **Before the LLM runs:** the card already carries a guardrail event: *"Proposed action cancel_po blocked before recommendation: cancel_po would breach open commitment(s) CMT00560."*
- **In the open commitments panel:** `CMT00560` is listed with *affected by: cancel_po*.
- **In the options table:**
  - accepting the delay costs 21 blocked production days;
  - switching supplier or moving stock from Lakeshore both arrive in time for about a dollar;
  - cancelling looks almost as cheap, but carries the breach.
- **Recommendation:** a spot buy from an alternate source, or a transfer. The rationale cites the commitment record, the memory documents it drew on, and the simulated numbers, quoted exactly.

If the model had tried to recommend cancellation anyway, the trace would show the rejection and the retry. I have a test for exactly that path, because I didn't want to trust it on vibes.

The same machinery handles the subtler failure: precedents. On a held-out set of disruptions we deliberately include cases where the most similar past decision *worked*, but conditions have changed. A typical one: "accept the delay" worked for a supplier in August, but that supplier reliably slips another week or two in November and December.
- The card scores every retrieved precedent against today's simulated options.
- It marks the ones that would be a bad choice today.
- The model is required to say why a precedent applies or doesn't.

In my runs on that held-out set, the agent picked the best action on every scenario and did not fall for any of the trap precedents. I care less about the headline than about the fact that the reason is visible on every card.

## Closing the loop

When a planner accepts a recommendation, the agent writes it back:
- a decision row and a commitment row in an append-only table;
- a retained summary in Hindsight, with the decision's business date as its timestamp.

The next disruption for that supplier recalls it like any other precedent, and the commitment we just made is now one the guardrail protects. That's the part I find most satisfying. The system's memory of what it promised grows the same way a planner's does.

Writing back into a shared memory bank has a cost, though. If an evaluation run can see memories written by a demo run, the evaluation is contaminated. I track every retain in a bank-wide ledger, tie runtime memories to the database that produced them, and only let native `reflect` run when nothing in the bank could post-date the question. It's a small amount of bookkeeping that saved me from misleading numbers.

## What I learned

1. **Put policy in memory and enforcement in code.** Hindsight directives are excellent at shaping what the model notices and cites. Anything that must never happen belongs in a check the model can't negotiate with.
2. **Make risks properties of options, not prose.** Once `breaches_commitments` was a field on the option, everything downstream could act on it: the ranking, the guardrail, the UI, the logged decision.
3. **"As of" is a feature you build everywhere or nowhere.** A commitment's final status, a decision's final outcome, a document written next month: any of them leaks the future. I enforce the date in SQL views, in recall filtering, and in the rules for when reflection is allowed.
4. **Retain in order, with real timestamps and IDs.** Hindsight rewards it. Temporal recall, supersession and hit-to-source resolution all got easier because every memory kept its original date and document ID. The [Hindsight documentation](https://hindsight.vectorize.io/) is explicit about retain metadata for a reason.
5. **Keep the numbers deterministic.** The LLM is good at weighing evidence and explaining a choice. It should not be the thing that decides what an expedite costs.

The agent that nearly cancelled a purchase order and broke a contract wasn't wrong about the logistics. It just didn't know about a promise we'd made three weeks earlier. Giving it memory fixed the knowing. Making the promise part of the math fixed the rest.
