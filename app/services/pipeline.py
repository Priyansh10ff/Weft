"""Modality dispatch + enrichment: file on disk -> list of KnowledgeNodes.

``run_pipeline`` is the single entry point used by both synchronous uploads
and background jobs:

1. extract with the modality's processor (derived artifacts go to this
   source's own ``data/derived/{source_id}/`` directory),
2. attribute speech to speakers (audio/video, needs Gemini),
3. extract typed entities from spoken/written text.

Enrichment failures never fail the upload; they are reported as warnings
and stored on the source record.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.schemas.knowledge import KnowledgeNode, MediaModality, SourceAsset
from app.services import gemini
from app.services.audio_processor import AudioProcessingError, process_audio
from app.services.entity_extractor import extract_entities
from app.services.image_processor import ImageProcessingError, process_image
from app.services.json_processor import JsonProcessingError, process_json
from app.services.pdf_processor import PdfProcessingError, process_pdf
from app.services.speaker_attribution import attribute_speakers
from app.services.storage import derivative_directory
from app.services.video_processor import MediaProcessingError, process_video

_log = logging.getLogger(__name__)

ProgressCallback = Callable[[str, float], None]


class ProcessingFailed(RuntimeError):
    """The source could not be extracted at all (bad file, provider down, ...)."""


@dataclass
class PipelineResult:
    nodes: list[KnowledgeNode]
    warnings: list[str] = field(default_factory=list)
    enrichment: dict[str, Any] = field(default_factory=dict)


def _fmt(seconds: float) -> str:
    minutes, secs = divmod(max(0, int(seconds)), 60)
    return f"{minutes:02d}:{secs:02d}"


def audio_nodes(speech: list[dict[str, Any]], filename: str) -> list[KnowledgeNode]:
    """One SPEECH node per Whisper segment, carrying Whisper's confidence."""
    nodes = []
    for seg in speech:
        text = str(seg["text"]).strip()
        if not text:
            continue
        start, end = float(seg["start_time"]), float(seg["end_time"])
        nodes.append(
            KnowledgeNode(
                content=text,
                transcript=text,
                modality=MediaModality.AUDIO,
                timestamp=f"{_fmt(start)} - {_fmt(end)}",
                source=filename,
                confidence=seg.get("confidence"),
                provenance={"start_seconds": start, "end_seconds": end},
            )
        )
    return nodes


def _extract(asset: SourceAsset, path: Path) -> list[KnowledgeNode]:
    source_id = str(asset.source_id)
    try:
        if asset.modality is MediaModality.VIDEO:
            return process_video(str(path), output_dir=derivative_directory(source_id, "frames"))
        if asset.modality is MediaModality.AUDIO:
            return audio_nodes(process_audio(str(path)), asset.filename)
        if asset.modality is MediaModality.IMAGE:
            return [process_image(str(path))]
        if asset.modality is MediaModality.PDF:
            return process_pdf(str(path), output_dir=derivative_directory(source_id, "pages"))
        if asset.modality is MediaModality.JSON:
            return process_json(str(path))
    except (
        MediaProcessingError,
        AudioProcessingError,
        ImageProcessingError,
        PdfProcessingError,
        JsonProcessingError,
    ) as exc:
        raise ProcessingFailed(str(exc)) from exc
    raise ProcessingFailed(f"Unsupported modality: {asset.modality}")


def attach_speakers(
    nodes: list[KnowledgeNode], media_path: Path, modality: MediaModality, *, client: Any = None
) -> int:
    """Label speech with speakers; returns the number of labelled segments."""
    refs: list[tuple[int, int | None, dict[str, Any]]] = []
    for n, node in enumerate(nodes):
        if modality is MediaModality.AUDIO:
            refs.append(
                (
                    n,
                    None,
                    {
                        "start_time": node.provenance.get("start_seconds", 0.0),
                        "end_time": node.provenance.get("end_seconds", 0.0),
                        "text": node.transcript or "",
                    },
                )
            )
            continue
        for s, seg in enumerate(node.attributes.get("speech_segments") or []):
            refs.append(
                (
                    n,
                    s,
                    {
                        "start_time": seg["start_seconds"],
                        "end_time": seg["end_seconds"],
                        "text": seg["text"],
                    },
                )
            )
    if not refs:
        return 0

    labels = attribute_speakers(media_path, [r[2] for r in refs], client=client)
    labelled = 0
    for (n, s, _), label in zip(refs, labels, strict=True):
        if label is None:
            continue
        labelled += 1
        node = nodes[n]
        if s is not None:
            node.attributes["speech_segments"][s]["speaker"] = label["name"] or label["label"]
        speakers = node.attributes.setdefault("speakers", [])
        if not any(existing["label"] == label["label"] for existing in speakers):
            speakers.append(label)
    return labelled


def attach_entities(nodes: list[KnowledgeNode], *, client: Any = None) -> str:
    """Add typed entities extracted from each node's text; returns the method."""
    targets = [(i, (node.transcript or "").strip()) for i, node in enumerate(nodes)]
    targets = [(i, text) for i, text in targets if text]
    if not targets:
        return "none"
    extracted, method = extract_entities([text for _, text in targets], client=client)
    for (i, _), entities in zip(targets, extracted, strict=True):
        node = nodes[i]
        known = {name.casefold() for name in node.entities}
        added = []
        for name, kind in entities:
            if name.casefold() in known:
                continue
            known.add(name.casefold())
            node.entities.append(name)
            node.entity_types.setdefault(name, kind)
            added.append(name)
        if added:
            node.attributes["text_entities"] = {"method": method, "names": added}
    return method


def run_pipeline(
    asset: SourceAsset,
    path: Path,
    *,
    progress: ProgressCallback | None = None,
) -> PipelineResult:
    settings = get_settings()
    report = progress or (lambda stage, fraction: None)

    report("extracting", 0.05)
    nodes = _extract(asset, path)
    if not nodes:
        raise ProcessingFailed(
            f"No speech, text or visual content could be extracted from {asset.filename}. "
            "Check that GEMINI_API_KEY / GROQ_API_KEY are set and the file is not empty."
        )
    result = PipelineResult(nodes=nodes)

    if (
        settings.speaker_attribution
        and asset.modality in (MediaModality.AUDIO, MediaModality.VIDEO)
        and nodes
    ):
        report("attributing speakers", 0.6)
        if not settings.gemini_enabled:
            result.warnings.append("Speaker attribution skipped: GEMINI_API_KEY is not set.")
        else:
            try:
                result.enrichment["speaker_segments"] = attach_speakers(nodes, path, asset.modality)
            except gemini.GeminiError as exc:
                _log.warning("Speaker attribution failed for %s: %s", asset.filename, exc)
                result.warnings.append(f"Speaker attribution failed: {exc}")

    if settings.entity_extraction and nodes:
        report("extracting entities", 0.8)
        result.enrichment["entity_method"] = attach_entities(nodes)
        if result.enrichment["entity_method"] == "heuristic" and settings.gemini_enabled:
            result.warnings.append("LLM entity extraction failed; used pattern-based fallback.")

    if any(node.attributes.get("visual_extraction_failed") for node in nodes):
        result.warnings.append("Vision analysis failed for some frames/pages; their text was kept.")
    if asset.modality is MediaModality.VIDEO and nodes and not nodes[0].attributes.get(
        "speech_available", True
    ):
        result.warnings.append("No transcript could be produced; video was indexed from visuals only.")
    report("extracted", 0.9)
    return result
