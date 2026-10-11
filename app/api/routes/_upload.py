"""Shared upload handling for every modality.

``handle_upload`` is the one code path behind ``/upload/*``:

1. validate the file against the modality and persist it under
   ``data/uploads/{source_id}/``,
2. reject files over ``MAX_UPLOAD_MB``,
3. skip re-processing when identical bytes were already ingested (SHA-256),
   unless ``force=true``,
4. either run the pipeline inline, or with ``background=true`` hand it to
   the job queue and return ``202`` with a job ID to poll.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from anyio import to_thread
from fastapi import HTTPException, Response, UploadFile, status

from app.config import get_settings
from app.db.repository import get_repository
from app.schemas.knowledge import (
    KnowledgeUploadResponse,
    MediaModality,
    SourceAsset,
    UploadAccepted,
)
from app.schemas.records import SourceStatus
from app.services.storage import UploadTooLarge, persist_upload

_log = logging.getLogger(__name__)

_EXTENSIONS: dict[MediaModality, set[str]] = {
    MediaModality.VIDEO: {".mp4"},
    MediaModality.AUDIO: {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"},
    MediaModality.IMAGE: {".jpg", ".jpeg", ".png"},
    MediaModality.PDF: {".pdf"},
    MediaModality.JSON: {".json", ".txt"},
}


def _matches_modality(file: UploadFile, modality: MediaModality) -> bool:
    """Accept a known extension, or a matching MIME type when there is none."""
    extension = Path(file.filename or "").suffix.lower()
    if extension:
        return extension in _EXTENSIONS[modality]
    content_type = (file.content_type or "").lower()
    if modality is MediaModality.PDF:
        return content_type == "application/pdf"
    if modality is MediaModality.JSON:
        return content_type in {"application/json", "text/json", "text/plain"}
    return content_type.startswith(f"{modality.value}/")


def acknowledge_upload(file: UploadFile, modality: MediaModality) -> UploadAccepted:
    """Validate metadata and create the source asset passed to processors."""
    filename = Path(file.filename or "").name
    if not filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The uploaded file must have a valid filename.",
        )
    if not _matches_modality(file, modality):
        allowed = ", ".join(sorted(_EXTENSIONS[modality]))
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Expected a {modality.value} file ({allowed}).",
        )
    source_asset = SourceAsset(
        source_id=uuid4(),
        filename=filename,
        modality=modality,
        content_type=file.content_type,
    )
    return UploadAccepted(upload_id=uuid4(), source_asset=source_asset)


def content_matches(path: Path, modality: MediaModality) -> bool:
    """Check the file's leading bytes, so a renamed file can't reach a decoder.

    Extensions and MIME types come from the client; the signature comes from
    the file itself.
    """
    with path.open("rb") as handle:
        head = handle.read(65536)
    if modality is MediaModality.PDF:
        return b"%PDF-" in head[:1024]
    if modality is MediaModality.IMAGE:
        return head.startswith(b"\x89PNG\r\n\x1a\n") or head.startswith(b"\xff\xd8\xff")
    if modality is MediaModality.VIDEO:
        return head[4:8] == b"ftyp"
    if modality is MediaModality.AUDIO:
        return (
            head.startswith((b"ID3", b"fLaC", b"OggS"))
            or (head[:4] == b"RIFF" and head[8:12] == b"WAVE")
            or head[4:8] == b"ftyp"  # m4a
            or (len(head) > 1 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0)  # MPEG / ADTS frame sync
        )
    if modality is MediaModality.JSON:
        if b"\x00" in head:
            return False
        try:
            head.decode("utf-8")
        except UnicodeDecodeError as exc:
            # A multi-byte character cut at the 64 KB boundary is fine.
            return exc.start >= len(head) - 3
        return True
    return True


def _discard(path: Path) -> None:
    shutil.rmtree(path.parent, ignore_errors=True)


async def handle_upload(
    file: UploadFile,
    modality: MediaModality,
    response: Response,
    *,
    background: bool = False,
    force: bool = False,
    recorded_at: datetime | None = None,
) -> KnowledgeUploadResponse:
    from app.services.ingestion import file_fingerprint, ingest_nodes
    from app.services.jobs import get_job_manager
    from app.services.pipeline import ProcessingFailed, run_pipeline
    from app.services.vector_store import VectorStoreError

    receipt = acknowledge_upload(file, modality)
    asset = receipt.source_asset
    limit_mb = get_settings().max_upload_mb
    try:
        path = await persist_upload(file, asset.source_id, max_bytes=limit_mb * 1024 * 1024)
    except UploadTooLarge as exc:
        raise HTTPException(status_code=413, detail=f"File exceeds the {limit_mb} MB upload limit.") from exc
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to store the uploaded file.",
        ) from exc

    if path.stat().st_size == 0:
        _discard(path)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The file is empty.")
    if not content_matches(path, modality):
        _discard(path)
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"The file's contents are not a valid {modality.value} file.",
        )

    repo = get_repository()
    if not force:
        sha256, _ = await to_thread.run_sync(file_fingerprint, path)
        existing = [
            s for s in repo.find_sources_by_sha256(sha256) if s.status is SourceStatus.INDEXED
        ]
        if existing:
            _discard(path)
            source = existing[0]
            return KnowledgeUploadResponse(
                success=True,
                status="deduplicated",
                processed_nodes=len(repo.list_segments(source.id)),
                source=source.filename,
                source_id=source.id,
                warnings=["Identical file already ingested; pass force=true to re-process."],
            )

    extra_attributes = {"recorded_at": recorded_at.isoformat()} if recorded_at else {}

    if background:
        job = get_job_manager().submit(asset, path, source_attributes=extra_attributes)
        response.status_code = status.HTTP_202_ACCEPTED
        return KnowledgeUploadResponse(
            success=True,
            status="queued",
            processed_nodes=0,
            source=asset.filename,
            source_id=str(asset.source_id),
            job_id=job.id,
        )

    try:
        pipeline = await to_thread.run_sync(lambda: run_pipeline(asset, path))
    except ProcessingFailed as exc:
        _log.warning("Processing failed for %s: %s", asset.filename, exc)
        raise HTTPException(
            status_code=422, detail=str(exc)
        ) from exc

    try:
        result = await to_thread.run_sync(
            lambda: ingest_nodes(
                asset,
                pipeline.nodes,
                file_path=path,
                source_attributes={
                    **extra_attributes,
                    "warnings": pipeline.warnings,
                    "enrichment": pipeline.enrichment,
                },
            )
        )
    except VectorStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{exc} The extracted records were saved; run a reindex to retry.",
        ) from exc

    return KnowledgeUploadResponse(
        success=True,
        processed_nodes=len(result.segments),
        source=result.source.filename,
        source_id=result.source.id,
        entity_count=result.entity_count,
        relation_count=result.relation_count,
        warnings=pipeline.warnings,
    )
