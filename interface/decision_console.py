"""CLI decision console: a free-text disruption report in, a decision card out.

Also answers history questions (--question, QA mode) and any supply-chain question routed to one of the
seven capabilities (--ask)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from agent.card import DecisionCard, QAResult


def _money(v):
    return "-" if v is None else f"${v:,.0f}"


def render_card(card: DecisionCard, console: Console, show_trace: bool = False) -> None:
    console.print(Panel(f"{card.report}\n\n[dim]as_of {card.as_of} | run {card.run_id}[/]", title="Disruption report"))
    if card.situation_summary:
        console.print(Panel(card.situation_summary, title="Situation"))

    t = Table(title="Candidate options (deterministic simulator)  ★ recommended  ▲ simulator best")
    for col in ("", "Action", "Arrival", "Stockout days", "Cost", "Risk", "Score", "Service impact (units)",
                "Breaches", "Note"):
        t.add_column(col)
    for o in card.options:
        mark = ("★" if o["action"] == card.recommended_action else "") + ("▲" if o["action"] == card.simulator_best_action else "")
        if o.get("feasible"):
            t.add_row(mark, o["action"], o["arrival"] or "-", str(o["stockout_days"]), _money(o["cost"]), o["risk"],
                      _money(o["score"]), str(o["service_impact_units"]), ", ".join(o["breaches_commitments"]) or "-",
                      o.get("note", ""))
        else:
            t.add_row(mark, f"[dim]{o['action']}[/]", "-", "-", "-", "-", "-", "-", "-", f"[dim]{o.get('note', '')}[/]")
    console.print(t)

    if card.precedents:
        p = Table(title="Precedents (structured decisions, scored against today's options)")
        for col in ("Decision", "Event", "Decided", "Action", "Outcome", "Today rank", "Applies today?", "Note"):
            p.add_column(col)
        for x in card.precedents:
            applies = "[green]yes[/]" if x["applies_today"] else "[red]no - misleading if copied[/]"
            p.add_row(x["decision_id"], x.get("event_id") or "-", x["decided_at"], x["decision_type"],
                      x.get("outcome_label") or "unknown yet", str(x.get("today_rank") or "-"), applies, x["note"])
        console.print(p)

    if card.open_commitments:
        c = Table(title="Open commitments")
        for col in ("Commitment", "Counterparty", "Due", "Text", "Affected by"):
            c.add_column(col)
        for x in card.open_commitments:
            hit = ", ".join(x.get("affected_by") or [])
            c.add_row(x["commitment_id"], x["counterparty_id"], x["due_date"], x["commitment_text"],
                      f"[red]{hit}[/]" if hit else "-")
        console.print(c)

    if card.guardrail_events:
        console.print(Panel("\n".join(card.guardrail_events), title="Guardrails", border_style="red"))

    rec = card.recommended_action or "[yellow]no recommendation[/]"
    extra = ("\n[yellow]Deviates from the simulator's lowest risk-adjusted option "
             f"({card.simulator_best_action}); see rationale.[/]") if card.deviates_from_simulator_best and card.recommended_action else ""
    console.print(Panel(f"[bold]{rec}[/]  (confidence: {card.confidence}){extra}\n\n{card.rationale}",
                        title="Recommendation", border_style="green"))

    cites = f"Docs: {', '.join(card.cited_doc_ids) or '-'}\nRecords: {', '.join(card.cited_record_ids) or '-'}"
    if card.unverifiable_citations:
        cites += f"\n[yellow]Unverifiable (dropped): {', '.join(card.unverifiable_citations)}[/]"
    if card.ungrounded_claims:
        cites += "\n[yellow]Ungrounded claims: " + "; ".join(card.ungrounded_claims) + "[/]"
    console.print(Panel(cites, title="Evidence"))

    if card.warnings:
        console.print(Panel("\n".join(card.warnings), title="Warnings", border_style="yellow"))
    if card.writeback or card.mock_actions:
        body = json.dumps(card.writeback, indent=1) if card.writeback else ""
        console.print(Panel(body + "\n" + "\n".join(card.mock_actions), title="Write-back / initiated response"))
    u = card.usage or {}
    console.print(f"[dim]memory hits {card.memory_hits} (dropped as future: {card.dropped_future_hits}) | reflect "
                  f"{card.reflect_mode} | LLM calls {u.get('calls')} | tokens in/out {u.get('input_tokens')}/"
                  f"{u.get('output_tokens')} | cost ${u.get('cost_usd')} | {card.latency_s}s[/]")
    if show_trace:
        tr = Table(title="Trace")
        for col in ("Kind", "Name", "ms", "Input", "Output"):
            tr.add_column(col)
        for s in card.trace:
            tr.add_row(s["kind"], s["name"], str(s["ms"]), s["input"], s["output"])
        console.print(tr)


def render_answer(res: QAResult, console: Console) -> None:
    body = (f"{res.answer}\n\nDocs: {', '.join(res.cited_doc_ids) or '-'}\nRecords: "
            f"{', '.join(res.cited_record_ids) or '-'}\n[dim]confidence {res.confidence} | as_of {res.as_of}[/]")
    if res.unverifiable_citations:
        body += f"\n[yellow]Unverifiable (dropped): {', '.join(res.unverifiable_citations)}[/]"
    console.print(Panel(body, title=res.question))


def render_capability(ans, console: Console) -> None:
    head = (f"[bold]{ans.capability}[/] | budget {ans.budget} | reflect {ans.reflect_mode} | as_of {ans.as_of} | "
            f"confidence {ans.confidence}")
    console.print(Panel(f"{head}\n\n{ans.answer}", title=ans.question))
    if ans.structured:
        console.print(Panel(json.dumps(ans.structured, indent=1), title="Structured output"))
    for name, rows in ans.ground_truth.items():
        t = Table(title=f"{name} (database, as of {ans.as_of})")
        cols = list(rows[0]) if rows else ["(no rows)"]
        for c in cols:
            t.add_column(c)
        for r in rows[:15]:
            t.add_row(*[str(r.get(c)) for c in cols])
        console.print(t)
    checked = {True: "[green]checked[/]", False: "[red]NOT checked[/]", None: "n/a"}[ans.commitments_checked]
    cites = (f"Docs: {', '.join(ans.cited_doc_ids) or '-'}\nRecords: {', '.join(ans.cited_record_ids) or '-'}\n"
             f"Open commitments: {checked}")
    if ans.unverifiable_citations:
        cites += f"\n[yellow]Unverifiable: {', '.join(ans.unverifiable_citations)}[/]"
    console.print(Panel(cites, title="Evidence"))
    if ans.warnings:
        console.print(Panel("\n".join(ans.warnings), title="Warnings", border_style="yellow"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Supply Chain Memory & Decision Agent console")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--report", help="free-text disruption report")
    src.add_argument("--report-file", help="file containing the report")
    src.add_argument("--scenario", help="holdout scenario id, e.g. HS05")
    src.add_argument("--question", help="ask a history question (QA mode; needs --as-of)")
    src.add_argument("--ask", help="ask any supply-chain question (routed to one of 7 capabilities)")
    ap.add_argument("--as-of", help="YYYY-MM-DD; defaults to the report date, then DEFAULT_AS_OF")
    ap.add_argument("--rpo", help="RM purchase order id if not in the report")
    ap.add_argument("--runs", help="comma-separated affected production run ids (first = the run that needs the RM)")
    ap.add_argument("--rm", help="raw material id if the PO has several")
    ap.add_argument("--writeback", action="store_true", help="log decision + commitment and retain a summary")
    ap.add_argument("--live-db", help="live DB path (default LIVE_DB_PATH)")
    ap.add_argument("--trace", action="store_true", help="print the full tool / LLM trace")
    ap.add_argument("--json-out", help="also write the card as JSON")
    a = ap.parse_args(argv)
    if a.question and not a.as_of:
        ap.error("--question needs --as-of")

    from agent import agent_core
    from agent.agent_core import AgentError
    from agent.llm import LLMRefusal, LLMUnavailable
    from agent.config import load_settings
    from agent.scenarios import holdout_context, load_holdout
    from agent.setup_data import ensure_db

    settings = load_settings()
    ensure_db(settings)
    agent = agent_core.build_agent(settings, live_db_path=Path(a.live_db) if a.live_db else None)
    console = Console()
    try:
        if a.ask:
            from agent.capabilities import ask
            from agent.parsing import parse_report

            if parse_report(a.ask).rpo_ids:  # a disruption report: run the simulated decision loop instead
                card = agent.run(a.ask, as_of=a.as_of, writeback=a.writeback)
                render_card(card, console, show_trace=a.trace)
                out = card.to_dict()
            else:
                ans = ask(agent, a.ask, a.as_of)
                render_capability(ans, console)
                out = ans.to_dict()
        elif a.question:
            res = agent.answer_question(a.question, a.as_of)
            render_answer(res, console)
            out = res.to_dict()
        else:
            if a.scenario:
                s = load_holdout(settings)[a.scenario]
                report, as_of, ctx = s["day0_report"], a.as_of or s["day0"], holdout_context(s)
            else:
                report = a.report or Path(a.report_file).read_text()
                as_of = a.as_of
                ctx = {k: v for k, v in {"rpo_id": a.rpo, "rm_id": a.rm,
                                         "run_ids": a.runs.split(",") if a.runs else None}.items() if v}
            card = agent.run(report, as_of=as_of, context=ctx, writeback=a.writeback)
            render_card(card, console, show_trace=a.trace)
            out = card.to_dict()
    except (AgentError, LLMRefusal, LLMUnavailable) as ex:
        console.print(f"[red]{type(ex).__name__}:[/] {ex}")
        return 1
    if a.json_out:
        Path(a.json_out).write_text(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
