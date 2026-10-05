"""Cross-modal and temporal linking: turns isolated segments into a graph.

After a source is ingested, every one of its segments is compared with the
rest of the knowledge base and typed edges are added:

* ``co_occurs``   windows of the same video scene: different stretches of
                  speech while the same visual was on screen.
* ``shows_same``  a slide or frame that appears again later in the same
                  recording (perceptual-hash duplicate keyframe).
* ``depicts``     a visual segment (diagram, screenshot, slide, rendered
                  page) and a speech/text segment in *another* source that
                  talk about the same thing: e.g. the architecture diagram in
                  a PDF and the stand-up where someone explained it.
* ``corroborates`` two segments in different sources that state the same
                  thing (both textual, or both visual).
* ``same_as``     entities that are the same thing under different surface
                  forms ("read replica" / "read replicas").

Two independent signals decide whether two segments are related:

1. **Shared entities** (after ``same_as`` resolution). Explainable and
   precise: two segments that both mention "circuit breaker" and
   "payments API" are about the same thing.
2. **Embedding similarity** between one segment's full document (speech or
   text plus what is shown) and the other's, from the vector index.

An edge is added when there are enough shared entities, or one shared
entity plus moderate similarity, or very high similarity alone. Every edge
records *why* it exists (shared entities, similarity), so retrieval and the
UI can explain it.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from app.db.repository import KnowledgeRepository, get_repository, normalize_entity_name
from app.schemas.records import (
    LINKER_RELATIONS,
    NodeKind,
    RelationRecord,
    RelationType,
    SegmentKind,
    SegmentRecord,
)
from app.services.vector_store import (
    VectorStore,
    VectorStoreError,
    document_for_segment,
    get_knowledge_vector_store,
)

_log = logging.getLogger(__name__)

# Thresholds. Similarity is cosine similarity of the default MiniLM-L6
# embeddings, where paraphrases of the same statement usually score > 0.6
# and unrelated technical text < 0.35.
MIN_SHARED_ENTITIES = 2
SIMILARITY_WITH_ENTITY = 0.42
SIMILARITY_ALONE = 0.62
CANDIDATES_FROM_INDEX = 8
MAX_LINKS_PER_SEGMENT = 5
SAME_AS_CONFIDENCE = 0.9

_CROSS_SOURCE = (RelationType.DEPICTS, RelationType.CORROBORATES)


@dataclass
class LinkReport:
    source_id: str
    created: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.created.values())


# ----------------------------------------------------------------- entities

_PLURAL_RULES = ((re.compile(r"ies$"), "y"), (re.compile(r"(ss|us|is)$"), r"\1"), (re.compile(r"s$"), ""))


def _singular(word: str) -> str:
    if len(word) <= 3:
        return word
    for pattern, repl in _PLURAL_RULES:
        if pattern.search(word):
            return pattern.sub(repl, word, count=1)
    return word


def entity_key(name: str) -> str:
    """Resolution key: normalized, leading article dropped, last word singular."""
    words = normalize_entity_name(name).split()
    if words and words[0] in {"the", "a", "an"}:
        words = words[1:]
    if not words:
        return ""
    words[-1] = _singular(words[-1])
    return " ".join(words)


def resolve_entities(repo: KnowledgeRepository) -> int:
    """Add ``same_as`` edges between entities that share a resolution key."""
    buckets: dict[str, list[Any]] = defaultdict(list)
    for entity in repo.all_entities():
        key = entity_key(entity.name)
        if key:
            buckets[key].append(entity)
    edges: list[RelationRecord] = []
    for members in buckets.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda e: e.id)
        anchor = members[0]
        for other in members[1:]:
            edges.append(
                RelationRecord(
                    src_kind=NodeKind.ENTITY,
                    src_id=anchor.id,
                    dst_kind=NodeKind.ENTITY,
                    dst_id=other.id,
                    relation=RelationType.SAME_AS,
                    confidence=SAME_AS_CONFIDENCE,
                    attributes={"method": "surface-form", "names": [anchor.name, other.name]},
                )
            )
    return repo.add_relations(edges)


# ------------------------------------------------------------ within source


def _scene(segment: SegmentRecord) -> int | None:
    value = (segment.attributes.get("provenance") or {}).get("scene_index")
    return int(value) if isinstance(value, (int, float)) else None


def within_source_relations(segments: list[SegmentRecord]) -> list[RelationRecord]:
    edges: list[RelationRecord] = []
    by_scene: dict[int, list[SegmentRecord]] = defaultdict(list)
    for seg in segments:
        scene = _scene(seg)
        if scene is not None:
            by_scene[scene].append(seg)

    for scene, members in by_scene.items():
        members.sort(key=lambda s: s.ordinal)
        for current, following in zip(members, members[1:]):
            edges.append(
                RelationRecord(
                    src_kind=NodeKind.SEGMENT,
                    src_id=current.id,
                    dst_kind=NodeKind.SEGMENT,
                    dst_id=following.id,
                    relation=RelationType.CO_OCCURS,
                    confidence=round(min(current.confidence, following.confidence), 4),
                    attributes={"reason": "same scene", "scene_index": scene},
                )
            )

    for seg in segments:
        original = seg.attributes.get("keyframe_duplicate_of_scene")
        if not isinstance(original, (int, float)) or int(original) not in by_scene:
            continue
        first = by_scene[int(original)][0]
        if first.id == seg.id:
            continue
        edges.append(
            RelationRecord(
                src_kind=NodeKind.SEGMENT,
                src_id=seg.id,
                dst_kind=NodeKind.SEGMENT,
                dst_id=first.id,
                relation=RelationType.SHOWS_SAME,
                confidence=round(min(seg.confidence, first.confidence), 4),
                attributes={"reason": "same slide shown again", "scene_index": int(original)},
            )
        )
    return edges


# ------------------------------------------------------------- across sources


def _is_visual_only_side(a: SegmentRecord, b: SegmentRecord) -> bool:
    """True when ``a`` carries visual evidence that ``b`` lacks."""
    return a.has_visual() and not b.has_visual()


def classify(a: SegmentRecord, b: SegmentRecord) -> tuple[RelationType, SegmentRecord, SegmentRecord]:
    """Pick the relation type and its direction for a related pair."""
    if _is_visual_only_side(a, b):
        return RelationType.DEPICTS, a, b
    if _is_visual_only_side(b, a):
        return RelationType.DEPICTS, b, a
    first, second = (a, b) if a.id < b.id else (b, a)
    return RelationType.CORROBORATES, first, second


def score_pair(shared: int, similarity: float | None) -> float | None:
    """Return link strength in [0, 1], or ``None`` when the pair should not be linked."""
    sim = similarity or 0.0
    linked = (
        shared >= MIN_SHARED_ENTITIES
        or (shared >= 1 and sim >= SIMILARITY_WITH_ENTITY)
        or sim >= SIMILARITY_ALONE
    )
    if not linked:
        return None
    entity_strength = min(0.95, 0.45 + 0.15 * shared) if shared else 0.0
    return round(max(sim, entity_strength), 4)


def cross_source_relations(
    segment: SegmentRecord,
    *,
    repo: KnowledgeRepository,
    store: VectorStore | None,
    groups: dict[str, str],
    members: dict[str, list[str]],
    entity_ids: list[str],
    entity_names: dict[str, str],
    source_filename: str | None,
) -> list[RelationRecord]:
    candidates: dict[str, dict[str, Any]] = defaultdict(lambda: {"shared": set(), "similarity": None})

    # 1. Shared entities (expanded through same_as).
    canonical = {groups.get(e, e) for e in entity_ids}
    expanded = sorted({m for g in canonical for m in members.get(g, [g])})
    if expanded:
        for other_id, eid in repo.mentions_of(expanded, exclude_source_id=segment.source_id):
            candidates[other_id]["shared"].add(groups.get(eid, eid))

    # 2. Embedding neighbours from other sources.
    if store is not None and hasattr(store, "similar"):
        from app.schemas.records import SourceRecord  # local: avoid import cycle in type hints

        doc = document_for_segment(
            segment,
            SourceRecord(id=segment.source_id, filename=source_filename or "?", modality=segment.modality),
        )
        try:
            for hit in store.similar(doc, exclude_source_id=segment.source_id, limit=CANDIDATES_FROM_INDEX):
                candidates[hit.segment_id]["similarity"] = hit.similarity_score
        except VectorStoreError as exc:
            _log.warning("Similarity lookup failed for segment %s: %s", segment.id, exc)

    if not candidates:
        return []
    others = repo.get_segments(list(candidates))
    scored: list[tuple[float, SegmentRecord, dict[str, Any]]] = []
    for other_id, info in candidates.items():
        other = others.get(other_id)
        if other is None or other.source_id == segment.source_id:
            continue
        strength = score_pair(len(info["shared"]), info["similarity"])
        if strength is not None:
            scored.append((strength, other, info))
    scored.sort(key=lambda item: item[0], reverse=True)

    edges: list[RelationRecord] = []
    for strength, other, info in scored[:MAX_LINKS_PER_SEGMENT]:
        relation, src, dst = classify(segment, other)
        shared_names = sorted({entity_names.get(g, g) for g in info["shared"]})
        method = "+".join(
            part
            for part, present in (("entities", bool(info["shared"])), ("embedding", info["similarity"] is not None))
            if present
        )
        edges.append(
            RelationRecord(
                src_kind=NodeKind.SEGMENT,
                src_id=src.id,
                dst_kind=NodeKind.SEGMENT,
                dst_id=dst.id,
                relation=relation,
                confidence=round(strength * min(segment.confidence, other.confidence) ** 0.5, 4),
                attributes={
                    "shared_entities": shared_names,
                    "similarity": round(info["similarity"], 4) if info["similarity"] is not None else None,
                    "strength": strength,
                    "method": method,
                },
            )
        )
    return edges


# -------------------------------------------------------------------- driver


def link_source(
    source_id: str,
    *,
    repository: KnowledgeRepository | None = None,
    vector_store: VectorStore | None = None,
) -> LinkReport:
    """(Re)compute every linker relation that starts from ``source_id``'s segments."""
    repo = repository or get_repository()
    try:
        store = vector_store or get_knowledge_vector_store()
    except VectorStoreError:
        store = None
    report = LinkReport(source_id=source_id)
    source = repo.get_source(source_id)
    segments = repo.list_segments(source_id)
    if source is None or not segments:
        return report

    same_as = resolve_entities(repo)
    if same_as:
        report.created[RelationType.SAME_AS.value] = same_as
    groups = repo.same_as_groups()
    members: dict[str, list[str]] = defaultdict(list)
    for entity_id, group in groups.items():
        members[group].append(entity_id)

    mentions = repo.entities_for_segments([s.id for s in segments])
    entity_names: dict[str, str] = {}
    for pairs in mentions.values():
        for entity, _ in pairs:
            entity_names.setdefault(groups.get(entity.id, entity.id), entity.name)

    edges = within_source_relations(segments)
    for segment in segments:
        if segment.kind is SegmentKind.SPEECH and not (segment.text or "").strip():
            continue
        edges.extend(
            cross_source_relations(
                segment,
                repo=repo,
                store=store,
                groups=groups,
                members=members,
                entity_ids=[e.id for e, _ in mentions.get(segment.id, [])],
                entity_names=entity_names,
                source_filename=source.filename,
            )
        )

    with repo.transaction():
        repo.delete_relations(
            [r for r in LINKER_RELATIONS if r is not RelationType.SAME_AS],
            node_ids=[s.id for s in segments],
        )
        repo.add_relations(edges)
    for edge in edges:
        report.created[edge.relation.value] = report.created.get(edge.relation.value, 0) + 1
    return report


def relink_all(
    *,
    repository: KnowledgeRepository | None = None,
    vector_store: VectorStore | None = None,
) -> dict[str, int]:
    """Drop every linker relation and recompute the whole graph."""
    repo = repository or get_repository()
    repo.delete_relations(LINKER_RELATIONS)
    totals: dict[str, int] = {}
    for summary in repo.list_sources():
        report = link_source(summary.source.id, repository=repo, vector_store=vector_store)
        for key, value in report.created.items():
            totals[key] = totals.get(key, 0) + value
    stats = repo.stats().relations_by_type
    return {key: stats.get(key, 0) for key in (r.value for r in LINKER_RELATIONS)} | {"_created": sum(totals.values())}
