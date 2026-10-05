"""Chroma vector index over ``SegmentRecord`` IDs.

This is an *index*, not a store: each Chroma entry is keyed by a segment ID
and carries only the metadata needed for filtering and de-duplication.  The
evidence itself (text, visual summary, locator, entities, relations) lives
in SQLite and is hydrated by ``app.services.retrieval``.  The whole index can
therefore be dropped and rebuilt from the database (``python -m app.cli
reindex``).

Two collections are maintained:

* ``segments``           -- embeds locator context + text + visual summary.
* ``segments_text_only`` -- embeds text only; the text-centric RAG baseline.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.schemas.records import SegmentRecord, SourceRecord


class VectorStoreError(RuntimeError):
    """Base error for unavailable local indexing or retrieval capabilities."""


class VectorStoreDependencyError(VectorStoreError):
    """Raised when Chroma's local embedding dependencies are unavailable."""


DEFAULT_COLLECTION = "segments"


def default_index_path() -> Path:
    configured = os.getenv("CHROMA_PATH")
    return Path(configured) if configured else Path.cwd() / "chroma_db"


@dataclass
class VectorHit:
    """A raw nearest-neighbour match; hydrate it before showing it to anyone."""

    segment_id: str
    distance: float
    similarity_score: float
    document: str
    metadata: dict[str, Any] = field(default_factory=dict)


def document_for_segment(segment: SegmentRecord, source: SourceRecord | None) -> str:
    """Multimodal embedding document: locator context, text, and visual evidence.

    The ``[modality | source | locator]`` prefix anchors very short segments
    (a 3-word speech span, a one-line OCR result) in embedding space so they
    do not score similarly against every query.
    """
    prefix_parts = [
        segment.modality.value,
        source.filename if source else None,
        segment.locator.display(),
    ]
    prefix = "[" + " | ".join(p for p in prefix_parts if p) + "]"
    body = [prefix, (segment.text or "").strip(), (segment.visual_summary or "").strip()]
    return "\n".join(p for p in body if p) or "Multimodal knowledge item"


def _metadata_for_segment(
    segment: SegmentRecord, source: SourceRecord | None
) -> dict[str, str | int | float | bool]:
    return {
        "segment_id": segment.id,
        "source_id": segment.source_id,
        "source": source.filename if source else "",
        "modality": segment.modality.value,
        "kind": segment.kind.value,
        "confidence": float(segment.confidence),
        "has_visual": segment.has_visual(),
    }


