"""run_all.py - build the whole extension.

    python run_all.py --input <v1.0.0 CSV folder | zip | sqlite> --output out --seed 20250905 --scale default
Options: --stages 0,1,2,3,4,5  --config config.yaml  --llm-narratives  --skip-validate
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from sc_common import build_ctx, load_config, log  # noqa: E402


def load(name):
    spec = importlib.util.spec_from_file_location(f"sc_{name}", HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


STAGES = {
    "0": ["profile"],
    "1": ["gen_structure", "gen_rm_ops"],
    "2": ["gen_demand"],
    "3": ["gen_events"],
    "4": ["gen_narratives", "gen_eval"],
    "5": ["finalize"],
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--scale", default="default", choices=["small", "default"])
    ap.add_argument("--config")
    ap.add_argument("--stages", default="0,1,2,3,4,5")
    ap.add_argument("--llm-narratives", action="store_true")
    ap.add_argument("--skip-validate", action="store_true")
    a = ap.parse_args()
    t0 = time.time()
    ctx = build_ctx(a.input, a.output, load_config(a.config, a.scale, a.seed))
    log(f"parquet engine: {ctx.parquet or 'none (CSV + SQLite only; pip install pyarrow for Parquet)'}")
    for s in a.stages.split(","):
        for mod in STAGES[s.strip()]:
            m = load(mod)
            if mod == "gen_narratives":
                m.run(ctx, use_llm=a.llm_narratives)
            else:
                m.run(ctx)
    if not a.skip_validate:
        load("validate").run(ctx)
    log(f"done in {time.time() - t0:.0f}s -> {Path(a.output).resolve()}")


if __name__ == "__main__":
    main()
