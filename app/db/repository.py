"""SQLite-backed repository for sources, segments, entities and relations.

SQLite is the system of record: every field shown to a user after retrieval
comes from here.  The vector index (Chroma) only maps embeddings to segment
IDs and can be rebuilt from this database at any time.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import unicodedata
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from app.schemas.knowledge import MediaModality
from app.schemas.records import (
    BoundingBox,
    JobRecord,
    JobStatus,
    EntityRecord,
    EntitySummary,
    KnowledgeStats,
    Locator,
    NodeKind,
    RelationRecord,
    RelationType,
    SegmentKind,
    SegmentRecord,
    SourceRecord,
    SourceStatus,
    SourceSummary,
)
from app.services.storage import storage_root

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id           TEXT PRIMARY KEY,
    filename     TEXT NOT NULL,
    modality     TEXT NOT NULL,
    content_type TEXT,
    sha256       TEXT,
    size_bytes   INTEGER,
    storage_path TEXT,
    status       TEXT NOT NULL,
    ingested_at  TEXT NOT NULL,
    attributes   TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_sources_sha256 ON sources(sha256);

CREATE TABLE IF NOT EXISTS segments (
    id             TEXT PRIMARY KEY,
    source_id      TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    modality       TEXT NOT NULL,
    kind           TEXT NOT NULL,
    ordinal        INTEGER NOT NULL,
    text           TEXT,
    visual_summary TEXT,
    start_seconds  REAL,
    end_seconds    REAL,
    page_number    INTEGER,
    region         TEXT,
    locator_label  TEXT,
    frame_path     TEXT,
    confidence     REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    extractor      TEXT NOT NULL,
    attributes     TEXT NOT NULL DEFAULT '{}',
    created_at     TEXT NOT NULL,
    UNIQUE (source_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_segments_source_time
    ON segments(source_id, start_seconds, end_seconds);
CREATE INDEX IF NOT EXISTS idx_segments_modality ON segments(modality);

CREATE TABLE IF NOT EXISTS entities (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    normalized_name TEXT NOT NULL UNIQUE,
    entity_type     TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS relations (
    id         TEXT PRIMARY KEY,
    src_kind   TEXT NOT NULL,
    src_id     TEXT NOT NULL,
    dst_kind   TEXT NOT NULL,
    dst_id     TEXT NOT NULL,
    relation   TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    attributes TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (src_id, dst_id, relation)
);
CREATE INDEX IF NOT EXISTS idx_relations_src ON relations(src_id, relation);
CREATE INDEX IF NOT EXISTS idx_relations_dst ON relations(dst_id, relation);

CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    source_id    TEXT NOT NULL,
    filename     TEXT NOT NULL,
    modality     TEXT NOT NULL,
    file_path    TEXT NOT NULL,
    content_type TEXT,
    status       TEXT NOT NULL,
    stage        TEXT NOT NULL,
    progress     REAL NOT NULL DEFAULT 0,
    error        TEXT,
    warnings     TEXT NOT NULL DEFAULT '[]',
    result       TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, created_at);
"""

_WHITESPACE_RE = re.compile(r"\s+")
_SEPARATOR_RE = re.compile(r"[-_/]+")
_EDGE_PUNCT = " \t\n.,;:!?\"'()[]{}"


def normalize_entity_name(name: str) -> str:
    """Deduplication key: ``"Checkout-Service"`` and ``"checkout service"`` collide."""
    text = unicodedata.normalize("NFKC", name).casefold()
    text = _SEPARATOR_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip(_EDGE_PUNCT)


