"""Schemas that retain provenance and hierarchy for extracted multimodal data."""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MediaModality(str, Enum):
    """Supported source media categories."""

    VIDEO = "video"
    AUDIO = "audio"
    IMAGE = "image"
    PDF = "pdf"
    JSON = "json"


class TemporalLocation(BaseModel):
    """Where a datum occurs inside its original source.

    Video and audio normally use ``start_seconds``/``end_seconds``.  PDFs may
    use ``page_number``; images can leave this value as ``None``.
    """

    model_config = ConfigDict(extra="forbid")

    start_seconds: float | None = Field(default=None, ge=0)
    end_seconds: float | None = Field(default=None, ge=0)
    page_number: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_time_range(self) -> "TemporalLocation":
        """Prevent invalid media ranges from reaching the knowledge store."""
        if (
            self.start_seconds is not None
            and self.end_seconds is not None
            and self.end_seconds < self.start_seconds
        ):
            raise ValueError("end_seconds must be greater than or equal to start_seconds")
        return self


class SourceAsset(BaseModel):
    """The uploaded parent asset to which all derived knowledge is linked."""

    model_config = ConfigDict(extra="forbid")

    source_id: UUID = Field(default_factory=uuid4)
    filename: str = Field(min_length=1)
    modality: MediaModality
    content_type: str | None = None
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ExtractedKnowledgeBase(BaseModel):
    """Mandatory, provenance-preserving representation of extracted content.

    ``timestamp`` is intentionally present for every item but may be ``None``
    when a modality has no temporal location (for example, a whole image).
    ``source`` keeps the human-readable original filename or external ID.
    """

    model_config = ConfigDict(extra="forbid")

    content: str | dict[str, Any] = Field(
        ..., description="Extracted text or structured data from the source."
    )
    modality: MediaModality
    timestamp: TemporalLocation | None = Field(
        ...,
        description="Temporal or page location in the original source, if applicable.",
    )
    source: str = Field(
        ..., min_length=1, description="Original file name or source identifier."
    )
    confidence: float = Field(..., ge=0.0, le=1.0)
    source_id: UUID = Field(
        ..., description="Stable identifier of the parent SourceAsset."
    )
    parent_knowledge_id: UUID | None = Field(
        default=None, description="Containing extracted node, if this is a child segment."
    )


class ExtractedKnowledge(ExtractedKnowledgeBase):
    """An extracted node with explicit links to its source and parent node.

    These IDs mean transcript snippets, OCR blocks, captions, and PDF sections
    remain connected to both their original asset and any containing segment.
    """

    knowledge_id: UUID = Field(default_factory=uuid4)
    segment_index: int | None = Field(default=None, ge=0)
    attributes: dict[str, Any] = Field(default_factory=dict)


class KnowledgeNode(BaseModel):
    """A multimodal knowledge item suitable for vector indexing.

    This deliberately keeps the original extracted fields (text, source and
    timing) alongside optional visual context.  ``attributes`` preserves
    provider-specific details without making the vector-store schema brittle.
    """

    model_config = ConfigDict(extra="forbid")

    id: str | UUID | None = None
    content: str | dict[str, Any] | None = None
    transcript: str | None = None
    visual_summary: str | None = None
    modality: MediaModality | str
    timestamp: TemporalLocation | str | float | int | None = None
    source: str | None = None
    frame_path: str | None = None
    entities: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)
    attributes: dict[str, Any] = Field(default_factory=dict)
    confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Extractor-reported confidence; ingestion applies a per-extractor prior when unset.",
    )


class UploadAccepted(BaseModel):
    """Acknowledgement returned now; a worker can process it in a later phase."""

    model_config = ConfigDict(extra="forbid")

    upload_id: UUID
    status: Literal["accepted"] = "accepted"
    source_asset: SourceAsset
    extraction_started: bool = False


class UploadProcessingResult(BaseModel):
    """Synchronous processing response containing provenance-linked output."""

    model_config = ConfigDict(extra="forbid")

    upload_id: UUID
    status: Literal["completed", "partial"] = "completed"
    source_asset: SourceAsset
    extracted_knowledge: list[ExtractedKnowledgeBase] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    indexed_count: int = Field(default=0, ge=0)


class RetrievalHit(BaseModel):
    """A semantically ranked knowledge record returned by the local index."""

    model_config = ConfigDict(extra="forbid")

    record_id: str
    score: float = Field(ge=0.0, le=1.0)
    record: ExtractedKnowledgeBase


class QueryRequest(BaseModel):
    """Input accepted by the cross-modal retrieval endpoint."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=50)


class QueryResponse(BaseModel):
    """Cross-modal semantic search output with source provenance."""

    model_config = ConfigDict(extra="forbid")

    query: str
    results: list[RetrievalHit] = Field(default_factory=list)


class VideoUploadResponse(BaseModel):
    """Result returned after a video has been converted into knowledge nodes."""

    model_config = ConfigDict(extra="forbid")

    success: bool
    processed_nodes: int = Field(ge=0)
    source: str
    source_id: str | None = None
    entity_count: int = Field(default=0, ge=0)
    relation_count: int = Field(default=0, ge=0)


class KnowledgeQueryRequest(BaseModel):
    """Semantic query accepted by the KnowledgeNode Chroma collection."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    limit: int = Field(default=5, ge=1, le=50)


class KnowledgeQueryResult(BaseModel):
    """A display-ready multimodal Chroma search result."""

    model_config = ConfigDict(extra="forbid")

    transcript: str | None = None
    visual_summary: str | None = None
    timestamp: str | None = None
    frame_path: str | None = None
    source: str | None = None
    modality: str | None = None
    similarity_score: float = Field(ge=0.0, le=1.0)
    distance: float = Field(ge=0.0)
    segment_id: str | None = None
    source_id: str | None = None
    kind: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    entities: list[str] = Field(default_factory=list)


class AnswerSource(BaseModel):
    """One piece of evidence the synthesized answer drew on."""

    model_config = ConfigDict(extra="forbid")

    evidence_index: int | None = None
    cited: bool = True
    modality: str | None = None
    source: str | None = None
    locator: str | None = None
    similarity_score: float | None = None


class SynthesizedAnswerModel(BaseModel):
    """A grounded, cross-modal answer synthesized from retrieval evidence."""

    model_config = ConfigDict(extra="forbid")

    answer: str
    grounded: bool
    method: str
    sources: list[AnswerSource] = Field(default_factory=list)


class KnowledgeQueryResponse(BaseModel):
    """Clean response shape for knowledge-node retrieval."""

    model_config = ConfigDict(extra="forbid")

    query: str
    results: list[KnowledgeQueryResult] = Field(default_factory=list)
    answer: SynthesizedAnswerModel | None = None


class KnowledgeUploadResponse(BaseModel):
    """Result returned after an image or PDF is indexed as knowledge nodes."""

    model_config = ConfigDict(extra="forbid")

    success: bool
    processed_nodes: int = Field(ge=0)
    source: str
    source_id: str | None = None
    entity_count: int = Field(default=0, ge=0)
    relation_count: int = Field(default=0, ge=0)


class KnowledgeComparisonResponse(BaseModel):
    """Top result from multimodal retrieval alongside text-only retrieval."""

    model_config = ConfigDict(extra="forbid")

    query: str
    multimodal_result: KnowledgeQueryResult | None = None
    text_only_baseline_result: KnowledgeQueryResult | None = None
