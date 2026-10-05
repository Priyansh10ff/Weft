"""Video ingestion: scene-aligned speech + keyframe windows."""

from fastapi import APIRouter, File, Query, Response, UploadFile

from app.api.routes._upload import handle_upload
from app.schemas.knowledge import KnowledgeUploadResponse, MediaModality

router = APIRouter(prefix="/upload", tags=["uploads"])

_BACKGROUND = Query(False, description="Return 202 immediately and process as a background job.")
_FORCE = Query(False, description="Re-process even if identical bytes were already ingested.")


@router.post("/video", response_model=KnowledgeUploadResponse)
async def upload_video(
    response: Response,
    file: UploadFile = File(...),
    background: bool = _BACKGROUND,
    force: bool = _FORCE,
) -> KnowledgeUploadResponse:
    """Upload an MP4; scenes are detected, keyframes described, speech aligned and attributed to speakers."""
    return await handle_upload(
        file, MediaModality.VIDEO, response, background=background, force=force
    )
