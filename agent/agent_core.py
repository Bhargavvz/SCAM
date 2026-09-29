"""Agent core.

run(): parse -> deterministic simulation -> parallel memory recalls + SQL lookups -> reflect ->
Claude tool loop at low effort, ending in submit_recommendation, which the guardrails check ->
Claude rationale at high effort, number-checked -> DecisionCard (+ optional write-back).
answer_question(): the same tools in a QA loop, used by the eval harness.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agent import sql_tools
from agent.card import DecisionCard, QAResult
from agent.config import Settings
from agent.db import connect, live_memory_ids
from agent.guardrails import action_violations, split_citations, ungrounded_numbers
from agent.hindsight_tools import build_memory
from agent.llm import LLM, Usage
from agent.parsing import parse_report
from agent.simulate import TAXONOMY, SimulationError, best_option, build_state, simulate_options

MAX_STEPS = 10
MAX_REJECTIONS = 2


class AgentError(RuntimeError):
    """The agent could not produce a result."""


class MemoryUnavailable(AgentError):
    """Hindsight could not be reached; the agent refuses to reason as if memory were empty."""


def _j(obj) -> str:
    return json.dumps(obj, sort_keys=True, default=str)


def _strict(name, description, properties, required=None):
    return {"name": name, "description": description, "strict": True,
            "input_schema": {"type": "object", "properties": properties,
                             "required": required or list(properties), "additionalProperties": False}}


_STR_LIST = {"type": "array", "items": {"type": "string"}}
TOOL_RECALL = _strict("hindsight_recall",
                      "Search institutional memory (Hindsight: semantic + keyword + graph + temporal). Only memories "
                      "dated on or before as_of are returned; each hit has a doc_id to cite and lists later docs that "
                      "supersede it.",
                      {"query": {"type": "string"},
                       "window_days": {"type": ["integer", "null"], "description": "only the last N days before as_of, or null"}})
TOOL_REFLECT = _strict("hindsight_reflect",
                       "Synthesis over memory guided by the bank's mission and directives (what happened, what was "
                       "tried, what worked, what is still open). Respects as_of; returns the doc_ids it drew on.",
                       {"question": {"type": "string"}})
TOOL_DOC = _strict("memory_get_document", "Read the full text of a memory document by doc_id (only if dated on or before as_of).",
                   {"doc_id": {"type": "string"}})
TOOL_SQL = _strict("sql_query",
                   "Read-only SQLite query (one SELECT/WITH statement, max 200 rows) over the supply-chain tables as "
                   "known on as_of. Use plain table names (no main./live. prefixes). Schema is in the evidence pack.",
                   {"query": {"type": "string"}})
TOOL_SIM = _strict("simulate_action",
                   "Deterministic consequence simulation of one candidate action for this disruption: cost (USD), "
                   "stockout days, service impact, risk, open commitments breached. The only permitted source of "
                   "consequence numbers.",
                   {"action_type": {"type": "string", "enum": list(TAXONOMY)}})
TOOL_SUBMIT = _strict("submit_recommendation", "Submit the final structured recommendation (call exactly once).", {
    "situation_summary": {"type": "string"},
    "recommended_action": {"type": "string", "enum": list(TAXONOMY)},
    "key_reasons": _STR_LIST,
    "precedent_assessments": {"type": "array", "items": {
        "type": "object", "properties": {"ref": {"type": "string"}, "applies_today": {"type": "boolean"},
                                         "why": {"type": "string"}},
        "required": ["ref", "applies_today", "why"], "additionalProperties": False}},
    "commitments_considered": _STR_LIST,
    "cited_doc_ids": _STR_LIST,
    "cited_record_ids": _STR_LIST,
    "ungrounded_claims": _STR_LIST,
    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
})
TOOL_ANSWER = _strict("submit_answer", "Submit the final answer (call exactly once).", {
    "answer": {"type": "string"}, "cited_doc_ids": _STR_LIST, "cited_record_ids": _STR_LIST,
    "confidence": {"type": "string", "enum": ["low", "medium", "high"]}})
DECIDE_TOOLS = [TOOL_RECALL, TOOL_REFLECT, TOOL_DOC, TOOL_SQL, TOOL_SIM, TOOL_SUBMIT]
QA_TOOLS = [TOOL_RECALL, TOOL_REFLECT, TOOL_DOC, TOOL_SQL, TOOL_ANSWER]

SYSTEM_DECIDE = """You are the Supply Chain Memory & Decision Agent for a multi-plant consumer-goods manufacturer.
You get a disruption report and an evidence pack: memory hits from the Hindsight bank (each with a doc_id), structured
records (with record ids), open commitments, prior decisions scored against today's options, and a deterministic
simulation of the candidate actions.

