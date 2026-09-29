"""Supply Chain Memory & Decision Agent - minimal prototype (disruption type: RM supplier delay).

Core loop: parse report -> recall() history from Hindsight Cloud -> read-only DB checks (open commitments,
scorecard, stock) -> simulate 3 actions with deterministic formulas on historical rates -> recommend with
cited memory ids + DB record ids -> decision card. Numbers never come from the LLM.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

from queries import DEFAULT, LIMIT, QUERIES, run_query

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

BASE_URL = os.environ.get("HINDSIGHT_BASE_URL", "https://api.hindsight.vectorize.io")
BANK_ID = os.environ.get("HINDSIGHT_BANK_ID", "scm-memory-conv")
DB_PATH = Path(os.environ.get("SCM_DB_PATH", ROOT / "output" / "inventory-supply-chain-v1.0.0-extended.sqlite"))
USAGE_LOG = ROOT / "logs" / "hindsight_usage.jsonl"

# fixed business constants (documented in README; same values as the dataset's config.yaml cost model)
SERVICE_PENALTY_RATE = 0.10     # a production day lost to an RM stockout costs 10% of that day's FG contribution (recovery freight, penalties)
SWITCH_ADMIN = 1200.0           # PO admin / onboarding cost of re-sourcing
VOLUME_SHORTFALL_RATE = 0.05    # contract min-volume shortfall invoiced at 5% of unpurchased value
ACTIONS = ["expedite", "switch_supplier", "accept_delay"]


# ------------------------------------------------------------------ Hindsight Cloud
def client():
    from hindsight_client import Hindsight
    key = os.environ.get("HINDSIGHT_API_KEY")
    if not key:
        raise SystemExit("HINDSIGHT_API_KEY missing - add it to .env (see README).")
    return Hindsight(base_url=BASE_URL, api_key=key)


def log_usage(call: str, usage=None, est_tokens: int | None = None, extra: dict | None = None):
    USAGE_LOG.parent.mkdir(exist_ok=True)
    u = usage.to_dict() if hasattr(usage, "to_dict") else (usage or {})
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "call": call, "usage": u, "est_tokens": est_tokens, **(extra or {})}
    with open(USAGE_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, default=str) + "\n")
    tok = u.get("total_tokens") if u else None
    print(f"    [tokens] {call}: {tok if tok is not None else f'~{est_tokens} (estimated, API returns no usage for this call)'}")


# ------------------------------------------------------------------ read-only DB
def db():
    return sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)


def q(sql: str, *args) -> pd.DataFrame:
    with db() as con:
        return pd.read_sql(sql, con, params=args)


@dataclass
class Situation:
    as_of: str
    report: str
    po_id: str
    supplier_id: str
    rm_id: str
    plant_id: str
    qty: float
    promised_at: str
    eta: str
    slip_days: int
    evidence: list = field(default_factory=list)   # DB record ids used


def parse_report(text: str, as_of: str | None = None) -> Situation:
    """Pull IDs from free text, then complete everything else from the DB (the PO is the anchor)."""
    po = re.search(r"RMPO\d{6}", text)
    if not po:
        raise ValueError("Report must mention the delayed RM purchase order (e.g. RMPO038130).")
    po_id = po.group(0)
    dates = re.findall(r"\d{4}-\d{2}-\d{2}", text)
    h = q("SELECT * FROM rm_purchase_orders WHERE rm_po_id=?", po_id)
    if h.empty:
        raise ValueError(f"{po_id} not found in the DB.")
    h = h.iloc[0]
    ln = q("SELECT rm_id, SUM(quantity_ordered) qty FROM rm_purchase_order_lines WHERE rm_po_id=? GROUP BY rm_id", po_id).iloc[0]
    as_of = as_of or (dates[0] if dates else h.promised_at)
    rv = q("SELECT rm_po_revision_id, revised_at, new_expected_at FROM rm_po_revisions WHERE rm_po_id=? AND revised_at<=? "
           "ORDER BY revised_at", po_id, as_of)
    stated = [d for d in dates if d > h.promised_at]  # an ETA mentioned in the report wins over the DB
    eta = stated[-1] if stated else (rv.new_expected_at.iat[-1] if len(rv) else h.promised_at)
    slip = (pd.Timestamp(eta) - pd.Timestamp(h.promised_at)).days
    s = Situation(as_of, text, po_id, h.supplier_id, ln.rm_id, h.plant_id, float(ln.qty), h.promised_at, eta, slip)
    s.evidence += [po_id] + rv.rm_po_revision_id.tolist()
    return s


def unit_price(s: Situation, supplier: str | None = None) -> tuple[float, str | None]:
    r = q("SELECT catalog_line_id, unit_price FROM rm_supplier_catalog WHERE rm_id=? AND supplier_id=? AND price_valid_from<=? "
          "ORDER BY price_valid_from DESC LIMIT 1", s.rm_id, supplier or s.supplier_id, s.as_of)
    return (float(r.unit_price.iat[0]), r.catalog_line_id.iat[0]) if len(r) else (float("nan"), None)


def db_context(s: Situation) -> dict:
    """Everything the simulator needs, computed only from records dated <= as_of."""
    a = s.as_of
    ctx = {}
    # stock position and usage at the plant
    oh = q("SELECT o.opening_units + COALESCE((SELECT SUM(quantity_change) FROM rm_inventory_movements m WHERE m.rm_id=o.rm_id "
           "AND m.plant_id=o.plant_id AND m.movement_at<?),0) AS oh FROM rm_inventory_opening_balances o WHERE rm_id=? AND plant_id=?",
           a, s.rm_id, s.plant_id)
    use = q("SELECT -SUM(quantity_change)/56.0 AS d FROM rm_inventory_movements WHERE rm_id=? AND plant_id=? AND movement_type='consumption' "
            "AND movement_at>=date(?,'-56 day') AND movement_at<?", s.rm_id, s.plant_id, a, a)
    ctx["on_hand"] = float(oh.oh.iat[0])
    ctx["daily_usage"] = max(float(use.d.iat[0] or 0), 1e-6)
    ctx["cover_days"] = ctx["on_hand"] / ctx["daily_usage"]
    fgc = q("""SELECT SUM(r.produced_qty * e.unit_price * e.gross_margin_pct)/90.0 c, COUNT(*) n FROM production_runs r
               JOIN products_ext e ON e.product_id=r.product_id
               JOIN bill_of_materials b ON b.product_id=r.product_id AND b.rm_id=?
               WHERE r.plant_id=? AND r.actual_start>=date(?,'-90 day') AND r.actual_start<?""", s.rm_id, s.plant_id, a, a)
    ctx["fg_contribution_day"] = float(fgc.c.iat[0] or 0)
    ctx["fg_runs_90d"] = int(fgc.n.iat[0])
    ctx["price"], cat_id = unit_price(s)
    s.evidence.append(cat_id)
    # supplier history: how much further do POs slip after the first revised ETA? (same season if enough samples)
    hist = q("""SELECT p.rm_po_id, p.promised_at, p.received_at, MIN(r.revised_at) first_rev,
                       (SELECT new_expected_at FROM rm_po_revisions r2 WHERE r2.rm_po_id=p.rm_po_id AND r2.reason_code NOT LIKE 'expedite%'
                        ORDER BY revised_at LIMIT 1) first_eta
                FROM rm_purchase_orders p JOIN rm_po_revisions r ON r.rm_po_id=p.rm_po_id
                WHERE p.supplier_id=? AND p.received_at IS NOT NULL AND p.received_at<? GROUP BY p.rm_po_id""", s.supplier_id, a)
    hist = hist.dropna(subset=["first_eta"])
    hist["extra"] = (pd.to_datetime(hist.received_at) - pd.to_datetime(hist.first_eta)).dt.days.clip(lower=0)
    q4 = pd.Timestamp(a).month in (10, 11, 12)
    same = hist[pd.to_datetime(hist.promised_at).dt.month.isin([10, 11, 12]) == q4]
    base = same if len(same) >= 3 else hist
    ctx["extra_slip"] = float(base.extra.mean()) if len(base) else 0.0
    ctx["extra_slip_n"] = int(len(base))
    ctx["extra_slip_basis"] = ("same season (Q4)" if q4 else "same season (non-Q4)") if len(same) >= 3 else "all history"
    s.evidence += base.rm_po_id.tail(3).tolist()
    # expedite track record: supplier first, network fallback; fee rate from past expedite decisions (plant first)
    ex = q("""SELECT r.rm_po_id, r.old_expected_at, r.new_expected_at, p.received_at, p.supplier_id, p.plant_id
              FROM rm_po_revisions r JOIN rm_purchase_orders p ON p.rm_po_id=r.rm_po_id
              WHERE r.reason_code='expedite' AND p.received_at IS NOT NULL AND p.received_at<?""", a)
    ex["ok"] = pd.to_datetime(ex.received_at) <= pd.to_datetime(ex.new_expected_at)
    ex["pull"] = (pd.to_datetime(ex.old_expected_at) - pd.to_datetime(ex.new_expected_at)).dt.days
    exs = ex[ex.supplier_id == s.supplier_id]
    use_ex = exs if len(exs) >= 3 else ex
    ctx["exp_success"] = float(use_ex.ok.mean()) if len(use_ex) else 0.5
    ctx["exp_pull"] = float(use_ex.pull.mean()) if len(use_ex) else 5.0
    ctx["exp_basis"] = f"{len(use_ex)} past expedites ({'this supplier' if len(exs) >= 3 else 'network'})"
    s.evidence += use_ex.rm_po_id.tail(2).tolist()
    fee = q("""SELECT d.decision_id, d.action_cost, d.footprint_ids FROM decisions d WHERE d.decision_type='expedite' AND d.decided_at<?""", a)
    fee["po"] = fee.footprint_ids.map(lambda x: json.loads(x)[0])
    val = q("SELECT l.rm_po_id po, SUM(l.quantity_ordered*l.unit_cost) v, p.plant_id FROM rm_purchase_order_lines l "
            "JOIN rm_purchase_orders p ON p.rm_po_id=l.rm_po_id GROUP BY l.rm_po_id")
    fee = fee.merge(val, on="po")
    fee["rate"] = fee.action_cost / fee.v
    fp = fee[fee.plant_id == s.plant_id]
    use_fee = fp if len(fp) >= 3 else fee
    ctx["exp_fee_rate"] = float(use_fee.rate.median()) if len(use_fee) else 0.14
    ctx["exp_fee_basis"] = f"median of {len(use_fee)} expedite decisions ({'plant ' + s.plant_id if len(fp) >= 3 else 'network'})"
    s.evidence += use_fee.decision_id.tail(2).tolist()
    # alternate source (qualified, active at as_of) and its recent performance
    alt = q("""SELECT catalog_line_id, supplier_id, lead_time_days, unit_price, qualified_flag FROM rm_supplier_catalog
               WHERE rm_id=? AND supplier_id<>? AND price_valid_from<=? AND (price_valid_to IS NULL OR price_valid_to>=?)
               ORDER BY qualified_flag DESC, lead_time_days""", s.rm_id, s.supplier_id, a, a)
    ctx["alt"] = None
    if len(alt):
        r = alt.iloc[0]
        sc = q("SELECT month, avg_delay_days FROM supplier_scorecard_monthly WHERE supplier_id=? AND month<? AND avg_delay_days IS NOT NULL "
               "ORDER BY month DESC LIMIT 6", r.supplier_id, a[:7])
        ctx["alt"] = {"supplier_id": r.supplier_id, "lead": int(r.lead_time_days), "price": float(r.unit_price), "qualified": int(r.qualified_flag),
                      "avg_delay": float(sc.avg_delay_days.mean()) if len(sc) else 0.0, "catalog_line_id": r.catalog_line_id,
                      "scorecard_months": sc.month.tolist()}
        s.evidence.append(r.catalog_line_id)
    # incumbent scorecard (last 6 months) and open commitments tied to the supplier
    sc = q("SELECT month, otif_rate, avg_delay_days, commitments_made, commitments_kept FROM supplier_scorecard_monthly "
           "WHERE supplier_id=? AND month<? ORDER BY month DESC LIMIT 6", s.supplier_id, a[:7])
    ctx["scorecard"] = sc
    ctx["open_commitments"] = q("""SELECT commitment_id, commitment_text, made_at, due_date, quantity, penalty_or_credit FROM commitments
                                   WHERE counterparty_id=? AND made_at<=? AND (resolved_at IS NULL OR resolved_at>?)""",
                                s.supplier_id, a, a)
    return ctx


def commitment_conflicts(s: Situation, ctx: dict) -> list[dict]:
    """Switching cancels the incumbent PO: does that endanger an open min-volume commitment?"""
    out = []
    for c in ctx["open_commitments"].itertuples():
        if s.po_id in c.commitment_text:
            # an in-flight arrangement on this very PO (e.g. paid expedite / confirmed delivery) - cancelling breaks it
            d = q("SELECT decision_id, action_cost FROM decisions WHERE decision_id=(SELECT decision_id FROM commitments WHERE commitment_id=?)",
                  c.commitment_id)
            cost = float(d.action_cost.iat[0]) if len(d) else 0.0
            out.append({"commitment_id": c.commitment_id, "text": c.commitment_text, "kind": "po_arrangement",
                        "detail": f"open arrangement on {s.po_id} due {c.due_date}" + (f" (linked decision {d.decision_id.iat[0]})" if len(d) else ""),
                        "penalty": round(cost, 2)})
            continue
        if "purchase at least" not in c.commitment_text:
            continue
        bought = q("""SELECT COALESCE(SUM(l.quantity_received),0) v FROM rm_purchase_order_lines l JOIN rm_purchase_orders p ON p.rm_po_id=l.rm_po_id
                      WHERE p.supplier_id=? AND p.ordered_at>=? AND p.received_at<?""", s.supplier_id, c.made_at, s.as_of).v.iat[0]
        days_total = (pd.Timestamp(c.due_date) - pd.Timestamp(c.made_at)).days
        days_left = (pd.Timestamp(c.due_date) - pd.Timestamp(s.as_of)).days
        run_rate = bought / max(1, days_total - days_left)
        projected = bought + run_rate * days_left - s.qty
        if projected < c.quantity:
            gap = c.quantity - projected
            out.append({"commitment_id": c.commitment_id, "text": c.commitment_text, "kind": "min_volume",
                        "detail": f"bought {round(bought):,}, projected {round(projected):,} if {s.po_id} is cancelled -> shortfall {round(gap):,}",
                        "bought_to_date": round(bought),
                        "projected_if_cancelled": round(projected), "shortfall": round(gap),
                        "penalty": round(gap * ctx["price"] * VOLUME_SHORTFALL_RATE, 2)})
    return out


# ------------------------------------------------------------------ simulator (deterministic)
def simulate_action(action: str, s: Situation, ctx: dict) -> dict:
    t0 = pd.Timestamp(s.as_of)
    base_arrival = pd.Timestamp(s.eta) + pd.Timedelta(days=round(ctx["extra_slip"]))
    day_cost = ctx["fg_contribution_day"] * SERVICE_PENALTY_RATE
    if action == "accept_delay":
        arrival, direct = base_arrival, 0.0
        formula = f"ETA {s.eta} + historical extra slip {ctx['extra_slip']:.1f}d ({ctx['extra_slip_n']} POs, {ctx['extra_slip_basis']})"
    elif action == "expedite":
        pull = ctx["exp_pull"] * ctx["exp_success"]
        arrival = max(t0 + pd.Timedelta(days=2), base_arrival - pd.Timedelta(days=round(pull)))
        direct = s.qty * ctx["price"] * ctx["exp_fee_rate"]
        formula = (f"accept-delay arrival - pull {ctx['exp_pull']:.1f}d x success {ctx['exp_success']:.0%} ({ctx['exp_basis']}); "
                   f"fee = qty x price x {ctx['exp_fee_rate']:.3f} ({ctx['exp_fee_basis']})")
    elif action == "switch_supplier":
        a = ctx["alt"]
        if a is None:
            return {"action": action, "feasible": False, "note": "no alternate source in the catalog"}
        arrival = t0 + pd.Timedelta(days=a["lead"] + round(a["avg_delay"]) + (0 if a["qualified"] else 20))
        direct = max(0.0, a["price"] - ctx["price"]) * s.qty + SWITCH_ADMIN + (0 if a["qualified"] else 6000.0)
        formula = (f"{a['supplier_id']} lead {a['lead']}d + its avg delay {a['avg_delay']:.1f}d (scorecard {', '.join(a['scorecard_months'][:3])}...)"
                   f"{'' if a['qualified'] else ' + 20d qualification'}; cost = price diff x qty + admin {SWITCH_ADMIN:.0f}")
    else:
        raise ValueError(action)
    arrive_in = (arrival - t0).days
    stockout = max(0, int(round(arrive_in - ctx["cover_days"])))
    return {"action": action, "feasible": True, "arrival": str(arrival.date()), "stockout_days": stockout,
            "direct_cost": round(direct, 2), "shortage_cost": round(stockout * day_cost, 2),
            "total_cost": round(direct + stockout * day_cost, 2), "formula": formula}


# ------------------------------------------------------------------ memory
def recall_history(hs, s: Situation) -> list[dict]:
    query = (f"supplier delay {s.supplier_id} {s.rm_id} at {s.plant_id}: past slips, ETA revisions, expedites, supplier switches, "
             f"outcomes and lessons, commitments with {s.supplier_id}")
    r = hs.recall(bank_id=BANK_ID, query=query, budget="mid", max_tokens=3000, query_timestamp=f"{s.as_of}T12:00:00Z",
                  include_chunks=True, max_chunk_tokens=2000)
    s.chunk_text = " ".join(getattr(c, "text", "") or "" for c in (r.chunks or {}).values()) if isinstance(r.chunks, dict) else ""
    items = []
    for x in r.results or []:
        when = str(x.occurred_start or x.mentioned_at or "")[:10]
        if when and when > s.as_of:  # never use memories dated after the decision date
            continue
        items.append({"id": x.id, "document_id": x.document_id, "text": x.text, "when": when, "type": x.type})
    log_usage("recall", est_tokens=sum(len(i["text"]) for i in items) // 4, extra={"results": len(items), "bank": BANK_ID})
    # keep memories that mention this supplier or RM first, then the rest (not just top-1 similarity)
    rel = [i for i in items if s.supplier_id in i["text"] or s.rm_id in i["text"]]
    return rel + [i for i in items if i not in rel]


def reflect_precedents(hs, s: Situation, db_summary: str):
    r = hs.reflect(bank_id=BANK_ID, budget="low", max_tokens=700, include_facts=True, apply_all_directives=True,
                   query=(f"Before {s.as_of}: what happened in earlier delays involving {s.supplier_id} or {s.rm_id}, which actions "
                          f"(expedite, switch supplier, accept delay) were taken, and how did they turn out? Cite the source memories. "
                          f"Distinguish what happened from what was decided. Do not estimate costs."),
                   context=f"New report: {s.report}\nDB facts: {db_summary}")
    log_usage("reflect", usage=r.usage)
    facts = []
    if r.based_on is not None:
        bo = r.based_on.to_dict() if hasattr(r.based_on, "to_dict") else r.based_on
        for k in ("memories", "facts", "world", "experience", "observation"):
            for f in (bo.get(k) or []) if isinstance(bo, dict) else []:
                facts.append({"id": f.get("id"), "text": f.get("text", "")[:160]})
    return r.text, facts


# ------------------------------------------------------------------ recall -> which allowlisted DB queries to run
def plan_queries(hs, report: str, s: Situation) -> tuple[list[str], list[dict], str]:
    """Recall playbook memories for this kind of report; keep only query names that are on the allowlist."""
    if hs is None:
        return DEFAULT, [], "offline: default query set"
    r = hs.recall(bank_id=BANK_ID, query=f"Which data checks does the playbook say to run for this report: {report}",
                  budget="low", max_tokens=1500, include_chunks=True, max_chunk_tokens=1500, tags=["playbook"], tags_match="any")
    texts = [x.text for x in r.results or []]
    log_usage("recall(playbook)", est_tokens=sum(map(len, texts)) // 4, extra={"results": len(texts)})
    # take query names from the highest-ranked playbook memories only (first two that name any allowlisted query)
    names, used = [], 0
    for t in texts:
        found = [n for n in QUERIES if n in t.lower() or n.replace("_", " ") in t.lower()]
        if found:
            names += [n for n in found if n not in names]
            used += 1
        if used == 2:
            break
    hits = [{"id": x.id, "text": x.text[:160]} for x in (r.results or [])][:3]
    if not names:
        return DEFAULT, hits, "recall named no allowlisted query - fell back to the default set"
    return names, hits, f"chosen by recall from {len(r.results or [])} playbook memories"


def run_planned(names: list[str], s: Situation) -> dict[str, pd.DataFrame]:
    params = {"po_id": s.po_id, "supplier_id": s.supplier_id, "rm_id": s.rm_id, "plant_id": s.plant_id, "as_of": s.as_of}
    out = {}
    with db() as con:
        for n in names:
            out[n] = run_query(con, n, params)
    return out


def rows_for_memory(rows: dict[str, pd.DataFrame], k: int = 5) -> list[str]:
    """At most k rows, one per query first (in plan order), then the rest - compact 'query: col=val' lines."""
    picked, i = [], 0
    while len(picked) < k and any(i < len(df) for df in rows.values()):
        for n, df in rows.items():
            if i < len(df) and len(picked) < k:
                r = df.iloc[i]
                picked.append(f"{n}: " + ", ".join(f"{c}={v}" for c, v in r.items() if pd.notna(v)))
        i += 1
    return picked


# ------------------------------------------------------------------ decision
def decide(report: str, as_of: str | None = None, use_memory: bool = True, retain: bool = True, trace: bool = False) -> dict:
    s = parse_report(report, as_of)
    hs = client() if use_memory else None
    plan, plan_hits, plan_note = plan_queries(hs, report, s)
    db_rows = run_planned(plan, s)
    for df in db_rows.values():
        for c in df.columns:
            if c.endswith("_id") and c not in ("supplier_id", "plant_id", "rm_id"):
                s.evidence += df[c].dropna().astype(str).tolist()
    ctx = db_context(s)
    sims = [simulate_action(a, s, ctx) for a in ACTIONS]
    conflicts = commitment_conflicts(s, ctx)
    for x in sims:
        if x["action"] == "switch_supplier" and x.get("feasible") and conflicts:
            x["blocked_by"] = [c["commitment_id"] for c in conflicts]
            x["commitment_penalty"] = sum(c["penalty"] for c in conflicts)
    ok = [x for x in sims if x.get("feasible") and not x.get("blocked_by")]
    naive = min([x for x in sims if x.get("feasible")], key=lambda x: x["total_cost"])
    rec = min(ok, key=lambda x: x["total_cost"])
    memories, reflect_text, reflect_facts = [], None, []
    if use_memory:
        memories = recall_history(hs, s)
        sc = ctx["scorecard"]
        dbs = (f"{s.supplier_id} last {len(sc)} months OTIF {sc.otif_rate.mean():.0%}, avg delay {sc.avg_delay_days.mean():.1f}d; "
               f"{len(ctx['open_commitments'])} open commitments") if len(sc) else "no scorecard history"
        reflect_text, reflect_facts = reflect_precedents(hs, s, dbs)
    card = {"situation": s, "ctx": ctx, "sims": sims, "conflicts": conflicts, "naive": naive, "recommendation": rec,
            "memories": memories, "reflect": reflect_text, "reflect_facts": reflect_facts,
            "plan": plan, "plan_note": plan_note, "plan_hits": plan_hits, "db_rows": db_rows}
    card["rationale"] = rationale(card)
    card["retained_rows"] = rows_for_memory(db_rows)
    if retain and use_memory:
        # the query + at most 5 DB rows + the outcome of this conversation become memory for next time
        content = (f"Planner query ({s.as_of}): {report}\nData checked ({', '.join(plan)}):\n- " + "\n- ".join(card["retained_rows"]) +
                   f"\nRecommendation: {rec['action']} (simulated {rec['total_cost']:,.0f}, {rec['stockout_days']} stockout days)"
                   + (f"; switch flagged by {', '.join(k['commitment_id'] for k in conflicts)}" if conflicts else ""))
        r = hs.retain_batch(bank_id=BANK_ID, items=[{"content": content, "context": f"planner query and DB rows - {s.po_id}",
                                                     "timestamp": f"{s.as_of}T17:00:00Z", "document_id": f"query-{s.po_id}-{s.as_of}",
                                                     "tags": ["planner_query", f"supplier:{s.supplier_id}"]}])
        log_usage("retain(query+rows)", usage=r.usage, extra={"rows": len(card["retained_rows"])})
    if hs is not None:
        hs.close()
    return card


def rationale(c: dict) -> str:
    """Templated - every clause points at a simulator output, a DB record id, or a recalled memory id."""
    s, rec, ctx = c["situation"], c["recommendation"], c["ctx"]
    alts = [x for x in c["sims"] if x.get("feasible") and x is not rec]
    parts = [f"Recommend {rec['action'].replace('_', ' ')}: simulated total {rec['total_cost']:,.0f} "
             f"({rec['stockout_days']} stockout days, arrival {rec['arrival']}) vs "
             + "; ".join(f"{x['action'].replace('_', ' ')} {x['total_cost']:,.0f}/{x['stockout_days']}d" for x in alts) + " [simulator]."]
    parts.append(f"A stockout day is priced at {ctx['fg_contribution_day'] * SERVICE_PENALTY_RATE:,.0f} = 10% of the "
                 f"{ctx['fg_contribution_day']:,.0f}/day FG contribution from {ctx['fg_runs_90d']} runs using {s.rm_id} at {s.plant_id} "
                 f"in the last 90 days [production_runs, products_ext].")
    parts.append(f"Plant {s.plant_id} holds {ctx['on_hand']:,.1f} units = {ctx['cover_days']:.1f} days of cover [rm_inventory_movements as of {s.as_of}].")
    if ctx["extra_slip_n"]:
        parts.append(f"{s.supplier_id} POs historically slipped a further {ctx['extra_slip']:.1f} days after their first revised ETA "
                     f"({ctx['extra_slip_basis']}, n={ctx['extra_slip_n']}) [rm_po_revisions].")
    if c["conflicts"]:
        for k in c["conflicts"]:
            parts.append(f"FLAG: switching would cancel {s.po_id} and breach {k['commitment_id']} ('{k['text']}'; {k['detail']}; "
                         f"sunk/penalty ~{k['penalty']:,.0f}) [commitments.{k['commitment_id']}].")
        if c["naive"]["action"] == "switch_supplier":
            parts.append("The cheapest-looking option (switch supplier) was set aside because of this open commitment.")
    cited = [m for m in c["memories"] if s.supplier_id in m["text"] or s.rm_id in m["text"]][:3]
    if cited:
        parts.append("Precedents: " + " | ".join(f"\"{m['text'][:140]}\" [memory {m['id']}, doc {m['document_id']}, {m['when']}]" for m in cited))
    elif c["memories"] is not None and len(c["memories"]) == 0 and c["reflect"] is None:
        parts.append("Memory not consulted (offline run) - no precedent claims made.")
    else:
        parts.append("No recalled memory mentions this supplier or RM - no precedent claim made.")
    return " ".join(parts)
