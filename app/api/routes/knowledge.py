"""Read/maintenance API over the structured knowledge representation.

These endpoints expose the system of record directly so the representation
can be inspected independently of search: which sources exist, the segments
each produced (with locator, confidence, extractor), the entities they
mention, and the typed relations between them.
"""

from anyio import to_thread
from fastapi import APIRouter, HTTPException, Query, status

from app.db.repository import get_repository
from app.schemas.records import (
    EntityDetail,
    EntityMention,
    EntitySummary,
    KnowledgeStats,
    LinkedNode,
    NodeKind,
    SegmentDetail,
    SourceDetail,
    SourceSummary,
)
from app.services.ingestion import delete_source
from app.services.vector_store import VectorStoreError

router = APIRouter(tags=["knowledge"])


def _not_found(kind: str, item_id: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{kind} {item_id} not found")


@router.get("/stats", response_model=KnowledgeStats)
async def knowledge_stats() -> KnowledgeStats:
    return await to_thread.run_sync(lambda: get_repository().stats())


@router.get("/sources", response_model=list[SourceSummary])
async def list_sources() -> list[SourceSummary]:
    return await to_thread.run_sync(lambda: get_repository().list_sources())


@router.get("/sources/{source_id}", response_model=SourceDetail)
async def get_source(source_id: str) -> SourceDetail:
    def load() -> SourceDetail | None:
        repo = get_repository()
        source = repo.get_source(source_id)
        if source is None:
            return None
        return SourceDetail(source=source, segments=repo.list_segments(source_id))

    detail = await to_thread.run_sync(load)
    if detail is None:
        raise _not_found("Source", source_id)
    return detail


@router.delete("/sources/{source_id}", status_code=status.HTTP_200_OK)
async def remove_source(source_id: str) -> dict[str, int | str]:
    if await to_thread.run_sync(lambda: get_repository().get_source(source_id)) is None:
        raise _not_found("Source", source_id)
    try:
        deleted = await to_thread.run_sync(lambda: delete_source(source_id))
    except VectorStoreError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return {"source_id": source_id, "deleted_segments": deleted}


@router.get("/segments/{segment_id}", response_model=SegmentDetail)
async def get_segment(segment_id: str) -> SegmentDetail:
    def load() -> SegmentDetail | None:
        repo = get_repository()
        segment = repo.get_segment(segment_id)
        if segment is None:
            return None
        source = repo.get_source(segment.source_id)
        if source is None:
            return None
        mentions = repo.entities_for_segments([segment.id]).get(segment.id, [])

        edges = repo.relations_for(segment.id)
        other_segment_ids = [
            (edge.dst_id if direction == "out" else edge.src_id)
            for edge, direction in edges
            if (edge.dst_kind if direction == "out" else edge.src_kind) is NodeKind.SEGMENT
        ]
        other_entity_ids = [
            (edge.dst_id if direction == "out" else edge.src_id)
            for edge, direction in edges
            if (edge.dst_kind if direction == "out" else edge.src_kind) is NodeKind.ENTITY
        ]
        segments = repo.get_segments(other_segment_ids)
        entities = repo.get_entities(other_entity_ids)

        links: list[LinkedNode] = []
        for edge, direction in edges:
            node_kind = edge.dst_kind if direction == "out" else edge.src_kind
            node_id = edge.dst_id if direction == "out" else edge.src_id
            label: str | None = None
            if node_kind is NodeKind.SEGMENT and node_id in segments:
                other = segments[node_id]
                label = f"{other.modality.value} {other.locator.display() or ''}".strip()
            elif node_kind is NodeKind.ENTITY and node_id in entities:
                label = entities[node_id].name
            links.append(
                LinkedNode(
                    relation=edge,
                    direction=direction,
                    node_kind=node_kind,
                    node_id=node_id,
                    label=label,
                )
            )
        return SegmentDetail(
            segment=segment,
            source=source,
            entities=[EntityMention(entity=e, confidence=c) for e, c in mentions],
            links=links,
        )

    detail = await to_thread.run_sync(load)
    if detail is None:
        raise _not_found("Segment", segment_id)
    return detail


@router.get("/entities", response_model=list[EntitySummary])
async def list_entities(
    q: str | None = Query(default=None, description="Substring filter on the entity name."),
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[EntitySummary]:
    return await to_thread.run_sync(lambda: get_repository().list_entities(q, limit))


@router.get("/entities/{entity_id}", response_model=EntityDetail)
async def get_entity(entity_id: str) -> EntityDetail:
    def load() -> EntityDetail | None:
        repo = get_repository()
        entity = repo.get_entity(entity_id)
        if entity is None:
            return None
        return EntityDetail(entity=entity, segments=repo.segments_mentioning(entity_id))

    detail = await to_thread.run_sync(load)
    if detail is None:
        raise _not_found("Entity", entity_id)
    return detail