def default_database_path() -> Path:
    configured = os.getenv("KNOWLEDGE_DB_PATH")
    return Path(configured) if configured else storage_root() / "knowledge.db"


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _loads(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _ts(value: datetime) -> str:
    return value.isoformat()


class KnowledgeRepository:
    """Thread-safe access to the knowledge database.

    One connection is shared behind a re-entrant lock; FastAPI runs blocking
    work in a thread pool, and SQLite in WAL mode handles that comfortably
    for a single-node deployment.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_database_path()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._tx_depth = 0
        self._migrate()

    # ------------------------------------------------------------------ infra

    def _migrate(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES ('version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
            )

    @contextmanager
    def transaction(self) -> Iterator["KnowledgeRepository"]:
        """Atomic unit of work; nested calls join the outer transaction."""
        with self._lock:
            outermost = self._tx_depth == 0
            if outermost:
                self._conn.execute("BEGIN IMMEDIATE")
            self._tx_depth += 1
            try:
                yield self
            except BaseException:
                self._tx_depth -= 1
                if outermost:
                    self._conn.execute("ROLLBACK")
                raise
            else:
                self._tx_depth -= 1
                if outermost:
                    self._conn.execute("COMMIT")

    def _execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def _fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def _fetchone(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------------------------------------------------------- sources

    @staticmethod
    def _row_to_source(row: sqlite3.Row) -> SourceRecord:
        return SourceRecord(
            id=row["id"],
            filename=row["filename"],
            modality=MediaModality(row["modality"]),
            content_type=row["content_type"],
            sha256=row["sha256"],
            size_bytes=row["size_bytes"],
            storage_path=row["storage_path"],
            status=SourceStatus(row["status"]),
            ingested_at=datetime.fromisoformat(row["ingested_at"]),
            attributes=_loads(row["attributes"]),
        )

    def upsert_source(self, source: SourceRecord) -> SourceRecord:
        self._execute(
            """
            INSERT INTO sources (id, filename, modality, content_type, sha256, size_bytes,
                                 storage_path, status, ingested_at, attributes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                filename = excluded.filename,
                modality = excluded.modality,
                content_type = excluded.content_type,
                sha256 = excluded.sha256,
                size_bytes = excluded.size_bytes,
                storage_path = excluded.storage_path,
                status = excluded.status,
                attributes = excluded.attributes
            """,
            (
                source.id,
                source.filename,
                source.modality.value,
                source.content_type,
                source.sha256,
                source.size_bytes,
                source.storage_path,
                source.status.value,
                _ts(source.ingested_at),
                _dumps(source.attributes),
            ),
        )
        return source

    def set_source_status(self, source_id: str, status: SourceStatus) -> None:
        self._execute("UPDATE sources SET status = ? WHERE id = ?", (status.value, source_id))

    def get_source(self, source_id: str) -> SourceRecord | None:
        row = self._fetchone("SELECT * FROM sources WHERE id = ?", (source_id,))
        return self._row_to_source(row) if row else None

    def find_sources_by_sha256(self, sha256: str) -> list[SourceRecord]:
        rows = self._fetchall(
            "SELECT * FROM sources WHERE sha256 = ? ORDER BY ingested_at", (sha256,)
        )
        return [self._row_to_source(r) for r in rows]

    def list_sources(self) -> list[SourceSummary]:
        rows = self._fetchall(
            """
            SELECT s.*, COUNT(g.id) AS segment_count
            FROM sources s LEFT JOIN segments g ON g.source_id = s.id
            GROUP BY s.id ORDER BY s.ingested_at DESC
            """
        )
        return [
            SourceSummary(source=self._row_to_source(r), segment_count=r["segment_count"])
            for r in rows
        ]

    def delete_source(self, source_id: str) -> list[str]:
        """Delete a source, its segments and every edge touching them.

        Returns the deleted segment IDs so the caller can drop their vectors.
        Entities left with no mentions are pruned.
        """
        with self.transaction():
            segment_ids = [
                r["id"]
                for r in self._fetchall("SELECT id FROM segments WHERE source_id = ?", (source_id,))
            ]
            node_ids = [source_id, *segment_ids]
            for chunk in _chunks(node_ids, 500):
                marks = ",".join("?" * len(chunk))
                self._execute(
                    f"DELETE FROM relations WHERE src_id IN ({marks}) OR dst_id IN ({marks})",
                    [*chunk, *chunk],
                )
            self._execute("DELETE FROM sources WHERE id = ?", (source_id,))
            self._prune_orphan_entities()
        return segment_ids

    # --------------------------------------------------------------- segments

    @staticmethod
    def _row_to_segment(row: sqlite3.Row) -> SegmentRecord:
        region = _loads(row["region"])
        return SegmentRecord(
            id=row["id"],
            source_id=row["source_id"],
            modality=MediaModality(row["modality"]),
            kind=SegmentKind(row["kind"]),
            ordinal=row["ordinal"],
            text=row["text"],
            visual_summary=row["visual_summary"],
            locator=Locator(
                start_seconds=row["start_seconds"],
                end_seconds=row["end_seconds"],
                page_number=row["page_number"],
                region=BoundingBox(**region) if region else None,
                label=row["locator_label"],
            ),
            frame_path=row["frame_path"],
            confidence=row["confidence"],
            extractor=row["extractor"],
            attributes=_loads(row["attributes"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def add_segments(self, segments: list[SegmentRecord]) -> None:
        if not segments:
            return
        rows = [
            (
                s.id,
                s.source_id,
                s.modality.value,
                s.kind.value,
                s.ordinal,
                s.text,
                s.visual_summary,
                s.locator.start_seconds,
                s.locator.end_seconds,
                s.locator.page_number,
                _dumps(s.locator.region.model_dump()) if s.locator.region else None,
                s.locator.label,
                s.frame_path,
                s.confidence,
                s.extractor,
                _dumps(s.attributes),
                _ts(s.created_at),
            )
            for s in segments
        ]
        with self._lock:
            self._conn.executemany(
                """
                INSERT INTO segments (id, source_id, modality, kind, ordinal, text,
                    visual_summary, start_seconds, end_seconds, page_number, region,
                    locator_label, frame_path, confidence, extractor, attributes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    text = excluded.text,
                    visual_summary = excluded.visual_summary,
                    start_seconds = excluded.start_seconds,
                    end_seconds = excluded.end_seconds,
                    page_number = excluded.page_number,
                    region = excluded.region,
                    locator_label = excluded.locator_label,
                    frame_path = excluded.frame_path,
                    confidence = excluded.confidence,
                    extractor = excluded.extractor,
                    attributes = excluded.attributes
                """,
                rows,
            )

    def get_segment(self, segment_id: str) -> SegmentRecord | None:
        row = self._fetchone("SELECT * FROM segments WHERE id = ?", (segment_id,))
        return self._row_to_segment(row) if row else None

    def get_segments(self, segment_ids: list[str]) -> dict[str, SegmentRecord]:
        found: dict[str, SegmentRecord] = {}
        for chunk in _chunks(segment_ids, 500):
            marks = ",".join("?" * len(chunk))
            for row in self._fetchall(f"SELECT * FROM segments WHERE id IN ({marks})", chunk):
                found[row["id"]] = self._row_to_segment(row)
        return found

    def list_segments(self, source_id: str) -> list[SegmentRecord]:
        rows = self._fetchall(
            "SELECT * FROM segments WHERE source_id = ? ORDER BY ordinal", (source_id,)
        )
        return [self._row_to_segment(r) for r in rows]

    def iter_all_segments(self, batch_size: int = 500) -> Iterator[list[SegmentRecord]]:
        offset = 0
        while True:
            rows = self._fetchall(
                "SELECT * FROM segments ORDER BY source_id, ordinal LIMIT ? OFFSET ?",
                (batch_size, offset),
            )
            if not rows:
                return
            yield [self._row_to_segment(r) for r in rows]
            offset += batch_size

    # --------------------------------------------------------------- entities

    @staticmethod
    def _row_to_entity(row: sqlite3.Row) -> EntityRecord:
        return EntityRecord(
            id=row["id"],
            name=row["name"],
            normalized_name=row["normalized_name"],
            entity_type=row["entity_type"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def upsert_entity(self, name: str, entity_type: str = "concept") -> EntityRecord | None:
        """Return the entity for ``name``, creating it on first sight."""
        normalized = normalize_entity_name(name)
        if not normalized:
            return None
        with self.transaction():
            row = self._fetchone(
                "SELECT * FROM entities WHERE normalized_name = ?", (normalized,)
            )
            if row:
                existing = self._row_to_entity(row)
                # A specific type ("person", "metric") beats the generic default.
                if existing.entity_type == "concept" and entity_type != "concept":
                    self._execute(
                        "UPDATE entities SET entity_type = ? WHERE id = ?",
                        (entity_type, existing.id),
                    )
                    existing.entity_type = entity_type
                return existing
            entity = EntityRecord(
                name=name.strip(), normalized_name=normalized, entity_type=entity_type
            )
            self._execute(
                "INSERT INTO entities (id, name, normalized_name, entity_type, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    entity.id,
                    entity.name,
                    entity.normalized_name,
                    entity.entity_type,
                    _ts(entity.created_at),
                ),
            )
            return entity

    def get_entity(self, entity_id: str) -> EntityRecord | None:
        row = self._fetchone("SELECT * FROM entities WHERE id = ?", (entity_id,))
        return self._row_to_entity(row) if row else None

    def get_entities(self, entity_ids: list[str]) -> dict[str, EntityRecord]:
        found: dict[str, EntityRecord] = {}
        for chunk in _chunks(entity_ids, 500):
            marks = ",".join("?" * len(chunk))
            for row in self._fetchall(f"SELECT * FROM entities WHERE id IN ({marks})", chunk):
                found[row["id"]] = self._row_to_entity(row)
        return found

    def list_entities(self, query: str | None = None, limit: int = 100) -> list[EntitySummary]:
        params: list[Any] = [RelationType.MENTIONS.value]
        where = ""
        if query:
            where = "WHERE e.normalized_name LIKE ?"
            params.append(f"%{normalize_entity_name(query)}%")
        params.append(limit)
        rows = self._fetchall(
            f"""
            SELECT e.*,
                   COUNT(r.id)                 AS mention_count,
                   COUNT(DISTINCT g.source_id) AS source_count,
                   GROUP_CONCAT(DISTINCT g.modality) AS modalities
            FROM entities e
            LEFT JOIN relations r ON r.dst_id = e.id AND r.relation = ?
            LEFT JOIN segments g  ON g.id = r.src_id
            {where}
            GROUP BY e.id
            ORDER BY mention_count DESC, e.normalized_name
            LIMIT ?
            """,
            params,
        )
        return [
            EntitySummary(
                entity=self._row_to_entity(r),
                mention_count=r["mention_count"],
                source_count=r["source_count"],
                modalities=sorted(filter(None, (r["modalities"] or "").split(","))),
            )
            for r in rows
        ]

    def entities_for_segments(
        self, segment_ids: list[str]
    ) -> dict[str, list[tuple[EntityRecord, float]]]:
        """Map each segment ID to the entities it mentions (with confidence)."""
        result: dict[str, list[tuple[EntityRecord, float]]] = {sid: [] for sid in segment_ids}
        for chunk in _chunks(segment_ids, 500):
            marks = ",".join("?" * len(chunk))
            rows = self._fetchall(
                f"""
                SELECT r.src_id AS segment_id, r.confidence AS mention_confidence, e.*
                FROM relations r JOIN entities e ON e.id = r.dst_id
                WHERE r.relation = ? AND r.src_id IN ({marks})
                ORDER BY e.name
                """,
                [RelationType.MENTIONS.value, *chunk],
            )
            for row in rows:
                result[row["segment_id"]].append(
                    (self._row_to_entity(row), row["mention_confidence"])
                )
        return result

    def segments_mentioning(self, entity_id: str) -> list[SegmentRecord]:
        rows = self._fetchall(
            """
            SELECT g.* FROM relations r JOIN segments g ON g.id = r.src_id
            WHERE r.relation = ? AND r.dst_id = ?
            ORDER BY g.source_id, g.ordinal
            """,
            (RelationType.MENTIONS.value, entity_id),
        )
        return [self._row_to_segment(r) for r in rows]

    def _prune_orphan_entities(self) -> None:
        self._execute(
            "DELETE FROM entities WHERE id NOT IN (SELECT dst_id FROM relations) "
            "AND id NOT IN (SELECT src_id FROM relations)"
        )

    # -------------------------------------------------------------- relations

    @staticmethod
    def _row_to_relation(row: sqlite3.Row) -> RelationRecord:
        return RelationRecord(
            id=row["id"],
            src_kind=NodeKind(row["src_kind"]),
            src_id=row["src_id"],
            dst_kind=NodeKind(row["dst_kind"]),
            dst_id=row["dst_id"],
            relation=RelationType(row["relation"]),
            confidence=row["confidence"],
            attributes=_loads(row["attributes"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def add_relations(self, relations: list[RelationRecord]) -> int:
        """Insert edges; an existing (src, dst, type) edge keeps the higher confidence."""
        if not relations:
            return 0
        rows = [
            (
                r.id,
                r.src_kind.value,
                r.src_id,
                r.dst_kind.value,
                r.dst_id,
                r.relation.value,
                r.confidence,
                _dumps(r.attributes),
                _ts(r.created_at),
            )
            for r in relations
        ]
        with self._lock:
            before = self._conn.total_changes
            self._conn.executemany(
                """
                INSERT INTO relations (id, src_kind, src_id, dst_kind, dst_id, relation,
                                       confidence, attributes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(src_id, dst_id, relation) DO UPDATE SET
                    confidence = MAX(relations.confidence, excluded.confidence)
                """,
                rows,
            )
            return self._conn.total_changes - before

    def relations_for(
        self, node_id: str, relation: RelationType | None = None
    ) -> list[tuple[RelationRecord, str]]:
        """All edges touching ``node_id`` as ``(edge, "out" | "in")`` pairs."""
        params: list[Any] = [node_id, node_id]
        filter_sql = ""
        if relation is not None:
            filter_sql = "AND relation = ?"
            params.append(relation.value)
        rows = self._fetchall(
            f"SELECT * FROM relations WHERE (src_id = ? OR dst_id = ?) {filter_sql} "
            "ORDER BY relation, created_at",
            params,
        )
        return [
            (self._row_to_relation(r), "out" if r["src_id"] == node_id else "in") for r in rows
        ]

    # ------------------------------------------------------------------- jobs

    def create_job(self, job: JobRecord) -> JobRecord:
        self._execute(
            """
            INSERT INTO jobs (id, source_id, filename, modality, file_path, content_type, status,
                              stage, progress, error, warnings, result, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.id, job.source_id, job.filename, job.modality.value, job.file_path,
                job.content_type, job.status.value, job.stage, job.progress, job.error,
                json.dumps(job.warnings), _dumps(job.result),
                _ts(job.created_at), _ts(job.updated_at),
            ),
        )
        return job

    def update_job(self, job_id: str, **fields: Any) -> None:
        allowed = {"status", "stage", "progress", "error", "warnings", "result"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown job fields: {unknown}")
        values: dict[str, Any] = {}
        for key, value in fields.items():
            if key == "status":
                value = JobStatus(value).value
            elif key == "warnings":
                value = json.dumps(list(value))
            elif key == "result":
                value = _dumps(value)
            values[key] = value
        values["updated_at"] = _ts(datetime.now().astimezone())
        assignments = ", ".join(f"{key} = ?" for key in values)
        self._execute(f"UPDATE jobs SET {assignments} WHERE id = ?", [*values.values(), job_id])

    def get_job(self, job_id: str) -> JobRecord | None:
        row = self._fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return _row_to_job(row) if row else None

    def list_jobs(
        self, statuses: list[JobStatus] | None = None, limit: int = 50
    ) -> list[JobRecord]:
        if statuses:
            marks = ",".join("?" * len(statuses))
            rows = self._fetchall(
                f"SELECT * FROM jobs WHERE status IN ({marks}) ORDER BY created_at LIMIT ?",
                [*(s.value for s in statuses), limit],
            )
        else:
            rows = self._fetchall("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
        return [_row_to_job(r) for r in rows]

    # ------------------------------------------------------------------ stats

    def stats(self) -> KnowledgeStats:
        def count(table: str) -> int:
            row = self._fetchone(f"SELECT COUNT(*) AS n FROM {table}")
            return int(row["n"]) if row else 0

        by_modality = {
            r["modality"]: r["n"]
            for r in self._fetchall(
                "SELECT modality, COUNT(*) AS n FROM segments GROUP BY modality"
            )
        }
        by_relation = {
            r["relation"]: r["n"]
            for r in self._fetchall(
                "SELECT relation, COUNT(*) AS n FROM relations GROUP BY relation"
            )
        }
        return KnowledgeStats(
            sources=count("sources"),
            segments=count("segments"),
            entities=count("entities"),
            relations=count("relations"),
            segments_by_modality=by_modality,
            relations_by_type=by_relation,
        )


def _row_to_job(row: sqlite3.Row) -> JobRecord:
    warnings = json.loads(row["warnings"] or "[]")
    return JobRecord(
        id=row["id"],
        source_id=row["source_id"],
        filename=row["filename"],
        modality=MediaModality(row["modality"]),
        file_path=row["file_path"],
        content_type=row["content_type"],
        status=JobStatus(row["status"]),
        stage=row["stage"],
        progress=row["progress"],
        error=row["error"],
        warnings=warnings if isinstance(warnings, list) else [],
        result=_loads(row["result"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _chunks(items: list[Any], size: int) -> Iterator[list[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


_repository: KnowledgeRepository | None = None
_repository_lock = threading.Lock()


def get_repository() -> KnowledgeRepository:
    """Process-wide repository bound to ``KNOWLEDGE_DB_PATH`` (default ``data/knowledge.db``)."""
    global _repository
    with _repository_lock:
        if _repository is None:
            _repository = KnowledgeRepository()
        return _repository


def reset_repository(repository: KnowledgeRepository | None = None) -> None:
    """Swap the process-wide repository (tests, CLI tools)."""
    global _repository
    with _repository_lock:
        _repository = repository
