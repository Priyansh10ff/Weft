"""Maintenance commands for the knowledge store.

    python -m app.cli stats           # counts per table / modality / relation
    python -m app.cli reindex         # rebuild Chroma from SQLite
    python -m app.cli import-legacy   # move pre-SQLite Chroma data into SQLite
    python -m app.cli relink          # recompute cross-modal / temporal links
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from typing import Any
from uuid import UUID, uuid4

from app.db.repository import get_repository
from app.schemas.knowledge import KnowledgeNode, MediaModality, SourceAsset
from app.services.ingestion import ingest_nodes, rebuild_index
from app.services.vector_store import get_knowledge_vector_store

LEGACY_COLLECTION = "multimodal_knowledge"
_LEGACY_FIELDS = {"content", "transcript", "visual_summary", "timestamp", "frame_path"}


def _json_field(value: Any, expected: type) -> Any:
    if isinstance(value, expected):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return expected()
        return parsed if isinstance(parsed, expected) else expected()
    return expected()


def _legacy_node(metadata: dict[str, Any], document: str | None) -> KnowledgeNode | None:
    try:
        modality = MediaModality(str(metadata.get("modality")))
    except ValueError:
        return None
    fields = {k: metadata[k] for k in _LEGACY_FIELDS if isinstance(metadata.get(k), str)}
    if not fields.get("transcript") and not fields.get("content") and document:
        fields["content"] = document
    return KnowledgeNode(
        modality=modality,
        source=str(metadata.get("source") or "unknown"),
        entities=[str(e) for e in _json_field(metadata.get("entities"), list)],
        provenance=_json_field(metadata.get("provenance"), dict),
        **fields,
    )


def import_legacy(collection_name: str = LEGACY_COLLECTION) -> dict[str, int]:
    """Copy records from the old metadata-as-storage collection into SQLite.

    Records are grouped into one source per (filename, modality).  Re-running
    is safe: groups already imported are skipped.
    """
    store = get_knowledge_vector_store()
    repo = get_repository()
    try:
        legacy = store.client.get_collection(collection_name)
    except Exception:
        print(f"No legacy collection '{collection_name}' found; nothing to import.")
        return {"sources": 0, "segments": 0}

    payload = legacy.get(include=["documents", "metadatas"])
    groups: dict[tuple[str, str], list[KnowledgeNode]] = defaultdict(list)
    source_ids: dict[tuple[str, str], str] = {}
    seen: set[tuple[str, ...]] = set()
    for document, metadata in zip(payload.get("documents") or [], payload.get("metadatas") or []):
        node = _legacy_node(dict(metadata or {}), document)
        if node is None:
            continue
        modality = node.modality.value if hasattr(node.modality, "value") else str(node.modality)
        key = (node.source or "unknown", modality)
        # The old store has no source identity, so repeated uploads of one
        # file left duplicate rows; keep one copy of each (locator, content).
        fingerprint = (
            *key,
            str(node.timestamp or ""),
            str(node.transcript or node.content or ""),
            str(node.visual_summary or ""),
        )
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        groups[key].append(node)
        source_id = node.provenance.get("source_id")
        if source_id and key not in source_ids:
            source_ids[key] = str(source_id)

    already = {
        summary.source.attributes.get("legacy_key")
        for summary in repo.list_sources()
        if summary.source.attributes.get("legacy_key")
    }
    imported_sources = imported_segments = 0
    for (filename, modality), nodes in sorted(groups.items()):
        legacy_key = f"{collection_name}:{modality}:{filename}"
        if legacy_key in already:
            continue
        try:
            source_uuid = UUID(source_ids.get((filename, modality), ""))
        except ValueError:
            source_uuid = uuid4()
        if repo.get_source(str(source_uuid)) is not None:
            source_uuid = uuid4()
        asset = SourceAsset(
            source_id=source_uuid, filename=filename, modality=MediaModality(modality)
        )
        result = ingest_nodes(
            asset, nodes, source_attributes={"legacy_key": legacy_key}
        )
        imported_sources += 1
        imported_segments += len(result.segments)
        print(f"  imported {len(result.segments):>4} segments  {modality:<6} {filename}")
    return {"sources": imported_sources, "segments": imported_segments}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("stats", help="Print knowledge-store counts.")
    sub.add_parser("reindex", help="Drop and rebuild the vector index from SQLite.")
    sub.add_parser("relink", help="Recompute every cross-modal and temporal link.")
    legacy = sub.add_parser("import-legacy", help="Import the pre-SQLite Chroma collection.")
    legacy.add_argument("--collection", default=LEGACY_COLLECTION)
    args = parser.parse_args(argv)

    if args.command == "stats":
        print(get_repository().stats().model_dump_json(indent=2))
    elif args.command == "reindex":
        total = rebuild_index()
        print(f"Re-indexed {total} segments.")
    elif args.command == "relink":
        from app.services.linker import relink_all

        print(json.dumps(relink_all(), indent=2))
    elif args.command == "import-legacy":
        counts = import_legacy(args.collection)
        print(f"Imported {counts['sources']} sources / {counts['segments']} segments.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
