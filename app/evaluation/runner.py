"""Run a gold question set against Weft and against conventional RAG.

Four systems are compared on identical data:

* ``text_rag``          conventional text-centric RAG: dense search over
                        extracted text only (transcripts, page text, OCR).
* ``dense_multimodal``  dense search over text *and* visual descriptions.
* ``hybrid``            + keyword (BM25) and entity matching, fused (RRF).
* ``weft``              + question decomposition, graph expansion and
                        confidence weighting: the full system.

For the ``demo`` corpus the bundled sample data is seeded into a temporary,
isolated store, so evaluation never touches the user's library and every
run sees exactly the same data.
"""

from __future__ import annotations

import json
import shutil
import statistics
import tempfile
import time
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.db.repository import KnowledgeRepository, get_repository
from app.evaluation import metrics
from app.evaluation.dataset import Dataset, EvidenceRef, load_dataset
from app.services import retrieval
from app.services.storage import storage_root
from app.services.vector_store import VectorStore, get_knowledge_vector_store

SYSTEMS: dict[str, str] = {
    "text_rag": "Text-only RAG (baseline)",
    "dense_multimodal": "Dense, text + visual",
    "hybrid": "Hybrid (dense + keyword + entity)",
    "weft": "Weft (hybrid + decomposition + graph)",
}
BASELINE = "text_rag"
FULL = "weft"


def latest_report_path() -> Path:
    return storage_root() / "eval" / "latest.json"


@contextmanager
def corpus(
    dataset: Dataset,
    *,
    repository: KnowledgeRepository | None = None,
    vector_store: VectorStore | None = None,
    embedding_function: Any | None = None,
) -> Iterator[tuple[KnowledgeRepository, VectorStore]]:
    """Yield the repository/index to evaluate against."""
    if repository is not None and vector_store is not None:
        yield repository, vector_store
        return
    if dataset.corpus == "current":
        yield get_repository(), get_knowledge_vector_store()
        return
    tmp = Path(tempfile.mkdtemp(prefix="weft-eval-"))
    repo = KnowledgeRepository(tmp / "knowledge.db")
    store = VectorStore(tmp / "chroma", "eval_segments", embedding_function=embedding_function)
    try:
        from test_data.seed_cross_modal_demo import seed_demo

        seed_demo(repository=repo, vector_store=store, attach_media=False)
        if dataset.distractors:
            add_distractors(dataset.distractors, repo, store)
        yield repo, store
    finally:
        repo.close()
        del store
        shutil.rmtree(tmp, ignore_errors=True)


def add_distractors(name: str, repo: KnowledgeRepository, store: VectorStore) -> int:
    """Ingest a pack of near-topic hard negatives into the evaluation corpus."""
    from app.evaluation.dataset import load_distractors
    from app.schemas.knowledge import KnowledgeNode, MediaModality, SourceAsset
    from app.services.ingestion import ingest_nodes

    count = 0
    for spec in load_distractors(name):
        modality = MediaModality(spec["modality"])
        nodes = [
            KnowledgeNode(
                modality=modality,
                source=spec["filename"],
                content=n.get("content") or n.get("transcript"),
                transcript=n.get("transcript") or n.get("content"),
                visual_summary=n.get("visual_summary"),
                timestamp=n.get("timestamp"),
                entities=n.get("entities", []),
            )
            for n in spec["nodes"]
        ]
        ingest_nodes(SourceAsset(filename=spec["filename"], modality=modality), nodes,
                     source_attributes={"eval_distractor": True}, repository=repo, vector_store=store)
        count += len(nodes)
    return count


def run_system(
    system: str, query: str, k: int, repo: KnowledgeRepository, store: VectorStore
) -> list[dict[str, Any]]:
    if system == "text_rag":
        raw = store.search_text_only(query, k, min_score=0.0, max_per_source=k)
        return retrieval.hydrate(raw, repository=repo, hide_visual=True)
    if system == "dense_multimodal":
        raw = store.search(query, k, min_score=0.0, max_per_source=k)
        return retrieval.hydrate(raw, repository=repo)
    if system == "hybrid":
        return retrieval.retrieve(
            query, k, expand=False, decompose_query=False, min_dense_score=0.0,
            repository=repo, vector_store=store,
        ).hits
    if system == "weft":
        return retrieval.retrieve(query, k, min_dense_score=0.0, repository=repo, vector_store=store).hits
    raise ValueError(f"Unknown system: {system}")


