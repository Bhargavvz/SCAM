"""Run the whole pipeline: profile -> structure -> RM ops -> demand -> events -> narratives -> eval -> package -> validate.

python run_all.py --input data/source --output output --seed 20260928 --scale small|full [--llm-narratives] [--from STAGE]
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import time

import pandas as pd

import gen_demand
import gen_eval
import gen_events
import gen_narratives
import gen_rm_ops
import gen_structure
import profile as stage0
import validate
from common import banner, cli, source_sqlite_path

STAGES = ["profile", "structure", "rm_ops", "demand", "events", "narratives", "eval", "package", "validate"]

DESC = {
    "plants": "Our manufacturing plants (fictional).",
    "products_ext": "Extension of products: make/buy, plant, brand family, ABC class, price and margin.",
    "raw_materials": "Raw-material master (archetypes chosen per source product category).",
    "bill_of_materials": "Versioned BOM lines: RM qty per FG unit, scrap, validity window.",
    "rm_supplier_catalog": "RM x supplier x price-validity segment: rank, allocation, lead time, MOQ, price, contract.",
    "contracts": "Supplier contracts (terms, min volume, penalties, force majeure).",
    "rm_inventory_opening_balances": "RM opening stock per RM x plant at the simulation start.",
    "rm_purchase_orders": "RM purchase orders (FG PO columns + promised_at; plant instead of warehouse).",
    "rm_purchase_order_lines": "RM PO lines.",
    "rm_po_revisions": "Supplier ETA revisions and expedites on RM POs.",
    "rm_inventory_movements": "Signed RM stock movements (receipt, consumption, adjustment, transfer, scrap).",
    "rm_inventory_snapshots_weekly": "Sunday RM snapshots; on_hand = opening + cumulative movements.",
    "production_runs": "One run per make-product FG PO line (received or open); delay and RM-shortage cause.",
    "product_demand_weekly": "Sparse weekly demand/forecast per product x warehouse; fulfilled = existing sales exactly.",
    "disruption_events": "Disruptions, anchored in existing data or in the RM-side simulation.",
    "event_impacts": "Quantified impacts of each event on entities.",
    "event_links": "Event graph: caused, similar_to, recurrence_of, mitigated_by, superseded_by.",
    "negotiations": "Supplier negotiations (asks, offers, concessions, final terms).",
    "decisions": "Decisions: options, rationale, expected vs actual outcome, lesson, footprint.",
    "commitments": "Commitments made by suppliers, plants and planning, and how they resolved.",
    "supplier_scorecard_monthly": "Monthly supplier scorecard computed from FG + RM PO tables and commitments.",
}
KEYS = {"plants": "plant_id", "products_ext": "product_id", "raw_materials": "rm_id", "bill_of_materials": "bom_id",
        "rm_supplier_catalog": "catalog_line_id", "contracts": "contract_id", "rm_inventory_opening_balances": "rm_opening_balance_id",
        "rm_purchase_orders": "rm_po_id", "rm_purchase_order_lines": "rm_purchase_order_line_id", "rm_po_revisions": "rm_po_revision_id",
        "rm_inventory_movements": "rm_movement_id", "production_runs": "production_run_id", "disruption_events": "event_id",
        "negotiations": "negotiation_id", "decisions": "decision_id", "commitments": "commitment_id"}
FKS = [("products_ext.product_id", "products"), ("products_ext.plant_id", "plants"), ("bill_of_materials.product_id", "products"),
       ("bill_of_materials.rm_id", "raw_materials"), ("rm_supplier_catalog.rm_id", "raw_materials"), ("rm_supplier_catalog.supplier_id", "suppliers"),
       ("rm_supplier_catalog.contract_id", "contracts"), ("contracts.supplier_id", "suppliers"),
       ("rm_inventory_opening_balances.rm_id/plant_id", "raw_materials / plants"), ("rm_purchase_orders.supplier_id", "suppliers"),
       ("rm_purchase_orders.plant_id", "plants"), ("rm_purchase_order_lines.rm_po_id", "rm_purchase_orders"),
       ("rm_purchase_order_lines.rm_id", "raw_materials"), ("rm_po_revisions.rm_po_id", "rm_purchase_orders"),
       ("rm_inventory_movements.rm_purchase_order_line_id", "rm_purchase_order_lines"), ("rm_inventory_movements.production_run_id", "production_runs"),
       ("rm_inventory_snapshots_weekly.rm_id/plant_id", "raw_materials / plants"), ("production_runs.purchase_order_line_id", "purchase_order_lines"),
       ("production_runs.product_id", "products"), ("production_runs.plant_id", "plants"), ("production_runs.rm_shortage_rm_id", "raw_materials"),
       ("product_demand_weekly.product_id/warehouse_id", "products / warehouses"), ("product_demand_weekly.demand_shock_event_id", "disruption_events"),
       ("event_impacts.event_id", "disruption_events"), ("event_links.src_event_id/dst_event_id", "disruption_events"),
       ("decisions.event_id", "disruption_events"), ("commitments.decision_id", "decisions"), ("commitments.negotiation_id", "negotiations"),
       ("negotiations.supplier_id", "suppliers"), ("negotiations.contract_id", "contracts"), ("supplier_scorecard_monthly.supplier_id", "suppliers")]


def package(ctx):
    banner("PACKAGE - SQLite copy + README")
    src_db = source_sqlite_path(ctx.input)
    dst = ctx.output / "inventory-supply-chain-v1.0.0-extended.sqlite"
    if src_db:
        shutil.copyfile(src_db, dst)
        con = sqlite3.connect(dst)
        for p in sorted((ctx.output / "tables" / "parquet").glob("*.parquet")):
            df = pd.read_parquet(p)
            for c in df.columns:
                if pd.api.types.is_datetime64_any_dtype(df[c]):
                    df[c] = df[c].dt.strftime("%Y-%m-%d")
            df.to_sql(p.stem, con, index=False, if_exists="replace")
        con.commit()
        con.close()
        print(f"  wrote {dst.name}")
    cal = ctx.load_json("calibration.json")
    L = ["# Supply Chain Memory & Decision Agent - extended dataset", "",
         "Additive extension of the **Inventory & Supply Chain Dataset v1.0.0** with raw-material, production, demand,",
         "institutional-memory, narrative and evaluation data. The original tables are untouched (row counts + SHA-256 verified by `validate.py`).",
         "", "## Attribution", "",
         'Source data: Analytics Engineering, "Inventory & Supply Chain Dataset v1.0.0",',
         "https://www.analyticsengineering.com/datasets/inventory-supply-chain - licensed CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/).",
         "This extension is released under CC BY 4.0. All content is synthetic and fictional; no real companies or people; no PII.", "",
         "## Files", "",
         "- `tables/csv/*.csv`, `tables/parquet/*.parquet` - one file per new table",
         "- `inventory-supply-chain-v1.0.0-extended.sqlite` - copy of the v1.0.0 SQLite with the new tables appended "
         "(new tables store NULL; source tables keep their original '' blanks)",
         "- `memory_corpus.jsonl` - narrative docs, one Hindsight retain item per line (split=memory is ingested; split=holdout = Q4-2025)",
         "- `eval_questions.jsonl`, `holdout_scenarios.json`, `planted_patterns.json`, `validation_report.json`", "",
         "## Key modelling choices", ""]
    L += [f"- {a}" for a in cal["assumptions"]]
    L += ["- RM-side delays are causal: P(late) = 1 - reliability_score, slip ~ Gamma scaled by lead_time_days, plus country shocks and planted patterns.",
          "- Cascade back-fill: ~12% of late/partial make-product FG lines get an upstream chain "
          "(RM ETA revision -> RM below safety -> stockout -> blocked run -> the existing late FG receipt).",
          "- Decision cost = action cost + stockout days x (avg daily RM usage x price x `shortage_value_multiple`) + spot-buy premiums; "
          "outcome labels compare realised vs estimated.",
          "- Extra columns beyond the brief: `catalog_line_id`, `rm_po_revision_id` (keys), decisions.`action_cost`/`context_dependent`/`context_factors`, "
          "corpus `supersedes_doc_ids`/`author_role`/`event_id`.",
          "", "## Table relationships", ""]
    L += [f"- `{a}` -> `{b}`" for a, b in FKS]
    L += ["", "## Data dictionary", ""]
    for p in sorted((ctx.output / "tables" / "parquet").glob("*.parquet")):
        df = pd.read_parquet(p)
        L += [f"### `{p.stem}`", "", DESC.get(p.stem, ""), "", f"Rows: {len(df):,}", "",
              "| Column | What it means | Data type | Missing values | Key |", "|---|---|---|---|---|"]
        for c in df.columns:
            key = "PK" if KEYS.get(p.stem) == c else ("FK" if c.endswith("_id") else "")
            L.append(f"| `{c}` | {c.replace('_', ' ')} | {df[c].dtype} | {int(df[c].isna().sum()):,} | {key} |")
        L.append("")
    (ctx.output / "README.md").write_text("\n".join(L), encoding="utf-8")
    print("  wrote README.md")


def main():
    ctx = cli(__doc__)
    llm = "--llm-narratives" in sys.argv
    start = sys.argv[sys.argv.index("--from") + 1] if "--from" in sys.argv else "profile"
    fns = {"profile": stage0.run, "structure": gen_structure.run, "rm_ops": gen_rm_ops.run, "demand": gen_demand.run,
           "events": gen_events.run, "narratives": lambda c: gen_narratives.run(c, llm=llm), "eval": gen_eval.run,
           "package": package, "validate": validate.run}
    t0 = time.time()
    for s in STAGES[STAGES.index(start):]:
        if s == "narratives":
            (ctx.work / "holdout_plan.json").unlink(missing_ok=True)
        t = time.time()
        ok = fns[s](ctx)
        print(f"  [{s}] {time.time() - t:.0f}s", flush=True)
        ctx._cache = {k: v for k, v in ctx._cache.items() if k == "src"}
        if s == "validate" and ok is False:
            print(f"total {time.time() - t0:.0f}s")
            sys.exit(1)
    print(f"total {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
