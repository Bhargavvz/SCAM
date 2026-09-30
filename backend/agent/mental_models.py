"""The 12 planted-pattern Mental Models.

Each one is a standing question that names the entity and the behaviour to watch, with no effect sizes. Hindsight
answers it from the ingested memory and refreshes it after consolidation. All are tagged `planted-pattern` and named
"[Pxx] ...", so they can be listed, excluded from reflect (MENTAL_MODELS_IN_REFLECT=0) or deleted as a group."""
from __future__ import annotations

import argparse

from agent.bank_setup import make_client
from agent.config import load_settings

TAG = "planted-pattern"
PATTERN_MODELS = {
    "P01": ("SUP0247 Nov-Dec delivery reliability",
            "How late do SUP0247 raw-material deliveries promised in November and December arrive compared with "
            "other months, and what buffer should planners use for SUP0247 orders in Q4?"),
    "P02": ("February delays from China-based suppliers",
            "Do raw-material lots from China-based suppliers promised in February arrive later than in other months, "
            "by how much, and how should critical February items be ordered?"),
    "P03": ("RM0046 shortages after SUP0223 price increases",
            "Does RM0046 (single-sourced from SUP0223) run short in the weeks after SUP0223 price increases, how "
            "often has that happened, and what should we do when a new increase is announced?"),
    "P04": ("SUP0091 reliability by order size",
            "How does SUP0091's on-time delivery differ between small and large raw-material orders, and how should "
            "large orders to SUP0091 be placed?"),
    "P05": ("W005 expedite cost",
            "How do expedite costs for finished goods delivered into warehouse W005 compare with expedites into "
            "other warehouses, and how should expedites to W005 be handled?"),
    "P06": ("P02 quarter-start maintenance overruns",
            "How often are production runs at plant P02 planned in the first two weeks of a quarter delayed by "
            "maintenance overruns, and how should P02 be scheduled?"),
    "P07": ("RM0016 as substitute for RM0022 at P02",
            "What happened to incoming QA rejects of RM0016 at plant P02 after product IP00973 switched from RM0022 "
            "to RM0016, and what inspection or qualification is needed?"),
    "P08": ("P03 as a transfer donor plant",
            "How often do inter-plant raw-material transfers out of plant P03 leave P03 below safety stock compared "
            "with other donor plants, and which plants should be preferred as donors?"),
    "P09": ("SUP0237 recovery-date reliability",
            "When SUP0237 re-promises a late raw-material lot, how often does it miss the new date compared with other "
            "suppliers, and how should planners treat its revised dates?"),
    "P10": ("Forecast bias for consumables",
            "How do demand forecasts for the consumables category compare with actual demand, relative to other "
            "categories, and how should consumables forecasts be adjusted?"),
    "P11": ("SUP0179 lead-time trend",
            "How have actual lead times from SUP0179 changed month by month since January 2025, and what should we "
            "do about it?"),
    "P12": ("SUP0005 April finished-goods lateness",
            "How often are SUP0005 finished-goods purchase orders due in April late compared with other months, and "
            "how should April deliveries from SUP0005 be handled?"),
}


def model_name(pid: str) -> str:
    return f"[{pid}] {PATTERN_MODELS[pid][0]}"


def _get(obj, *names):
    for n in names:
        v = obj.get(n) if isinstance(obj, dict) else getattr(obj, n, None)
        if v is not None:
            return v
    return None


def _items(resp) -> list:
    return resp if isinstance(resp, list) else (_get(resp, "items", "mental_models") or [])


def list_pattern_models(client, bank_id: str) -> dict[str, dict]:
    names = {model_name(p) for p in PATTERN_MODELS}
    out = {}
    for m in _items(client.list_mental_models(bank_id=bank_id, detail="content")):
        name = _get(m, "name")
        if name in names:
            out[name] = {"id": _get(m, "id", "mental_model_id"), "name": name, "content": _get(m, "content")}
    return out


def create_pattern_models(client, bank_id: str) -> list[str]:
    have = list_pattern_models(client, bank_id)
    created = []
    for pid, (_, query) in PATTERN_MODELS.items():
        name = model_name(pid)
        if name in have:
            continue
        resp = client.create_mental_model(bank_id=bank_id, name=name, source_query=query, tags=[TAG, pid])
        client.update_mental_model(bank_id=bank_id, mental_model_id=_get(resp, "id", "mental_model_id"),
                                   trigger={"refresh_after_consolidation": True})
        created.append(name)
    return created


def delete_pattern_models(client, bank_id: str) -> list[str]:
    gone = []
    for name, m in list_pattern_models(client, bank_id).items():
        client.delete_mental_model(bank_id=bank_id, mental_model_id=m["id"])
        gone.append(name)
    return gone


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=["create", "list", "delete"])
    a = ap.parse_args(argv)
    s = load_settings()
    client = make_client(s)
    if a.action == "create":
        print("\n".join(create_pattern_models(client, s.hindsight_bank_id)) or "all 12 already exist")
    elif a.action == "delete":
        print("\n".join(delete_pattern_models(client, s.hindsight_bank_id)) or "none to delete")
    else:
        for name, m in sorted(list_pattern_models(client, s.hindsight_bank_id).items()):
            print(f"{name}\n  {(m['content'] or '(content not generated yet)')[:300]}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
