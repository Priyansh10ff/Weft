"""KnowledgeNode semantic retrieval endpoint."""

import json
import logging
import re
from typing import Any

from collections.abc import Iterator

from anyio import to_thread
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse

from app.schemas.knowledge import (
    AnswerSource,
    KnowledgeQueryRequest,
    KnowledgeComparisonResponse,
    KnowledgeQueryResponse,
    KnowledgeQueryResult,
    SynthesizedAnswerModel,
)
from app.services import retrieval
from app.services.answer_synthesizer import SynthesizedAnswer, stream_answer, synthesize_answer
from app.services.vector_store import VectorStoreError

_log = logging.getLogger(__name__)

router = APIRouter(tags=["retrieval"])

# Matches JSON-serialized TemporalLocation dicts like
# '{"end_seconds": 10.0, "page_number": null, "start_seconds": 5.0}'
_JSON_TIMESTAMP_RE = re.compile(r"^\{.*\}$", re.DOTALL)


def _fmt_seconds(seconds: float) -> str:
    mins, secs = divmod(max(0, int(seconds)), 60)
    return f"{mins:02d}:{secs:02d}"


def _sanitize_timestamp(raw: Any) -> str | None:
    """Return a clean human-readable timestamp string.

    Handles:
    - Already-clean strings like ``"01:15 - 01:20"`` or ``"Page 3"`` → pass through.
    - Raw JSON dict strings like ``'{"start_seconds": 5.0, "end_seconds": 10.0}'``
      → parse and reformat as ``"MM:SS - MM:SS"``.
    - ``TemporalLocation``-like objects → convert attributes.
    - ``None`` or empty → return ``None``.
    """
    if raw is None:
        return None

    # Already a clean string (not JSON)?
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return None
        if _JSON_TIMESTAMP_RE.match(stripped):
            # Try to parse and reformat it.
            try:
                obj = json.loads(stripped)
                page = obj.get("page_number")
                if page is not None:
                    return f"Page {page}"
                start = obj.get("start_seconds")
                end = obj.get("end_seconds")
                if start is not None and end is not None:
                    return f"{_fmt_seconds(float(start))} - {_fmt_seconds(float(end))}"
                if start is not None:
                    return _fmt_seconds(float(start))
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        return stripped  # already clean

    # Numeric fallback
    if isinstance(raw, (int, float)):
        return _fmt_seconds(float(raw))

    return None


