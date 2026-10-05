"""Read/maintenance API over the structured knowledge representation.

These endpoints expose the system of record directly so the representation
can be inspected independently of search: which sources exist, the segments
each produced (with locator, confidence, extractor), the entities they
mention, and the typed relations between them.
"""

from urllib.parse import quote

from anyio import to_thread
from fastapi import APIRouter, HTTPException, Query, status

from app.db.repository import get_repository
from app.schemas.records import (
    EntityDetail,
    EntityMention,
    EntityTimeline,
    GraphView,
    EntitySummary,
    KnowledgeStats,
    LinkedNode,
    NodeKind,
    SegmentDetail,
    SourceDetail,
    SourceRecord,
    SourceSummary,
)
from app.services.graph import entity_timeline, neighbourhood, segment_label
from app.services.ingestion import delete_source
from app.services.vector_store import VectorStoreError

router = APIRouter(tags=["knowledge"])


def media_url(source: SourceRecord) -> str | None:
    """URL of the original upload under the ``/uploads`` mount, if it was stored."""
    key = source.storage_path or ""
    if not key.startswith("uploads/"):
        return None
    return "/" + "/".join(quote(part) for part in key.split("/"))


def _not_found(kind: str, item_id: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{kind} {item_id} not found")


@router.get("/stats", response_model=KnowledgeStats)
async def knowledge_stats() -> KnowledgeStats:
    return await to_thread.run_sync(lambda: get_repository().stats())


@router.get("/sources", response_model=list[SourceSummary])
async def list_sources() -> list[SourceSummary]:
    summaries = await to_thread.run_sync(lambda: get_repository().list_sources())
    for summary in summaries:
        summary.media_url = media_url(summary.source)
    return summaries


@router.get("/sources/{source_id}", response_model=SourceDetail)
async def get_source(source_id: str) -> SourceDetail:
    def load() -> SourceDetail | None:
        repo = get_repository()
        source = repo.get_source(source_id)
        if source is None:
            return None
        return SourceDetail(
            source=source, segments=repo.list_segments(source_id), media_url=media_url(source)
        )

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
        filenames: dict[str, str | None] = {source.id: source.filename}

        links: list[LinkedNode] = []
        for edge, direction in edges:
            node_kind = edge.dst_kind if direction == "out" else edge.src_kind
            node_id = edge.dst_id if direction == "out" else edge.src_id
            extra: dict[str, str | None] = {}
            label: str | None = None
            if node_kind is NodeKind.SEGMENT and node_id in segments:
                other = segments[node_id]
                if other.source_id not in filenames:
                    other_source = repo.get_source(other.source_id)
                    filenames[other.source_id] = other_source.filename if other_source else None
                label = segment_label(other, filenames[other.source_id])
                extra = {
                    "modality": other.modality.value,
                    "source_id": other.source_id,
                    "source_filename": filenames[other.source_id],
                    "locator": other.locator.display(),
                    "frame_path": other.frame_path,
                    "snippet": (other.text or other.visual_summary or "")[:240] or None,
                }
            elif node_kind is NodeKind.ENTITY and node_id in entities:
                label = entities[node_id].name
            links.append(
                LinkedNode(
                    relation=edge,
                    direction=direction,
                    node_kind=node_kind,
                    node_id=node_id,
                    label=label,
                    **extra,
                )
            )
        return SegmentDetail(
            segment=segment,
            source=source,
            entities=[EntityMention(entity=e, confidence=c) for e, c in mentions],
            links=links,
            media_url=media_url(source),
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


@router.get("/entities/{entity_id}/timeline", response_model=EntityTimeline)
async def get_entity_timeline(entity_id: str) -> EntityTimeline:
    """Every mention of an entity (and its same_as aliases) in time order.

    Sources are placed by ``recorded_at`` when it was given at upload, else
    by ingestion time; mentions inside a source by their offset.
    """
    timeline = await to_thread.run_sync(lambda: entity_timeline(get_repository(), entity_id))
    if timeline is None:
        raise _not_found("Entity", entity_id)
    return timeline


@router.get("/graph/{node_id}", response_model=GraphView, tags=["knowledge"])
async def get_graph(
    node_id: str,
    depth: int = Query(default=1, ge=1, le=3),
    limit: int = Query(default=120, ge=1, le=500),
    include_next: bool = Query(default=False, description="Include within-source 'next' edges."),
) -> GraphView:
    """Neighbourhood of a segment or entity: nodes and typed, weighted edges."""
    view = await to_thread.run_sync(
        lambda: neighbourhood(get_repository(), node_id, depth=depth, limit=limit, include_next=include_next)
    )
    if view is None:
        raise _not_found("Node", node_id)
    return view


@router.post("/graph/relink", tags=["knowledge"])
async def relink_graph() -> dict[str, int]:
    """Recompute every cross-modal and temporal link (e.g. after tuning thresholds)."""
    from app.services.linker import relink_all

    try:
        return await to_thread.run_sync(relink_all)
    except VectorStoreError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.post("/demo/seed", tags=["system"])
async def seed_demo_data() -> dict[str, int]:
    """Load the bundled cross-modal demo scenario (idempotent; no API keys needed)."""
    from app.config import get_settings

    if not get_settings().demo_enabled:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Demo data is disabled.")

    def seed() -> dict[str, int]:
        from test_data.seed_cross_modal_demo import seed_demo

        results = seed_demo()
        return {
            "sources": len(results),
            "segments": sum(len(r.segments) for r in results),
            "relations": sum(r.relation_count for r in results),
        }

    try:
        return await to_thread.run_sync(seed)
    except VectorStoreError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
