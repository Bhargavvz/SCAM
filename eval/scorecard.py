"""Aggregate eval results into scorecard.json + scorecard.md."""
from __future__ import annotations

import json
import statistics
from collections import defaultdict
from pathlib import Path


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 3) if xs else None


def _cost(rows):
    costs = [(r.get("usage") or {}).get("cost_usd") for r in rows]
    known = [c for c in costs if c is not None]
    return {"mean_usd": _mean(known), "total_usd": round(sum(known), 4) if known else None}


def _latency(rows):
    lat = [r["latency_s"] for r in rows if r.get("latency_s") is not None]
    return {"mean_s": _mean(lat), "p50_s": round(statistics.median(lat), 2) if lat else None,
            "max_s": max(lat) if lat else None}


def _tokens(rows):
    return {"mean_input": _mean([(r.get("usage") or {}).get("input_tokens") for r in rows]),
            "mean_output": _mean([(r.get("usage") or {}).get("output_tokens") for r in rows])}


def summarize_questions(rows: list[dict]) -> dict:
    by = defaultdict(list)
    for r in rows:
        by[r["type"]].append(r)
    hops = defaultdict(list)
    for r in rows:
        hops[str(r.get("hop_count"))].append(r)
    return {
        "n": len(rows), "accuracy": _mean([float(r["correct"]) for r in rows]),
        "by_type": {t: {"n": len(rs), "accuracy": _mean([float(r["correct"]) for r in rs]),
                        "mean_score": _mean([r["score"] for r in rs])} for t, rs in sorted(by.items())},
        "by_hop_count": {h: {"n": len(rs), "accuracy": _mean([float(r["correct"]) for r in rs])}
                         for h, rs in sorted(hops.items())},
        "single_hop_fact_recall_accuracy": _mean([float(r["correct"]) for r in rows
                                                  if r["type"] == "fact_recall" and r.get("hop_count") == 1]),
        "citations": {k: _mean([r.get(k) for r in rows]) for k in
                      ("doc_precision", "doc_recall", "record_precision", "record_recall")},
        "future_doc_citations": sum(len(r.get("future_doc_citations") or []) for r in rows),
        "errors": sum(1 for r in rows if r.get("error")),
        "latency": _latency(rows), "tokens": _tokens(rows), "cost": _cost(rows),
    }


def summarize_holdout(rows: list[dict]) -> dict:
    traps = [r for r in rows if r.get("trap")]
    return {
        "n": len(rows), "accuracy": _mean([float(r["correct"]) for r in rows]),
        "trap_n": len(traps), "trap_accuracy": _mean([float(r["trap_pass"]) for r in traps]),
        "trap_results": {r["scenario_id"]: "PASS" if r["trap_pass"] else "FAIL" for r in traps},
        "precedent_recall": _mean([r.get("precedent_recall") for r in rows]),
        "commitments_recall": _mean([r.get("commitments_recall") for r in rows]),
        "simulator_parity_ok": sum(1 for r in rows if not r.get("parity_mismatches")),
        "errors": sum(1 for r in rows if r.get("error")),
        "latency": _latency(rows), "tokens": _tokens(rows), "cost": _cost(rows),
        "per_scenario": [{k: r.get(k) for k in ("scenario_id", "gold", "chosen", "correct", "trap", "trap_pass",
                                                "precedent_recall", "commitments_recall", "latency_s")} for r in rows],
    }


def summarize_patterns(rows: list[dict]) -> dict:
    per = defaultdict(dict)
    for r in rows:
        per[r["pattern_id"]]["on" if r["mental_models"] else "off"] = r["detected"]
    return {"per_pattern": dict(sorted(per.items())),
            "detected_with_mental_models": sum(1 for v in per.values() if v.get("on")),
            "detected_without_mental_models": sum(1 for v in per.values() if v.get("off"))}


def _read(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()] if path.exists() else []


def _question_section(title: str, s: dict) -> list[str]:
    lines = [f"## {title} (n={s['n']}, accuracy {s['accuracy']}, single-hop fact recall "
             f"{s['single_hop_fact_recall_accuracy']}, errors {s['errors']}, future-dated doc citations "
             f"{s['future_doc_citations']})", "",
             "| type | n | accuracy | mean key-fact score |", "|---|---|---|---|"]
    lines += [f"| {t} | {v['n']} | {v['accuracy']} | {v['mean_score']} |" for t, v in s["by_type"].items()]
    lines += ["", "| hop_count | n | accuracy |", "|---|---|---|"]
    lines += [f"| {h} | {v['n']} | {v['accuracy']} |" for h, v in s["by_hop_count"].items()]
    c = s["citations"]
    lines += ["", f"Citation precision/recall - docs {c['doc_precision']}/{c['doc_recall']}, records "
                  f"{c['record_precision']}/{c['record_recall']}",
              f"Latency {s['latency']} | tokens {s['tokens']} | cost {s['cost']}", ""]
    return lines


def write_scorecard(out_dir: Path) -> Path:
    q, h = _read(out_dir / "questions_results.jsonl"), _read(out_dir / "holdout_results.jsonl")
    qh, p = _read(out_dir / "questions_results_hindsight.jsonl"), _read(out_dir / "pattern_results.jsonl")
    data = {"questions": summarize_questions(q) if q else None,
            "questions_hindsight_mode": summarize_questions(qh) if qh else None,
            "holdout": summarize_holdout(h) if h else None,
            "patterns": summarize_patterns(p) if p else None}
    (out_dir / "scorecard.json").write_text(json.dumps(data, indent=2))
    lines = ["# Scorecard", ""]
    if data["questions"]:
        lines += _question_section("Eval questions - agent mode", data["questions"])
    if data["questions_hindsight_mode"]:
        lines += _question_section("Eval questions - hindsight mode (recall/reflect only)",
                                   data["questions_hindsight_mode"])
    if data["patterns"]:
        s = data["patterns"]
        lines += [f"## Pattern detection ({s['detected_with_mental_models']}/12 with Mental Models, "
                  f"{s['detected_without_mental_models']}/12 without)", "",
                  "| pattern | with Mental Models | without |", "|---|---|---|"]
        lines += [f"| {pid} | {v.get('on')} | {v.get('off')} |" for pid, v in s["per_pattern"].items()]
        lines.append("")
    if data["holdout"]:
        s = data["holdout"]
        lines += [f"## Holdout scenarios (n={s['n']}, accuracy {s['accuracy']}, trap accuracy {s['trap_accuracy']} "
                  f"on {s['trap_n']}, simulator parity {s['simulator_parity_ok']}/{s['n']}, errors {s['errors']})", "",
                  "| scenario | gold | chosen | correct | trap | trap pass | precedent recall | commitments recall | latency s |",
                  "|---|---|---|---|---|---|---|---|---|"]
        lines += [f"| {r['scenario_id']} | {r['gold']} | {r['chosen']} | {r['correct']} | {r['trap']} | {r['trap_pass']} | "
                  f"{r['precedent_recall']} | {r['commitments_recall']} | {r['latency_s']} |" for r in s["per_scenario"]]
        lines += ["", f"Latency {s['latency']} | tokens {s['tokens']} | cost {s['cost']}"]
    md = out_dir / "scorecard.md"
    md.write_text("\n".join(lines) + "\n")
    return md
