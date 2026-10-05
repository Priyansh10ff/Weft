"""Standalone PNG/JPEG ingestion endpoint."""

from pathlib import Path

from anyio import to_thread
from fastapi import APIRouter, File, HTTPException, UploadFile, status

from app.api.routes._upload import acknowledge_upload, ingest_upload
from app.schemas.knowledge import KnowledgeUploadResponse, MediaModality
from app.services.image_processor import ImageProcessingError, process_image
from app.services.storage import persist_upload

router = APIRouter(prefix="/upload", tags=["uploads"])


@router.post("/image", response_model=KnowledgeUploadResponse)
async def upload_image(file: UploadFile = File(...)) -> KnowledgeUploadResponse:
    """Analyze a PNG/JPEG image with vision and store it as one IMAGE segment."""
    if Path(file.filename or "").suffix.lower() not in {".png", ".jpg", ".jpeg"}:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Only PNG and JPEG image uploads are supported.",
        )
    receipt = acknowledge_upload(file, MediaModality.IMAGE)
    source_path = await persist_upload(file, receipt.source_asset.source_id)
    try:
        node = await to_thread.run_sync(process_image, str(source_path))
    except ImageProcessingError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    result = await ingest_upload(receipt, [node], source_path)
    return KnowledgeUploadResponse(
        success=True,
        processed_nodes=len(result.segments),
        source=result.source.filename,
        source_id=result.source.id,
        entity_count=result.entity_count,
        relation_count=result.relation_count,
    )
