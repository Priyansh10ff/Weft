"""Turn processor output into persisted, indexed knowledge.

Processors (video/audio/image/pdf/json) emit ``KnowledgeNode`` objects.  This
module is the single write path that converts them into the canonical
records of ``app.schemas.records``:

1. register the ``SourceRecord`` (with content hash and storage key),
2. convert every node into a ``SegmentRecord`` with a parsed ``Locator``,
   a confidence and the extractor that produced it,
3. upsert mentioned entities and add ``MENTIONS`` edges,
4. add ``NEXT`` edges between consecutive segments of time/page-ordered sources,
5. commit all of the above atomically to SQLite, then
6. embed the segments into the vector index.

If step 6 fails the records stay in SQLite with status ``index_failed`` and
``rebuild_index`` can re-embed them later; SQLite is the source of truth.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.db.repository import KnowledgeRepository, get_repository
from app.schemas.knowledge import KnowledgeNode, MediaModality, SourceAsset, TemporalLocation
from app.schemas.records import (
    Locator,
    NodeKind,
    RelationRecord,
    RelationType,
    SegmentKind,
    SegmentRecord,
    SourceRecord,
    SourceStatus,
)
from app.services.storage import storage_key
from app.services.vector_store import VectorStore, VectorStoreError, get_knowledge_vector_store

_log = logging.getLogger(__name__)

# Placeholder strings processors emit when a vision call fails.  They must
# never reach the index as if they were real visual evidence.
_VISUAL_PLACEHOLDERS = {"[visual description unavailable]"}

# Modalities whose segments have a natural reading order (time or page).
_ORDERED_MODALITIES = {MediaModality.VIDEO, MediaModality.AUDIO, MediaModality.PDF}

# Prior confidence per extraction pipeline, used until processors report a
# measured value on ``KnowledgeNode.confidence``.  Ordered roughly by how
# lossy each extraction step is: structured input is taken verbatim, a PDF
# text layer is exact, ASR and vision models are probabilistic.
_EXTRACTOR_PRIORS: dict[tuple[MediaModality, str], tuple[str, float]] = {
    (MediaModality.JSON, "record"): ("structured-input", 1.0),
    (MediaModality.JSON, "note"): ("plain-text", 1.0),
    (MediaModality.PDF, "text"): ("pdf-text-layer+vision", 0.9),
    (MediaModality.PDF, "visual"): ("pdf-vision-only", 0.7),
    (MediaModality.AUDIO, "speech"): ("whisper", 0.85),
    (MediaModality.VIDEO, "av"): ("whisper+vision", 0.8),
    (MediaModality.IMAGE, "vision"): ("vision-ocr", 0.75),
}
_VISUAL_FAILURE_PENALTY = 0.2


@dataclass
class IngestResult:
    source: SourceRecord
    segments: list[SegmentRecord] = field(default_factory=list)
    entity_count: int = 0
    relation_count: int = 0


# --------------------------------------------------------------------------
# Locator parsing
# --------------------------------------------------------------------------

_CLOCK = r"\d{1,2}(?::\d{2}){1,2}(?:\.\d+)?"
_RANGE_RE = re.compile(rf"^\s*({_CLOCK})\s*(?:-|–|—|to)\s*({_CLOCK})\s*$", re.IGNORECASE)
_SINGLE_RE = re.compile(rf"^\s*({_CLOCK})\s*$")
_PAGE_RE = re.compile(r"^\s*pages?\s*(\d+)(?:\s*(?:-|–|to)\s*(\d+))?\s*$", re.IGNORECASE)


def _clock_to_seconds(value: str) -> float:
    total = 0.0
    for part in value.split(":"):
        total = total * 60 + float(part)
    return total


def _as_float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _as_page(value: Any) -> int | None:
    try:
        page = None if value is None else int(value)
    except (TypeError, ValueError):
        return None
    return page if page and page >= 1 else None


def locator_from_node(node: KnowledgeNode) -> Locator:
    """Recover a structured ``Locator`` from a node's provenance and timestamp.

    Structured provenance fields win; the display string in ``timestamp``
    (``"02:10 - 02:34"``, ``"Page 3"``, ``"ticket-4471"``) is parsed as a
    fallback so no locator information is lost.
    """
    provenance = node.provenance or {}
    start = _as_float(provenance.get("start_seconds", provenance.get("frame_timestamp_seconds")))
    end = _as_float(provenance.get("end_seconds", provenance.get("window_end_seconds")))
    page = _as_page(provenance.get("page_number"))
    label: str | None = None

    timestamp = node.timestamp
    if isinstance(timestamp, TemporalLocation):
        start = start if start is not None else timestamp.start_seconds
        end = end if end is not None else timestamp.end_seconds
        page = page if page is not None else timestamp.page_number
    elif isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
        start = start if start is not None else float(timestamp)
    elif isinstance(timestamp, str) and timestamp.strip():
        text = timestamp.strip()
        if match := _RANGE_RE.match(text):
            if start is None and end is None:
                start, end = _clock_to_seconds(match[1]), _clock_to_seconds(match[2])
        elif match := _SINGLE_RE.match(text):
            if start is None:
                start = _clock_to_seconds(match[1])
        elif match := _PAGE_RE.match(text):
            if page is None:
                page = int(match[1])
            if match[2]:
                label = text  # keep "Pages 3-4" verbatim
        else:
            label = text

    if start is not None and end is not None and end < start:
        start, end = end, start
    return Locator(start_seconds=start, end_seconds=end, page_number=page, label=label)


# --------------------------------------------------------------------------
# Node -> Segment conversion
# --------------------------------------------------------------------------


def _node_text(node: KnowledgeNode) -> str | None:
    transcript = (node.transcript or "").strip()
    if transcript:
        return transcript
    content = node.content
    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False, sort_keys=True)
    content = (content or "").strip()
    return content or None


def _clean_visual(summary: str | None) -> tuple[str | None, bool]:
    """Return ``(summary, failed)`` with failure placeholders removed."""
    if not summary or not summary.strip():
        return None, False
    lines = [line for line in summary.splitlines() if line.strip()]
    kept = [line for line in lines if line.strip().casefold() not in _VISUAL_PLACEHOLDERS]
    failed = len(kept) < len(lines)
    return ("\n".join(kept).strip() or None), failed


def _kind_for(node: KnowledgeNode, modality: MediaModality) -> SegmentKind:
    if modality is MediaModality.VIDEO:
        return SegmentKind.AV_WINDOW
    if modality is MediaModality.AUDIO:
        return SegmentKind.SPEECH
    if modality is MediaModality.IMAGE:
        return SegmentKind.IMAGE
    if modality is MediaModality.PDF:
        return SegmentKind.PAGE
    if (node.provenance or {}).get("kind") == "plain_text_note":
        return SegmentKind.NOTE
    return SegmentKind.RECORD


def _extractor_and_prior(
    modality: MediaModality, kind: SegmentKind, has_text: bool
) -> tuple[str, float]:
    if modality is MediaModality.JSON:
        key = "note" if kind is SegmentKind.NOTE else "record"
    elif modality is MediaModality.PDF:
        key = "text" if has_text else "visual"
    elif modality is MediaModality.AUDIO:
        key = "speech"
    elif modality is MediaModality.VIDEO:
        key = "av"
    else:
        key = "vision"
    return _EXTRACTOR_PRIORS[(modality, key)]


def node_to_segment(
    node: KnowledgeNode, *, source_id: str, modality: MediaModality, ordinal: int
) -> SegmentRecord:
    kind = _kind_for(node, modality)
    text = _node_text(node)
    visual_summary, visual_failed = _clean_visual(node.visual_summary)
    extractor, prior = _extractor_and_prior(modality, kind, has_text=bool(text))

    confidence = node.confidence if node.confidence is not None else prior
    attributes: dict[str, Any] = dict(node.attributes)
    if node.provenance:
        attributes["provenance"] = node.provenance
    if visual_failed:
        attributes["visual_extraction_failed"] = True
        if node.confidence is None:
            confidence = max(0.0, confidence - _VISUAL_FAILURE_PENALTY)
    attributes["confidence_source"] = "extractor" if node.confidence is not None else "prior"

    return SegmentRecord(
        source_id=source_id,
        modality=modality,
        kind=kind,
        ordinal=ordinal,
        text=text,
        visual_summary=visual_summary,
        locator=locator_from_node(node),
        frame_path=(node.frame_path or "").strip() or None,
        confidence=round(confidence, 4),
        extractor=extractor,
        attributes=attributes,
    )


def _node_modality(node: KnowledgeNode, fallback: MediaModality) -> MediaModality:
    try:
        return MediaModality(
            node.modality.value if hasattr(node.modality, "value") else node.modality
        )
    except ValueError:
        return fallback


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def file_fingerprint(path: Path) -> tuple[str, int]:
    """SHA-256 and size of a file, streamed so large videos stay off the heap."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def build_source_record(asset: SourceAsset, file_path: Path | None = None) -> SourceRecord:
    sha256: str | None = None
    size: int | None = None
    key: str | None = None
    if file_path is not None and file_path.is_file():
        sha256, size = file_fingerprint(file_path)
        key = storage_key(file_path)
    return SourceRecord(
        id=str(asset.source_id),
        filename=asset.filename,
        modality=asset.modality,
        content_type=asset.content_type,
        sha256=sha256,
        size_bytes=size,
        storage_path=key,
        ingested_at=asset.received_at,
    )


