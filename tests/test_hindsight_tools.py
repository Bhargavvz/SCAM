from types import SimpleNamespace

import pytest

from agent.corpus import load_index
from agent.hindsight_tools import HindsightMemory, RetainLedger, load_bank_policy


class FakeHindsight:
    """Signature deliberately lacks temporal_window/tags to prove kwargs are filtered."""

    def __init__(self, results=(), reflect_text="native answer", based_on=()):
        self.results, self.reflect_text, self.based_on = list(results), reflect_text, list(based_on)
        self.calls = []

    def recall(self, bank_id, query, budget="mid", max_tokens=4096, types=None, query_timestamp=None):
        self.calls.append(("recall", dict(bank_id=bank_id, query=query, types=types, query_timestamp=query_timestamp)))
        return SimpleNamespace(results=self.results)

    def reflect(self, bank_id, query, budget="low", include_facts=False):
        self.calls.append(("reflect", dict(query=query)))
        return SimpleNamespace(text=self.reflect_text, based_on=SimpleNamespace(memories=self.based_on))

    def retain(self, bank_id, content, context=None, timestamp=None, document_id=None, metadata=None):
        self.calls.append(("retain", dict(document_id=document_id, timestamp=timestamp)))


@pytest.fixture(scope="module")
def index(base_settings):
    return load_index(base_settings.corpus_path)


def hit(text, **kw):
    return SimpleNamespace(text=text, type="world", **kw)


def make(index, tmp_path, client, live_ids=frozenset(), synth=None):
    return HindsightMemory(client, "bank", index, RetainLedger(tmp_path / "ledger.jsonl"), lambda: set(live_ids),
                           synthesizer=synth, policy=("mission", ["cite evidence"]))


def test_recall_filters_future_and_unresolved(index, tmp_path):
    holdout_doc = next(d for d in index.by_id.values() if d.split == "holdout")
    client = FakeHindsight([
        hit("slip", document_id="DOC000248"),
        hit("expedite", document_id="DOC000254"),
        hit("future", document_id=holdout_doc.doc_id),
        hit("orphan", document_id=None, mentioned_at=None, context=None),
    ])
    mem = make(index, tmp_path, client)
    r = mem.recall("RPO003179 ETA", "2023-06-03")
    assert [h.doc_id for h in r.hits] == ["DOC000248"]
    assert (r.raw_count, r.dropped_future, r.dropped_unresolved) == (4, 2, 1)
    kw = client.calls[0][1]
    assert kw["types"] == ["world", "experience"] and kw["query_timestamp"].startswith("2023-06-03T23:59:59")


def test_recall_resolves_by_timestamp_and_context(index, tmp_path):
    d = index.by_id["DOC000254"]
    client = FakeHindsight([hit("expedite", document_id=None, mentioned_at=d.timestamp.isoformat(), context=d.context)])
    r = make(index, tmp_path, client).recall("q", "2023-06-11")
    assert r.hits[0].doc_id == "DOC000254"


def test_supersession_annotated_only_when_visible(index, tmp_path):
    client = FakeHindsight([hit("slip", document_id="DOC000248")])
    mem = make(index, tmp_path, client)
    assert mem.recall("q", "2023-06-11").hits[0].superseded_by == [{"doc_id": "DOC000254", "date": "2023-06-04"}]
    assert mem.recall("q", "2023-06-03").hits[0].superseded_by == []


def test_live_hits_only_visible_with_matching_live_db(index, tmp_path):
    live_hit = hit("agent decision", document_id="DECL00001.ab12", mentioned_at="2025-10-14T12:00:00-05:00")
    mine = make(index, tmp_path, FakeHindsight([live_hit]), live_ids={"DECL00001.ab12"})
    other = make(index, tmp_path, FakeHindsight([live_hit]), live_ids={"DECL00001.ffff"})
    r = mine.recall("q", "2025-10-19")
    assert [(h.doc_id, h.source) for h in r.hits] == [("DECL00001", "live")]
    assert mine.recall("q", "2025-10-13").dropped_future == 1
    assert other.recall("q", "2025-10-19").hits == []


