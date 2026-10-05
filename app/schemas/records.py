"""Canonical knowledge representation persisted in the SQLite system of record.

The model is a small property graph:

* ``SourceRecord``   -- one uploaded asset (a video, a PDF, a JSON file, ...).
* ``SegmentRecord``  -- one unit of evidence extracted from a source: a
  transcript window aligned with a video frame, an audio speech span, a PDF
  page, an image, a JSON record.  Every segment carries its modality, an
  exact ``Locator`` back into the source, a confidence and the extractor
  that produced it.
* ``EntityRecord``   -- a named thing (system, person, metric, concept)
  mentioned by one or more segments, deduplicated across files by a
  normalized name.
* ``RelationRecord`` -- a typed, directed, confidence-weighted edge between
  any two of the above.

ChromaDB only stores embeddings keyed by ``SegmentRecord.id``; everything a
caller sees after retrieval is hydrated from these records.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.knowledge import MediaModality


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid4())


def format_seconds(seconds: float) -> str:
    """``75.4`` -> ``"01:15"``; switches to ``H:MM:SS`` past one hour."""
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


class SegmentKind(str, Enum):
    """What kind of evidence unit a segment is, independent of file type."""

    AV_WINDOW = "av_window"  # video: speech in a time window + the frame shown
    SPEECH = "speech"  # audio-only transcript span
    IMAGE = "image"  # standalone image: OCR text + visual description
    PAGE = "page"  # one PDF page: text layer + rendered/embedded visuals
    RECORD = "record"  # one structured JSON record
    NOTE = "note"  # free text file


class NodeKind(str, Enum):
    """Endpoint types a relation can connect."""

    SOURCE = "source"
    SEGMENT = "segment"
    ENTITY = "entity"


class RelationType(str, Enum):
    """Edge vocabulary of the knowledge graph.

    ``MENTIONS`` and ``NEXT`` are produced at ingestion time.  The remaining
    types are part of the schema contract and are produced by the
    cross-modal linking stage.
    """

    MENTIONS = "mentions"  # segment -> entity
    NEXT = "next"  # segment -> following segment in the same source
    CO_OCCURS = "co_occurs"  # speech <-> visual evidence at the same time
    DEPICTS = "depicts"  # visual segment -> segment/entity it illustrates
    CORROBORATES = "corroborates"  # segments in different sources stating the same fact
    SAME_AS = "same_as"  # entity <-> entity resolved as identical
    SUPERSEDES = "supersedes"  # later fact replaces an earlier one


class BoundingBox(BaseModel):
    """Image region in normalized ``[0, 1]`` coordinates (origin top-left)."""

    model_config = ConfigDict(extra="forbid")

    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    width: float = Field(gt=0.0, le=1.0)
    height: float = Field(gt=0.0, le=1.0)


class Locator(BaseModel):
    """Exact position of a segment inside its source.

    Any combination may be set: a video segment has a time range, a PDF
    segment a page (and later a region), a JSON record only a ``label``
    such as ``"ticket-4471"``.
    """

    model_config = ConfigDict(extra="forbid")

    start_seconds: float | None = Field(default=None, ge=0)
    end_seconds: float | None = Field(default=None, ge=0)
    page_number: int | None = Field(default=None, ge=1)
    region: BoundingBox | None = None
    label: str | None = None

    @model_validator(mode="after")
    def _validate_range(self) -> "Locator":
        if (
            self.start_seconds is not None
            and self.end_seconds is not None
            and self.end_seconds < self.start_seconds
        ):
            raise ValueError("end_seconds must be >= start_seconds")
        return self

    def is_empty(self) -> bool:
        return (
            self.start_seconds is None
            and self.end_seconds is None
            and self.page_number is None
            and self.region is None
            and not self.label
        )

    def display(self) -> str | None:
        """Human-readable locator, e.g. ``"02:10 - 02:34"`` or ``"Page 3"``."""
        parts: list[str] = []
        if self.start_seconds is not None and self.end_seconds is not None:
            parts.append(
                f"{format_seconds(self.start_seconds)} - {format_seconds(self.end_seconds)}"
            )
        elif self.start_seconds is not None:
            parts.append(format_seconds(self.start_seconds))
        if self.page_number is not None:
            parts.append(f"Page {self.page_number}")
        if self.label and not parts:
            parts.append(self.label)
        return ", ".join(parts) or None

    def sort_key(self) -> tuple[float, float]:
        """Order segments within a source by time first, then page."""
        return (
            self.start_seconds if self.start_seconds is not None else float("inf"),
            float(self.page_number) if self.page_number is not None else float("inf"),
        )


class SourceStatus(str, Enum):
    INDEXED = "indexed"
    INDEX_FAILED = "index_failed"


class SourceRecord(BaseModel):
    """An uploaded asset; the root of every provenance chain."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    filename: str = Field(min_length=1)
    modality: MediaModality
    content_type: str | None = None
    sha256: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    storage_path: str | None = None
    status: SourceStatus = SourceStatus.INDEXED
    ingested_at: datetime = Field(default_factory=_utc_now)
    attributes: dict[str, Any] = Field(default_factory=dict)


