"""Audio upload route: Whisper transcript spans become SPEECH segments."""

import asyncio
import logging

from fastapi import APIRouter, File, HTTPException, UploadFile, status

from app.api.routes._upload import acknowledge_upload, ingest_upload
from app.schemas.knowledge import KnowledgeNode, KnowledgeUploadResponse, MediaModality
from app.services.audio_processor import AudioProcessingError, process_audio
from app.services.storage import persist_upload

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/upload", tags=["uploads"])


def _fmt_time(seconds: float) -> str:
    minutes, secs = divmod(max(0, int(seconds)), 60)
    return f"{minutes:02d}:{secs:02d}"


@router.post("/audio", response_model=KnowledgeUploadResponse)
async def upload_audio(file: UploadFile = File(...)) -> KnowledgeUploadResponse:
    """Persist audio, transcribe it, and store one segment per speech span."""
    receipt = acknowledge_upload(file, MediaModality.AUDIO)
    source_path = await persist_upload(file, receipt.source_asset.source_id)

    try:
        speech_segments = await asyncio.to_thread(process_audio, str(source_path))
    except AudioProcessingError as exc:
        _log.exception("Audio transcription failed for %s", receipt.source_asset.filename)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    nodes: list[KnowledgeNode] = []
    for speech in speech_segments:
        text = str(speech["text"]).strip()
        if not text:
            continue
        start = float(speech["start_time"])
        end = float(speech["end_time"])
        nodes.append(
            KnowledgeNode(
                content=text,
                transcript=text,
                modality=MediaModality.AUDIO,
                timestamp=f"{_fmt_time(start)} - {_fmt_time(end)}",
                source=receipt.source_asset.filename,
                provenance={"start_seconds": start, "end_seconds": end},
            )
        )

    result = await ingest_upload(receipt, nodes, source_path)
    return KnowledgeUploadResponse(
        success=True,
        processed_nodes=len(result.segments),
        source=result.source.filename,
        source_id=result.source.id,
        entity_count=result.entity_count,
        relation_count=result.relation_count,
    )
