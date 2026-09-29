"""Configure the `supply-chain-memory` bank: reflect mission (+ the four disposition traits), the 8 directives and
Hindsight's numeric disposition. Idempotent - safe to run before and after ingestion.

Hindsight's disposition is three 1-5 scales, so the build prompt's four traits map to skepticism 4 (cautious about
switches), literalism 4 (data-driven, exact figures), empathy 2, and are also written into the reflect mission."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from agent.config import Settings, load_settings

MISSION = (
    "Institutional supply-chain memory for a multi-plant consumer-goods manufacturer. This bank stores the complete "
    "operational history: suppliers, raw materials, plants, distribution centers, purchase orders, disruption events, "
    "negotiations, commitments, and past decisions with their outcomes. Use it to recall specific historical facts "
    "with evidence, connect causally related events across the supply chain, track open commitments and obligations, "
    "evaluate consequences of candidate actions by referencing precedents, and recommend responses grounded in what "
    "worked before.")
TRAITS = [
    "Cautious about supplier switches - always check commitment obligations first.",
    "Data-driven - quantify cost and stockout-day impacts.",
    "Precedent-aware - always look for similar past situations.",
    "Proactive - flag risks even when not asked about them.",
]
DIRECTIVES = [
    ("Cite evidence", "Cite the evidence (document dates and record IDs such as RPO, PO, EVT, DEC, CMT) behind every "
                      "claim and recommendation."),
    ("List affected commitments", "Always list open commitments that a recommended action could affect."),
    ("Check commitments before cancelling", "Never recommend cancelling a purchase order without first checking open "
                                            "commitments with that supplier."),
    ("Prefer the latest document", "Prefer the most recent document when facts conflict; say which earlier statement "
                                   "was superseded."),
    ("Contrast precedents", "When citing a precedent, state how today's conditions differ from the precedent's "
                            "conditions."),
    ("Flag known supplier patterns", "When a supplier has a known pattern (seasonal delays, size-dependent reliability, "
                                     "etc.), flag it explicitly."),
    ("Show prediction, confidence, last time", "For decision recommendations, always show: predicted outcome, "
                                               "confidence level, and what happened last time."),
    ("Facts vs observations", "Distinguish between facts (from documents) and observations (consolidated patterns) in "
                              "responses."),
]
DISPOSITION = {"disposition_skepticism": 4, "disposition_literalism": 4, "disposition_empathy": 2}


def reflect_mission() -> str:
    return MISSION + "\n\nHow you reason:\n" + "\n".join(f"- {t}" for t in TRAITS)


def make_client(settings: Settings):
    from hindsight_client import Hindsight

    return Hindsight(base_url=settings.hindsight_base_url,
                     **({"api_key": settings.hindsight_api_key} if settings.hindsight_api_key else {}))


def _name(item):
    return item.get("name") if isinstance(item, dict) else getattr(item, "name", None)


def _existing_directive_names(client, bank_id: str, state_path: Path) -> set[str]:
    lister = getattr(client, "list_directives", None)
    if lister is None:
        return set(json.loads(state_path.read_text())) if state_path.exists() else set()
    resp = lister(bank_id=bank_id)
    items = resp if isinstance(resp, list) else (getattr(resp, "items", None) or getattr(resp, "directives", None)
                                                 or (resp.get("items") if isinstance(resp, dict) else None) or [])
    return {_name(i) for i in items}


def configure_bank(client, bank_id: str, state_path: Path) -> list[str]:
    log = []
    try:
        client.create_bank(bank_id=bank_id)
        log.append(f"created bank {bank_id}")
    except Exception as ex:  # the client raises when the bank already exists; real failures surface just below
        log.append(f"bank {bank_id} already exists ({type(ex).__name__})")
    client.update_bank_config(bank_id, reflect_mission=reflect_mission(), **DISPOSITION)
    log.append(f"reflect mission + disposition {DISPOSITION} set")
    have = _existing_directive_names(client, bank_id, state_path)
    created = []
    for name, content in DIRECTIVES:
        if name not in have:
            client.create_directive(bank_id=bank_id, name=name, content=content)
            created.append(name)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(sorted((have | set(created)) - {None})))
    log.append(f"directives: {len(created)} created, {len(DIRECTIVES) - len(created)} already present")
    return log


def main() -> int:
    s = load_settings()
    if not s.hindsight_bank_id:
        print("HINDSIGHT_BANK_ID is empty - set it in .env", file=sys.stderr)
        return 1
    for line in configure_bank(make_client(s), s.hindsight_bank_id,
                               s.retention_log_path.with_name("bank_setup_state.json")):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
