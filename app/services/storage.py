"""Safe, asynchronous staging helpers for uploaded media and derivatives."""

import os
from pathlib import Path
from uuid import UUID

from anyio import open_file
from fastapi import UploadFile


_CHUNK_SIZE = 1024 * 1024


def storage_root() -> Path:
    """Return the configurable root for source uploads and derived artifacts."""
    configured_root = os.getenv("MEDIA_STORAGE_ROOT")
    root = Path(configured_root) if configured_root else Path.cwd() / "data"
    return root.resolve()


def derivative_directory(source_id: UUID | str, *parts: str) -> Path:
    """Create and return a directory that belongs only to one source asset.

    Frames, page renders and other derived artifacts live here, so two
    uploads can never overwrite each other's evidence.
    """
    directory = storage_root() / "derived" / str(source_id)
    for part in parts:
        directory = directory / part
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def derived_url(path: Path) -> str:
    """URL under the ``/derived`` static mount for a file in a derivative directory."""
    try:
        relative = path.resolve().relative_to(storage_root() / "derived")
    except ValueError:
        return f"/derived/{path.name}"
    return f"/derived/{relative.as_posix()}"


async def persist_upload(upload: UploadFile, source_id: UUID) -> Path:
    """Stream an UploadFile to source-id-scoped storage without loading it all in RAM."""
    filename = Path(upload.filename or "").name or "upload.bin"
    destination = storage_root() / "uploads" / str(source_id) / filename
    destination.parent.mkdir(parents=True, exist_ok=True)

    await upload.seek(0)
    async with await open_file(destination, "wb") as destination_file:
        while chunk := await upload.read(_CHUNK_SIZE):
            await destination_file.write(chunk)
    await upload.seek(0)
    return destination


def storage_key(path: Path) -> str:
    """Expose a portable key instead of an absolute local filesystem path."""
    try:
        return path.resolve().relative_to(storage_root()).as_posix()
    except ValueError:
        return path.name
