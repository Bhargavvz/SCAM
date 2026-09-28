"""Stage 1a - plants, product extension, raw materials, BOM, catalog, contracts.

Also chooses the entities behind the RM-side planted patterns (pattern_plan)
so that Stage 1b (RM operations) can simulate them causally.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from sc_common import Ctx, banner, log, make_ids, write_table


def _nice_round(x: float) -> float:
    if x <= 0:
        return 1.0
    mag = 10 ** math.floor(math.log10(x))
    for step in (1, 2, 2.5, 5, 10):
        if x <= step * mag:
            return float(step * mag)
    return float(10 * mag)


# --------------------------------------------------------------------------- plants
def build_plants(ctx: Ctx) -> pd.DataFrame:
    cfg, c = ctx.cfg["plants"], ctx.calib
    rng = ctx.rng("plants")
    regions = list(c["warehouse_regions"].keys())  # sorted by capacity desc
    n = min(cfg["count"], len(regions))
    names = list(rng.permutation(cfg["site_names"]))[:n]
    rows = []
    for i, reg in enumerate(regions[:n]):
        rows.append({
            "plant_id": f"P{i + 1:02d}",
            "plant_name": f"{names[i]} Plant",
            "region": reg,
            "country_code": cfg["region_country"].get(reg, cfg["default_country"]),
            "capacity_units_per_week": 0,
        })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- products_ext
def build_products_ext(ctx: Ctx, plants: pd.DataFrame) -> pd.DataFrame:
    cfg, c = ctx.cfg, ctx.calib
    prod = ctx.src["products"].copy()
    rng = ctx.rng("products_ext")
    arch = prod.category.map(lambda k: c["category_archetypes"][k]["archetype"])
    make_p = prod.category.map(lambda k: c["category_archetypes"][k]["make_prob"]).astype(float)
    gm_mu = prod.category.map(lambda k: c["category_archetypes"][k]["gm_mean"]).astype(float)
    gm_sd = prod.category.map(lambda k: c["category_archetypes"][k]["gm_sd"]).astype(float)
    prod["archetype"] = arch
    prod["sourcing_mode"] = np.where(rng.random(len(prod)) < make_p.values, "make", "buy")
    gm = np.clip(rng.normal(gm_mu.values, gm_sd.values), 0.12, 0.70)
    prod["gross_margin_pct"] = np.round(gm * 100, 1)
    prod["unit_price"] = np.round(prod.unit_cost / (1 - prod.gross_margin_pct / 100), 2)

    # brand families: per category, contiguous groups of a shuffled order
    size = cfg["products"]["brand_family_size"]
    suffixes = ["", " Pro", " Eco", " Max", " Pure", " Plus", " Care", " Prime"]
    fam = pd.Series(index=prod.index, dtype=object)
    for cat, idx in prod.groupby("category").groups.items():
        idx = rng.permutation(np.array(list(idx)))
        n_fam = max(1, int(round(len(idx) / size)))
        stems = cfg["products"]["brand_stems"].get(c["category_archetypes"][cat]["archetype"], ["Generic"])
        names = [f"{stems[i % len(stems)]}{suffixes[(i // len(stems)) % len(suffixes)]}" for i in range(n_fam)]
        for j, chunk in enumerate(np.array_split(idx, n_fam)):
            fam.loc[chunk] = names[j]
    prod["brand_family"] = fam.values

    # plants: whole brand families (make members) go to one plant, balancing volume by plant weight
    units = pd.Series(c["sales"]["units_by_product"])
    prod["units_sold"] = prod.product_id.map(units).fillna(0)
    w = pd.Series(c["warehouse_regions"]).reindex(plants.region).values.astype(float)
    w = w / w.sum()
    load = np.zeros(len(plants))
    fam_vol = prod[prod.sourcing_mode == "make"].groupby("brand_family").units_sold.sum().sort_values(ascending=False)
    fam_plant = {}
    for f, v in fam_vol.items():
        j = int(np.argmin((load + v) / w))
        load[j] += v
        fam_plant[f] = plants.plant_id.iloc[j]
    prod["plant_id"] = np.where(prod.sourcing_mode == "make", prod.brand_family.map(fam_plant), None)

    # ABC on sales value
    val = (prod.units_sold * prod.unit_price).sort_values(ascending=False)
    cum = val.cumsum() / val.sum()
    a_cut, b_cut = cfg["products"]["abc_cutoffs"]
    abc = pd.Series(np.where(cum <= a_cut, "A", np.where(cum <= b_cut, "B", "C")), index=val.index)
    prod["abc_class"] = abc.reindex(prod.index).values
    return prod


# --------------------------------------------------------------------------- raw materials + BOM
def build_rms_and_bom(ctx: Ctx, prod: pd.DataFrame):
    cfg = ctx.cfg["raw_materials"]
    rng = ctx.rng("rm_bom")
    make = prod[prod.sourcing_mode == "make"].reset_index(drop=True)
    n_make = len(make)
    R = max(cfg["min_count"], int(round(cfg["per_make_product"] * n_make)))
    cats_cfg = cfg["rm_categories"]
    common_cats = cfg["common_categories"]
    n_common = max(1, int(round(cfg["common_share"] * R)))

    # demand weight of each rm category across make products (excluding common part)
    arch_counts = make.archetype.value_counts()
    wcat: dict[str, float] = {}
    for a, n in arch_counts.items():
        ws = cfg["archetype_rm_weights"].get(a) or cfg["archetype_rm_weights"]["formulated_goods"]
        tot = sum(ws.values())
        for k, v in ws.items():
            wcat[k] = wcat.get(k, 0) + n * v / tot
    for a, n in arch_counts.items():  # signature categories count double (they are specific)
        for k in cfg["signature_categories"].get(a, []):
            wcat[k] = wcat.get(k, 0) + 0.3 * n
    n_rest = R - n_common
    cat_list = sorted(wcat, key=lambda k: -wcat[k])
    tot = sum(wcat.values())
    alloc = {k: max(2, int(round(n_rest * wcat[k] / tot))) for k in cat_list}
    # adjust to exactly n_rest
    while sum(alloc.values()) > n_rest:
        k = max(alloc, key=lambda z: alloc[z] - n_rest * wcat[z] / tot)
        if alloc[k] <= 2:
            break
        alloc[k] -= 1
    while sum(alloc.values()) < n_rest:
        k = max(alloc, key=lambda z: n_rest * wcat[z] / tot - alloc[z])
        alloc[k] += 1
    common_alloc = {k: 0 for k in common_cats}
    for i in range(n_common):
        common_alloc[common_cats[i % len(common_cats)]] += 1

    rows = []
    used_names: set[str] = set()

    def name_for(cat: str, i: int) -> str:
        base = cats_cfg[cat]["names"]
        nm = base[i % len(base)]
        k = i // len(base)
        if k:
            nm = f"{nm} Grade {chr(ord('A') + k)}"
        while nm in used_names:
            nm += "*"
        used_names.add(nm)
        return nm

    counter: dict[str, int] = {}
    for is_common, source in ((True, common_alloc), (False, alloc)):
        for cat, n in source.items():
            for _ in range(n):
                i = counter.get(cat, 0)
                counter[cat] = i + 1
                cc = cats_cfg[cat]
                rows.append({
                    "rm_name": name_for(cat, i), "rm_category": cat, "uom": cc["uom"],
                    "std_unit_cost": round(float(rng.uniform(*cc["cost"])), 4),
                    "is_hazmat": int(rng.random() < cc["hazmat"]),
                    "is_common": is_common,
                })
    rms = pd.DataFrame(rows)
    rms.insert(0, "rm_id", make_ids("RM", len(rms), 4))

    # substitute groups within category
    sg, sg_i = {}, 1
    for cat, g in rms.groupby("rm_category"):
        ids = list(rng.permutation(g.rm_id.values))
        while len(ids) >= 2:
            k = 3 if (len(ids) >= 3 and rng.random() < 0.3) else 2
            grp, ids = ids[:k], ids[k:]
            if rng.random() < cfg["substitute_group_prob"]:
                for r in grp:
                    sg[r] = f"SG{sg_i:03d}"
                sg_i += 1
    rms["substitute_group_id"] = rms.rm_id.map(sg)

    # ---- BOM v1 -------------------------------------------------------
    kmin, kmax = cfg["bom_size"]
    k_p = np.clip(np.round(rng.triangular(kmin - 0.49, cfg["bom_size_mode"], kmax + 0.49, n_make)), kmin, kmax).astype(int)
    common = rms[rms.is_common]
    cov = rng.uniform(*cfg["common_coverage"], len(common))
    incl = rng.random((n_make, len(common))) < cov[None, :]
    # per-category zipf order for filler picks
    zipf_w = {}
    for cat, g in rms[~rms.is_common].groupby("rm_category"):
        ids = g.rm_id.values[rng.permutation(len(g))]
        w = 1.0 / np.arange(1, len(ids) + 1) ** cfg["zipf_a"]
        zipf_w[cat] = (ids, w / w.sum())
    # family signature picks
    fam_sig: dict[str, list[str]] = {}
    for fam_name, g in make.groupby("brand_family"):
        a = g.archetype.iloc[0]
        cands = rms[rms.rm_category.isin(cfg["signature_categories"].get(a, [])) & ~rms.is_common]
        if len(cands) == 0:
            fam_sig[fam_name] = []
            continue
        n_sig = int(rng.integers(cfg["signature_rms_per_brand"][0], cfg["signature_rms_per_brand"][1] + 1))
        fam_sig[fam_name] = list(rng.choice(cands.rm_id.values, size=min(n_sig, len(cands)), replace=False))

    bom_rows = []
    common_ids = common.rm_id.values
    med_cost = float(prod.unit_cost.median())
    rm_idx = rms.set_index("rm_id")
    for i, p in make.iterrows():
        chosen = list(common_ids[incl[i]])
        sig = [s for s in fam_sig.get(p.brand_family, []) if s not in chosen]
        chosen += sig
        k = int(k_p[i])
        if len(chosen) > k:  # drop some commons (keep signature)
            drop_pool = [r for r in chosen if r in set(common_ids)]
            n_drop = min(len(chosen) - k, len(drop_pool))
            drop = set(rng.choice(drop_pool, size=n_drop, replace=False)) if n_drop else set()
            chosen = [r for r in chosen if r not in drop]
        ws = cfg["archetype_rm_weights"].get(p.archetype) or cfg["archetype_rm_weights"]["formulated_goods"]
        wc = {kk: v for kk, v in ws.items() if kk in zipf_w}
        guard = 0
        while len(chosen) < k and guard < 200:
            guard += 1
            cat = rng.choice(list(wc), p=np.array(list(wc.values())) / sum(wc.values()))
            ids, w = zipf_w[cat]
            pick = rng.choice(ids, p=w)
            if pick not in chosen:
                chosen.append(pick)
        mult = (p.unit_cost / med_cost) ** 0.3
        for r in chosen:
            cc = cats_cfg[rm_idx.at[r, "rm_category"]]
            lo, hi = cc["qty"]
            q = math.exp(rng.uniform(math.log(lo), math.log(hi))) * (mult if hi > lo else 1.0)
            bom_rows.append({
                "product_id": p.product_id, "rm_id": r, "qty_per_unit": round(max(q, 0.0005), 4),
                "uom": rm_idx.at[r, "uom"], "scrap_pct": round(float(rng.uniform(*cc["scrap"])), 2),
                "effective_from": pd.Timestamp(cfg["bom_v1_effective_from"]), "effective_to": pd.NaT,
                "bom_version": 1, "change_reason": "initial release",
            })
    bom = pd.DataFrame(bom_rows)
    return rms, bom


# --------------------------------------------------------------------------- requirements estimate
def rm_requirement_estimate(ctx: Ctx, prod: pd.DataFrame, bom: pd.DataFrame) -> pd.DataFrame:
    """Annual RM requirement per rm/plant from existing FG receipts of make products."""
    pol = ctx.src["purchase_order_lines"]
    po = ctx.src["purchase_orders"]
    lines = pol.merge(po[["purchase_order_id", "received_at"]], on="purchase_order_id")
    lines = lines[lines.quantity_received > 0]
    mk = prod[prod.sourcing_mode == "make"][["product_id", "plant_id"]]
    vol = lines.merge(mk, on="product_id").groupby(["product_id", "plant_id"]).quantity_received.sum().reset_index()
    years = (pd.Timestamp(ctx.calib["window_end"]) - pd.Timestamp(ctx.calib["window_start"])).days / 365.25
    b = bom[bom.bom_version == 1].merge(vol, on="product_id")
    b["req"] = b.quantity_received * b.qty_per_unit * (1 + b.scrap_pct / 100) / years
    return b.groupby(["rm_id", "plant_id"]).req.sum().reset_index(name="annual_req")


# --------------------------------------------------------------------------- catalog + contracts
def build_catalog_contracts(ctx: Ctx, rms: pd.DataFrame, req: pd.DataFrame):
    cfg, ccfg = ctx.cfg["raw_materials"], ctx.cfg["contracts"]
    rng = ctx.rng("catalog")
    sup = ctx.src["suppliers"].copy()
    sup_cat = pd.Series(ctx.calib["supplier_stats"]["supplier_category"])
    sup["src_category"] = sup.supplier_id.map(sup_cat)
    aff = cfg["supplier_category_affinity"]
    links = []
    # each rm category is served by a compact pool of specialist suppliers (so suppliers carry several related RMs)
    cat_pool = {}
    for cat, g in rms.groupby("rm_category"):
        cands = sup[sup.src_category.isin(aff.get(cat, []))]
        if len(cands) < 3:
            cands = sup
        k = int(min(len(cands), max(3, math.ceil(len(g) * 0.7) + 1)))
        cat_pool[cat] = cands.iloc[rng.permutation(len(cands))[:k]]
    n_single = int(round(cfg["single_source_share"] * len(rms)))
    single_set = set(rng.choice(rms[~rms.is_common].rm_id.values, size=n_single, replace=False))
    for r in rms.itertuples():
        pool = cat_pool[r.rm_category]
        u = rng.random()
        n_src = 1 if r.rm_id in single_set else (2 if u < 0.70 else 3)
        picks = list(rng.choice(pool.supplier_id.values, size=n_src, replace=False))
        if n_src == 1:
            alloc = [100.0]
        elif n_src == 2:
            a = float(rng.choice([65, 70, 75, 80, 85, 90]))
            alloc = [a, 100 - a]
        else:
            a = float(rng.choice([60, 65, 70, 75, 80]))
            b = float(rng.choice([10, 15, 20, 25]))
            b = min(b, 100 - a - 5)
            alloc = [a, b, 100 - a - b]
        ranks = ["primary", "secondary", "tertiary"]
        for j, s in enumerate(picks):
            links.append({"rm_id": r.rm_id, "supplier_id": s, "sourcing_rank": ranks[j], "allocation_pct": alloc[j], "qualified_flag": 1})
        if n_src == 1 and rng.random() < cfg["unqualified_secondary_prob"]:
            s2 = rng.choice(pool.supplier_id[~pool.supplier_id.isin(picks)].values)
            links.append({"rm_id": r.rm_id, "supplier_id": s2, "sourcing_rank": "secondary", "allocation_pct": 0.0, "qualified_flag": 0})
    links = pd.DataFrame(links)
    links = links.merge(sup[["supplier_id", "lead_time_days"]], on="supplier_id")
    links = links.merge(rms[["rm_id", "is_hazmat", "std_unit_cost", "rm_category"]], on="rm_id")
    links["lead_time_days"] = (links.lead_time_days + links.is_hazmat * cfg["hazmat_extra_lead_days"]).astype(int)
    links["base_price"] = np.round(links.std_unit_cost * rng.uniform(0.92, 1.12, len(links)), 4)
    rq = req.groupby("rm_id").annual_req.agg(["sum", "median"]).rename(columns={"sum": "req_total", "median": "req_plant_med"})
    links = links.merge(rq, left_on="rm_id", right_index=True, how="left").fillna({"req_total": 0, "req_plant_med": 0})
    links["moq"] = [
        _nice_round(max(1.0, m / 52 * ctx.cfg["rm_ops"]["moq_weeks"] * float(rng.uniform(0.4, 0.9)))) for m in links.req_plant_med
    ]

    # contracts per supplier with rolling terms covering the whole ledger
    ws, we = pd.Timestamp(ctx.calib["window_start"]), pd.Timestamp(ctx.calib["window_end"])
    terms, probs = zip(*ccfg["term_choices"])
    contracts, cid = [], 1
    sup_terms: dict[str, list[dict]] = {}
    for s, g in links.groupby("supplier_id"):
        term = int(rng.choice(terms, p=np.array(probs) / sum(probs)))
        offset = int(rng.integers(2, term + 1))
        start = (ws - pd.DateOffset(months=offset)).replace(day=1)
        cats = sorted(g.rm_category.unique())
        index_linked = any(c.endswith("resin") or c in ("pulp", "sap") for c in cats)
        spend = float((g.req_total * g.allocation_pct / 100 * g.base_price).sum())
        lst = []
        while start <= we:
            end = start + pd.DateOffset(months=term) - pd.Timedelta(days=1)
            pt = ("index_linked: quarterly adjustment to published resin/pulp index, +/-5% cap" if index_linked else
                  rng.choice(["fixed: firm prices for the term", "fixed: firm prices, annual review on volume tiers", "cost_plus: raw-material cost + fixed conversion fee"]))
            pen = rng.choice([
                "1.5% credit on PO value per full week of delay beyond 5 days, capped at 10%",
                "2% credit per week late beyond agreed ETA; shortfall below minimum volume billed at 8%",
                "Late delivery: supplier pays expedite freight; minimum-volume shortfall billed at 5% of shortfall value",
                "1% credit per day late beyond 7 days, capped at 12%; buyer liable for 6% of any minimum-volume shortfall",
            ])
            row = {"contract_id": f"CTR{cid:04d}", "supplier_id": s, "scope": "RM supply agreement: " + ", ".join(c.replace("_", " ") for c in cats),
                   "valid_from": start, "valid_to": end, "price_terms": pt,
                   "min_volume": float(max(5000, round(spend * term / 12 * rng.uniform(*ccfg["min_volume_share"]), -3))),
                   "penalty_clause": pen, "force_majeure_flag": int(rng.random() < ccfg["force_majeure_prob"])}
            contracts.append(row)
            lst.append(row)
            cid += 1
            start = end + pd.Timedelta(days=1)
        sup_terms[s] = lst
    contracts = pd.DataFrame(contracts)

    # catalog periods = contract terms, prices drift per renewal
    cat_rows = []
    for l in links.itertuples():
        price = l.base_price
        for k, t in enumerate(sup_terms[l.supplier_id]):
            if k > 0:
                price = round(price * (1 + float(rng.uniform(*ccfg["annual_price_drift"]))), 4)
            cat_rows.append({"rm_id": l.rm_id, "supplier_id": l.supplier_id, "sourcing_rank": l.sourcing_rank,
                             "allocation_pct": l.allocation_pct, "lead_time_days": int(l.lead_time_days), "moq": l.moq,
                             "unit_price": price, "price_valid_from": t["valid_from"], "price_valid_to": t["valid_to"],
                             "contract_id": t["contract_id"], "qualified_flag": l.qualified_flag, "change_driver": "initial" if k == 0 else "contract_renewal"})
    catalog = pd.DataFrame(cat_rows)
    return catalog, contracts


def split_catalog(catalog: pd.DataFrame, rm_id: str, date: pd.Timestamp, update: dict, driver: str, supplier_id: str | None = None) -> pd.DataFrame:
    """Split catalog rows of rm (optionally one supplier) at `date` and apply `update`
    ({supplier_id: {col: value}} or {col: value} when supplier_id given) from then on."""
    date = pd.Timestamp(date)
    m = (catalog.rm_id == rm_id) & (catalog.price_valid_from < date) & (catalog.price_valid_to >= date)
    if supplier_id is not None:
        m &= catalog.supplier_id == supplier_id
    left = catalog[m].copy()
    right = catalog[m].copy()
    left["price_valid_to"] = date - pd.Timedelta(days=1)
    right["price_valid_from"] = date
    right["change_driver"] = driver
    for idx in right.index:
        s = right.at[idx, "supplier_id"]
        upd = update if supplier_id is not None else update.get(s, {})
        for col, val in upd.items():
            right.at[idx, col] = val
    # later periods of the same rm/supplier also get non-price updates
    later = (catalog.rm_id == rm_id) & (catalog.price_valid_from >= date)
    if supplier_id is not None:
        later &= catalog.supplier_id == supplier_id
    cat2 = catalog.copy()
    for idx in cat2[later].index:
        s = cat2.at[idx, "supplier_id"]
        upd = update if supplier_id is not None else update.get(s, {})
        for col, val in upd.items():
            if col != "unit_price":
                cat2.at[idx, col] = val
    cat2 = pd.concat([cat2[~m], left, right], ignore_index=True)
    return cat2.sort_values(["rm_id", "supplier_id", "price_valid_from"]).reset_index(drop=True)


# --------------------------------------------------------------------------- pattern plan + BOM changes
def plan_patterns_and_changes(ctx: Ctx, prod, rms, bom, catalog, req):
    cfg = ctx.cfg
    rng = ctx.rng("pattern_plan")
    sup = ctx.src["suppliers"]
    prim = catalog[(catalog.sourcing_rank == "primary") & (catalog.change_driver == "initial")]
    usage = req.groupby("rm_id").agg(plants=("plant_id", "nunique"), req=("annual_req", "sum")).reset_index()
    sup_rm = prim.merge(usage, on="rm_id").groupby("supplier_id").agg(n_rm=("rm_id", "nunique"), plants=("plants", "sum")).reset_index()
    sup_rm = sup_rm.merge(sup, on="supplier_id")
    taken: set[str] = set()
    plan: dict = {}

    def pick_supplier(df, key):
        d = df[~df.supplier_id.isin(taken)].sort_values(key, ascending=False)
        s = d.supplier_id.iloc[0]
        taken.add(s)
        return s

    en = set(cfg["patterns"]["enabled"])
    if "P01_supplier_q4_slip" in en:
        plan["P01"] = {"supplier_id": pick_supplier(sup_rm.assign(k=sup_rm.n_rm * 10 + sup_rm.plants), "k")}
    if "P02_country_feb_congestion" in en:
        rm_sup_country = prim.merge(sup, on="supplier_id").country_code.value_counts()
        pref = [c for c in ("CN", "VN") if c in rm_sup_country.index]
        plan["P02"] = {"country_code": pref[0] if pref else rm_sup_country.index[0]}
    # single-source RM with wide usage for price-spike pattern
    ss_ids = set(catalog[catalog.qualified_flag == 1].groupby("rm_id").supplier_id.nunique().loc[lambda s: s == 1].index)
    if "P03_price_spike_shortage" in en:
        # estimated runs per (rm, plant) = make-product FG lines per plant using the RM; cascades are feasible
        # when a plant runs the RM every few days, not several times a day
        pol_, po_ = ctx.src["purchase_order_lines"], ctx.src["purchase_orders"]
        fl = pol_.merge(po_[["purchase_order_id", "status"]], on="purchase_order_id")
        fl = fl[fl.status != "cancelled"].merge(prod[["product_id", "plant_id"]], on="product_id")
        er = bom[bom.bom_version == 1][["product_id", "rm_id"]].merge(fl[["product_id", "plant_id"]], on="product_id").groupby(["rm_id", "plant_id"]).size().rename("runs").reset_index()
        band = er[er.runs.between(40, 200)].groupby("rm_id").runs.sum().rename("band_runs")
        cand = rms[rms.rm_id.isin(ss_ids) & ~rms.is_common][["rm_id"]].merge(band, left_on="rm_id", right_index=True)
        cand = cand.sort_values("band_runs", ascending=False)
        rid = cand.rm_id.iloc[0] if len(cand) else sorted(ss_ids)[0]
        s = prim[prim.rm_id == rid].supplier_id.iloc[0]
        taken.add(s)
        wsd = pd.Timestamp(ctx.calib["window_start"])
        spikes = [wsd + pd.DateOffset(months=int(m)) + pd.Timedelta(days=int(rng.integers(0, 20))) for m in (8, 20, 30)]
        plan["P03"] = {"rm_id": rid, "supplier_id": s, "spike_dates": [d.strftime("%Y-%m-%d") for d in spikes]}
    if "P04_small_po_promises" in en:
        plan["P04"] = {"supplier_id": pick_supplier(sup_rm.assign(k=sup_rm.n_rm), "k")}
    if "P09_recovery_commitments_breached" in en:
        plan["P09"] = {"supplier_id": pick_supplier(sup_rm.assign(k=sup_rm.n_rm + rng.random(len(sup_rm))), "k")}
    if "P11_leadtime_creep" in en:
        plan["P11"] = {"supplier_id": pick_supplier(sup_rm.assign(k=sup_rm.plants + rng.random(len(sup_rm))), "k"), "from": "2025-01-01"}
    if "P08_transfer_source_depletion" in en:
        pl_combos = req.groupby("plant_id").rm_id.nunique().sort_values(ascending=False)
        plan["P08"] = {"plant_id": pl_combos.index[0]}
    if "P06_plant_quarter_start_maintenance" in en:
        pl = prod[prod.sourcing_mode == "make"].plant_id.value_counts()
        plan["P06"] = {"plant_id": pl.index[min(1, len(pl) - 1)], "weeks_of_quarter": [1, 2]}

    # ---- BOM changes (reformulation / substitution) -------------------------
    bc = cfg["raw_materials"]
    make_pids = prod[prod.sourcing_mode == "make"].product_id.values
    n_chg = max(2, int(round(bc["bom_change_share"] * len(make_pids))))
    n_sub = int(round(n_chg * bc["bom_change_substitution_share"]))
    sgm = rms.set_index("rm_id").substitute_group_id
    grp_members = rms.dropna(subset=["substitute_group_id"]).groupby("substitute_group_id").rm_id.apply(list).to_dict()
    b1 = bom[bom.bom_version == 1]
    sub_cand = b1[b1.rm_id.map(sgm).notna() & ~b1.rm_id.isin(rms[rms.is_common].rm_id)]
    # prefer single-source RMs as the replaced material
    sub_cand = sub_cand.assign(pref=sub_cand.rm_id.isin(ss_ids).astype(int) + rng.random(len(sub_cand)))
    sub_cand = sub_cand.sort_values("pref", ascending=False).drop_duplicates("product_id")
    ch_start, ch_end = pd.Timestamp("2023-04-01"), pd.Timestamp(ctx.calib["window_end"]) - pd.Timedelta(days=45)
    changes = []
    for r in sub_cand.head(n_sub).itertuples():
        others = [x for x in grp_members[sgm[r.rm_id]] if x != r.rm_id]
        newr = rng.choice(others)
        if (b1[(b1.product_id == r.product_id)].rm_id == newr).any():
            continue
        d = ch_start + pd.Timedelta(days=int(rng.integers(0, (ch_end - ch_start).days)))
        changes.append({"product_id": r.product_id, "date": d.normalize(), "kind": "substitution", "old_rm": str(r.rm_id), "new_rm": str(newr)})
    done = {c["product_id"] for c in changes}
    ref_pool = [p for p in rng.permutation(make_pids) if p not in done][: n_chg - len(changes)]
    for p in ref_pool:
        d = ch_start + pd.Timedelta(days=int(rng.integers(0, (ch_end - ch_start).days)))
        changes.append({"product_id": p, "date": d.normalize(), "kind": "reformulation", "old_rm": None, "new_rm": None})
    # P07: a substitution whose new RM has elevated scrap
    subs = [c for c in changes if c["kind"] == "substitution"]
    if "P07_substitute_scrap" in en and subs:
        c7 = sorted(subs, key=lambda c: c["date"])[len(subs) // 3]
        c7["high_scrap"] = True
        plan["P07"] = {"product_id": c7["product_id"], "old_rm": str(c7["old_rm"]), "new_rm": str(c7["new_rm"]), "date": c7["date"].strftime("%Y-%m-%d")}

    # apply changes -> versioned BOM rows
    rm_cat = rms.set_index("rm_id").rm_category
    new_rows = []
    bom = bom.copy()
    for c in sorted(changes, key=lambda c: c["date"]):
        cur = bom[(bom.product_id == c["product_id"]) & bom.effective_to.isna()]
        v = int(cur.bom_version.max())
        bom.loc[cur.index, "effective_to"] = c["date"] - pd.Timedelta(days=1)
        nv = cur.copy()
        nv["bom_version"] = v + 1
        nv["effective_from"] = c["date"]
        nv["effective_to"] = pd.NaT
        if c["kind"] == "substitution":
            i = nv.index[nv.rm_id == c["old_rm"]][0]
            nv.at[i, "rm_id"] = c["new_rm"]
            nv.at[i, "qty_per_unit"] = round(nv.at[i, "qty_per_unit"] * float(rng.uniform(0.95, 1.10)), 4)
            if c.get("high_scrap"):
                nv.at[i, "scrap_pct"] = round(min(15.0, nv.at[i, "scrap_pct"] * 2.5), 2)
            nv["change_reason"] = f"substitution: {c['old_rm']} replaced by {c['new_rm']} ({rm_cat[c['new_rm']].replace('_', ' ')}, same substitute group)"
        else:
            k = int(rng.integers(1, 3))
            idxs = rng.choice(nv.index, size=min(k, len(nv)), replace=False)
            parts = []
            for i in idxs:
                f = float(rng.choice([-1, 1]) * rng.uniform(0.05, 0.15))
                nv.at[i, "qty_per_unit"] = round(max(0.0005, nv.at[i, "qty_per_unit"] * (1 + f)), 4)
                parts.append(f"{nv.at[i, 'rm_id']} {'+' if f > 0 else ''}{f * 100:.0f}%")
            why = rng.choice(["cost-down", "performance upgrade", "regulatory update", "pack-weight reduction"])
            nv["change_reason"] = f"reformulation ({why}): " + ", ".join(parts)
            c["detail"] = nv["change_reason"].iloc[0]
        new_rows.append(nv)
    if new_rows:
        bom = pd.concat([bom] + new_rows, ignore_index=True)
    bom = bom.sort_values(["product_id", "bom_version", "rm_id"]).reset_index(drop=True)
    bom.insert(0, "bom_id", make_ids("BOM", len(bom), 6))
    return plan, changes, bom


def apply_price_spikes(ctx: Ctx, catalog: pd.DataFrame, plan: dict, rms: pd.DataFrame, req: pd.DataFrame):
    rng = ctx.rng("price_spikes")
    ccfg = ctx.cfg["contracts"]
    spikes = []
    if "P03" in plan:
        for d in plan["P03"]["spike_dates"]:
            spikes.append({"rm_id": plan["P03"]["rm_id"], "supplier_id": plan["P03"]["supplier_id"], "date": pd.Timestamp(d), "pattern": "P03"})
    prim = catalog[(catalog.sourcing_rank == "primary") & (catalog.change_driver == "initial")]
    pool = prim[~prim.rm_id.isin([s["rm_id"] for s in spikes])].drop_duplicates("rm_id")
    extra = max(0, ccfg["price_spike_rms"] - (1 if spikes else 0))
    ws, we = pd.Timestamp(ctx.calib["window_start"]), pd.Timestamp(ctx.calib["window_end"])
    for r in pool.sample(n=min(extra, len(pool)), random_state=int(rng.integers(1 << 30))).itertuples():
        d = ws + pd.Timedelta(days=int(rng.integers(120, (we - ws).days - 60)))
        spikes.append({"rm_id": r.rm_id, "supplier_id": r.supplier_id, "date": d.normalize(), "pattern": None})
    for s in sorted(spikes, key=lambda s: s["date"]):
        cur = catalog[(catalog.rm_id == s["rm_id"]) & (catalog.supplier_id == s["supplier_id"]) &
                      (catalog.price_valid_from <= s["date"]) & (catalog.price_valid_to >= s["date"])]
        if cur.empty:
            continue
        old = float(cur.unit_price.iloc[0])
        pct = float(rng.uniform(*ccfg["price_spike_pct"]))
        new = round(old * (1 + pct), 4)
        s.update({"old_price": old, "new_price": new, "pct": round(pct * 100, 1)})
        if cur.price_valid_from.iloc[0] == s["date"]:
            catalog.loc[cur.index, "unit_price"] = new
            catalog.loc[cur.index, "change_driver"] = "price_spike"
        else:
            catalog = split_catalog(catalog, s["rm_id"], s["date"], {"unit_price": new}, "price_spike", supplier_id=s["supplier_id"])
    return catalog, [s for s in spikes if "new_price" in s]


def run(ctx: Ctx):
    banner("STAGE 1a - plants, products_ext, raw materials, BOM, catalog, contracts")
    plants = build_plants(ctx)
    prod = build_products_ext(ctx, plants)
    rms, bom = build_rms_and_bom(ctx, prod)
    req = rm_requirement_estimate(ctx, prod, bom)
    catalog, contracts = build_catalog_contracts(ctx, rms, req)

    # single source / criticality (initial catalog state)
    q = catalog[(catalog.qualified_flag == 1) & (catalog.change_driver == "initial")].groupby("rm_id").supplier_id.nunique()
    rms["is_single_source"] = rms.rm_id.map(q).fillna(1).eq(1).astype(int)
    spend = req.groupby("rm_id").annual_req.sum() * rms.set_index("rm_id").std_unit_cost
    spend = spend.reindex(rms.rm_id).fillna(0)
    rank = spend.rank(pct=True).values
    sig_cats = {c for v in ctx.cfg["raw_materials"]["signature_categories"].values() for c in v}
    crit = np.where((rms.is_single_source.values == 1) | (rank >= 0.85) | (rms.rm_category.isin(sig_cats).values & (rank >= 0.75)), "A",
                    np.where((rank < 0.4) & rms.substitute_group_id.notna().values, "C", "B"))
    rms["criticality"] = crit

    plan, changes, bom = plan_patterns_and_changes(ctx, prod, rms, bom, catalog, req)
    catalog, spikes = apply_price_spikes(ctx, catalog, plan, rms, req)

    # plant capacity from weekly FG output of make products (received_at week)
    pol, po = ctx.src["purchase_order_lines"], ctx.src["purchase_orders"]
    out = pol.merge(po[["purchase_order_id", "received_at"]], on="purchase_order_id").merge(prod[["product_id", "plant_id"]], on="product_id")
    out = out[out.plant_id.notna() & (out.quantity_received > 0)]
    wk = out.groupby(["plant_id", out.received_at.dt.to_period("W")]).quantity_received.sum()
    rng = ctx.rng("capacity")
    cap = wk.groupby(level=0).quantile(0.95)
    plants["capacity_units_per_week"] = [int(math.ceil(cap.get(p, 0) * rng.uniform(*ctx.cfg["plants"]["capacity_headroom"]) / 100) * 100) for p in plants.plant_id]

    write_table(ctx, "plants", plants)
    write_table(ctx, "products_ext", prod)
    write_table(ctx, "raw_materials", rms)
    write_table(ctx, "bill_of_materials", bom)
    write_table(ctx, "contracts", contracts)
    ctx.save_internal("structure", {"prod": prod, "rms": rms, "catalog": catalog, "req": req,
                                    "pattern_plan": plan, "bom_changes": changes, "price_spikes": spikes})

    make = prod[prod.sourcing_mode == "make"]
    b1 = bom[bom.bom_version == 1]
    cov = b1.groupby("rm_id").product_id.nunique() / len(make)
    top = cov.sort_values(ascending=False).head(max(1, int(round(0.1 * len(rms)))))
    log(f"plants: {len(plants)} | make products: {len(make):,} ({len(make) / len(prod):.0%}) | RMs: {len(rms)} | BOM rows: {len(bom):,}")
    log(f"BOM size per product: {b1.groupby('product_id').size().describe()[['min', 'mean', 'max']].round(2).to_dict()}")
    log(f"commonality: top {len(top)} RMs (10%) coverage min {top.min():.1%} / max {top.max():.1%} of make products")
    log(f"single-source RMs: {rms.is_single_source.mean():.1%}; criticality {rms.criticality.value_counts().to_dict()}")
    log(f"BOM changes: {len(changes)} products ({len(changes) / len(make):.1%}); substitutions {sum(c['kind'] == 'substitution' for c in changes)}")
    log(f"catalog rows: {len(catalog):,}; contracts: {len(contracts)}; price spikes: {len(spikes)}")
    log(f"pattern plan: {plan}")
