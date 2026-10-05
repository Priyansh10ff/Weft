"""PDF ingestion: one page segment per page."""

from fastapi import APIRouter, File, Query, Response, UploadFile

from app.api.routes._upload import handle_upload
from app.schemas.knowledge import KnowledgeUploadResponse, MediaModality

router = APIRouter(prefix="/upload", tags=["uploads"])

_BACKGROUND = Query(False, description="Return 202 immediately and process as a background job.")
_FORCE = Query(False, description="Re-process even if identical bytes were already ingested.")


@router.post("/pdf", response_model=KnowledgeUploadResponse)
async def upload_pdf(
    response: Response,
    file: UploadFile = File(...),
    background: bool = _BACKGROUND,
    force: bool = _FORCE,
) -> KnowledgeUploadResponse:
    """Upload a PDF; each page keeps its text layer plus a vision analysis of the rendered page."""
    return await handle_upload(
        file, MediaModality.PDF, response, background=background, force=force
    )