def ingest_nodes(
    asset: SourceAsset,
    nodes: list[KnowledgeNode],
    *,
    file_path: Path | None = None,
    source_attributes: dict[str, Any] | None = None,
    repository: KnowledgeRepository | None = None,
    vector_store: VectorStore | None = None,
) -> IngestResult:
    """Persist ``nodes`` from one source and index them for retrieval.

    Raises ``VectorStoreError`` when embedding fails; the SQLite records are
    kept (status ``index_failed``) so the upload is not lost.
    """
    repo = repository or get_repository()
    source = build_source_record(asset, file_path)
    if source_attributes:
        source.attributes.update(source_attributes)

    modality_nodes = [(node, _node_modality(node, asset.modality)) for node in nodes]
    if asset.modality in _ORDERED_MODALITIES:
        modality_nodes.sort(key=lambda pair: locator_from_node(pair[0]).sort_key())

    segments = [
        node_to_segment(node, source_id=source.id, modality=modality, ordinal=index)
        for index, (node, modality) in enumerate(modality_nodes)
    ]

    relations: list[RelationRecord] = []
    entity_ids: set[str] = set()
    with repo.transaction():
        repo.upsert_source(source)
        repo.add_segments(segments)

        for segment, (node, _) in zip(segments, modality_nodes, strict=True):
            seen: set[str] = set()
            for name in node.entities:
                entity = repo.upsert_entity(str(name))
                if entity is None or entity.id in seen:
                    continue
                seen.add(entity.id)
                entity_ids.add(entity.id)
                relations.append(
                    RelationRecord(
                        src_kind=NodeKind.SEGMENT,
                        src_id=segment.id,
                        dst_kind=NodeKind.ENTITY,
                        dst_id=entity.id,
                        relation=RelationType.MENTIONS,
                        confidence=segment.confidence,
                        attributes={"surface_form": str(name)},
                    )
                )

        if asset.modality in _ORDERED_MODALITIES:
            for current, following in zip(segments, segments[1:]):
                attributes: dict[str, Any] = {}
                if (
                    current.locator.end_seconds is not None
                    and following.locator.start_seconds is not None
                ):
                    attributes["gap_seconds"] = round(
                        following.locator.start_seconds - current.locator.end_seconds, 3
                    )
                relations.append(
                    RelationRecord(
                        src_kind=NodeKind.SEGMENT,
                        src_id=current.id,
                        dst_kind=NodeKind.SEGMENT,
                        dst_id=following.id,
                        relation=RelationType.NEXT,
                        attributes=attributes,
                    )
                )

        repo.add_relations(relations)

    store = vector_store or get_knowledge_vector_store()
    try:
        store.index_segments(segments, {source.id: source})
    except VectorStoreError:
        repo.set_source_status(source.id, SourceStatus.INDEX_FAILED)
        source.status = SourceStatus.INDEX_FAILED
        _log.exception("Indexing failed for source %s (%s)", source.id, source.filename)
        raise

    return IngestResult(
        source=source,
        segments=segments,
        entity_count=len(entity_ids),
        relation_count=len(relations),
    )


def delete_source(
    source_id: str,
    *,
    repository: KnowledgeRepository | None = None,
    vector_store: VectorStore | None = None,
) -> int:
    """Remove a source everywhere; returns the number of segments deleted."""
    repo = repository or get_repository()
    segment_ids = repo.delete_source(source_id)
    (vector_store or get_knowledge_vector_store()).delete_segments(segment_ids)
    return len(segment_ids)


def rebuild_index(
    *,
    repository: KnowledgeRepository | None = None,
    vector_store: VectorStore | None = None,
) -> int:
    """Re-embed every segment in SQLite into a fresh vector index."""
    repo = repository or get_repository()
    store = vector_store or get_knowledge_vector_store()
    store.reset()
    sources = {summary.source.id: summary.source for summary in repo.list_sources()}
    total = 0
    for batch in repo.iter_all_segments():
        store.index_segments(batch, sources)
        total += len(batch)
    for source_id, source in sources.items():
        if source.status is not SourceStatus.INDEXED:
            repo.set_source_status(source_id, SourceStatus.INDEXED)
    return total
