"""Phase 1: structured representation, persistence, and hydration."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.db.repository import KnowledgeRepository, normalize_entity_name
from app.schemas.knowledge import KnowledgeNode, MediaModality, SourceAsset, TemporalLocation
from app.schemas.records import Locator, RelationType, SegmentKind, SourceStatus
from app.services import retrieval
from app.services.ingestion import (
    delete_source,
    ingest_nodes,
    locator_from_node,
    rebuild_index,
)
from app.services.vector_store import VectorStoreError


def _video_nodes() -> list[KnowledgeNode]:
    # Deliberately out of order: ingestion must sort by time.
    return [
        KnowledgeNode(
            transcript="here is the read replica diagram",
            visual_summary="Diagram: primary database with two read replicas behind a cache",
            modality=MediaModality.VIDEO,
            timestamp="00:03 - 00:06",
            source="arch.mp4",
            frame_path="/frames/a.jpg",
            entities=["Read Replica", "Redis cache"],
            provenance={"frame_timestamp_seconds": 3.0, "window_end_seconds": 6.0},
        ),
        KnowledgeNode(
            transcript="we need to reduce database load",
            visual_summary="[Visual description unavailable]",
            modality=MediaModality.VIDEO,
            timestamp="00:00 - 00:03",
            source="arch.mp4",
            frame_path="/frames/b.jpg",
            entities=["database load"],
            provenance={"frame_timestamp_seconds": 0.0, "window_end_seconds": 3.0},
        ),
    ]


def _ingest_video(repo, store):
    asset = SourceAsset(filename="arch.mp4", modality=MediaModality.VIDEO)
    return ingest_nodes(asset, _video_nodes(), repository=repo, vector_store=store)


# ---------------------------------------------------------------- locators


@pytest.mark.parametrize(
    ("node_kwargs", "expected"),
    [
        ({"timestamp": "02:10 - 02:34"}, Locator(start_seconds=130, end_seconds=154)),
        ({"timestamp": "1:02:03"}, Locator(start_seconds=3723)),
        ({"timestamp": "Page 3"}, Locator(page_number=3)),
        ({"timestamp": "Pages 3-4"}, Locator(page_number=3, label="Pages 3-4")),
        ({"timestamp": "ticket-4471"}, Locator(label="ticket-4471")),
        ({"timestamp": 12.5}, Locator(start_seconds=12.5)),
        (
            {"timestamp": TemporalLocation(start_seconds=1, end_seconds=2)},
            Locator(start_seconds=1, end_seconds=2),
        ),
        (
            {"timestamp": "00:00 - 00:01", "provenance": {"start_seconds": 5, "end_seconds": 9}},
            Locator(start_seconds=5, end_seconds=9),
        ),
        ({"provenance": {"page_number": 7}}, Locator(page_number=7)),
        ({}, Locator()),
    ],
)
def test_locator_parsing(node_kwargs, expected):
    node = KnowledgeNode(modality=MediaModality.VIDEO, **node_kwargs)
    assert locator_from_node(node) == expected


def test_locator_display():
    assert Locator(start_seconds=130, end_seconds=154).display() == "02:10 - 02:34"
    assert Locator(start_seconds=3723).display() == "1:02:03"
    assert Locator(page_number=3).display() == "Page 3"
    assert Locator(label="ticket-4471").display() == "ticket-4471"
    assert Locator().display() is None


def test_entity_normalization():
    assert normalize_entity_name("Checkout-Service") == normalize_entity_name("checkout  service")
    assert normalize_entity_name("payments_API.") == "payments api"
    assert normalize_entity_name("  ") == ""


# --------------------------------------------------------------- ingestion


def test_ingest_builds_ordered_segments_with_provenance(repo, store, tmp_path: Path):
    media = tmp_path / "arch.mp4"
    media.write_bytes(b"fake-video-bytes")
    asset = SourceAsset(filename="arch.mp4", modality=MediaModality.VIDEO)
    result = ingest_nodes(asset, _video_nodes(), file_path=media, repository=repo, vector_store=store)

    source = repo.get_source(result.source.id)
    assert source is not None
    assert source.sha256 and len(source.sha256) == 64
    assert source.size_bytes == len(b"fake-video-bytes")
    assert source.status is SourceStatus.INDEXED

    segments = repo.list_segments(source.id)
    assert [s.ordinal for s in segments] == [0, 1]
    first, second = segments
    assert first.locator.start_seconds == 0 and first.locator.end_seconds == 3
    assert second.locator.start_seconds == 3
    assert all(s.kind is SegmentKind.AV_WINDOW for s in segments)
    assert all(s.extractor == "whisper+vision" for s in segments)

    # Failed vision placeholder is dropped and penalizes confidence.
    assert first.visual_summary is None
    assert first.attributes["visual_extraction_failed"] is True
    assert first.confidence < second.confidence
    assert second.visual_summary.startswith("Diagram")


def test_ingest_creates_mentions_and_next_relations(repo, store):
    result = _ingest_video(repo, store)
    segments = repo.list_segments(result.source.id)

    next_edges = repo.relations_for(segments[0].id, RelationType.NEXT)
    assert len(next_edges) == 1
    edge, direction = next_edges[0]
    assert direction == "out" and edge.dst_id == segments[1].id
    assert edge.attributes["gap_seconds"] == 0

    mentions = repo.entities_for_segments([s.id for s in segments])
    assert {e.name for e, _ in mentions[segments[1].id]} == {"Read Replica", "Redis cache"}
    assert result.entity_count == 3
    assert result.relation_count == 3 + 1  # 3 mentions + 1 next


def test_entities_are_shared_across_sources_and_modalities(repo, store):
    _ingest_video(repo, store)
    pdf_asset = SourceAsset(filename="design.pdf", modality=MediaModality.PDF)
    ingest_nodes(
        pdf_asset,
        [
            KnowledgeNode(
                content="Add read replicas to offload reporting queries.",
                modality=MediaModality.PDF,
                timestamp="Page 2",
                source="design.pdf",
                entities=["read-replica"],
            )
        ],
        repository=repo,
        vector_store=store,
    )
    summaries = {s.entity.normalized_name: s for s in repo.list_entities()}
    replica = summaries["read replica"]
    assert replica.mention_count == 2
    assert replica.source_count == 2
    assert replica.modalities == ["pdf", "video"]
    assert len(repo.segments_mentioning(replica.entity.id)) == 2


def test_confidence_priors_and_overrides(repo, store):
    asset = SourceAsset(filename="tickets.json", modality=MediaModality.JSON)
    result = ingest_nodes(
        asset,
        [
            KnowledgeNode(content="ticket a", modality=MediaModality.JSON),
            KnowledgeNode(content="ticket b", modality=MediaModality.JSON, confidence=0.42),
        ],
        repository=repo,
        vector_store=store,
    )
    a, b = repo.list_segments(result.source.id)
    assert a.confidence == 1.0 and a.attributes["confidence_source"] == "prior"
    assert b.confidence == 0.42 and b.attributes["confidence_source"] == "extractor"
    assert a.kind is SegmentKind.RECORD
    # JSON records have no time order, so no NEXT edges.
    assert repo.stats().relations_by_type.get("next") is None


def test_index_failure_keeps_records(repo, store):
    store.fail_next_index = True
    asset = SourceAsset(filename="arch.mp4", modality=MediaModality.VIDEO)
    with pytest.raises(VectorStoreError):
        ingest_nodes(asset, _video_nodes(), repository=repo, vector_store=store)
    source = repo.get_source(str(asset.source_id))
    assert source is not None and source.status is SourceStatus.INDEX_FAILED
    assert len(repo.list_segments(source.id)) == 2
    assert store.count() == 0

    assert rebuild_index(repository=repo, vector_store=store) == 2
    assert store.count() == 2
    assert repo.get_source(source.id).status is SourceStatus.INDEXED


def test_failed_transaction_rolls_back(repo):
    with pytest.raises(RuntimeError):
        with repo.transaction():
            repo.upsert_entity("ghost")
            raise RuntimeError("boom")
    assert repo.stats().entities == 0


def test_delete_source_removes_segments_edges_vectors_and_orphans(repo, store):
    result = _ingest_video(repo, store)
    assert delete_source(result.source.id, repository=repo, vector_store=store) == 2
    stats = repo.stats()
    assert (stats.sources, stats.segments, stats.relations, stats.entities) == (0, 0, 0, 0)
    assert store.count() == 0


# --------------------------------------------------------------- retrieval


def test_retrieval_hydrates_from_sqlite(repo, store):
    _ingest_video(repo, store)
    hits = retrieval.search("read replica diagram cache", 3, vector_store=store, repository=repo)
    top = hits[0]
    assert top["metadata"]["modality"] == "video"
    assert top["metadata"]["source"] == "arch.mp4"
    assert top["timestamp"] == "00:03 - 00:06"
    assert top["metadata"]["visual_summary"].startswith("Diagram")
    assert set(top["metadata"]["entities"]) == {"Read Replica", "Redis cache"}
    assert 0 < top["metadata"]["confidence"] <= 1


def test_text_only_baseline_hides_visual_evidence(repo, store):
    _ingest_video(repo, store)
    hits = retrieval.search("read replica diagram", 3, text_only=True, vector_store=store, repository=repo)
    assert hits and all(h["metadata"]["visual_summary"] is None for h in hits)


def test_stale_index_entries_are_dropped(repo, store):
    result = _ingest_video(repo, store)
    repo.delete_source(result.source.id)  # SQLite only; index left stale on purpose
    assert store.count() == 2
    assert retrieval.search("database load", 5, vector_store=store, repository=repo) == []


def test_repository_persists_across_connections(tmp_path: Path, store):
    path = tmp_path / "persist.db"
    first = KnowledgeRepository(path)
    asset = SourceAsset(filename="arch.mp4", modality=MediaModality.VIDEO)
    ingest_nodes(asset, _video_nodes(), repository=first, vector_store=store)
    first.close()
    second = KnowledgeRepository(path)
    try:
        assert second.stats().segments == 2
    finally:
        second.close()
