"""PDF ingestion endpoint: one PAGE segment per page."""

from anyio import to_thread
from fastapi import APIRouter, File, HTTPException, UploadFile, status

from app.api.routes._upload import acknowledge_upload, ingest_upload
from app.schemas.knowledge import KnowledgeUploadResponse, MediaModality
from app.services.pdf_processor import PdfProcessingError, process_pdf
from app.services.storage import persist_upload

router = APIRouter(prefix="/upload", tags=["uploads"])


@router.post("/pdf", response_model=KnowledgeUploadResponse)
async def upload_pdf(file: UploadFile = File(...)) -> KnowledgeUploadResponse:
    """Extract page text and visual artifacts from a PDF, then store every page."""
    receipt = acknowledge_upload(file, MediaModality.PDF)
    source_path = await persist_upload(file, receipt.source_asset.source_id)
    try:
        nodes = await to_thread.run_sync(process_pdf, str(source_path))
    except PdfProcessingError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    result = await ingest_upload(receipt, nodes, source_path)
    return KnowledgeUploadResponse(
        success=True,
        processed_nodes=len(result.segments),
        source=result.source.filename,
        source_id=result.source.id,
        entity_count=result.entity_count,
        relation_count=result.relation_count,
    )
