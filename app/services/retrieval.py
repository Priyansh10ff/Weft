"""Retrieval = vector search over segment IDs + hydration from SQLite.

Every hit returned here is rebuilt from the system of record, so callers get
the full provenance chain (segment -> source, locator, confidence, entities)
regardless of what the vector index happens to store.  Index entries whose
segment no longer exists in SQLite are dropped instead of being shown.
"""

from __future__ import annotations

from typing import Any

from app.db.repository import KnowledgeRepository, get_repository
from app.services.vector_store import VectorHit, VectorStore, get_knowledge_vector_store


def search(
    query: str,
    limit: int = 5,
    *,
    text_only: bool = False,
    vector_store: VectorStore | None = None,
    repository: KnowledgeRepository | None = None,
) -> list[dict[str, Any]]:
    """Return hydrated hits for ``query``.

    ``text_only=True`` searches the text-only baseline index and hides
    visual summaries from the result, so the baseline is shown exactly the
    evidence it was able to match on.
    """
    store = vector_store or get_knowledge_vector_store()
    raw = store.search_text_only(query, limit) if text_only else store.search(query, limit)
    return hydrate(raw, repository=repository, hide_visual=text_only)


def hydrate(
    hits: list[VectorHit],
    *,
    repository: KnowledgeRepository | None = None,
    hide_visual: bool = False,
) -> list[dict[str, Any]]:
    if not hits:
        return []
    repo = repository or get_repository()
    segment_ids = [h.segment_id for h in hits]
    segments = repo.get_segments(segment_ids)
    entities = repo.entities_for_segments(list(segments))
    sources = {
        sid: repo.get_source(sid) for sid in {s.source_id for s in segments.values()}
    }

    hydrated: list[dict[str, Any]] = []
    for hit in hits:
        segment = segments.get(hit.segment_id)
        if segment is None:
            continue  # stale index entry; SQLite is authoritative
        source = sources.get(segment.source_id)
        locator = segment.locator.display()
        visual = None if hide_visual else segment.visual_summary
        entity_names = [entity.name for entity, _ in entities.get(segment.id, [])]
        metadata = {
            "segment_id": segment.id,
            "source_id": segment.source_id,
            "source": source.filename if source else None,
            "modality": segment.modality.value,
            "kind": segment.kind.value,
            "transcript": segment.text,
            "visual_summary": visual,
            "timestamp": locator,
            "frame_path": segment.frame_path,
            "confidence": segment.confidence,
            "extractor": segment.extractor,
            "entities": entity_names,
        }
        hydrated.append(
            {
                "id": segment.id,
                "segment": segment,
                "document": hit.document,
                "metadata": metadata,
                "transcript": segment.text,
                "timestamp": locator,
                "frame_path": segment.frame_path,
                "source": metadata["source"],
                "distance": hit.distance,
                "similarity_score": hit.similarity_score,
            }
        )
    return hydrated
