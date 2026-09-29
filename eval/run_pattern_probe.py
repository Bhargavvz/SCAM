"""Pattern-detection probe: for each planted pattern, ask a neutral question about its entity and check whether the
answer surfaces the pattern. Runs with the Mental Models and without them (MENTAL_MODELS_IN_REFLECT=0), so the score
is not just the answer key being read back."""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import replace
from pathlib import Path

import yaml

from agent.agent_core import build_agent
from agent.capabilities import ask
from agent.config import ROOT, load_settings
from agent.setup_data import ensure_db
from eval.scorecard import write_scorecard

PROBES = Path(__file__).with_name("pattern_probe.yaml")


def load_probes() -> list[dict]:
    return yaml.safe_load(PROBES.read_text())


def detected(text: str, rules: list[str]) -> bool:
    return all(re.search(r, text, re.I) for r in rules)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mental-models", choices=["on", "off", "both"], default="both")
    ap.add_argument("--out", default=str(ROOT / "eval" / "out"))
    a = ap.parse_args(argv)
    base = load_settings()
    ensure_db(base)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    results = out / "pattern_results.jsonl"
    results.write_text("")
    modes = {"on": [True], "off": [False], "both": [True, False]}[a.mental_models]
    for flag in modes:
        live = out / "pattern_live.sqlite"
        live.unlink(missing_ok=True)
        try:
            agent = build_agent(replace(base, mental_models_in_reflect=flag), live_db_path=live)
        except ValueError as ex:  # client cannot exclude mental models: say so instead of mislabelling results
            print(f"mental_models={flag}: skipped - {ex}")
            continue
        for p in load_probes():
            row = {"pattern_id": p["id"], "mental_models": flag, "question": p["question"]}
            try:
                ans = ask(agent, p["question"], base.default_as_of)
            except Exception as ex:  # harness: record and continue
                row.update(detected=False, error=f"{type(ex).__name__}: {ex}")
            else:
                text = ans.answer + " " + json.dumps(ans.structured or {})
                row.update(capability=ans.capability, reflect_mode=ans.reflect_mode, detected=detected(text, p["rules"]),
                           answer=ans.answer, cited_doc_ids=ans.cited_doc_ids, cited_record_ids=ans.cited_record_ids,
                           latency_s=ans.latency_s,
                           error=None if ans.reflect_mode == "native" else
                           "native reflect unavailable (runtime retains in the bank) - Mental Models not consulted")
            with results.open("a") as fh:
                fh.write(json.dumps(row) + "\n")
            print(f"{p['id']} mental_models={flag} detected={row['detected']} {row.get('error') or ''}")
    print(f"scorecard: {write_scorecard(out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