def _sanitize_frame_path(raw: Any) -> str | None:
    """Return a valid URL path string or ``None``.

    Guards against broken sentinel values that callers must never pass to
    ``st.image()``:  ``0``, ``"0"``, empty strings, and non-string types.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        return None
    stripped = raw.strip()
    if not stripped or stripped == "0":
        return None
    return stripped


def _to_query_result(hit: dict[str, Any]) -> KnowledgeQueryResult:
    """Flatten a hydrated hit into the stable demo-facing response contract."""
    metadata = hit.get("metadata") or {}

    # Transcript: prefer the dedicated field; fall back to content so the UI
    # never shows "No transcript available" when text exists in content.
    transcript_raw = hit.get("transcript") or metadata.get("transcript")
    content_raw = metadata.get("content") or hit.get("document")
    transcript = transcript_raw or content_raw or None

    # Visual summary — only meaningful for visual modalities.
    visual_summary = metadata.get("visual_summary") or None

    # Timestamp: must always be a clean human-readable string.
    timestamp_raw = hit.get("timestamp") or metadata.get("timestamp")
    timestamp = _sanitize_timestamp(timestamp_raw)

    # Frame path: must be a valid URL string or None — never "0" or falsy garbage.
    frame_path_raw = hit.get("frame_path") or metadata.get("frame_path")
    frame_path = _sanitize_frame_path(frame_path_raw)

    return KnowledgeQueryResult(
        transcript=transcript,
        visual_summary=visual_summary,
        timestamp=timestamp,
        frame_path=frame_path,
        source=hit.get("source") or metadata.get("source"),
        modality=metadata.get("modality"),
        similarity_score=float(hit["similarity_score"]),
        distance=float(hit["distance"]),
        segment_id=metadata.get("segment_id"),
        source_id=metadata.get("source_id"),
        kind=metadata.get("kind"),
        confidence=metadata.get("confidence"),
        entities=list(metadata.get("entities") or []),
        speakers=list(metadata.get("speakers") or []),
        score=hit.get("score"),
        signals=hit.get("signals") or {},
        via=hit.get("via"),
        matched_span=hit.get("matched_span"),
        matched_regions=hit.get("matched_regions") or [],
    )


async def _retrieve(request: KnowledgeQueryRequest) -> retrieval.RetrievalResult:
    try:
        return await to_thread.run_sync(
            lambda: retrieval.retrieve(
                request.query,
                request.limit,
                hybrid=request.hybrid,
                expand=request.expand,
                decompose_query=request.decompose,
                modalities=[m.value for m in request.modalities] if request.modalities else None,
            )
        )
    except VectorStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=str(exc)
        ) from exc


def _answer_model(synthesized: SynthesizedAnswer) -> SynthesizedAnswerModel:
    return SynthesizedAnswerModel(
        answer=synthesized.answer,
        grounded=synthesized.grounded,
        method=synthesized.method,
        sources=[AnswerSource(**s) for s in synthesized.sources],
    )


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@router.post("/query", response_model=KnowledgeQueryResponse)
async def query_knowledge(request: KnowledgeQueryRequest) -> KnowledgeQueryResponse:
    """Search the segment index and synthesize a grounded answer from hydrated evidence."""
    result = await _retrieve(request)
    hits = result.hits

    # Synthesize one grounded answer from the retrieved evidence instead of
    # leaving the caller to eyeball raw hits. This is what actually connects
    # cross-modal evidence (transcript + visual_summary + page text) into a
    # single answer, rather than stopping at "convert media to text, then
    # vector search".
    synthesized = await to_thread.run_sync(
        lambda: synthesize_answer(request.query, hits, subqueries=result.subqueries)
    )

    return KnowledgeQueryResponse(
        query=request.query,
        subqueries=result.subqueries,
        strategy=result.strategy,
        results=[_to_query_result(hit) for hit in hits],
        answer=_answer_model(synthesized),
    )


@router.post("/query/stream")
async def query_stream(request: KnowledgeQueryRequest) -> StreamingResponse:
    """Same as ``/query``, as server-sent events: the evidence first, then the answer as it is written.

    Events: ``retrieval`` (query, subqueries, strategy, results), any number of
    ``delta`` (``{"text": ...}``), one ``answer`` (the final answer with its
    sources), and ``done``. A failure after retrieval is sent as ``error``.
    """
    result = await _retrieve(request)
    head = {
        "query": request.query,
        "subqueries": result.subqueries,
        "strategy": result.strategy,
        "results": [_to_query_result(hit).model_dump(mode="json") for hit in result.hits],
    }

    def events() -> Iterator[str]:
        yield _sse("retrieval", head)
        try:
            for kind, payload in stream_answer(request.query, result.hits, subqueries=result.subqueries):
                if kind == "delta":
                    yield _sse("delta", {"text": payload})
                else:
                    yield _sse("answer", _answer_model(payload).model_dump(mode="json"))
        except Exception as exc:  # pragma: no cover - stream_answer already degrades
            _log.exception("Answer stream failed")
            yield _sse("error", {"detail": str(exc)})
        yield _sse("done", {})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/query/compare", response_model=KnowledgeComparisonResponse)
async def compare_query(request: KnowledgeQueryRequest) -> KnowledgeComparisonResponse:
    """Compare combined multimodal retrieval with transcript-only retrieval."""
    try:
        multimodal_hits, baseline_hits = await to_thread.run_sync(
            lambda: (
                retrieval.retrieve(request.query, request.limit).hits,
                retrieval.search(request.query, request.limit, text_only=True),
            )
        )
    except VectorStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=str(exc)
        ) from exc

    return KnowledgeComparisonResponse(
        query=request.query,
        multimodal_result=_to_query_result(multimodal_hits[0]) if multimodal_hits else None,
        text_only_baseline_result=_to_query_result(baseline_hits[0]) if baseline_hits else None,
    )
