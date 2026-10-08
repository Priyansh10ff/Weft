"""Hybrid, graph-aware retrieval over the multimodal knowledge base.

``retrieve`` answers a question in five steps:

1. **Decompose** multi-part questions ("what was decided, who explained it,
   and where was the diagram shown?") into sub-questions, each retrieved on
   its own so no part is drowned out by the others.
2. **Search three ways** for every (sub-)question:
   * dense: embedding similarity over text + visual descriptions (Chroma),
   * keyword: BM25 over text, visual descriptions, OCR, entity and speaker
     names (SQLite FTS5), which catches exact identifiers such as
     ``max_retries=5`` or ``ticket #4471`` that embeddings blur,
   * entity: segments that mention an entity named in the question.
3. **Fuse** the rankings with reciprocal rank fusion (RRF), which needs no
   score calibration between the three very different scorers.
4. **Expand along the graph**: the strongest hits pull in the evidence the
   linker connected to them (the slide shown while it was said, the PDF
   page that explains it), with a decayed score and a record of *why*.
5. **Weight by confidence** and keep source diversity, then attach precise
   provenance: the best-matching sentence (with its own timestamp) inside a
   video window, and the best-matching OCR blocks (with bounding boxes)
   inside an image or page.

Every hit is hydrated from SQLite, and carries the signals that produced
it, so the answer layer and the UI can explain each piece of evidence.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from app.db.repository import KnowledgeRepository, get_repository, normalize_entity_name
from app.schemas.records import RelationType, SegmentRecord
from app.services.vector_store import VectorHit, VectorStore, VectorStoreError, get_knowledge_vector_store

RRF_K = 60
WEIGHTS = {"dense": 1.0, "keyword": 1.0, "entity": 0.8}
EXPANSION_SEEDS = 5
EXPANSION_DECAY = {
    RelationType.DEPICTS: 0.75,
    RelationType.CO_OCCURS: 0.7,
    RelationType.SHOWS_SAME: 0.6,
    RelationType.CORROBORATES: 0.6,
}
MAX_PER_SOURCE = 3
CANDIDATES = 30
# Sub-questions refine the full question rather than outvote it: together they
# carry this share of one vote.
SUBQUERY_SHARE = 0.5
# A sub-question's best hit is reserved a slot only if it also ranks in the
# full question's top COVERAGE_POOL candidates (keeps decoys out).
COVERAGE_POOL = 12
# Link reinforcement: a candidate linked to a top seed gains this fraction of
# the seed's decayed score.
LINK_BOOST = 0.05

_STOPWORDS = {
    "a", "about", "after", "again", "all", "also", "an", "and", "any", "are", "as", "at", "be",
    "because", "been", "before", "being", "between", "both", "but", "by", "can", "could", "did",
    "do", "does", "doing", "during", "each", "for", "from", "further", "had", "has", "have",
    "having", "he", "her", "here", "him", "his", "how", "i", "if", "in", "into", "is", "it", "its",
    "just", "know", "me", "more", "most", "my", "no", "nor", "not", "now", "of", "off", "on",
    "once", "only", "or", "other", "our", "out", "over", "own", "same", "she", "should", "so",
    "some", "such", "than", "that", "the", "their", "them", "then", "there", "these", "they",
    "this", "those", "through", "to", "too", "under", "until", "up", "very", "was", "we", "were",
    "what", "when", "where", "which", "while", "who", "whom", "why", "will", "with", "would",
    "you", "your", "shown", "show", "said", "say", "tell", "explain", "explained", "discussed",
    # Generic question words that match everything in meeting/incident data.
    "team", "issue", "problem", "thing", "things", "way", "work", "worked", "know", "look",
    "like", "use", "used", "get", "got", "make", "made", "happen", "happened",
}
_WORD = re.compile(r"[a-z0-9]+")
_SPLIT = re.compile(
    r"(?:\?\s+|[,;]\s*(?:and\s+)?|\s+and\s+)(?=(?:who|what|where|when|why|how|which|did|does|is|are|was|were)\b)",
    re.IGNORECASE,
)


def tokens(text: str) -> list[str]:
    return [t for t in _WORD.findall(text.lower()) if len(t) > 1 and t not in _STOPWORDS]


# ------------------------------------------------------------------- planning


def decompose(query: str) -> list[str]:
    """Split a multi-part question at clause boundaries that start a new question.

    ``"What architecture reduced DB load, who explained it, and where was the
    diagram shown?"`` -> three sub-questions. Parts with fewer than two
    words are merged back into the previous part.
    """
    parts = [p.strip(" ?,;") for p in _SPLIT.split(query) if p and p.strip(" ?,;")]
    merged: list[str] = []
    for part in parts:
        if merged and len(part.split()) < 3:
            merged[-1] = f"{merged[-1]} {part}"
        else:
            merged.append(part)
    if len(merged) <= 1:
        return [query.strip()]
    # Later parts often refer back ("did *it* help?") or are too thin to stand
    # alone ("what were the results?"): carry the first part's topic into them.
    topic = " ".join(tokens(merged[0])[:6])
    head = set(tokens(merged[0]))
    return [merged[0]] + [part if head & set(tokens(part)) else f"{part} ({topic})" for part in merged[1:]]


def fts_query(text: str) -> str:
    """FTS5 MATCH expression: OR of quoted content words, prefix match for long ones."""
    words = list(dict.fromkeys(tokens(text)))[:16]
    return " OR ".join(f'"{w}"*' if len(w) >= 5 else f'"{w}"' for w in words)


# --------------------------------------------------------------------- signals


@dataclass
class Candidate:
    segment_id: str
    ranks: dict[str, int] = field(default_factory=dict)
    similarity: float | None = None
    keyword_score: float | None = None
    matched_entities: set[str] = field(default_factory=set)
    rrf: float = 0.0
    via: dict[str, Any] | None = None
    boosts: list[str] = field(default_factory=list)


def _dense(
    store: VectorStore | None, query: str, limit: int, min_score: float | None = None
) -> list[VectorHit]:
    if store is None:
        return []
    try:
        return store.search(query, limit, min_score=min_score, max_per_source=limit)
    except (VectorStoreError, ValueError):
        return []


def _entity_matches(repo: KnowledgeRepository, query: str) -> dict[str, set[str]]:
    """``segment_id -> {entity names}`` for entities named in the query."""
    from app.services.linker import _singular

    def key(text: str) -> str:
        return " ".join(_singular(w) for w in normalize_entity_name(text).split())

    normalized = f" {key(query)} "
    hits = [
        e for e in repo.all_entities()
        if len(e.normalized_name) > 2 and f" {key(e.normalized_name)} " in normalized
    ]
    if not hits:
        return {}
    groups = repo.same_as_groups()
    wanted = {groups.get(e.id, e.id) for e in hits}
    ids = [eid for eid, g in groups.items() if g in wanted] + [e.id for e in hits]
    names = {e.id: e.name for e in hits}
    result: dict[str, set[str]] = defaultdict(set)
    for segment_id, entity_id in repo.mentions_of(sorted(set(ids))):
        result[segment_id].add(names.get(entity_id) or names.get(groups.get(entity_id, ""), "") or "entity")
    return result


def search_one(
    query: str,
    *,
    repo: KnowledgeRepository,
    store: VectorStore | None,
    hybrid: bool = True,
    candidates: int = CANDIDATES,
    min_dense_score: float | None = None,
) -> dict[str, Candidate]:
    """Fuse dense, keyword and entity rankings for one (sub-)question."""
    pool: dict[str, Candidate] = {}

    def add(kind: str, ordered_ids: list[str]) -> None:
        for rank, segment_id in enumerate(ordered_ids, start=1):
            cand = pool.setdefault(segment_id, Candidate(segment_id))
            if kind not in cand.ranks:
                cand.ranks[kind] = rank
                cand.rrf += WEIGHTS[kind] / (RRF_K + rank)

    dense = _dense(store, query, candidates, min_dense_score)
    add("dense", [h.segment_id for h in dense])
    for hit in dense:
        pool[hit.segment_id].similarity = hit.similarity_score

    if hybrid:
        keyword = repo.keyword_search(fts_query(query), candidates)
        add("keyword", [sid for sid, _ in keyword])
        for sid, score in keyword:
            pool[sid].keyword_score = round(score, 4)
        entity = _entity_matches(repo, query)
        ordered = sorted(entity, key=lambda sid: len(entity[sid]), reverse=True)[:candidates]
        add("entity", ordered)
        for sid in ordered:
            pool[sid].matched_entities |= entity[sid]
    return pool


def _via(relation: Any, direction: str, seed: str) -> dict[str, Any]:
    return {
        "relation": relation.relation.value,
        "direction": direction,
        "from_segment_id": seed,
        "shared_entities": relation.attributes.get("shared_entities") or [],
    }


def _rrf_max(kinds: int) -> float:
    return sum(sorted(WEIGHTS.values(), reverse=True)[:kinds]) / (RRF_K + 1)


# ------------------------------------------------------------------- precision


def best_span(segment: SegmentRecord, query_terms: set[str]) -> dict[str, Any] | None:
    """The sentence inside a video window that best matches the question."""
    spans = segment.attributes.get("speech_segments") or []
    best, best_score = None, 0.0
    for span in spans:
        words = set(tokens(str(span.get("text", ""))))
        if not words:
            continue
        score = len(words & query_terms) / (len(query_terms) or 1)
        if score > best_score:
            best, best_score = span, score
    if best is None or len(spans) < 2:
        return None
    return {
        "start_seconds": best.get("start_seconds"),
        "end_seconds": best.get("end_seconds"),
        "text": best.get("text"),
        "speaker": best.get("speaker"),
        "score": round(best_score, 3),
    }


def best_regions(segment: SegmentRecord, query_terms: set[str], top: int = 3) -> list[dict[str, Any]]:
    """OCR blocks (with bounding boxes) that contain the question's words."""
    scored = []
    for block in segment.attributes.get("ocr_blocks") or []:
        if not block.get("box"):
            continue
        words = set(tokens(str(block.get("text", ""))))
        overlap = len(words & query_terms)
        if overlap:
            scored.append((overlap / (len(query_terms) or 1), block))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [{"text": b["text"], "box": b["box"], "score": round(s, 3)} for s, b in scored[:top]]


