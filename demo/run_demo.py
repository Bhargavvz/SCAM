"""Scripted walkthrough: runs each demo scenario, renders it, checks expected vs actual."""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import replace
from pathlib import Path

import yaml
from rich.console import Console

DEMO_DIR = Path(__file__).resolve().parent
_DECLINE = re.compile(r"no record|no evidence|not found|no documented|no (?:such|matching)|could not find|"
                      r"does not (?:contain|show)|nothing in (?:memory|the records)", re.I)


def check_expectations(spec: dict, result: dict) -> list[dict]:
    card, ans, cap = result.get("card") or {}, result.get("answer") or {}, result.get("capability") or {}
    rec = card.get("recommended_action")
    out = []

    def add(name, expected, actual, ok):
        out.append({"check": name, "expected": expected, "actual": actual, "ok": bool(ok)})

    for k, v in spec.items():
        if k == "recommended_action":
            add(k, v, rec, rec == v)
        elif k == "recommended_action_in":
            add(k, v, rec, rec in v)
        elif k == "not_action":
            add(k, f"not {v}", rec, rec is not None and rec != v)
        elif k == "guardrail_mentions":
            ev = card.get("guardrail_events") or []
            add(k, v, ev, any(v in e for e in ev))
        elif k == "commitments_flagged":
            got = sorted(c["commitment_id"] for c in card.get("open_commitments") or [])
            add(k, v, got, set(v) <= set(got))
        elif k == "precedent_not_applicable":
            ps = card.get("precedents") or []
            add(k, f"{v} precedent marked not applicable", [(p["decision_type"], p["applies_today"]) for p in ps],
                any(p["decision_type"] == v and not p["applies_today"] for p in ps))
        elif k == "answer_contains":
            add(k, v, ans.get("answer"), all(x in (ans.get("answer") or "") for x in v))
        elif k == "cites_doc":
            add(k, v, ans.get("cited_doc_ids"), v in (ans.get("cited_doc_ids") or []))
        elif k == "second_has_live_precedent":
            ps = (result.get("cards") or [{}, {}])[1].get("precedents") or []
            add(k, v, [p.get("decision_id") for p in ps if p.get("source") == "live"],
                any(p.get("source") == "live" for p in ps) == v)
        elif k == "capability":
            add(k, v, cap.get("capability"), cap.get("capability") == v)
        elif k == "answer_matches":
            text = cap.get("answer") or ""
            add(k, v, text[:120], all(re.search(p, text, re.I) for p in v))
        elif k == "has_citations":
            n = len(cap.get("cited_doc_ids") or []) + len(cap.get("cited_record_ids") or [])
            add(k, v, n, (n > 0) == v)
        elif k == "commitments_checked":
            add(k, v, cap.get("commitments_checked"), cap.get("commitments_checked") == v)
        elif k == "grounded_or_declines":
            n = len(cap.get("cited_doc_ids") or []) + len(cap.get("cited_record_ids") or [])
            declines = bool(_DECLINE.search(cap.get("answer") or ""))
            add(k, "cites evidence or says there is no record", {"citations": n, "declines": declines},
                (n > 0 or declines) == v)
        else:
            add(k, v, "unknown check", False)
    return out


def load_specs() -> list[dict]:
    return yaml.safe_load((DEMO_DIR / "scenarios.yaml").read_text())


def run_spec(agent, spec: dict, holdout: dict) -> dict:
    """Run one demo scenario; returns {"card"|"answer"|"capability"|"cards": ...} (or {"error": ...})."""
    from agent.capabilities import ask
    from agent.scenarios import holdout_context

    try:
        if spec["kind"] == "holdout":
            s = holdout[spec["scenario"]]
            return {"card": agent.run(s["day0_report"], as_of=s["day0"], context=holdout_context(s)).to_dict()}
        if spec["kind"] == "report":
            return {"card": agent.run(spec["report"], as_of=spec["as_of"], context=spec.get("context")).to_dict()}
        if spec["kind"] == "question":
            return {"answer": agent.answer_question(spec["question"], spec["as_of"]).to_dict()}
        if spec["kind"] == "ask":
            return {"capability": ask(agent, spec["question"], spec.get("as_of")).to_dict()}
        cards = []  # chain: first step writes back, the next one should see it
        for i, sid in enumerate(spec["steps"]):
            s = holdout[sid]
            cards.append(agent.run(s["day0_report"], as_of=s["day0"], context=holdout_context(s),
                                   writeback=(i == 0)).to_dict())
        return {"cards": cards}
    except Exception as ex:  # demo harness: the failure is the actual result
        return {"error": f"{type(ex).__name__}: {ex}"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", nargs="*", help="demo ids to run, e.g. D2 D3")
    ap.add_argument("--no-retain", action="store_true", help="write live rows but do not retain into Hindsight")
    a = ap.parse_args(argv)

    from agent.agent_core import build_agent
    from agent.capabilities import CapabilityAnswer
    from agent.card import DecisionCard, QAResult
    from agent.config import load_settings
    from agent.scenarios import load_holdout
    from agent.setup_data import ensure_db
    from interface.decision_console import render_answer, render_capability, render_card

    settings = load_settings()
    if a.no_retain:
        settings = replace(settings, writeback_retain=False)
    ensure_db(settings)
    out = DEMO_DIR / "out"
    out.mkdir(exist_ok=True)
    live = out / "live_demo.sqlite"
    live.unlink(missing_ok=True)  # every demo run starts from an empty live DB
    agent = build_agent(settings, live_db_path=live)
    holdout = load_holdout(settings)
    console = Console(record=True, width=170)
    specs = [s for s in load_specs() if not a.only or s["id"] in a.only]
    report = ["# Demo walkthrough - expected vs actual", ""]
    all_ok = True
    for spec in specs:
        console.rule(f"{spec['id']} - {spec['title']}")
        result = run_spec(agent, spec, holdout)
        if "error" in result:
            console.print(f"[red]{spec['id']} failed: {result['error']}[/]")
        for c in [result.get("card"), *(result.get("cards") or [])]:
            if c:
                render_card(DecisionCard(**c), console)
        if result.get("answer"):
            render_answer(QAResult(**result["answer"]), console)
        if result.get("capability"):
            render_capability(CapabilityAnswer(**result["capability"]), console)
        (out / f"{spec['id']}.json").write_text(json.dumps(result, indent=2, default=str))
        checks = check_expectations(spec["expect"], result)
        all_ok &= all(c["ok"] for c in checks)
        report += [f"## {spec['id']} - {spec['title']}", ""]
        if "error" in result:
            report += [f"**Error:** {result['error']}", ""]
        report += ["| check | expected | actual | result |", "|---|---|---|---|"]
        report += [f"| {c['check']} | {c['expected']} | {c['actual']} | {'PASS' if c['ok'] else 'FAIL'} |" for c in checks]
        report.append("")
    (out / "demo_report.md").write_text("\n".join(report))
    (out / "demo_console.txt").write_text(console.export_text())
    print(f"demo report: {out / 'demo_report.md'} ({'all checks passed' if all_ok else 'some checks FAILED'})")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