class VectorStore:
    """Persistent ChromaDB index keyed by segment ID."""

    # Results at or below this cosine similarity are indistinguishable from
    # noise for MiniLM-L6 embeddings and are suppressed.
    _MIN_SCORE = 0.30
    # Stop many short segments from one file flooding the top-k list.
    _MAX_PER_SOURCE = 2

    def __init__(
        self,
        persistence_path: str | Path | None = None,
        collection_name: str = DEFAULT_COLLECTION,
        embedding_function: Any | None = None,
    ) -> None:
        try:
            import chromadb
            from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
        except ImportError as exc:
            raise VectorStoreDependencyError(
                "chromadb must be installed to use VectorStore."
            ) from exc

        self.persistence_path = Path(persistence_path) if persistence_path else default_index_path()
        self.collection_name = collection_name
        self.text_only_collection_name = f"{collection_name}_text_only"
        try:
            self.persistence_path.mkdir(parents=True, exist_ok=True)
            self.client = chromadb.PersistentClient(path=str(self.persistence_path))
            self.embedding_function = embedding_function or DefaultEmbeddingFunction()
            self._open_collections()
        except Exception as exc:
            raise VectorStoreError("Unable to initialize the ChromaDB collections.") from exc

    def _open_collections(self) -> None:
        self.collection = self.client.get_or_create_collection(
            name=self.collection_name,
            metadata={"hnsw:space": "cosine"},
            embedding_function=self.embedding_function,
        )
        self.text_only_collection = self.client.get_or_create_collection(
            name=self.text_only_collection_name,
            metadata={"hnsw:space": "cosine"},
            embedding_function=self.embedding_function,
        )

    # ---------------------------------------------------------------- writes

    def index_segments(
        self, segments: list[SegmentRecord], sources: Mapping[str, SourceRecord]
    ) -> None:
        """Upsert embeddings for ``segments`` (idempotent by segment ID)."""
        if not segments:
            return
        try:
            ids = [s.id for s in segments]
            metadatas = [_metadata_for_segment(s, sources.get(s.source_id)) for s in segments]
            self.collection.upsert(
                ids=ids,
                documents=[document_for_segment(s, sources.get(s.source_id)) for s in segments],
                metadatas=metadatas,
            )
            text_entries = [
                (s.id, s.text.strip(), m)
                for s, m in zip(segments, metadatas, strict=True)
                if s.text and s.text.strip()
            ]
            if text_entries:
                self.text_only_collection.upsert(
                    ids=[e[0] for e in text_entries],
                    documents=[e[1] for e in text_entries],
                    metadatas=[e[2] for e in text_entries],
                )
        except Exception as exc:
            raise VectorStoreError("Unable to index segments in ChromaDB.") from exc

    def delete_segments(self, segment_ids: list[str]) -> None:
        if not segment_ids:
            return
        try:
            self.collection.delete(ids=segment_ids)
            self.text_only_collection.delete(ids=segment_ids)
        except Exception as exc:
            raise VectorStoreError("Unable to delete segments from ChromaDB.") from exc

    def reset(self) -> None:
        """Drop and recreate both collections (used by ``rebuild_index``)."""
        try:
            for name in (self.collection_name, self.text_only_collection_name):
                try:
                    self.client.delete_collection(name)
                except Exception:
                    pass  # Collection did not exist yet.
            self._open_collections()
        except Exception as exc:
            raise VectorStoreError("Unable to reset the ChromaDB collections.") from exc

    def count(self) -> int:
        return int(self.collection.count())

    # ----------------------------------------------------------------- reads

    def search(
        self,
        query: str,
        limit: int = 5,
        min_score: float | None = None,
        max_per_source: int | None = None,
    ) -> list[VectorHit]:
        """Nearest segments in the multimodal (text + visual) index."""
        return self._search(self.collection, query, limit, min_score, max_per_source)

    def search_text_only(
        self,
        query: str,
        limit: int = 5,
        min_score: float | None = None,
        max_per_source: int | None = None,
    ) -> list[VectorHit]:
        """Nearest segments when only extracted text was embedded (baseline)."""
        return self._search(self.text_only_collection, query, limit, min_score, max_per_source)

    def similar(
        self, text: str, *, exclude_source_id: str | None = None, limit: int = 8
    ) -> list[VectorHit]:
        """Nearest segments to ``text`` in the multimodal index, optionally from other sources only.

        Used by the cross-modal linker; no relevance threshold or per-source
        cap is applied here, the linker does its own scoring.
        """
        if not text.strip():
            return []
        try:
            available = self.collection.count()
            if available == 0:
                return []
            kwargs: dict[str, Any] = {
                "query_texts": [text],
                "n_results": min(limit, available),
                "include": ["documents", "metadatas", "distances"],
            }
            if exclude_source_id:
                kwargs["where"] = {"source_id": {"$ne": exclude_source_id}}
            results = self.collection.query(**kwargs)
        except Exception as exc:
            raise VectorStoreError("Unable to query the ChromaDB index.") from exc
        return [
            VectorHit(
                segment_id=str(segment_id),
                distance=float(distance),
                similarity_score=max(0.0, min(1.0, 1.0 - float(distance))),
                document=document or "",
                metadata=dict(metadata or {}),
            )
            for segment_id, document, metadata, distance in zip(
                results.get("ids", [[]])[0],
                results.get("documents", [[]])[0],
                results.get("metadatas", [[]])[0],
                results.get("distances", [[]])[0],
            )
        ]

    def _search(
        self,
        collection: Any,
        query: str,
        limit: int,
        min_score: float | None,
        max_per_source: int | None,
    ) -> list[VectorHit]:
        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("query must not be blank")
        if limit < 1:
            raise ValueError("limit must be at least 1")
        threshold = self._MIN_SCORE if min_score is None else min_score
        per_source = self._MAX_PER_SOURCE if max_per_source is None else max_per_source

        try:
            available = collection.count()
            if available == 0:
                return []
            results = collection.query(
                query_texts=[normalized_query],
                n_results=min(limit * 4, available),
                include=["documents", "metadatas", "distances"],
            )
        except Exception as exc:
            raise VectorStoreError("Unable to search the ChromaDB index.") from exc

        hits: list[VectorHit] = []
        per_source_counts: dict[str, int] = {}
        for segment_id, document, metadata, distance in zip(
            results.get("ids", [[]])[0],
            results.get("documents", [[]])[0],
            results.get("metadatas", [[]])[0],
            results.get("distances", [[]])[0],
        ):
            metadata = dict(metadata or {})
            numeric_distance = float(distance)
            score = max(0.0, 1.0 - numeric_distance)
            if score < threshold:
                continue
            source_key = str(metadata.get("source_id") or metadata.get("source") or "unknown")
            if per_source_counts.get(source_key, 0) >= per_source:
                continue
            per_source_counts[source_key] = per_source_counts.get(source_key, 0) + 1
            hits.append(
                VectorHit(
                    segment_id=str(segment_id),
                    distance=numeric_distance,
                    similarity_score=min(1.0, score),
                    document=document or "",
                    metadata=metadata,
                )
            )
            if len(hits) >= limit:
                break
        return hits


_knowledge_store_instance: VectorStore | None = None
_knowledge_store_lock = threading.Lock()


def get_knowledge_vector_store() -> VectorStore:
    """Process-wide vector index (``CHROMA_PATH``, default ``./chroma_db``)."""
    global _knowledge_store_instance
    with _knowledge_store_lock:
        if _knowledge_store_instance is None:
            _knowledge_store_instance = VectorStore()
        return _knowledge_store_instance


def reset_knowledge_vector_store(store: VectorStore | None = None) -> None:
    """Swap the process-wide vector index (tests, CLI tools)."""
    global _knowledge_store_instance
    with _knowledge_store_lock:
        _knowledge_store_instance = store
