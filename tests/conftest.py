"""Shared fixtures: an isolated SQLite repository and an in-memory vector index."""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.repository import KnowledgeRepository, reset_repository  # noqa: E402
from app.schemas.records import SegmentRecord, SourceRecord  # noqa: E402
from app.services.vector_store import (  # noqa: E402
    VectorHit,
    VectorStoreError,
    document_for_segment,
    reset_knowledge_vector_store,
)

_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN.findall(text.lower()))


class FakeVectorStore:
    """Deterministic stand-in for Chroma: Jaccard overlap instead of embeddings."""

    def __init__(self) -> None:
        self.multimodal: dict[str, tuple[str, dict]] = {}
        self.text_only: dict[str, tuple[str, dict]] = {}
        self.fail_next_index = False

    def index_segments(
        self, segments: list[SegmentRecord], sources: Mapping[str, SourceRecord]
    ) -> None:
        if self.fail_next_index:
            self.fail_next_index = False
            raise VectorStoreError("simulated index outage")
        for s in segments:
            meta = {"segment_id": s.id, "source_id": s.source_id, "modality": s.modality.value}
            self.multimodal[s.id] = (document_for_segment(s, sources.get(s.source_id)), meta)
            if s.text:
                self.text_only[s.id] = (s.text, meta)

    def delete_segments(self, segment_ids: list[str]) -> None:
        for sid in segment_ids:
            self.multimodal.pop(sid, None)
            self.text_only.pop(sid, None)

    def reset(self) -> None:
        self.multimodal.clear()
        self.text_only.clear()

    def count(self) -> int:
        return len(self.multimodal)

    def _search(self, pool: dict, query: str, limit: int) -> list[VectorHit]:
        q = _tokens(query)
        scored = []
        for sid, (doc, meta) in pool.items():
            d = _tokens(doc)
            score = len(q & d) / len(q | d) if q | d else 0.0
            if score > 0:
                scored.append(VectorHit(sid, 1 - score, score, doc, dict(meta)))
        scored.sort(key=lambda h: h.similarity_score, reverse=True)
        return scored[:limit]

    def search(self, query: str, limit: int = 5, **_: object) -> list[VectorHit]:
        return self._search(self.multimodal, query, limit)

    def search_text_only(self, query: str, limit: int = 5, **_: object) -> list[VectorHit]:
        return self._search(self.text_only, query, limit)


@pytest.fixture()
def repo(tmp_path: Path) -> Iterator[KnowledgeRepository]:
    repository = KnowledgeRepository(tmp_path / "knowledge.db")
    reset_repository(repository)
    yield repository
    reset_repository(None)
    repository.close()


@pytest.fixture()
def store() -> Iterator[FakeVectorStore]:
    fake = FakeVectorStore()
    reset_knowledge_vector_store(fake)  # type: ignore[arg-type]
    yield fake
    reset_knowledge_vector_store(None)


@pytest.fixture(autouse=True)
def _offline(monkeypatch, tmp_path):
    """No test may reach a real provider or the repo's ./data directory."""
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setenv("MEDIA_STORAGE_ROOT", str(tmp_path / "media"))
    from app.services import gemini

    gemini.reset_client()
    yield
    gemini.reset_client()


class FakeGemini:
    """Stands in for ``google.genai.Client``; ``responder(contents) -> dict``."""

    def __init__(self, responder):
        import json
        from types import SimpleNamespace

        outer = self
        self.calls: list[list] = []

        class _Models:
            def generate_content(self, model, contents, config):
                outer.calls.append(list(contents))
                return SimpleNamespace(text=json.dumps(responder(contents)))

        self.models = _Models()


@pytest.fixture()
def fake_gemini():
    return FakeGemini


def _fake_similar(self, text, *, exclude_source_id=None, limit=8):
    pool = {k: v for k, v in self.multimodal.items() if v[1].get("source_id") != exclude_source_id}
    return self._search(pool, text, limit)


FakeVectorStore.similar = _fake_similar
