"""Shared upload validation and acknowledgement helpers.

``acknowledge_upload`` validates an upload and mints the ``SourceAsset`` that
every derived record links back to; ``ingest_upload`` hands processor output
to the ingestion service.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from fastapi import HTTPException, UploadFile, status

from app.schemas.knowledge import MediaModality, SourceAsset, UploadAccepted

if TYPE_CHECKING:
    from app.services.ingestion import IngestResult


_EXTENSIONS: dict[MediaModality, set[str]] = {
    MediaModality.VIDEO: {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"},
    MediaModality.AUDIO: {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"},
    MediaModality.IMAGE: {".jpg", ".jpeg", ".png", ".webp", ".gif", ".tiff", ".bmp"},
    MediaModality.PDF: {".pdf"},
    MediaModality.JSON: {".json", ".txt"},
}


def _matches_modality(file: UploadFile, modality: MediaModality) -> bool:
    """Accept a known extension or an appropriate MIME type supplied by a client."""
    extension = Path(file.filename or "").suffix.lower()
    content_type = (file.content_type or "").lower()

    if extension in _EXTENSIONS[modality]:
        return True
    if modality is MediaModality.PDF:
        return content_type == "application/pdf"
    if modality is MediaModality.JSON:
        return content_type in {"application/json", "text/json", "text/plain"}
    return content_type.startswith(f"{modality.value}/")


def acknowledge_upload(file: UploadFile, modality: MediaModality) -> UploadAccepted:
    """Validate metadata and create the source asset passed to processors."""
    filename = file.filename
    if not filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The uploaded file must have a filename.",
        )
    if not _matches_modality(file, modality):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Expected a {modality.value} file.",
        )

    safe_filename = Path(filename).name
    if not safe_filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The uploaded file must have a valid filename.",
        )

    source_asset = SourceAsset(
        source_id=uuid4(),
        filename=safe_filename,
        modality=modality,
        content_type=file.content_type,
    )
    return UploadAccepted(upload_id=uuid4(), source_asset=source_asset)


async def ingest_upload(
    receipt: UploadAccepted, nodes: list, source_path: Path | None
) -> IngestResult:
    """Persist processor output for one upload and index it.

    Maps an indexing failure to 503; the records are kept in SQLite with
    status ``index_failed`` so ``python -m app.cli reindex`` can recover them.
    """
    from anyio import to_thread

    from app.services.ingestion import ingest_nodes
    from app.services.vector_store import VectorStoreError

    try:
        return await to_thread.run_sync(
            lambda: ingest_nodes(receipt.source_asset, nodes, file_path=source_path)
        )
    except VectorStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{exc} The extracted records were saved; run a reindex to retry.",
        ) from exc
