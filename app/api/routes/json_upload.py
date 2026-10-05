"""Structured JSON ingestion endpoint (single record or array of records)."""

from anyio import to_thread
from fastapi import APIRouter, File, HTTPException, UploadFile, status

from app.api.routes._upload import acknowledge_upload, ingest_upload
from app.schemas.knowledge import KnowledgeUploadResponse, MediaModality
from app.services.json_processor import JsonProcessingError, process_json
from app.services.storage import persist_upload

router = APIRouter(prefix="/upload", tags=["uploads"])


@router.post("/json", response_model=KnowledgeUploadResponse)
async def upload_json(file: UploadFile = File(...)) -> KnowledgeUploadResponse:
    """Store a .json file (object/array of records) or a .txt note as segments."""
    receipt = acknowledge_upload(file, MediaModality.JSON)
    source_path = await persist_upload(file, receipt.source_asset.source_id)
    try:
        nodes = await to_thread.run_sync(process_json, str(source_path))
    except JsonProcessingError as exc:
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