# ---------------------------------------------------------------------- driver


@dataclass
class RetrievalResult:
    query: str
    subqueries: list[str]
    hits: list[dict[str, Any]]
    strategy: dict[str, Any]


def retrieve(
    query: str,
    limit: int = 8,
    *,
    hybrid: bool = True,
    expand: bool = True,
    decompose_query: bool = True,
    modalities: list[str] | None = None,
    min_dense_score: float | None = None,
    vector_store: VectorStore | None = None,
    repository: KnowledgeRepository | None = None,
) -> RetrievalResult:
    query = query.strip()
    if not query:
        raise ValueError("query must not be blank")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    repo = repository or get_repository()
    try:
        store = vector_store or get_knowledge_vector_store()
    except VectorStoreError:
        store = None

    subqueries = decompose(query) if decompose_query else [query]
    runs = [search_one(query, repo=repo, store=store, hybrid=hybrid, min_dense_score=min_dense_score)]
    if len(subqueries) > 1:
        runs += [
            search_one(q, repo=repo, store=store, hybrid=hybrid, min_dense_score=min_dense_score)
            for q in subqueries
        ]

    # Fuse across the full question and its sub-questions (RRF again, over
    # each run's own ordering), keeping the best signals seen for each hit.
    fused: dict[str, Candidate] = {}
    sub_weight = SUBQUERY_SHARE / max(1, len(runs) - 1)
    for i, run in enumerate(runs):
        weight = 1.0 if i == 0 else sub_weight
        ordered = sorted(run.values(), key=lambda c: c.rrf, reverse=True)
        for rank, cand in enumerate(ordered, start=1):
            merged = fused.setdefault(cand.segment_id, Candidate(cand.segment_id))
            merged.rrf += weight / (RRF_K + rank) * (cand.rrf / _rrf_max(3))
            for kind, r in cand.ranks.items():
                merged.ranks[kind] = min(r, merged.ranks.get(kind, r))
            if cand.similarity is not None:
                merged.similarity = max(cand.similarity, merged.similarity or 0.0)
            if cand.keyword_score is not None:
                merged.keyword_score = max(cand.keyword_score, merged.keyword_score or 0.0)
            merged.matched_entities |= cand.matched_entities

    segments = repo.get_segments(list(fused))
    fused = {sid: c for sid, c in fused.items() if sid in segments}
    if modalities:
        allowed = set(modalities)
        fused = {sid: c for sid, c in fused.items() if segments[sid].modality.value in allowed}

    top = max((c.rrf for c in fused.values()), default=1.0) or 1.0
    scores = {sid: c.rrf / top for sid, c in fused.items()}

    # Graph expansion from the strongest seeds.
    expanded = 0
    if expand and scores:
        seeds = sorted(scores, key=scores.get, reverse=True)[:EXPANSION_SEEDS]
        seed_set = set(seeds)
        for seed in seeds:
            for relation, direction in repo.relations_for(seed):
                decay = EXPANSION_DECAY.get(relation.relation)
                if decay is None:
                    continue
                other = relation.dst_id if direction == "out" else relation.src_id
                score = scores[seed] * decay * relation.confidence
                if other not in segments:
                    found = repo.get_segment(other)
                    if found is None:
                        continue
                    if modalities and found.modality.value not in set(modalities):
                        continue
                    segments[other] = found
                if other in scores:
                    # Already a candidate: corroboration from a top hit in another
                    # modality lifts it instead of being ignored. Seeds keep
                    # their own order; only the tail is lifted.
                    lift = 0.0 if other in seed_set else LINK_BOOST * score
                    if lift > 0:
                        scores[other] += lift
                        fused[other].boosts.append(relation.relation.value)
                        if not fused[other].ranks and fused[other].via is None:
                            fused[other].via = _via(relation, direction, seed)
                    continue
                # A direct hit reachable through a link gets reinforced; a
                # segment found *only* through the graph records why.
                direct = other in fused and bool(fused[other].ranks)
                if other not in fused:
                    fused[other] = Candidate(other)
                    expanded += 1
                if not direct:
                    fused[other].via = _via(relation, direction, seed)
                scores[other] = score

    # Confidence weighting + per-source diversity.
    final = {sid: scores[sid] * (0.6 + 0.4 * segments[sid].confidence) for sid in scores}
    ordered = sorted(final, key=final.get, reverse=True)
    cap = MAX_PER_SOURCE if limit > 5 else 2
    picked: list[str] = []
    per_source: dict[str, int] = defaultdict(int)

    def take(sid: str) -> bool:
        src = segments[sid].source_id
        if sid in picked or sid not in final or per_source[src] >= cap:
            return False
        per_source[src] += 1
        picked.append(sid)
        return True

    # Coverage: each part of a multi-part question gets its best hit a slot,
    # provided the full question also ranks it (a decoy that only matches a
    # thin sub-question does not qualify).
    if len(runs) > 1 and COVERAGE_POOL:
        pool = set(sorted(runs[0], key=lambda sid: runs[0][sid].rrf, reverse=True)[:COVERAGE_POOL])
        for run in runs[1:]:
            if len(picked) >= limit - 1:
                break
            for cand in sorted(run.values(), key=lambda c: c.rrf, reverse=True)[:3]:
                if cand.segment_id in pool and (cand.segment_id in picked or take(cand.segment_id)):
                    break
    for sid in ordered:
        if len(picked) >= limit:
            break
        take(sid)
    picked.sort(key=final.get, reverse=True)

    terms = set(tokens(query))
    hits = hydrate_segments(
        picked,
        repository=repo,
        extra={
            sid: {
                "score": round(final[sid], 4),
                "signals": {
                    "dense_rank": fused[sid].ranks.get("dense"),
                    "keyword_rank": fused[sid].ranks.get("keyword"),
                    "entity_rank": fused[sid].ranks.get("entity"),
                    "keyword_score": fused[sid].keyword_score,
                    "matched_entities": sorted(fused[sid].matched_entities),
                    "link_boosts": sorted(set(fused[sid].boosts)),
                },
                "via": fused[sid].via,
                "similarity_score": fused[sid].similarity or 0.0,
                "matched_span": best_span(segments[sid], terms),
                "matched_regions": best_regions(segments[sid], terms),
            }
            for sid in picked
        },
    )
    return RetrievalResult(
        query=query,
        subqueries=subqueries if len(subqueries) > 1 else [],
        hits=hits,
        strategy={
            "hybrid": hybrid,
            "expanded": expand,
            "expanded_hits": sum(1 for h in hits if h.get("via")),
            "subqueries": len(subqueries) if len(subqueries) > 1 else 0,
            "candidates": len(fused),
        },
    )


