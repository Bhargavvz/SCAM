import json
import re

from eval.run_pattern_probe import detected, load_probes


def test_probes_cover_all_patterns_neutrally(base_settings):
    patterns = json.loads((base_settings.dataset_dir / "planted_patterns.json").read_text())["patterns"]
    probes = load_probes()
    assert [p["id"] for p in probes] == [p["pattern_id"] for p in patterns]
    for p in probes:
        assert not re.search(r"\d+\s*%|\bdays?\b.*\blate\b|november|february|april|quarter", p["question"], re.I), p["id"]
        for r in p["rules"]:
            re.compile(r)


def test_detected_requires_every_rule():
    rules = ["nov|dec", "late|delay"]
    assert detected("SUP0247 deliveries promised in November arrive ~13 days late.", rules)
    assert not detected("SUP0247 is usually late.", rules)
