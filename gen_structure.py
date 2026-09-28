"""Stage 1a - structure: plants, products_ext, raw_materials, bill_of_materials,
rm_supplier_catalog, contracts, plus the planted-pattern entity plan (_work/pattern_plan.json)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from common import Ctx, banner, cat_param, cli, fmt_ids

D = pd.Timestamp


def _round_nice(x: float) -> float:
    if x <= 0:
        return 1.0
    mag = 10 ** np.floor(np.log10(x))
    return float(np.ceil(x / mag * 2) / 2 * mag)


def build_plants(ctx: Ctx, src, cal) -> pd.DataFrame:
    cfg = ctx.cfg["plants"]
    W = src["warehouses"]
    recv = src["purchase_order_lines"].merge(src["purchase_orders"], on="purchase_order_id")
    vol = recv.groupby(recv.warehouse_id.map(W.set_index("warehouse_id").region)).quantity_received.sum()
    regions = vol.sort_values(ascending=False).index.tolist()
    n = int(cfg["count"])
    regions = (regions * 2)[:n]
    rows = []
    for i, r in enumerate(regions, 1):
        rows.append({"plant_id": f"P{i:02d}", "plant_name": f"{r} Plant {i:02d}", "region": r,
                     "country_code": cfg["region_country"].get(r, cfg["region_country"]["_default"]),
                     "capacity_units_per_week": 0})
    return pd.DataFrame(rows)


def transit_days(cfg, plant_region: str, wh_region: str) -> int:
    t = cfg["region_transit_days"]
    if plant_region == wh_region:
        return t["same"]
    adj = {tuple(sorted(p)) for p in t["adjacent_pairs"]}
    return t["adjacent"] if tuple(sorted((plant_region, wh_region))) in adj else t["far"]


def build_products_ext(ctx: Ctx, src, plants) -> pd.DataFrame:
    rng = ctx.rng("products_ext")
    cfg = ctx.cfg
    P, W = src["products"], src["warehouses"]
    cats = P.category.to_numpy()
    make_p = np.array([cat_param(cfg, "make_prob", c) for c in cats])
    make = rng.random(len(P)) < make_p

    # plant = plant in the region that receives most of the product's units, else nearest by transit
    recv = src["purchase_order_lines"].merge(src["purchase_orders"], on="purchase_order_id")
    recv["region"] = recv.warehouse_id.map(W.set_index("warehouse_id").region)
    top_region = recv.groupby(["product_id", "region"]).quantity_received.sum().reset_index() \
        .sort_values(["product_id", "quantity_received"], ascending=[True, False]).drop_duplicates("product_id") \
        .set_index("product_id").region
    plant_by_region = {r: plants[plants.region == r].plant_id.tolist() for r in plants.region.unique()}
    plant_ids = []
    for pid, is_make in zip(P.product_id, make):
        if not is_make:
            plant_ids.append(None)
            continue
        reg = top_region.get(pid, None)
        if reg in plant_by_region:
            plant_ids.append(rng.choice(plant_by_region[reg]))
        else:
            tt = plants.region.map(lambda pr: transit_days(cfg, pr, reg)).to_numpy()
            cand = plants.plant_id.to_numpy()[tt == tt.min()]
            plant_ids.append(rng.choice(cand))

    margins = np.array([rng.uniform(*cat_param(cfg, "margin", c)) for c in cats]).round(4)
    price = (P.unit_cost.to_numpy() / (1 - margins)).round(2)

    # brand families: distinct words per category
    words = list(cfg["brand_family_words"])
    k = int(cfg["brand_families_per_category"])
    fam_map = {}
    for i, c in enumerate(sorted(P.category.unique())):
        fam_map[c] = [words[(i * k + j) % len(words)] for j in range(k)]
    fam = np.array([fam_map[c][int(rng.integers(k))] for c in cats])

    sales = src["inventory_movements"].query("movement_type == 'sale'")
    units = (-sales.groupby("product_id").quantity_change.sum()).reindex(P.product_id).fillna(0).to_numpy()
    rev = units * price
    order = np.argsort(-rev)
    cum = np.cumsum(rev[order]) / rev.sum()
    abc = np.empty(len(P), dtype=object)
    abc[order] = np.where(cum <= 0.80, "A", np.where(cum <= 0.95, "B", "C"))

    return pd.DataFrame({"product_id": P.product_id, "sourcing_mode": np.where(make, "make", "buy"),
                         "plant_id": pd.array(plant_ids, dtype="string"), "brand_family": fam, "abc_class": abc,
                         "unit_price": price, "gross_margin_pct": margins})


def build_raw_materials(ctx: Ctx, src, pext) -> pd.DataFrame:
    rng = ctx.rng("raw_materials")
    cfg, rc = ctx.cfg, ctx.cfg["rm"]
    P = src["products"].merge(pext, on="product_id")
    mk = P[P.sourcing_mode == "make"]
    n_make = len(mk)
    n_rm = max(40, int(round(rc["per_make_product"] * n_make)))
    n_common = max(4, int(round(rc["common_frac"] * n_rm)))
    commons = [cfg["common_archetypes"][i % len(cfg["common_archetypes"])] for i in range(n_common)]
    # specific RM categories proportional to make-count x mix weight
    w = {}
    for cat, cnt in mk.category.value_counts().items():
        mix = cat_param(cfg, "rm_mix", cat)
        tot = sum(mix.values())
        for rc_, wt in mix.items():
            w[rc_] = w.get(rc_, 0) + cnt * wt / tot
    keys = sorted(w)
    probs = np.array([w[k] for k in keys]) / sum(w.values())
    n_spec = n_rm - n_common
    alloc = np.maximum(1, np.floor(probs * n_spec)).astype(int)
    while alloc.sum() < n_spec:
        alloc[np.argmax(probs * n_spec - alloc)] += 1
    while alloc.sum() > n_spec:
        alloc[np.argmax(alloc)] -= 1
    cats = commons + [k for k, a in zip(keys, alloc) for _ in range(a)]
    rows, name_count = [], {}
    for i, c in enumerate(cats):
        a = cfg["rm_archetypes"][c]
        stem = a["names"][name_count.get(c, 0) % len(a["names"])]
        name_count[c] = name_count.get(c, 0) + 1
        grade = (name_count[c] - 1) // len(a["names"]) + 1
        rows.append({"rm_id": f"RM{i + 1:04d}", "rm_name": f"{stem}" + (f" G{grade}" if grade > 1 else ""),
                     "rm_category": c, "uom": a["uom"], "std_unit_cost": round(float(rng.uniform(*a["cost"])), 4),
                     "criticality": "B", "is_single_source": 0,
                     "is_hazmat": int(rng.random() < a["hazmat"]), "substitute_group_id": pd.NA,
                     "is_common": i < n_common})
    rm = pd.DataFrame(rows)
    # single source
    ss_idx = rng.choice(len(rm), size=int(round(rc["single_source_frac"] * len(rm))), replace=False)
    rm.loc[ss_idx, "is_single_source"] = 1
    # substitute groups within category
    g = 1
    for c, grp in rm.groupby("rm_category"):
        ids = rng.permutation(grp.index.to_numpy())
        take = list(ids[: int(round(rc["substitute_group_frac"] * len(ids)))])
        chunks = [take[j: j + 2] for j in range(0, len(take), 2)]
        if chunks and len(chunks[-1]) == 1:
            last = chunks.pop()
            if chunks:
                chunks[-1] += last
        for chunk in chunks:
            rm.loc[chunk, "substitute_group_id"] = f"SG{g:03d}"
            g += 1
    rm["substitute_group_id"] = rm.substitute_group_id.astype("string")
    return rm


def build_bom(ctx: Ctx, src, pext, rm) -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = ctx.rng("bom")
    cfg, rc = ctx.cfg, ctx.cfg["rm"]
    P = src["products"].merge(pext, on="product_id")
    mk = P[P.sourcing_mode == "make"].reset_index(drop=True)
    common = rm[rm.is_common]
    usage = rng.uniform(*rc["common_usage"], size=len(common))
    spec = rm[~rm.is_common]
    spec_by_cat = {c: g.rm_id.to_numpy() for c, g in spec.groupby("rm_category")}
    zipf_w = {c: 1.0 / np.arange(1, len(v) + 1) ** rc["specific_zipf"] for c, v in spec_by_cat.items()}
    arche = cfg["rm_archetypes"]
    rm_ix = rm.set_index("rm_id")
    lo, hi = rc["bom_size"]
    rows = []
    for _, p in mk.iterrows():
        k = int(rng.integers(lo, hi + 1))
        cm = common.rm_id.to_numpy()[rng.random(len(common)) < usage]
        if len(cm) > k - 1:
            cm = rng.choice(cm, size=k - 1, replace=False)
        need = k - len(cm)
        mix = cat_param(cfg, "rm_mix", p.category)
        pool, pw = [], []
        for c, wt in mix.items():
            if c in spec_by_cat:
                pool.extend(spec_by_cat[c])
                pw.extend(wt * zipf_w[c] / zipf_w[c].sum())
        pool, pw = np.array(pool), np.array(pw)
        if len(pool) < need:
            extra = spec.rm_id[~spec.rm_id.isin(pool)].to_numpy()
            pool = np.concatenate([pool, extra])
            pw = np.concatenate([pw, np.full(len(extra), pw.min() if len(pw) else 1.0)])
        sp = rng.choice(pool, size=need, replace=False, p=pw / pw.sum())
        for r in list(cm) + list(sp):
            a = arche[rm_ix.at[r, "rm_category"]]
            q = rng.uniform(*a["qty"])
            q = float(round(q)) if a["uom"] == "ea" and a["qty"][0] >= 1 else round(q, 4)
            rows.append({"product_id": p.product_id, "rm_id": r, "qty_per_unit": q, "uom": a["uom"],
                         "scrap_pct": round(float(rng.uniform(0.005, 0.04)), 4),
                         "effective_from": D("2022-01-01"), "effective_to": pd.NaT, "bom_version": 1,
                         "change_reason": "initial"})
    bom = pd.DataFrame(rows)

    # versioned changes for ~5% of make products (reformulation / substitution)
    n_chg = int(round(rc["bom_change_frac"] * len(mk)))
    chg = rng.choice(mk.product_id.to_numpy(), size=n_chg, replace=False)
    sub_groups = rm.dropna(subset=["substitute_group_id"]).groupby("substitute_group_id").rm_id.apply(list).to_dict()
    rm_group = rm.set_index("rm_id").substitute_group_id.to_dict()
    new_rows = []
    days = pd.date_range("2023-03-01", "2025-09-01", freq="D")
    for pid in chg:
        d = days[int(rng.integers(len(days)))]
        cur = bom[(bom.product_id == pid)].copy()
        bom.loc[cur.index, "effective_to"] = d - pd.Timedelta(days=1)
        nv = cur.copy()
        subs = [i for i, r in nv.rm_id.items() if pd.notna(rm_group.get(r)) and
                any(x not in nv.rm_id.values for x in sub_groups[rm_group[r]])]
        if subs and rng.random() < 0.5:
            i = subs[int(rng.integers(len(subs)))]
            alt = [x for x in sub_groups[rm_group[nv.at[i, "rm_id"]]] if x not in nv.rm_id.values]
            nv.at[i, "rm_id"] = alt[int(rng.integers(len(alt)))]
            reason = "substitution"
        else:
            for i in rng.choice(nv.index.to_numpy(), size=min(2, len(nv)), replace=False):
                if nv.at[i, "uom"] != "ea" or nv.at[i, "qty_per_unit"] < 1:
                    nv.at[i, "qty_per_unit"] = round(nv.at[i, "qty_per_unit"] * float(rng.uniform(0.85, 1.15)), 4)
            reason = "reformulation"
        nv["effective_from"], nv["effective_to"], nv["bom_version"], nv["change_reason"] = d, pd.NaT, 2, reason
        new_rows.append(nv)
    bom = pd.concat([bom] + new_rows, ignore_index=True).sort_values(["product_id", "bom_version", "rm_id"])
    bom.insert(0, "bom_id", fmt_ids("BOM", len(bom), 6))
    return bom.reset_index(drop=True), mk


def expected_usage(src, pext, bom) -> pd.DataFrame:
    """Planned RM usage per rm x plant from FG make-lines (ordered qty x v1 BOM) - for MOQ sizing only."""
    L = src["purchase_order_lines"].merge(src["purchase_orders"][["purchase_order_id", "status"]], on="purchase_order_id")
    L = L[L.status != "cancelled"].merge(pext[pext.sourcing_mode == "make"][["product_id", "plant_id"]], on="product_id")
    b1 = bom[bom.bom_version == 1]
    u = L[["product_id", "plant_id", "quantity_ordered"]].merge(b1[["product_id", "rm_id", "qty_per_unit"]], on="product_id")
    u["qty"] = u.quantity_ordered * u.qty_per_unit
    return u.groupby(["rm_id", "plant_id"]).qty.sum().reset_index()


def pick_pattern_entities(ctx: Ctx, src, rm, cat_rows, plants, pext) -> dict:
    """Choose the entities that carry planted patterns (data-driven choices, recorded for later stages)."""
    rng = ctx.rng("pattern_entities")
    S = src["suppliers"].set_index("supplier_id")
    prim = cat_rows[cat_rows.sourcing_rank == "primary"].drop_duplicates(["rm_id", "supplier_id"])
    sec = cat_rows[cat_rows.sourcing_rank == "secondary"].drop_duplicates(["rm_id", "supplier_id"])
    n_prim = prim.supplier_id.value_counts()
    used = set()

    def take(series, exclude=()):
        for s in series.index:
            if s not in used and s not in exclude:
                used.add(s)
                return s
        raise RuntimeError("not enough suppliers for patterns")

    mid = n_prim[[0.84 <= S.at[s, "reliability_score"] <= 0.93 for s in n_prim.index]]
    q4 = take(mid)
    small = take(n_prim)
    creep = take(n_prim)
    qual = take(sec.supplier_id.value_counts())
    domestic = set(plants.country_code)
    ctry = prim.supplier_id.map(S.country_code)
    ctry = ctry[~ctry.isin(domestic)].value_counts()
    feb_country = ctry.index[0]
    # price-spike single-source RM: the most widely used RM that is (made) single-source
    singles = cat_rows.groupby("rm_id").supplier_id.nunique()
    single_ids = singles[singles == 1].index
    usage = rm.set_index("rm_id").loc[single_ids, "usage_qty"].sort_values(ascending=False)
    spike_rm = usage.index[0]
    spike_sup = prim[prim.rm_id == spike_rm].supplier_id.iat[0]
    pl = plants.plant_id.tolist()
    exp_plant, labor_plant = rng.choice(pl, size=2, replace=False)
    fams = pext.brand_family.value_counts()
    return {
        "q4_slip_supplier": q4, "small_po_supplier": small, "lead_time_creep_supplier": creep,
        "secondary_quality_supplier": qual, "feb_port_country": feb_country,
        "price_spike_rm": spike_rm, "price_spike_supplier": spike_sup,
        "expedite_cost_plant": str(exp_plant), "summer_labor_plant": str(labor_plant),
        "promo_underforecast_family": fams.index[int(rng.integers(min(4, len(fams))))],
    }


def build_catalog_contracts(ctx: Ctx, src, rm, bom, pext, plants):
    rng = ctx.rng("catalog")
    cfg, rc, cc = ctx.cfg, ctx.cfg["rm"], ctx.cfg["contracts"]
    S = src["suppliers"]
    pool = rng.choice(S.supplier_id.to_numpy(), size=min(int(rc["rm_supplier_count"]), len(S)), replace=False)
    Sx = S.set_index("supplier_id")
    use = expected_usage(src, pext, bom)
    rm = rm.merge(use.groupby("rm_id").qty.sum().rename("usage_qty"), on="rm_id", how="left").fillna({"usage_qty": 0})
    weeks = (D(ctx.cfg["window_end"]) - D(ctx.cfg["window_start"])).days / 7

    base = []
    for r in rm.itertuples():
        n = 1 if r.is_single_source else (3 if rng.random() < rc["tertiary_frac"] else 2)
        sups = rng.choice(pool, size=n, replace=False)
        sec_share = float(rng.uniform(*rc["secondary_share"]))
        allocs = [100.0] if n == 1 else ([round(100 - sec_share * 100, 1), round(sec_share * 100, 1)] if n == 2 else
                                         [round(100 - sec_share * 100, 1), round(sec_share * 100 - 5, 1), 5.0])
        wk = max(r.usage_qty / weeks / max(1, len(plants)), 1e-3)
        for rank, (s, a) in enumerate(zip(sups, allocs)):
            lt = int(max(3, Sx.at[s, "lead_time_days"] + round(rng.normal(0, 2))))
            prem = [rng.uniform(0.92, 1.10), rng.uniform(1.00, 1.12), rng.uniform(1.06, 1.22)][rank]
            base.append({"rm_id": r.rm_id, "supplier_id": s, "sourcing_rank": ["primary", "secondary", "tertiary"][rank],
                         "allocation_pct": a, "lead_time_days": lt,
                         "moq": _round_nice(wk * rng.uniform(*ctx.cfg["rm_ops"]["moq_weeks"])),
                         "base_price": round(r.std_unit_cost * prem, 4),
                         "qualified_flag": int(not (rank == 2 and rng.random() < rc["tertiary_unqualified_prob"]))})
    base = pd.DataFrame(base)

    # contracts per supplier, chained terms covering the simulation window
    sim_start, end = D(ctx.cfg["sim_start"]), D(ctx.cfg["window_end"])
    crow = []
    cid = 1
    for s, g in base.groupby("supplier_id"):
        start = D("2022-01-01") + pd.Timedelta(days=int(rng.integers(0, 330)))
        start = start.replace(day=1)
        while start <= end:
            term = int(rng.choice(cc["term_months"]))
            vt = start + pd.DateOffset(months=term) - pd.Timedelta(days=1)
            scope_rms = sorted(g.rm_id)
            cats = sorted(set(rm.set_index("rm_id").loc[scope_rms, "rm_category"]))
            crow.append({"contract_id": f"CT{cid:05d}", "supplier_id": s,
                         "scope": f"Supply of {', '.join(scope_rms[:6])}{' and others' if len(scope_rms) > 6 else ''} ({', '.join(cats)})",
                         "valid_from": start, "valid_to": vt,
                         "price_terms": str(rng.choice(cc["price_terms"])),
                         "min_volume": 0.0, "penalty_clause": str(rng.choice(cc["penalty_clauses"])),
                         "force_majeure_flag": int(rng.random() < cc["force_majeure_prob"])})
            cid += 1
            start = vt + pd.Timedelta(days=1)
    contracts = pd.DataFrame(crow)
    # min volume = ~60% of expected annualised purchases of the supplier's allocation during the term
    exp_sup = base.merge(rm[["rm_id", "usage_qty"]], on="rm_id")
    exp_sup["vol"] = exp_sup.usage_qty * exp_sup.allocation_pct / 100 / weeks * 52
    vol = exp_sup.groupby("supplier_id").vol.sum()
    term_years = (contracts.valid_to - contracts.valid_from).dt.days / 365
    contracts["min_volume"] = (contracts.supplier_id.map(vol) * term_years * rng.uniform(0.45, 0.7, len(contracts))).round(-1)
    ctx._cache["_rm_usage"] = rm
    return base, contracts, rm


def price_timeline(ctx: Ctx, base, contracts, rm, pat) -> pd.DataFrame:
    """Catalog rows = rm x supplier x validity segment. Breakpoints: contract renewals, January resin
    increases (planted), price spikes on the single-source RM (planted) and their partial rollback."""
    rng = ctx.rng("price_timeline")
    cc, pp = ctx.cfg["contracts"], ctx.cfg["patterns"]
    end = D(ctx.cfg["window_end"])
    rmcat = rm.set_index("rm_id").rm_category
    spike_dates = sorted(pd.to_datetime(rng.choice(pd.date_range("2023-04-01", "2025-06-30", freq="MS"),
                                                   size=pp["price_spike_shortage"]["spikes"], replace=False)))
    # keep spikes >= 8 months apart
    sd = [spike_dates[0]]
    for d in spike_dates[1:]:
        if (d - sd[-1]).days >= 240:
            sd.append(d)
    while len(sd) < pp["price_spike_shortage"]["spikes"]:
        sd.append(sd[-1] + pd.DateOffset(months=9))
    spikes = [d + pd.Timedelta(days=int(rng.integers(0, 20))) for d in sd]
    rows = []
    for b in base.itertuples():
        cts = contracts[contracts.supplier_id == b.supplier_id].sort_values("valid_from")
        pts = {}
        for i, c in enumerate(cts.itertuples()):
            chg = 1 + rng.uniform(*cc["renewal_price_change"]) if i else 1.0
            pts[c.valid_from] = (chg, c.contract_id, "contract_renewal" if i else "contract_start")
        if rmcat[b.rm_id] == pp["january_resin_increase"]["rm_category"]:
            for y in (2023, 2024, 2025):
                d = D(f"{y}-01-01")
                pts[d] = (None, None, "january_resin_increase")
        if b.rm_id == pat["price_spike_rm"]:
            for d in spikes:
                pts[d] = (None, None, "price_spike")
                pts[d + pd.Timedelta(days=int(rng.integers(75, 120)))] = (None, None, "negotiated_rollback")
        keys = sorted(pts)
        cur_price, cur_ct, seg = None, None, []
        for k in keys:
            p, ct, why = pts[k]
            if ct is not None:
                cur_ct = ct
                cur_price = b.base_price if cur_price is None else round(cur_price * p, 4)
            if why == "january_resin_increase" and cur_price is not None:
                cur_price = round(cur_price * (1 + rng.uniform(*pp["january_resin_increase"]["pct"])), 4)
            elif why == "price_spike" and cur_price is not None:
                cur_price = round(cur_price * (1 + rng.uniform(*pp["price_spike_shortage"]["spike_pct"])), 4)
            elif why == "negotiated_rollback" and cur_price is not None:
                cur_price = round(cur_price * (1 - rng.uniform(0.04, 0.09)), 4)
            if cur_price is None:
                continue
            ctid = cts[(cts.valid_from <= k) & (cts.valid_to >= k)].contract_id
            seg.append((k, cur_price, ctid.iat[0] if len(ctid) else cur_ct, why))
        for j, (k, p, ct, why) in enumerate(seg):
            vt = seg[j + 1][0] - pd.Timedelta(days=1) if j + 1 < len(seg) else pd.NaT
            if pd.notna(vt) and vt < k:
                continue
            rows.append({"rm_id": b.rm_id, "supplier_id": b.supplier_id, "sourcing_rank": b.sourcing_rank,
                         "allocation_pct": b.allocation_pct, "lead_time_days": b.lead_time_days, "moq": b.moq,
                         "unit_price": p, "price_valid_from": k, "price_valid_to": vt, "contract_id": ct,
                         "qualified_flag": b.qualified_flag, "_why": why})
    cat = pd.DataFrame(rows)
    cat = cat[(cat.price_valid_to.isna()) | (cat.price_valid_to >= D(ctx.cfg["sim_start"]))]
    cat = cat[cat.price_valid_from <= end]
    return cat, [str(d.date()) for d in spikes]


def apply_primary_switches(ctx: Ctx, cat: pd.DataFrame, pat) -> tuple[pd.DataFrame, list]:
    """A handful of RMs change primary supplier mid-window (rank swap) - state updates for the memory layer."""
    rng = ctx.rng("primary_switch")
    multi = cat.groupby("rm_id").supplier_id.nunique()
    cands = [r for r in multi[multi >= 2].index if r != pat["price_spike_rm"]]
    # prefer RMs whose primary is one of the unreliable pattern suppliers
    prim = cat[cat.sourcing_rank == "primary"].drop_duplicates("rm_id").set_index("rm_id").supplier_id
    pref = [r for r in cands if prim.get(r) in (pat["q4_slip_supplier"], pat["lead_time_creep_supplier"])]
    rest = [r for r in cands if r not in pref]
    chosen = pref[:4] + list(rng.choice(rest, size=min(4, len(rest)), replace=False))
    switches, out = [], [cat]
    for r in chosen:
        d = D("2024-01-15") + pd.Timedelta(days=int(rng.integers(0, 560)))
        rows = cat[cat.rm_id == r]
        p_old = rows[rows.sourcing_rank == "primary"].supplier_id.iat[0]
        p_new = rows[rows.sourcing_rank == "secondary"].supplier_id.iat[0]
        # split every open segment at d; after d swap ranks/allocations of primary and secondary
        seg = rows[(rows.price_valid_to.isna()) | (rows.price_valid_to >= d)]
        seg = seg[seg.price_valid_from < d]
        new = seg.copy()
        cat.loc[seg.index, "price_valid_to"] = d - pd.Timedelta(days=1)
        new["price_valid_from"] = d
        new["_why"] = "primary_supplier_changed"
        later = rows[rows.price_valid_from >= d]
        a_old = rows[rows.supplier_id == p_old].allocation_pct.iat[0]
        a_new = rows[rows.supplier_id == p_new].allocation_pct.iat[0]
        for df in (new, later):
            m_old, m_new = df.supplier_id == p_old, df.supplier_id == p_new
            df.loc[m_old, ["sourcing_rank", "allocation_pct"]] = ["secondary", a_new]
            df.loc[m_new, ["sourcing_rank", "allocation_pct"]] = ["primary", a_old]
        cat.loc[later.index, ["sourcing_rank", "allocation_pct"]] = later[["sourcing_rank", "allocation_pct"]]
        out.append(new)
        switches.append({"rm_id": r, "date": str(d.date()), "old_primary": p_old, "new_primary": p_new})
    cat = pd.concat(out, ignore_index=True)
    return cat, switches


def run(ctx: Ctx):
    banner("STAGE 1a - structure / BOM / catalog / contracts")
    src, cal = ctx.src(), ctx.load_json("calibration.json")
    plants = build_plants(ctx, src, cal)
    pext = build_products_ext(ctx, src, plants)
    rm = build_raw_materials(ctx, src, pext)
    bom, mk = build_bom(ctx, src, pext, rm)
    base, contracts, rm = build_catalog_contracts(ctx, src, rm, bom, pext, plants)

    # mark planned single-source carrier
    pat = pick_pattern_entities(ctx, src, rm, base, plants, pext)
    cat, spikes = price_timeline(ctx, base, contracts, rm, pat)
    cat, switches = apply_primary_switches(ctx, cat, pat)
    cat = cat.sort_values(["rm_id", "price_valid_from", "sourcing_rank"]).reset_index(drop=True)
    cat.insert(0, "catalog_line_id", fmt_ids("CAT", len(cat), 5))
    pat["price_spike_dates"] = spikes
    pat["primary_switches"] = switches

    # criticality from usage breadth, single-sourcing and cost
    n_make = int((pext.sourcing_mode == "make").sum())
    share = bom.groupby("rm_id").product_id.nunique() / n_make
    rm["bom_share"] = rm.rm_id.map(share).fillna(0)
    spend = rm.usage_qty * rm.std_unit_cost
    q = spend.rank(pct=True)
    rm["criticality"] = np.where((rm.is_common) | (rm.is_single_source == 1) & (q > 0.4) | (q > 0.85), "A",
                                 np.where(q > 0.35, "B", "C"))

    # plant weekly capacity from FG make receipts (units) routed to each plant
    L = src["purchase_order_lines"].merge(src["purchase_orders"], on="purchase_order_id")
    L = L[L.quantity_received > 0].merge(pext[["product_id", "plant_id"]], on="product_id").dropna(subset=["plant_id"])
    wk = L.groupby(["plant_id", pd.Grouper(key="received_at", freq="W-MON")]).quantity_received.sum()
    cap = (wk.groupby("plant_id").quantile(0.95) * ctx.cfg["plants"]["capacity_headroom"]).round(-2)
    plants["capacity_units_per_week"] = plants.plant_id.map(cap).fillna(cap.mean()).astype(int)

    ctx.save_json(pat, "pattern_plan.json")
    ctx.write(plants, "plants")
    ctx.write(pext, "products_ext")
    ctx.write(rm.drop(columns=["is_common", "usage_qty", "bom_share"]), "raw_materials")
    ctx.write(rm[["rm_id", "is_common", "usage_qty", "bom_share"]], "rm_meta", work=True)
    ctx.write(bom, "bill_of_materials")
    ctx.write(cat.drop(columns=["_why"]), "rm_supplier_catalog")
    ctx.write(cat[["catalog_line_id", "_why"]], "catalog_meta", work=True)
    ctx.write(contracts, "contracts")

    print(f"plants {len(plants)} | make products {n_make} / {len(pext)} | RMs {len(rm)} "
          f"(common {int(rm.is_common.sum())}, single-source {int(rm.is_single_source.sum())}) | BOM rows {len(bom)}")
    print(f"RMs in >30% of make-products: {(share > 0.30).sum()} ({(share > 0.30).mean():.1%} of RMs)")
    print(f"BOM size per product: {bom[bom.bom_version == 1].groupby('product_id').size().describe()[['min', 'mean', 'max']].to_dict()}")
    print(f"versioned BOM products: {bom[bom.bom_version > 1].product_id.nunique()} "
          f"({bom[bom.bom_version > 1].drop_duplicates('product_id').change_reason.value_counts().to_dict()})")
    print(f"catalog rows {len(cat)} | contracts {len(contracts)} | single-source RMs by catalog: "
          f"{(cat.groupby('rm_id').supplier_id.nunique() == 1).mean():.1%}")
    print(f"pattern entities: { {k: v for k, v in pat.items() if k != 'primary_switches'} }")


if __name__ == "__main__":
    run(cli(__doc__))