# ------------------------------------------------------------------- hydration


def hydrate_segments(
    segment_ids: list[str],
    *,
    repository: KnowledgeRepository | None = None,
    extra: dict[str, dict[str, Any]] | None = None,
    hide_visual: bool = False,
) -> list[dict[str, Any]]:
    """Build the hit dicts used by the API and the answer synthesizer."""
    if not segment_ids:
        return []
    repo = repository or get_repository()
    segments = repo.get_segments(segment_ids)
    entities = repo.entities_for_segments(list(segments))
    sources = {sid: repo.get_source(sid) for sid in {s.source_id for s in segments.values()}}
    extra = extra or {}

    hits: list[dict[str, Any]] = []
    for segment_id in segment_ids:
        segment = segments.get(segment_id)
        if segment is None:
            continue
        source = sources.get(segment.source_id)
        info = extra.get(segment_id, {})
        locator = segment.locator.display()
        speakers = [s.get("name") or s.get("label") for s in segment.attributes.get("speakers") or []]
        metadata = {
            "segment_id": segment.id,
            "source_id": segment.source_id,
            "source": source.filename if source else None,
            "modality": segment.modality.value,
            "kind": segment.kind.value,
            "transcript": segment.text,
            "visual_summary": None if hide_visual else segment.visual_summary,
            "timestamp": locator,
            "frame_path": segment.frame_path,
            "confidence": segment.confidence,
            "extractor": segment.extractor,
            "entities": [entity.name for entity, _ in entities.get(segment.id, [])],
            "speakers": [s for s in speakers if s],
        }
        similarity = float(info.get("similarity_score", 0.0) or 0.0)
        hits.append(
            {
                "id": segment.id,
                "segment": segment,
                "document": info.get("document", ""),
                "metadata": metadata,
                "transcript": segment.text,
                "timestamp": locator,
                "frame_path": segment.frame_path,
                "source": metadata["source"],
                "distance": max(0.0, 1.0 - similarity),
                "similarity_score": similarity,
                **{k: v for k, v in info.items() if k not in {"similarity_score", "document"}},
            }
        )
    return hits


def hydrate(
    hits: list[VectorHit],
    *,
    repository: KnowledgeRepository | None = None,
    hide_visual: bool = False,
) -> list[dict[str, Any]]:
    """Hydrate raw vector hits (keeps their similarity and document)."""
    return hydrate_segments(
        [h.segment_id for h in hits],
        repository=repository,
        hide_visual=hide_visual,
        extra={h.segment_id: {"similarity_score": h.similarity_score, "document": h.document} for h in hits},
    )


def search(
    query: str,
    limit: int = 5,
    *,
    text_only: bool = False,
    vector_store: VectorStore | None = None,
    repository: KnowledgeRepository | None = None,
) -> list[dict[str, Any]]:
    """Plain dense search (the text-only variant is the conventional RAG baseline).

    ``text_only=True`` searches embeddings of extracted text only and hides
    visual summaries, so the baseline is shown exactly what it matched on.
    """
    store = vector_store or get_knowledge_vector_store()
    raw = store.search_text_only(query, limit) if text_only else store.search(query, limit)
    return hydrate(raw, repository=repository, hide_visual=text_only)
