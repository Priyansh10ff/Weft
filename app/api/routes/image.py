"""Standalone PNG/JPEG ingestion."""

from datetime import datetime

from fastapi import APIRouter, File, Query, Response, UploadFile

from app.api.routes._upload import handle_upload
from app.schemas.knowledge import KnowledgeUploadResponse, MediaModality

router = APIRouter(prefix="/upload", tags=["uploads"])

_BACKGROUND = Query(False, description="Return 202 immediately and process as a background job.")
_FORCE = Query(False, description="Re-process even if identical bytes were already ingested.")
_RECORDED_AT = Query(
    None,
    description="When the content was recorded or written (ISO 8601). Orders sources on timelines; defaults to upload time.",
)


@router.post("/image", response_model=KnowledgeUploadResponse)
async def upload_image(
    response: Response,
    file: UploadFile = File(...),
    background: bool = _BACKGROUND,
    force: bool = _FORCE,
    recorded_at: datetime | None = _RECORDED_AT,
) -> KnowledgeUploadResponse:
    """Upload a PNG/JPEG; vision extracts a description, OCR blocks, regions and typed entities."""
    return await handle_upload(
        file, MediaModality.IMAGE, response, background=background, force=force,
        recorded_at=recorded_at,
    )