class SegmentRecord(BaseModel):
    """One retrievable unit of evidence, always traceable to its source."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    source_id: str
    modality: MediaModality
    kind: SegmentKind
    ordinal: int = Field(ge=0, description="Position of the segment within its source.")
    text: str | None = Field(
        default=None, description="Speech transcript, OCR text, page text or record text."
    )
    visual_summary: str | None = Field(
        default=None, description="Description of what is visually shown, if anything."
    )
    locator: Locator = Field(default_factory=Locator)
    frame_path: str | None = Field(
        default=None, description="URL path of the frame/page/image that is the visual evidence."
    )
    confidence: float = Field(ge=0.0, le=1.0)
    extractor: str = Field(description="Pipeline that produced the segment, e.g. 'whisper+gemini'.")
    attributes: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utc_now)

    def has_visual(self) -> bool:
        return bool(self.visual_summary and self.visual_summary.strip())


class EntityRecord(BaseModel):
    """A named concept shared across segments, files and modalities."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    name: str = Field(min_length=1, description="Display name, as first seen.")
    normalized_name: str = Field(min_length=1, description="Deduplication key.")
    entity_type: str = "concept"
    created_at: datetime = Field(default_factory=_utc_now)


class RelationRecord(BaseModel):
    """Directed, typed, confidence-weighted edge of the knowledge graph."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    src_kind: NodeKind
    src_id: str
    dst_kind: NodeKind
    dst_id: str
    relation: RelationType
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    attributes: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utc_now)


# ---------------------------------------------------------------------------
# Read models returned by the inspection API
# ---------------------------------------------------------------------------


class SourceSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: SourceRecord
    segment_count: int = Field(ge=0)


class SourceDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: SourceRecord
    segments: list[SegmentRecord] = Field(default_factory=list)


class EntityMention(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity: EntityRecord
    confidence: float = Field(ge=0.0, le=1.0)


class LinkedNode(BaseModel):
    """One edge seen from a given node, with the node on the other end."""

    model_config = ConfigDict(extra="forbid")

    relation: RelationRecord
    direction: str = Field(pattern="^(out|in)$")
    node_kind: NodeKind
    node_id: str
    label: str | None = None


class SegmentDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segment: SegmentRecord
    source: SourceRecord
    entities: list[EntityMention] = Field(default_factory=list)
    links: list[LinkedNode] = Field(default_factory=list)


class EntitySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity: EntityRecord
    mention_count: int = Field(ge=0)
    source_count: int = Field(ge=0)
    modalities: list[str] = Field(default_factory=list)


class EntityDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity: EntityRecord
    segments: list[SegmentRecord] = Field(default_factory=list)


class KnowledgeStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sources: int
    segments: int
    entities: int
    relations: int
    segments_by_modality: dict[str, int] = Field(default_factory=dict)
    relations_by_type: dict[str, int] = Field(default_factory=dict)
