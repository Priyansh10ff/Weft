"""Phase 3: cross-modal and temporal relationships."""

from __future__ import annotations

import io
import json

import pytest
from fastapi.testclient import TestClient

from app.schemas.knowledge import KnowledgeNode, MediaModality, SourceAsset
from app.schemas.records import RelationType
from app.services.graph import entity_timeline, neighbourhood
from app.services.ingestion import delete_source, ingest_nodes
from app.services.linker import classify, entity_key, relink_all, score_pair


def _ingest(repo, store, filename, modality, nodes, **attrs):
    return ingest_nodes(
        SourceAsset(filename=filename, modality=modality), nodes,
        source_attributes=attrs or None, repository=repo, vector_store=store,
    )


def _edges(repo, relation):
    rows = repo._fetchall("SELECT * FROM relations WHERE relation = ?", (relation.value,))
    return [repo._row_to_relation(r) for r in rows]


def test_entity_key_resolution_rules():
    assert entity_key("Read Replicas") == entity_key("read replica")
    assert entity_key("the Payments-API") == "payments api"
    assert entity_key("latencies") == "latency"
    assert entity_key("status") == "status" and entity_key("bus") == "bus"
    assert entity_key("API") == "api"


def test_score_pair_rules():
    assert score_pair(2, None) == pytest.approx(0.75)
    assert score_pair(1, 0.5) == 0.6
    assert score_pair(1, 0.3) is None
    assert score_pair(0, 0.7) == 0.7
    assert score_pair(0, 0.5) is None


def test_visual_and_spoken_evidence_become_depicts(repo, store):
    audio = _ingest(repo, store, "standup.mp3", MediaModality.AUDIO, [
        KnowledgeNode(modality=MediaModality.AUDIO, transcript="We put Redis in front of the read replicas to cut database load.",
                      provenance={"start_seconds": 10, "end_seconds": 18}, entities=["Redis", "read replicas"]),
    ])
    image = _ingest(repo, store, "arch.png", MediaModality.IMAGE, [
        KnowledgeNode(modality=MediaModality.IMAGE, transcript="API  Redis  Replica 1  Replica 2",
                      visual_summary="Architecture diagram: API -> Redis cache -> two read replicas",
                      entities=["Redis", "read replica"]),
    ])
    depicts = _edges(repo, RelationType.DEPICTS)
    assert len(depicts) == 1
    edge = depicts[0]
    assert edge.src_id == image.segments[0].id and edge.dst_id == audio.segments[0].id
    # "read replica" and "read replicas" are resolved via same_as and count as shared.
    assert {entity_key(n) for n in edge.attributes["shared_entities"]} == {"redis", "read replica"}
    assert "entities" in edge.attributes["method"]
    assert image.link_count >= 1
    assert len(_edges(repo, RelationType.SAME_AS)) == 1


def test_text_sources_corroborate_and_weak_pairs_do_not_link(repo, store):
    _ingest(repo, store, "postmortem.json", MediaModality.JSON, [
        KnowledgeNode(modality=MediaModality.JSON, content="Retries now use exponential backoff and a circuit breaker.",
                      entities=["exponential backoff", "circuit breaker"]),
    ])
    _ingest(repo, store, "notes.txt", MediaModality.JSON, [
        KnowledgeNode(modality=MediaModality.JSON, content="exponential backoff circuit breaker shipped",
                      entities=["Exponential Backoff", "circuit breakers"]),
        KnowledgeNode(modality=MediaModality.JSON, content="lunch menu", entities=["circuit breaker"]),
    ])
    corroborates = _edges(repo, RelationType.CORROBORATES)
    assert len(corroborates) == 1
    assert corroborates[0].src_id < corroborates[0].dst_id  # canonical direction, no duplicates
    shared = corroborates[0].attributes["shared_entities"]
    assert {entity_key(n) for n in shared} == {"circuit breaker", "exponential backoff"}


def test_same_scene_and_repeated_slide(repo, store):
    def window(start, end, scene, dup=None):
        attrs = {"keyframe_duplicate_of_scene": dup} if dup is not None else {}
        return KnowledgeNode(
            modality=MediaModality.VIDEO, transcript=f"speech {start}", visual_summary=f"slide {scene}",
            provenance={"start_seconds": start, "end_seconds": end, "scene_index": scene}, attributes=attrs,
        )

    result = _ingest(repo, store, "talk.mp4", MediaModality.VIDEO, [
        window(0, 30, 0), window(30, 55, 0), window(55, 70, 1), window(70, 90, 2, dup=0),
    ])
    s = result.segments
    co = _edges(repo, RelationType.CO_OCCURS)
    assert [(e.src_id, e.dst_id) for e in co] == [(s[0].id, s[1].id)]
    assert co[0].attributes["scene_index"] == 0
    same = _edges(repo, RelationType.SHOWS_SAME)
    assert [(e.src_id, e.dst_id) for e in same] == [(s[3].id, s[0].id)]


