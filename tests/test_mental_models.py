import json

from agent.mental_models import (PATTERN_MODELS, TAG, create_pattern_models, delete_pattern_models,
                                 list_pattern_models, model_name)

COUNTRY = {"CN": "China"}
MONTH = {4: "April"}


class FakeMMClient:
    def __init__(self):
        self.models, self.updates = {}, []

    def create_mental_model(self, bank_id, name, source_query, tags):
        mid = f"mm{len(self.models) + 1}"
        self.models[mid] = {"id": mid, "name": name, "source_query": source_query, "tags": tags, "content": "..."}
        return {"id": mid}

    def update_mental_model(self, bank_id, mental_model_id, **kw):
        self.updates.append((mental_model_id, kw))

    def list_mental_models(self, bank_id, detail="content"):
        return list(self.models.values())

    def delete_mental_model(self, bank_id, mental_model_id):
        del self.models[mental_model_id]


def test_one_model_per_planted_pattern(base_settings):
    patterns = json.loads((base_settings.dataset_dir / "planted_patterns.json").read_text())["patterns"]
    assert sorted(PATTERN_MODELS) == sorted(p["pattern_id"] for p in patterns)
    for p in patterns:
        query = PATTERN_MODELS[p["pattern_id"]][1]
        for v in p["entities"].values():
            if isinstance(v, str):
                assert v in query or COUNTRY.get(v, "\0") in query, (p["pattern_id"], v)
            elif isinstance(v, int):
                assert MONTH[v] in query
        assert "%" not in query  # standing questions, not the answer key's effect sizes


def test_create_is_idempotent_and_tagged():
    c = FakeMMClient()
    assert len(create_pattern_models(c, "b")) == 12
    assert create_pattern_models(c, "b") == []
    assert all(m["tags"][0] == TAG for m in c.models.values())
    assert all(kw == {"trigger": {"refresh_after_consolidation": True}} for _, kw in c.updates)
    assert model_name("P01") in list_pattern_models(c, "b")


def test_delete_only_removes_pattern_models():
    c = FakeMMClient()
    c.create_mental_model("b", "Team notes", "unrelated", ["other"])
    create_pattern_models(c, "b")
    assert len(delete_pattern_models(c, "b")) == 12
    assert [m["name"] for m in c.models.values()] == ["Team notes"]
