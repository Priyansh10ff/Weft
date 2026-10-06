"""Phase 4: hybrid, graph-aware retrieval."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db.repository import KnowledgeRepository
from app.schemas.knowledge import KnowledgeNode, MediaModality, SourceAsset
from app.services import retrieval
from app.services.ingestion import delete_source, ingest_nodes
from app.services.retrieval import decompose, fts_query, retrieve


def _ingest(repo, store, filename, modality, nodes):
    return ingest_nodes(SourceAsset(filename=filename, modality=modality), nodes,
                        repository=repo, vector_store=store)


def test_decompose_problem_statement_question():
    q = "What architecture was discussed for reducing database load, who explained it, and where was the corresponding diagram shown?"
    parts = decompose(q)
    assert len(parts) == 3
    assert parts[0].startswith("What architecture")
    assert parts[1].startswith("who explained it") and "architecture" in parts[1]  # pronoun carries the topic
    assert parts[2].startswith("where was the corresponding diagram shown")
    assert decompose("What does the retry policy look like?") == ["What does the retry policy look like?"]
    assert len(decompose("How did the team fix the checkout timeout issue and how do we know it worked?")) == 2


def test_fts_query_is_safe():
    assert fts_query('max_retries=5 "OR" (ticket #4471)') == '"max" OR "retries"* OR "ticket"* OR "4471"'
    assert fts_query("the and of") == ""


def test_keyword_search_finds_exact_identifiers(repo, store):
    _ingest(repo, store, "policy.json", MediaModality.JSON, [
        KnowledgeNode(modality=MediaModality.JSON, content="Retry policy: base_delay_ms=200, max_retries=5, jitter=full"),
        KnowledgeNode(modality=MediaModality.JSON, content="Unrelated lunch order"),
    ])
    store.multimodal.clear()  # dense search finds nothing: keyword search must carry it
    result = retrieve("what is max_retries set to", 3, repository=repo, vector_store=store)
    assert result.hits and "max_retries=5" in result.hits[0]["transcript"]
    assert result.hits[0]["signals"]["keyword_rank"] == 1
    assert result.hits[0]["signals"]["dense_rank"] is None

    plain = retrieve("what is max_retries set to", 3, hybrid=False, repository=repo, vector_store=store)
    assert plain.hits == []


def test_entity_named_in_question(repo, store):
    _ingest(repo, store, "notes.json", MediaModality.JSON, [
        KnowledgeNode(modality=MediaModality.JSON, content="We moved hot reads off the primary.", entities=["read replicas"]),
    ])
    store.multimodal.clear()
    hits = retrieve("Tell me about read replica usage", 3, repository=repo, vector_store=store).hits
    assert hits and hits[0]["signals"]["matched_entities"] == ["read replicas"]
    assert hits[0]["signals"]["entity_rank"] == 1


def test_graph_expansion_pulls_in_linked_visual(repo, store):
    _ingest(repo, store, "standup.mp3", MediaModality.AUDIO, [
        KnowledgeNode(modality=MediaModality.AUDIO, transcript="We cut database load with a cache in front of replicas.",
                      provenance={"start_seconds": 0, "end_seconds": 6}, entities=["Redis", "read replicas"]),
    ])
    image = _ingest(repo, store, "arch.png", MediaModality.IMAGE, [
        KnowledgeNode(modality=MediaModality.IMAGE, transcript="API  Redis  Replica",
                      visual_summary="Boxes and arrows", entities=["Redis", "read replicas"]),
    ])
    query = "how did we cut database load"
    with_graph = retrieve(query, 5, repository=repo, vector_store=store)
    ids = [h["id"] for h in with_graph.hits]
    assert image.segments[0].id in ids
    linked = next(h for h in with_graph.hits if h["id"] == image.segments[0].id)
    if linked.get("via"):
        assert linked["via"]["relation"] == "depicts"
    without = retrieve(query, 5, expand=False, hybrid=False, repository=repo, vector_store=store)
    assert image.segments[0].id not in [h["id"] for h in without.hits]
    assert with_graph.strategy["expanded"] is True


def test_confidence_weighting_and_source_cap(repo, store):
    _ingest(repo, store, "a.json", MediaModality.JSON, [
        KnowledgeNode(modality=MediaModality.JSON, content="kafka consumer lag alert", confidence=0.3),
    ])
    _ingest(repo, store, "b.json", MediaModality.JSON, [
        KnowledgeNode(modality=MediaModality.JSON, content="kafka consumer lag alert", confidence=1.0),
    ])
    hits = retrieve("kafka consumer lag alert", 2, repository=repo, vector_store=store).hits
    assert hits[0]["source"] == "b.json" and hits[0]["score"] > hits[1]["score"]

    _ingest(repo, store, "many.json", MediaModality.JSON, [
        KnowledgeNode(modality=MediaModality.JSON, content=f"zebra stripes note {i}") for i in range(6)
    ])
    hits = retrieve("zebra stripes", 10, repository=repo, vector_store=store).hits
    assert sum(1 for h in hits if h["source"] == "many.json") == retrieval.MAX_PER_SOURCE


def test_matched_span_and_regions(repo, store):
    _ingest(repo, store, "talk.mp4", MediaModality.VIDEO, [
        KnowledgeNode(
            modality=MediaModality.VIDEO, transcript="Welcome everyone. The circuit breaker opens after ten failures.",
            visual_summary="Slide with a retry diagram", frame_path="/derived/x.jpg",
            provenance={"start_seconds": 0, "end_seconds": 30, "scene_index": 0},
            attributes={
                "speech_segments": [
                    {"start_seconds": 0, "end_seconds": 4, "text": "Welcome everyone.", "speaker": "Priya"},
                    {"start_seconds": 12, "end_seconds": 18, "text": "The circuit breaker opens after ten failures.", "speaker": "Ravi"},
                ],
                "ocr_blocks": [
                    {"text": "Circuit breaker: 10 failures / 30s", "box": {"x": 0.1, "y": 0.2, "width": 0.5, "height": 0.1}},
                    {"text": "Agenda", "box": {"x": 0.1, "y": 0.05, "width": 0.2, "height": 0.05}},
                ],
            },
        ),
    ])
    hit = retrieve("when does the circuit breaker open", 1, repository=repo, vector_store=store).hits[0]
    assert hit["matched_span"]["start_seconds"] == 12 and hit["matched_span"]["speaker"] == "Ravi"
    assert [r["text"] for r in hit["matched_regions"]] == ["Circuit breaker: 10 failures / 30s"]


def test_modality_filter(repo, store):
    _ingest(repo, store, "x.json", MediaModality.JSON, [KnowledgeNode(modality=MediaModality.JSON, content="otter facts")])
    _ingest(repo, store, "x.mp3", MediaModality.AUDIO, [KnowledgeNode(modality=MediaModality.AUDIO, transcript="otter facts")])
    hits = retrieve("otter facts", 5, modalities=["audio"], repository=repo, vector_store=store).hits
    assert hits and {h["metadata"]["modality"] for h in hits} == {"audio"}


def test_fts_follows_ingest_delete_and_backfills(tmp_path, store):
    repo = KnowledgeRepository(tmp_path / "k.db")
    result = _ingest(repo, store, "a.json", MediaModality.JSON,
                     [KnowledgeNode(modality=MediaModality.JSON, content="quokka migration", entities=["Quokka"])])
    assert repo.keyword_search('"quokka"')
    repo._execute("DELETE FROM segments_fts")
    repo.close()
    reopened = KnowledgeRepository(tmp_path / "k.db")  # backfills on open
    assert reopened.keyword_search('"quokka"')
    delete_source(result.source.id, repository=reopened, vector_store=store)
    assert reopened.keyword_search('"quokka"') == []
    reopened.close()


@pytest.fixture()
def client(repo, store):
    from main import app

    return TestClient(app)


def test_query_api_exposes_retrieval_explanations(client):
    import io
    import json

    records = [{"text": "Retry policy uses max_retries=5 and a circuit breaker", "entities": ["circuit breaker"]}]
    client.post("/upload/json", files={"file": ("p.json", io.BytesIO(json.dumps(records).encode()), "application/json")})
    body = client.post("/query", json={"query": "What is max_retries, and who set the circuit breaker?", "limit": 3}).json()
    assert body["subqueries"] and body["strategy"]["hybrid"] is True
    top = body["results"][0]
    assert top["score"] is not None and "keyword_rank" in top["signals"]
    assert body["answer"]["sources"]

    plain = client.post("/query", json={"query": "max_retries", "hybrid": False, "expand": False, "decompose": False}).json()
    assert plain["strategy"]["hybrid"] is False and plain["subqueries"] == []