def test_classify_direction():
    from app.schemas.records import SegmentKind, SegmentRecord

    def seg(i, visual):
        return SegmentRecord(id=i, source_id="s" + i, modality=MediaModality.PDF, kind=SegmentKind.PAGE, ordinal=0,
                             text="t", visual_summary="v" if visual else None, confidence=0.9, extractor="x")

    rel, src, dst = classify(seg("b", False), seg("a", True))
    assert rel is RelationType.DEPICTS and src.id == "a"
    rel, src, dst = classify(seg("b", True), seg("a", True))
    assert rel is RelationType.CORROBORATES and (src.id, dst.id) == ("a", "b")


def test_relink_is_idempotent_and_delete_cleans_up(repo, store):
    a = _ingest(repo, store, "a.json", MediaModality.JSON, [
        KnowledgeNode(modality=MediaModality.JSON, content="kafka consumer lag", entities=["Kafka", "consumer lag"])])
    _ingest(repo, store, "b.json", MediaModality.JSON, [
        KnowledgeNode(modality=MediaModality.JSON, content="kafka lag alert", entities=["kafka", "consumer lag"])])
    first = relink_all(repository=repo, vector_store=store)
    second = relink_all(repository=repo, vector_store=store)
    assert first == second and first["corroborates"] == 1

    delete_source(a.source.id, repository=repo, vector_store=store)
    assert _edges(repo, RelationType.CORROBORATES) == []


def test_entity_timeline_orders_by_recorded_time(repo, store):
    late = _ingest(repo, store, "after.json", MediaModality.JSON, [
        KnowledgeNode(modality=MediaModality.JSON, content="p99 is 310ms", entities=["p99 latency"])],
        recorded_at="2026-03-14T00:00:00+00:00")
    early = _ingest(repo, store, "before.mp3", MediaModality.AUDIO, [
        KnowledgeNode(modality=MediaModality.AUDIO, transcript="p99 was 4.2 seconds", entities=["p99 latencies"],
                      provenance={"start_seconds": 5, "end_seconds": 9})],
        recorded_at="2026-03-01T00:00:00+00:00")
    entity = next(e for e in repo.all_entities() if e.name == "p99 latency")
    timeline = entity_timeline(repo, entity.id)
    assert [e.source.id for e in timeline.entries] == [early.source.id, late.source.id]
    assert timeline.entries[0].when_source == "recorded_at"
    assert timeline.entries[0].offset_seconds == 5
    assert [a.name for a in timeline.aliases] == ["p99 latencies"]


def test_neighbourhood(repo, store):
    result = _ingest(repo, store, "a.json", MediaModality.JSON, [
        KnowledgeNode(modality=MediaModality.JSON, content="x", entities=["Kafka"])])
    view = neighbourhood(repo, result.segments[0].id)
    kinds = {n.kind.value for n in view.nodes}
    assert kinds == {"segment", "entity"} and len(view.edges) == 1
    assert view.edges[0].relation is RelationType.MENTIONS
    assert neighbourhood(repo, "missing") is None


@pytest.fixture()
def client(repo, store):
    from main import app

    return TestClient(app)


def test_api_links_timeline_graph_and_recorded_at(client, repo):
    def post(name, payload, **params):
        return client.post("/upload/json", params=params,
                           files={"file": (name, io.BytesIO(json.dumps(payload).encode()), "application/json")}).json()

    first = post("a.json", [{"text": "Redis cache in front of replicas", "entities": ["Redis", "read replicas"]}],
                 recorded_at="2026-02-01T10:00:00Z")
    post("b.json", [{"text": "Diagram shows Redis and replicas", "visual_summary": "Redis -> replicas diagram",
                     "entities": ["redis", "read replica"]}])
    assert repo.get_source(first["source_id"]).attributes["recorded_at"].startswith("2026-02-01T10:00:00")

    seg_id = client.get(f"/sources/{first['source_id']}").json()["segments"][0]["id"]
    links = client.get(f"/segments/{seg_id}").json()["links"]
    depicts = [l for l in links if l["relation"]["relation"] == "depicts"]
    assert depicts and depicts[0]["source_filename"] == "b.json" and depicts[0]["modality"] == "json"
    assert depicts[0]["relation"]["attributes"]["shared_entities"]

    entity_id = client.get("/entities", params={"q": "redis"}).json()[0]["entity"]["id"]
    timeline = client.get(f"/entities/{entity_id}/timeline").json()
    assert len(timeline["entries"]) == 2
    assert timeline["entries"][0]["when"].startswith("2026-02-01")

    graph = client.get(f"/graph/{seg_id}", params={"depth": 2}).json()
    assert any(e["relation"] == "depicts" for e in graph["edges"])
    assert client.get("/graph/nope").status_code == 404
    assert client.post("/graph/relink").json()["depicts"] >= 1


def test_demo_seed_is_linked_across_modalities(client, repo):
    client.post("/demo/seed")
    stats = repo.stats().relations_by_type
    assert stats.get("depicts", 0) >= 2 and stats.get("corroborates", 0) >= 2