def test_native_reflect_guard(index, tmp_path):
    client = FakeHindsight([hit("slip", document_id="DOC000248")], based_on=[hit("x", document_id="DOC000248")])
    calls = []
    mem = make(index, tmp_path, client, synth=lambda q, hits, pol: calls.append((q, len(hits), pol)) or "local answer")
    # before the corpus horizon -> local synthesis over as-of-filtered recall
    r = mem.reflect("what happened?", "2023-06-11")
    assert (r.mode, r.text, r.doc_ids) == ("local_filtered", "local answer", ["DOC000248"])
    assert "mission" in calls[0][2]
    # after the horizon with an empty ledger -> native
    assert mem.reflect("what happened?", "2025-10-14").mode == "native"
    # a retain from another live DB makes native reflect unsafe
    mem.ledger.append({"bank_id": "bank", "document_id": "DECL00001.zz", "timestamp": "2025-10-01T12:00:00-05:00", "status": "ok"})
    assert mem.native_reflect_allowed("2025-10-14") is False


def test_get_document_respects_as_of(index, tmp_path):
    mem = make(index, tmp_path, FakeHindsight())
    assert mem.get_document("DOC000254", "2023-06-03") is None
    doc = mem.get_document("DOC000248", "2023-06-11")
    assert doc["doc_id"] == "DOC000248" and doc["superseded_by"] == [{"doc_id": "DOC000254", "date": "2023-06-04"}]


def test_retain_logs_to_ledger(index, tmp_path):
    client = FakeHindsight()
    mem = make(index, tmp_path, client)
    status = mem.retain_experience(document_id="DECL00001.ab12", content="c", context="agent decision log",
                                   timestamp="2025-10-14T12:00:00-05:00", metadata={"k": "v"})
    assert status == "ok"
    assert client.calls[-1] == ("retain", {"document_id": "DECL00001.ab12", "timestamp": "2025-10-14T12:00:00-05:00"})
    assert mem.ledger.doc_ids("bank") == {"DECL00001.ab12"}


def test_bank_policy_matches_bank_setup():
    mission, directives = load_bank_policy()
    assert "supply-chain memory" in mission and "Proactive" in mission
    assert len(directives) == 8 and any("open commitments" in d for d in directives)


class SchemaReflectClient(FakeHindsight):
    def reflect(self, bank_id, query, budget="low", include_facts=False, response_schema=None,
                exclude_mental_models=False):
        self.calls.append(("reflect", dict(response_schema=response_schema, exclude=exclude_mental_models)))
        return SimpleNamespace(text="t", structured_output={"recommendation": "x"},
                               based_on=SimpleNamespace(memories=[]))


def test_reflect_schema_and_mental_model_exclusion(index, tmp_path):
    client = SchemaReflectClient()
    mem = HindsightMemory(client, "bank", index, RetainLedger(tmp_path / "l.jsonl"), lambda: set(),
                          use_mental_models=False)
    r = mem.reflect("q", "2025-10-14", budget="high", response_schema={"type": "object"})
    assert r.structured == {"recommendation": "x"}
    assert client.calls[-1] == ("reflect", {"response_schema": {"type": "object"}, "exclude": True})


def test_mental_model_exclusion_unsupported_fails_loudly(index, tmp_path):
    with pytest.raises(ValueError, match="exclude_mental_models"):
        HindsightMemory(FakeHindsight(), "bank", index, RetainLedger(tmp_path / "l.jsonl"), lambda: set(),
                        use_mental_models=False)


@pytest.mark.live
def test_live_recall_is_resolvable(base_settings):
    from agent.hindsight_tools import build_memory

    mem = build_memory(base_settings, lambda: set())
    r = mem.recall("Why did RPO003179 slip and what is its latest ETA?", "2023-06-11")
    assert r.hits, f"no visible hits: raw={r.raw_count} future={r.dropped_future} unresolved={r.dropped_unresolved}"
    assert any("RPO003179" in h.text for h in r.hits)


def test_failed_or_pending_retains_still_disable_native_reflect(index, tmp_path):
    mem = make(index, tmp_path, FakeHindsight())
    mem.ledger.append({"bank_id": "bank", "document_id": "DECL00009.zz", "timestamp": "2025-10-01T12:00:00-05:00",
                       "status": "error: read timeout"})
    assert mem.native_reflect_allowed("2025-10-14") is False


class FailingRetainClient(FakeHindsight):
    def retain(self, **kw):
        raise TimeoutError("read timeout")


def test_retain_writes_pending_entry_before_calling_hindsight(index, tmp_path):
    mem = make(index, tmp_path, FailingRetainClient())
    status = mem.retain_experience(document_id="DECL00001.ab12", content="c", context="x",
                                   timestamp="2025-10-14T12:00:00-05:00", metadata={})
    assert status.startswith("error")
    assert [e["status"] for e in mem.ledger.entries()][0] == "pending"
    assert mem.native_reflect_allowed("2025-10-14") is False
