"""Phase 5: gold-set evaluation against text-only RAG."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.evaluation import metrics
from app.evaluation.dataset import EvidenceRef, available_datasets, load_dataset
from app.evaluation.runner import evaluate, score_question, to_markdown

pytest.importorskip("chromadb")
from tests.test_chroma_index import HashingEmbedding  # noqa: E402


def test_evidence_ref_matching():
    page = EvidenceRef(source="spec.pdf", locator="Page 3")
    assert page.matches("spec.pdf", {"page_number": 3})
    assert not page.matches("spec.pdf", {"page_number": 4})
    assert not page.matches("other.pdf", {"page_number": 3})
    span = EvidenceRef(source="talk.mp4", locator="00:12-00:25")
    assert span.matches("talk.mp4", {"start_seconds": 12, "end_seconds": 25})
    assert span.matches("talk.mp4", {"start_seconds": 10, "end_seconds": 20})  # overlaps 8s
    assert not span.matches("talk.mp4", {"start_seconds": 0, "end_seconds": 12})
    point = EvidenceRef(source="talk.mp4", locator="00:15")
    assert point.matches("talk.mp4", {"start_seconds": 12, "end_seconds": 25})
    label = EvidenceRef(source="t.json", locator="ticket-4471")
    assert label.matches("t.json", {"label": "ticket-4471"}) and not label.matches("t.json", {"label": "x"})
    assert EvidenceRef(source="a.png").matches("A.PNG", {})


def test_metrics():
    assert metrics.recall_at_k({0, 2}, 4) == 0.5
    assert metrics.complete_at_k({0, 1}, 2) == 1.0 and metrics.complete_at_k({0}, 2) == 0.0
    assert metrics.reciprocal_rank([3, 5]) == pytest.approx(1 / 3)
    assert metrics.ndcg_at_k([True, False, True], 2, 3) == pytest.approx((1 + 1 / 2) / (1 + 1 / 1.58496), rel=1e-3)
    assert metrics.term_coverage("p99 fell to 310 ms", ["310", "4200"]) == 0.5


def test_bundled_dataset_is_valid():
    assert "demo" in available_datasets()
    data = load_dataset("demo")
    assert len(data.questions) >= 20
    assert {q.type for q in data.questions} >= {"cross_modal", "visual_only", "exact_identifier", "multi_part", "text"}


def test_score_question_counts_required_and_relevant():
    from app.schemas.records import Locator

    class Seg:
        def __init__(self, **loc):
            self.locator = Locator(**loc)

    hits = [
        {"source": "noise.json", "segment": Seg(), "metadata": {"modality": "json"}},
        {"source": "spec.pdf", "segment": Seg(page_number=3), "metadata": {"modality": "pdf"}},
        {"source": "talk.mp4", "segment": Seg(start_seconds=0, end_seconds=10), "metadata": {"modality": "video"}},
    ]
    required = [EvidenceRef(source="spec.pdf", locator="Page 3"), EvidenceRef(source="dash.png")]
    also = [EvidenceRef(source="talk.mp4")]
    out = score_question(hits, required, also, 5, {"spec.pdf": "pdf", "dash.png": "image", "talk.mp4": "video"})
    assert out["recall"] == 0.5 and out["complete"] == 0.0
    assert out["mrr"] == 0.5
    assert out["modality_coverage"] == 0.5
    assert out["missed_required"] == ["dash.png#*"]


def test_end_to_end_on_demo_corpus():
    report = evaluate("demo", k=5, embedding_function=HashingEmbedding())
    assert set(report["systems"]) == {"text_rag", "dense_multimodal", "hybrid", "weft"}
    assert report["question_count"] == len(report["questions"])
    weft = report["systems"]["weft"]["metrics"]
    base = report["systems"]["text_rag"]["metrics"]
    assert 0 <= base["recall"] <= 1 and 0 <= weft["recall"] <= 1
    # Visual-only questions are unanswerable from text alone.
    assert report["systems"]["text_rag"]["by_type"]["visual_only"]["complete"] <= report["systems"]["weft"]["by_type"]["visual_only"]["complete"]
    assert weft["complete"] >= base["complete"]
    md = to_markdown(report)
    assert "| Weft (hybrid + decomposition + graph) |" in md and "Complete@5 by question type" in md


def test_eval_api(repo, store, monkeypatch, tmp_path):
    import app.api.routes.evaluation as route
    from main import app

    real = route.evaluate
    monkeypatch.setattr(route, "evaluate", lambda name, **kw: real(name, embedding_function=HashingEmbedding(), **kw))
    client = TestClient(app)
    assert client.get("/eval/latest").status_code == 404
    assert "demo" in client.get("/eval/datasets").json()["datasets"]
    report = client.post("/eval/run", json={"dataset": "demo", "k": 3}).json()
    assert report["k"] == 3
    assert client.get("/eval/latest").json()["generated_at"] == report["generated_at"]
    assert client.post("/eval/run", json={"dataset": "nope"}).status_code == 404
