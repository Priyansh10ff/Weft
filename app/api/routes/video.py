"""Video ingestion endpoint: transcript windows aligned with sampled frames."""

from pathlib import Path

from anyio import to_thread
from fastapi import APIRouter, File, HTTPException, UploadFile, status

from app.api.routes._upload import acknowledge_upload, ingest_upload
from app.schemas.knowledge import MediaModality, VideoUploadResponse
from app.services.storage import persist_upload
from app.services.video_processor import MediaProcessingError, process_video

router = APIRouter(prefix="/upload", tags=["uploads"])


@router.post("/video", response_model=VideoUploadResponse)
async def upload_video(file: UploadFile = File(...)) -> VideoUploadResponse:
    """Save an MP4, build timestamp-aligned AV_WINDOW segments, and store them."""
    if Path(file.filename or "").suffix.lower() != ".mp4":
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Only MP4 video uploads are supported.",
        )
    receipt = acknowledge_upload(file, MediaModality.VIDEO)
    try:
        source_path = await persist_upload(file, receipt.source_asset.source_id)
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to save the uploaded video.",
        ) from exc
    try:
        nodes = await to_thread.run_sync(process_video, str(source_path))
    except MediaProcessingError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    result = await ingest_upload(receipt, nodes, source_path)
    return VideoUploadResponse(
        success=True,
        processed_nodes=len(result.segments),
        source=result.source.filename,
        source_id=result.source.id,
        entity_count=result.entity_count,
        relation_count=result.relation_count,
    )