def _locator(hit: dict[str, Any]) -> dict[str, Any]:
    segment = hit.get("segment")
    return segment.locator.model_dump() if segment is not None else {}


def score_question(
    hits: list[dict[str, Any]],
    required: list[EvidenceRef],
    also_relevant: list[EvidenceRef],
    k: int,
    modality_of: dict[str, str],
) -> dict[str, Any]:
    found_required: set[int] = set()
    flags: list[bool] = []
    retrieved = []
    for rank, hit in enumerate(hits[:k], start=1):
        filename, loc = hit.get("source"), _locator(hit)
        req = [i for i, ref in enumerate(required) if ref.matches(filename, loc)]
        rel = bool(req) or any(ref.matches(filename, loc) for ref in also_relevant)
        found_required.update(req)
        flags.append(rel)
        retrieved.append({
            "rank": rank,
            "source": filename,
            "locator": hit.get("timestamp"),
            "modality": (hit.get("metadata") or {}).get("modality"),
            "relevant": rel,
            "required": bool(req),
            "via": (hit.get("via") or {}).get("relation"),
        })
    relevant_ranks = [r["rank"] for r in retrieved if r["relevant"]]
    needed_modalities = {modality_of.get(ref.source.casefold()) for ref in required} - {None}
    covered = {
        modality_of.get(required[i].source.casefold()) for i in found_required
    } - {None}
    return {
        "recall": metrics.recall_at_k(found_required, len(required)),
        "complete": metrics.complete_at_k(found_required, len(required)),
        "mrr": metrics.reciprocal_rank(relevant_ranks),
        "ndcg": metrics.ndcg_at_k(flags, len(required) + len(also_relevant), k),
        "precision": metrics.precision_at_k(flags, k),
        "modality_coverage": len(covered) / len(needed_modalities) if needed_modalities else 1.0,
        "found_required": [required[i].key() for i in sorted(found_required)],
        "missed_required": [ref.key() for i, ref in enumerate(required) if i not in found_required],
        "retrieved": retrieved,
    }


_METRICS = ("recall", "complete", "mrr", "ndcg", "precision", "modality_coverage")


def evaluate(
    dataset: Dataset | str = "demo",
    *,
    k: int = 5,
    systems: list[str] | None = None,
    with_answers: bool = False,
    repository: KnowledgeRepository | None = None,
    vector_store: VectorStore | None = None,
    embedding_function: Any | None = None,
) -> dict[str, Any]:
    data = load_dataset(dataset) if isinstance(dataset, str) else dataset
    names = systems or list(SYSTEMS)
    unknown = set(names) - set(SYSTEMS)
    if unknown:
        raise ValueError(f"Unknown systems: {sorted(unknown)}")

    with corpus(data, repository=repository, vector_store=vector_store, embedding_function=embedding_function) as (repo, store):
        modality_of = {s.source.filename.casefold(): s.source.modality.value for s in repo.list_sources()}
        corpus_segments = repo.stats().segments
        embedding = getattr(getattr(store, "embedding_function", None), "name", None)
        embedding = embedding() if callable(embedding) else type(getattr(store, "embedding_function", None)).__name__
        per_system: dict[str, list[dict[str, Any]]] = defaultdict(list)
        latencies: dict[str, list[float]] = defaultdict(list)
        questions_out = []
        for q in data.questions:
            entry = {"id": q.id, "type": q.type, "question": q.question,
                     "required": [r.key() for r in q.required], "results": {}}
            for name in names:
                started = time.perf_counter()
                hits = run_system(name, q.question, k, repo, store)
                latencies[name].append((time.perf_counter() - started) * 1000)
                scored = score_question(hits, q.required, q.also_relevant, k, modality_of)
                if with_answers and name in (BASELINE, FULL):
                    from app.services.answer_synthesizer import synthesize_answer

                    answer = synthesize_answer(q.question, hits)
                    scored["answer"] = answer.answer
                    scored["answer_method"] = answer.method
                    scored["answer_term_coverage"] = metrics.term_coverage(answer.answer, q.expected_terms)
                scored["type"] = q.type
                per_system[name].append(scored)
                entry["results"][name] = scored
            questions_out.append(entry)

    summary: dict[str, Any] = {}
    for name in names:
        rows = per_system[name]
        by_type: dict[str, dict[str, Any]] = {}
        for qtype in sorted({r["type"] for r in rows}):
            subset = [r for r in rows if r["type"] == qtype]
            by_type[qtype] = {m: round(metrics.mean([r[m] for r in subset]), 4) for m in ("recall", "complete", "mrr")}
            by_type[qtype]["n"] = len(subset)
        block = {m: round(metrics.mean([r[m] for r in rows]), 4) for m in _METRICS}
        block["latency_ms_p50"] = round(statistics.median(latencies[name]), 1) if latencies[name] else None
        answered = [r["answer_term_coverage"] for r in rows if r.get("answer_term_coverage") is not None]
        if answered:
            block["answer_term_coverage"] = round(metrics.mean(answered), 4)
        summary[name] = {"label": SYSTEMS[name], "metrics": block, "by_type": by_type}

    improvement = {}
    if BASELINE in summary and FULL in summary:
        base, full = summary[BASELINE]["metrics"], summary[FULL]["metrics"]
        improvement = {m: round(full[m] - base[m], 4) for m in _METRICS}

    return {
        "dataset": data.name,
        "description": data.description,
        "corpus": data.corpus + (f"+{data.distractors}" if data.distractors else ""),
        "corpus_segments": corpus_segments,
        "k": k,
        "question_count": len(data.questions),
        "embedding": embedding,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "systems": summary,
        "improvement_over_baseline": improvement,
        "questions": questions_out,
    }