Rules:
- Every cost, stockout-day and service number must come from the simulation (options / simulate_action). Never estimate one.
- Cite doc_ids (DOC...) and record ids (EVT, DEC, DECL, CMT, CMTL, RPO, REV, PR, ...) for each claim. List anything you
  cannot ground under ungrounded_claims instead of asserting it.
- Check open commitments before choosing. Never recommend an action whose simulation lists breached commitments.
- A precedent is evidence, not an answer. For each precedent you rely on or reject, say whether today's conditions match
  (compare today's simulated score of that action with the best option) in precedent_assessments.
- When an older and a newer memory disagree, use the newer one and name the superseded doc.
- Only information dated on or before as_of exists.
Use tools only when the evidence pack is missing something, then call submit_recommendation."""

SYSTEM_RATIONALE = """Write the rationale section of a supply-chain decision card for planners, 120-220 words, plain prose.
Use only the facts in the JSON you are given. Quote costs and stockout days exactly as they appear in the options table
(USD with a $ sign). Cite doc_ids and record ids inline in square brackets. For any precedent you mention, say how today
differs. Name every open commitment the action touches. If ungrounded_claims is non-empty, say those points are not
backed by evidence. Mention any guardrail event."""

SYSTEM_QA = """You answer questions about a manufacturer's supply-chain history using institutional memory (Hindsight)
and the database, as known on as_of. Nothing dated after as_of exists. When sources conflict, use the most recent and
name the superseded doc. Quote ids, dates and numbers exactly as the sources state them. Cite every doc_id and record id
you used. If you cannot find the answer, say so. Answer in 1-3 sentences, then call submit_answer."""


def build_precedents(decisions: list[dict], options: list[dict], limit: int = 6) -> list[dict]:
    feasible = sorted((o for o in options if o.get("feasible")), key=lambda o: (o["score"], o["action"]))
    rank = {o["action"]: i + 1 for i, o in enumerate(feasible)}
    score = {o["action"]: o["score"] for o in feasible}
    best = feasible[0]["action"] if feasible else None
    out = []
    for d in decisions[:limit]:
        a = d["decision_type"]
        if best is None:
            note = "no simulation available today"
        elif a == best:
            note = "same action is the best option today"
        elif a in rank:
            note = (f"today {a} ranks {rank[a]} of {len(feasible)} "
                    f"(score {score[a]:,.0f} vs {score[best]:,.0f} for {best})")
        else:
            note = f"{a} is not a feasible simulated option today"
        out.append({**{k: d.get(k) for k in ("decision_id", "event_id", "decided_at", "decision_type", "outcome_label",
                                             "expected_cost", "actual_cost", "actual_stockout_days", "lesson_text", "source")},
                    "today_score": score.get(a), "today_rank": rank.get(a), "best_today": best,
                    "applies_today": a == best, "note": note})
    return out


def annotate_commitments(commitments: list[dict], options: list[dict]) -> list[dict]:
    return [{**c, "affected_by": [o["action"] for o in options if c["commitment_id"] in o.get("breaches_commitments", [])]}
            for c in commitments]


def _short(obj, n: int = 600) -> str:
    s = obj if isinstance(obj, str) else _j(obj)
    return s if len(s) <= n else s[:n] + "..."


class DecisionAgent:
    def __init__(self, settings: Settings, llm, memory, live_db_path: Path):
        self.settings, self.llm, self.memory, self.live_db_path = settings, llm, memory, live_db_path

    # ---------------------------------------------------------------- helpers
    def _connect(self, as_of: str) -> sqlite3.Connection:
        return connect(self.settings.db_path, self.live_db_path, as_of)

    @staticmethod
    def _trace(trace: list, kind: str, name: str, inp, out, t0: float) -> None:
        trace.append({"kind": kind, "name": name, "input": _short(inp), "output": _short(out),
                      "ms": round((time.monotonic() - t0) * 1000)})

    @staticmethod
    def _tool_result(block, content: str, error: bool = False) -> dict:
        r = {"type": "tool_result", "tool_use_id": block.id, "content": content}
        if error:
            r["is_error"] = True
        return r

    def _run_tool(self, con, block, as_of: str, state, seen_docs: set, trace: list) -> str:
        t0 = time.monotonic()
        name, inp = block.name, block.input
        try:
            if name == "hindsight_recall":
                try:
                    r = self.memory.recall(inp["query"], as_of, window_days=inp.get("window_days"))
                except Exception as ex:  # external service error goes back to the model as a tool error
                    raise AgentError(f"Hindsight recall failed: {ex}") from ex
                seen_docs.update(h.doc_id for h in r.hits if h.doc_id)
                out = r.to_payload()
            elif name == "hindsight_reflect":
                try:
                    refl = self.memory.reflect(inp["question"], as_of)
                except Exception as ex:  # external service error goes back to the model as a tool error
                    raise AgentError(f"Hindsight reflect failed: {ex}") from ex
                seen_docs.update(h.doc_id for h in refl.hits if h.doc_id)
                out = {"mode": refl.mode, "text": refl.text, "doc_ids": refl.doc_ids,
                       "memories": [h.to_dict() for h in refl.hits[:15]]}
            elif name == "memory_get_document":
                out = self.memory.get_document(inp["doc_id"], as_of) or {
                    "error": f"{inp['doc_id']} does not exist or is dated after {as_of}"}
                if "doc_id" in out:
                    seen_docs.add(out["doc_id"])
            elif name == "sql_query":
                out = sql_tools.run_sql(con, inp["query"])
            elif name == "simulate_action":
                if state is None:
                    out = {"error": "no scenario state for this report - simulation unavailable"}
                else:
                    out = next(o for o in simulate_options(con, state) if o["action"] == inp["action_type"])
            else:
                out = {"error": f"unknown tool {name}"}
        except (sql_tools.SQLGuardError, sqlite3.Error, SimulationError, AgentError) as ex:
            out = {"error": str(ex)}
        self._trace(trace, "tool", name, inp, out, t0)
        return _j(out)

    def _gather(self, report: str, ent_str: str, as_of: str) -> dict:
        queries = {"broad": (report, None),
                   "entity": (f"History of disruptions, decisions, negotiations and commitments involving {ent_str}", None),
                   "recent": (f"Recent problems, promises and changes for {ent_str}", 120)}
        try:
            with ThreadPoolExecutor(max_workers=3) as ex:
                futs = {k: ex.submit(self.memory.recall, q, as_of, window_days=w) for k, (q, w) in queries.items()}
                return {k: f.result() for k, f in futs.items()}
        except Exception as ex:  # any client/network error: refuse to continue on empty memory
            raise MemoryUnavailable(f"Hindsight recall failed ({ex}); check HINDSIGHT_BASE_URL / "
                                    f"HINDSIGHT_BANK_ID") from ex

    # ---------------------------------------------------------------- decision mode
    def run(self, report: str, *, as_of: str | None = None, context: dict | None = None,
            writeback: bool = False) -> DecisionCard:
        t0 = time.monotonic()
        self.llm.usage = Usage()
        ctx = context or {}
        ents = parse_report(report)
        as_of = as_of or ents.report_date or self.settings.default_as_of
        card = DecisionCard(run_id=f"run-{uuid.uuid4().hex[:8]}", as_of=as_of, report=report, entities=ents.to_dict())
        con = self._connect(as_of)
        state = None
        try:
            sup = (ents.supplier_ids or [None])[0]
            rm = ctx.get("rm_id") or (ents.rm_ids or [None])[0]
            plant = (ents.plant_ids or [None])[0]
            rpo = ctx.get("rpo_id") or (ents.rpo_ids or [None])[0]
            runs = list(ctx.get("run_ids") or ents.run_ids)

            # 1. deterministic simulation (also canonicalises supplier / plant / rm from the PO)
            ts = time.monotonic()
            try:
                if not rpo:
                    raise SimulationError("the report names no RM purchase order (RPO...)")
                state = build_state(con, rpo_id=rpo, run_ids=runs, day0=as_of, rm_id=rm)
                card.options = simulate_options(con, state)
                card.scenario_state = state.to_dict()
                sup, rm, plant = state.supplier_id, state.rm_id, state.plant_id
                best = best_option(card.options)
                card.simulator_best_action = best["action"] if best else None
                self._trace(card.trace, "step", "simulate_options", card.scenario_state,
                            {o["action"]: o.get("score") for o in card.options}, ts)
            except SimulationError as ex:
                card.warnings.append(f"Cannot simulate actions: {ex}. No consequence numbers are shown.")

            # 2. evidence fan-out: Hindsight in parallel, SQL on this thread
            ts = time.monotonic()
            ent_str = ", ".join(x for x in (sup, rm, plant, rpo, *runs) if x) or report[:200]
            recalls = self._gather(report, ent_str, as_of)
            commitments = sql_tools.open_commitments(con, [sup, plant], as_of)
            scorecard = sql_tools.supplier_scorecard(con, sup) if sup else []
            events = sql_tools.prior_events(con, sup, rm, plant)
            links = sql_tools.event_links_for(con, [e["event_id"] for e in events])
            decisions = sql_tools.prior_decisions(con, sup, rm, as_of)
            card.related_event_ids = [e["event_id"] for e in events]
            card.precedents = build_precedents(decisions, card.options)
            card.open_commitments = annotate_commitments(commitments, card.options)
            self._trace(card.trace, "step", "gather", ent_str,
                        {k: len(r.hits) for k, r in recalls.items()} | {"commitments": len(commitments),
                                                                          "events": len(events), "decisions": len(decisions)}, ts)

            # 3. reflect
            ts = time.monotonic()
            refl = self.memory.reflect(f"Regarding {ent_str}: what happened before, what was tried, what worked or failed "
                                       f"and why, and what is still open or promised?", as_of)
            card.reflect_mode, card.reflection = refl.mode, refl.text
            self._trace(card.trace, "step", "reflect", refl.mode, refl.text or "", ts)
            hits, seen = [], set()
            for h in [*(h for r in recalls.values() for h in r.hits), *refl.hits]:
                key = (h.doc_id, h.text)
                if key not in seen:
                    seen.add(key)
                    hits.append(h)
            card.memory_hits = len(hits)
            card.dropped_future_hits = sum(r.dropped_future for r in recalls.values())
            seen_docs = {h.doc_id for h in hits if h.doc_id}

            if ents.proposed_action and card.options:
                v = action_violations(ents.proposed_action, card.options)
                if v:
                    card.guardrail_events.append(
                        f"Proposed action {ents.proposed_action} blocked before recommendation: {v[0]}")

            if not card.options:
                card.rationale = ("No recommendation: consequences could not be simulated, and the agent does not "
                                  "estimate them. Provide the RPO id and the affected production run ids.")
                return self._finish(card, t0)

            # 4-6. decide (low effort, guarded)
            evidence = {
                "as_of": as_of, "report": report, "entities": card.entities, "scenario_state": card.scenario_state,
                "options": card.options, "simulator_best_action": card.simulator_best_action,
                "precedents": card.precedents, "open_commitments": card.open_commitments,
                "supplier_scorecard_recent": scorecard, "prior_events": events, "event_links": links,
                "memory_hits": [h.to_dict() for h in hits[:30]],
                "reflection": {"mode": refl.mode, "text": refl.text},
                "guardrail_prechecks": card.guardrail_events, "decision_taxonomy": list(TAXONOMY),
                "schema": sql_tools.schema_summary(con),
            }
            sub = self._decide(con, evidence, card, seen_docs, as_of, state)
            card.recommended_action = sub["recommended_action"]
            card.situation_summary = sub["situation_summary"]
            card.key_reasons = sub["key_reasons"]
            card.precedent_assessments = sub["precedent_assessments"]
            card.ungrounded_claims = sub["ungrounded_claims"]
            card.confidence = sub["confidence"]
            card.cited_doc_ids, card.cited_record_ids, card.unverifiable_citations = split_citations(
                con, sub["cited_doc_ids"], sub["cited_record_ids"], seen_docs)
            if not card.cited_doc_ids and not card.cited_record_ids:
                card.warnings.append("The recommendation cites no traceable evidence.")
            card.deviates_from_simulator_best = card.recommended_action != card.simulator_best_action

            # rationale (high effort), then number check
            card.rationale = self._rationale(card)
            extra = [v for p in card.precedents for v in (p.get("expected_cost"), p.get("actual_cost"))]
            bad = ungrounded_numbers(card.rationale, card.options, extra)
            if bad:
                card.warnings.append("Rationale quotes numbers not produced by the simulator or the database: "
                                     + ", ".join(bad))
        finally:
            con.close()

        # 7. optional write-back (own connection to the live DB)
        if writeback and card.recommended_action:
            from agent.log_decision import log_decision
            card.writeback, card.mock_actions = log_decision(self.settings, self.live_db_path, self.memory, card, state)
        return self._finish(card, t0)

    def _decide(self, con, evidence: dict, card: DecisionCard, seen_docs: set, as_of: str, state) -> dict:
        messages = [{"role": "user", "content": "Evidence pack (JSON):\n" + _j(evidence)
                     + "\n\nDecide. Use tools only if something is missing, then call submit_recommendation."}]
        rejections = 0
        for _ in range(MAX_STEPS):
            ts = time.monotonic()
            resp = self.llm.create(system=SYSTEM_DECIDE, messages=messages, tools=DECIDE_TOOLS,
                                   effort=self.settings.llm_structured_effort,
                                   temperature=self.settings.llm_temperature_structured)
            messages.append({"role": "assistant", "content": resp.content})
            uses = [b for b in resp.content if b.type == "tool_use"]
            self._trace(card.trace, "llm", "decide", f"turn {len(messages) // 2}", [u.name for u in uses], ts)
            if not uses:
                messages.append({"role": "user", "content": "Call submit_recommendation now."})
                continue
            results, accepted = [], None
            for u in uses:
                if u.name != "submit_recommendation":
                    results.append(self._tool_result(u, self._run_tool(con, u, as_of, state, seen_docs, card.trace)))
                    continue
                action = u.input["recommended_action"]
                violations = action_violations(action, card.options)
                if violations and rejections < MAX_REJECTIONS:
                    rejections += 1
                    card.guardrail_events.append(f"Rejected {action}: {violations[0]}")
                    results.append(self._tool_result(
                        u, "REJECTED by guardrail: " + "; ".join(violations) + ". Choose another action.", error=True))
                elif violations:
                    safe = best_option(card.options)
                    safe_action = safe["action"] if safe else None
                    card.guardrail_events.append(f"Override: {action} still violates ({violations[0]}); using "
                                                 f"{safe_action} (lowest risk-adjusted score without violations)")
                    accepted = {**u.input, "recommended_action": safe_action}
                    results.append(self._tool_result(u, "accepted with guardrail override"))
                else:
                    accepted = u.input
                    results.append(self._tool_result(u, "accepted"))
            messages.append({"role": "user", "content": results})
            if accepted:
                return accepted
        raise AgentError(f"no recommendation after {MAX_STEPS} model turns")

    def _rationale(self, card: DecisionCard) -> str:
        facts = {"as_of": card.as_of, "recommended_action": card.recommended_action,
                 "simulator_best_action": card.simulator_best_action, "situation_summary": card.situation_summary,
                 "key_reasons": card.key_reasons, "options": [o for o in card.options if o.get("feasible")],
                 "precedents": card.precedents, "precedent_assessments": card.precedent_assessments,
                 "open_commitments": card.open_commitments, "cited_doc_ids": card.cited_doc_ids,
                 "cited_record_ids": card.cited_record_ids, "ungrounded_claims": card.ungrounded_claims,
                 "guardrail_events": card.guardrail_events}
        ts = time.monotonic()
        resp = self.llm.create(system=SYSTEM_RATIONALE, messages=[{"role": "user", "content": _j(facts)}],
                               effort=self.settings.llm_rationale_effort,
                               temperature=self.settings.llm_temperature_rationale)
        out = "".join(b.text for b in resp.content if b.type == "text").strip()
        self._trace(card.trace, "llm", "rationale", "", out, ts)
        return out

    def _finish(self, card: DecisionCard, t0: float) -> DecisionCard:
        card.usage = self.llm.usage.to_dict()
        card.latency_s = round(time.monotonic() - t0, 2)
        return card

    # ---------------------------------------------------------------- QA mode (eval harness)
    def answer_question(self, question: str, as_of: str) -> QAResult:
        t0 = time.monotonic()
        self.llm.usage = Usage()
        res = QAResult(question=question, as_of=as_of)
        con = self._connect(as_of)
        try:
            try:
                rec = self.memory.recall(question, as_of)
            except Exception as ex:  # any client/network error
                raise MemoryUnavailable(f"Hindsight recall failed ({ex}); check HINDSIGHT_BASE_URL") from ex
            res.memory_hits, res.dropped_future_hits = len(rec.hits), rec.dropped_future
            seen = set(rec.doc_ids())
            messages = [{"role": "user", "content": f"as_of: {as_of}\nQuestion: {question}\n\nInitial memory recall "
                         f"(JSON):\n{_j(rec.to_payload())}\n\nSchema:\n{sql_tools.schema_summary(con)}"}]
            for _ in range(MAX_STEPS):
                ts = time.monotonic()
                resp = self.llm.create(system=SYSTEM_QA, messages=messages, tools=QA_TOOLS,
                                       effort=self.settings.llm_structured_effort,
                                       temperature=self.settings.llm_temperature_structured)
                messages.append({"role": "assistant", "content": resp.content})
                uses = [b for b in resp.content if b.type == "tool_use"]
                self._trace(res.trace, "llm", "qa", "", [u.name for u in uses], ts)
                if not uses:
                    messages.append({"role": "user", "content": "Call submit_answer now."})
                    continue
                results, final = [], None
                for u in uses:
                    if u.name == "submit_answer":
                        final = u.input
                        results.append(self._tool_result(u, "accepted"))
                    else:
                        results.append(self._tool_result(u, self._run_tool(con, u, as_of, None, seen, res.trace)))
                messages.append({"role": "user", "content": results})
                if final:
                    res.answer, res.confidence = final["answer"], final["confidence"]
                    res.cited_doc_ids, res.cited_record_ids, res.unverifiable_citations = split_citations(
                        con, final["cited_doc_ids"], final["cited_record_ids"], seen)
                    res.usage = self.llm.usage.to_dict()
                    res.latency_s = round(time.monotonic() - t0, 2)
                    return res
            raise AgentError(f"no answer after {MAX_STEPS} model turns")
        finally:
            con.close()


def make_synthesizer(llm, settings: Settings):
    """Local stand-in for Hindsight reflect when native reflect could see post-as_of memory."""
    def synthesize(question: str, hits, policy_text: str) -> str:
        resp = llm.create(system=policy_text + "\nSynthesize only from the memories given. Cite doc_ids in "
                                               "brackets. Say what is uncertain or missing.",
                          messages=[{"role": "user", "content": _j({"question": question,
                                                                    "memories": [h.to_dict() for h in hits[:40]]})}],
                          effort=settings.llm_structured_effort, temperature=settings.llm_temperature_structured)
        return "".join(b.text for b in resp.content if b.type == "text").strip()
    return synthesize


def build_agent(settings: Settings, *, live_db_path: Path | None = None, llm=None, memory=None) -> DecisionAgent:
    live = live_db_path or settings.live_db_path
    llm = llm or LLM(settings)
    if memory is None:
        memory = build_memory(settings, lambda: live_memory_ids(live), synthesizer=make_synthesizer(llm, settings))
    return DecisionAgent(settings, llm, memory, live)


if __name__ == "__main__":  # Stage 2 smoke: one hardcoded scenario end to end with the full trace
    from agent.config import load_settings
    from agent.scenarios import holdout_context, load_holdout
    from agent.setup_data import ensure_db

    s = load_settings()
    ensure_db(s)
    sc = load_holdout(s)["HS05"]
    card = build_agent(s, live_db_path=s.live_db_path.with_name("smoke_live.sqlite")).run(
        sc["day0_report"], as_of=sc["day0"], context=holdout_context(sc))
    for step in card.trace:
        print(f"[{step['kind']:>4}] {step['name']:<22} {step['ms']:>6} ms  in={step['input']}\n       out={step['output']}")
    print(json.dumps({k: card.to_dict()[k] for k in ("recommended_action", "simulator_best_action", "guardrail_events",
                                                     "cited_doc_ids", "cited_record_ids", "warnings", "usage",
                                                     "latency_s")}, indent=2))
    print("\nRATIONALE:\n" + card.rationale)
