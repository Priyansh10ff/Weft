"""Real ChromaDB integration for the segment index.

Uses a deterministic hashing embedding so the test runs offline; semantic
quality with the default MiniLM model is covered by
``test_data/test_cross_modal_query.py``.
"""

from __future__ import annotations

import hashlib
import math
import re

import pytest

chromadb = pytest.importorskip("chromadb")

from chromadb import Documents, EmbeddingFunction, Embeddings  # noqa: E402

from app.schemas.knowledge import KnowledgeNode, MediaModality, SourceAsset  # noqa: E402
from app.services import retrieval  # noqa: E402
from app.services.ingestion import delete_source, ingest_nodes, rebuild_index  # noqa: E402
from app.services.vector_store import VectorStore  # noqa: E402

_DIM = 256


class HashingEmbedding(EmbeddingFunction[Documents]):
    def __init__(self) -> None:
        pass

    def __call__(self, input: Documents) -> Embeddings:  # noqa: A002
        vectors = []
        for text in input:
            vec = [0.0] * _DIM
            for token in re.findall(r"[a-z0-9]+", text.lower()):
                vec[int(hashlib.md5(token.encode()).hexdigest(), 16) % _DIM] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            vectors.append([v / norm for v in vec])
        return vectors

    @staticmethod
    def name() -> str:
        return "test-hashing"

    def get_config(self) -> dict:
        return {}

    @staticmethod
    def build_from_config(config: dict) -> "HashingEmbedding":
        return HashingEmbedding()


@pytest.fixture()
def chroma(tmp_path):
    return VectorStore(tmp_path / "chroma", "itest", embedding_function=HashingEmbedding())


def _ingest(repo, chroma, filename, modality, nodes):
    return ingest_nodes(
        SourceAsset(filename=filename, modality=modality), nodes, repository=repo, vector_store=chroma
    )


def test_chroma_round_trip(repo, chroma):
    video = _ingest(
        repo,
        chroma,
        "arch.mp4",
        MediaModality.VIDEO,
        [
            KnowledgeNode(
                transcript="this slide explains caching",
                visual_summary="redis cache diagram in front of postgres read replicas",
                modality=MediaModality.VIDEO,
                timestamp="00:10 - 00:20",
            ),
            KnowledgeNode(
                transcript="questions from the audience",
                modality=MediaModality.VIDEO,
                timestamp="00:20 - 00:30",
            ),
        ],
    )
    _ingest(
        repo,
        chroma,
        "notes.json",
        MediaModality.JSON,
        [KnowledgeNode(content="lunch menu for friday", modality=MediaModality.JSON)],
    )
    assert chroma.count() == 3

    hits = retrieval.search(
        "redis cache postgres replicas", 2, vector_store=chroma, repository=repo
    )
    assert hits[0]["metadata"]["source"] == "arch.mp4"
    assert hits[0]["timestamp"] == "00:10 - 00:20"

    # Visual-only words cannot be matched by the text-only baseline.
    baseline = retrieval.search(
        "redis cache postgres replicas", 2, text_only=True, vector_store=chroma, repository=repo
    )
    assert all(h["id"] != hits[0]["id"] for h in baseline)

    delete_source(video.source.id, repository=repo, vector_store=chroma)
    assert chroma.count() == 1

    chroma.reset()
    assert chroma.count() == 0
    assert rebuild_index(repository=repo, vector_store=chroma) == 1
    assert chroma.count() == 1