def save_report(report: dict[str, Any], path: Path | None = None) -> Path:
    target = path or latest_report_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return target


def load_latest_report() -> dict[str, Any] | None:
    path = latest_report_path()
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def to_markdown(report: dict[str, Any]) -> str:
    k = report["k"]
    names = list(report["systems"])
    pct = lambda v: f"{v * 100:.0f}%"  # noqa: E731
    lines = [
        "# Evaluation",
        "",
        f"Dataset **{report['dataset']}**: {report['question_count']} questions over a {report.get('corpus_segments', '?')}-segment "
        f"corpus ({report['corpus']}), top-{k} retrieval, "
        f"embedding `{report['embedding']}`, generated {report['generated_at'][:19]}Z.",
        "",
        report.get("description", ""),
        "",
        f"| System | Recall@{k} | Complete@{k} | MRR | nDCG@{k} | Modality coverage | p50 latency |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name in names:
        m = report["systems"][name]["metrics"]
        lines.append(
            f"| {report['systems'][name]['label']} | {pct(m['recall'])} | {pct(m['complete'])} | "
            f"{m['mrr']:.2f} | {m['ndcg']:.2f} | {pct(m['modality_coverage'])} | {m['latency_ms_p50']} ms |"
        )
    if report.get("improvement_over_baseline"):
        d = report["improvement_over_baseline"]
        lines += [
            "",
            f"**Weft vs text-only RAG:** Recall@{k} {d['recall'] * 100:+.0f} pts, "
            f"Complete@{k} {d['complete'] * 100:+.0f} pts, MRR {d['mrr']:+.2f}, "
            f"modality coverage {d['modality_coverage'] * 100:+.0f} pts.",
        ]
    types = sorted({t for n in names for t in report["systems"][n]["by_type"]})
    lines += ["", f"## Complete@{k} by question type", "", "| Type | n | " + " | ".join(report["systems"][n]["label"] for n in names) + " |",
              "| --- | --- | " + " | ".join("---" for _ in names) + " |"]
    for t in types:
        n = report["systems"][names[0]]["by_type"].get(t, {}).get("n", 0)
        cells = [pct(report["systems"][s]["by_type"].get(t, {}).get("complete", 0.0)) for s in names]
        lines.append(f"| {t} | {n} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "Metrics: **Recall@k** share of required evidence retrieved; **Complete@k** share of questions where *all* "
        "required evidence (often spread over several modalities) was retrieved; **MRR** reciprocal rank of the first "
        "relevant hit; **nDCG@k** ranking quality; **modality coverage** share of the modalities a correct answer "
        "needs that appear among the retrieved required evidence.",
        "",
        "Regenerate with `python -m app.evaluation run --markdown EVALUATION.md`.",
    ]
    return "\n".join(lines) + "\n"
