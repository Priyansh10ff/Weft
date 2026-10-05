"""Video -> scene-aligned audio-visual knowledge nodes.

Pipeline:

1. Transcribe the audio track (Whisper) into timestamped speech segments,
   each with Whisper's own confidence.
2. Decode the video once, sampling a frame signature every
   ``SCENE_SAMPLE_SECONDS`` and cut it into scenes where the picture changes
   (``app.services.scene_detector``).
3. Split long scenes into windows at speech boundaries.
4. Save one keyframe per scene into this source's own derived directory and
   describe it with the vision model; keyframes that are perceptual
   duplicates of an earlier one (the same slide shown again) reuse its
   analysis instead of paying for another call.
5. Emit one node per window: the speech said in it, what was on screen,
   exact time range, scene, keyframe and per-sentence timestamps.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.config import get_settings
from app.schemas.knowledge import KnowledgeNode, MediaModality
from app.services import scene_detector as sd
from app.services.audio_processor import AudioProcessingError, process_audio
from app.services.image_processor import ImageProcessingError, analyze_image
from app.services.storage import derivative_directory, derived_url

_log = logging.getLogger(__name__)

# Confidence assumed for a successful vision description (the model reports
# none); a failed description contributes nothing.
VISION_CONFIDENCE = 0.75


class MediaProcessingError(RuntimeError):
    """Raised when a video cannot be decoded, sampled, or described."""


class FfmpegUnavailableError(MediaProcessingError):
    """Retained for backwards compatibility with the upload route."""


def _fmt(seconds: float) -> str:
    minutes, secs = divmod(max(0, int(seconds)), 60)
    return f"{minutes:02d}:{secs:02d}"


def _speech_confidence(speech: list[dict[str, Any]]) -> float | None:
    """Duration-weighted mean of Whisper segment confidences."""
    total = weight = 0.0
    for seg in speech:
        conf = seg.get("confidence")
        if conf is None:
            continue
        duration = max(0.1, float(seg["end_time"]) - float(seg["start_time"]))
        total += float(conf) * duration
        weight += duration
    return round(total / weight, 4) if weight else None


def window_confidence(
    speech: list[dict[str, Any]], has_visual: bool, visual_failed: bool
) -> float | None:
    """Combine speech and vision confidence for one audio-visual window."""
    parts: list[float] = []
    speech_conf = _speech_confidence(speech)
    if speech_conf is not None:
        parts.append(speech_conf)
    if has_visual:
        parts.append(VISION_CONFIDENCE)
    if not parts:
        return None
    value = sum(parts) / len(parts)
    if visual_failed:
        value *= 0.9
    return round(value, 4)


def process_video(
    file_path: str,
    *,
    output_dir: Path | None = None,
    gemini_client: Any | None = None,
    groq_client: Any | None = None,
    transcribe: Any = process_audio,
    describe: Any = analyze_image,
) -> list[KnowledgeNode]:
    """Turn a video file into scene-aligned ``KnowledgeNode`` windows."""
    settings = get_settings()
    video_path = Path(file_path)
    if not video_path.is_file():
        raise MediaProcessingError(f"Video source does not exist: {video_path}")
    try:
        import cv2
    except ImportError as exc:
        raise MediaProcessingError("OpenCV is required to process video files.") from exc

    frames_dir = output_dir or derivative_directory(f"adhoc-{uuid4()}", "frames")
    frames_dir.mkdir(parents=True, exist_ok=True)

    # 1. Speech. A video without a usable audio track is still indexed visually.
    speech_available = True
    try:
        speech = transcribe(str(video_path), client=groq_client)
    except AudioProcessingError as exc:
        _log.warning("No transcript for %s, indexing visuals only: %s", video_path.name, exc)
        speech, speech_available = [], False
    speech = sorted(speech, key=lambda s: float(s["start_time"]))

    # 2-3. Scenes and windows.
    try:
        _, duration, signatures = sd.sample_video(video_path, settings.scene_sample_seconds)
    except ValueError as exc:
        raise MediaProcessingError(str(exc)) from exc
    if not signatures:
        raise MediaProcessingError(f"No decodable frames in {video_path.name}")
    if speech:
        duration = max(duration, float(speech[-1]["end_time"]))
    scenes = sd.detect_scenes(
        signatures,
        duration,
        threshold=settings.scene_cut_threshold,
        min_scene_seconds=settings.scene_min_seconds,
    )
    windows = sd.plan_windows(scenes, speech, max_window_seconds=settings.window_max_seconds)
    window_speech = sd.assign_speech(windows, speech)

    # 4. Keyframes, deduplicated by perceptual hash.
    by_index = {sig.frame_index: sig for sig in signatures}
    keyframe_path: dict[int, Path] = {}
    duplicate_of: dict[int, int] = {}
    unique: list[int] = []
    for scene in scenes:
        sig = by_index[scene.keyframe_index]
        original = next(
            (
                s
                for s in unique
                if sd.hamming(by_index[scenes[s].keyframe_index].dhash, sig.dhash)
                <= settings.keyframe_dup_hamming
            ),
            None,
        )
        if original is not None:
            duplicate_of[scene.index] = original
            keyframe_path[scene.index] = keyframe_path[original]
            continue
        frame = sd.read_frame(video_path, scene.keyframe_index)
        if frame is None:
            continue
        path = frames_dir / f"scene_{scene.index:03d}_{_fmt(scene.keyframe_time).replace(':', '_')}.jpg"
        if not cv2.imwrite(str(path), frame):
            raise MediaProcessingError(f"Unable to save keyframe {path.name}")
        keyframe_path[scene.index] = path
        unique.append(scene.index)

    def _analyze(scene_index: int) -> dict[str, Any] | None:
        try:
            return describe(keyframe_path[scene_index], client=gemini_client)
        except ImageProcessingError as exc:
            _log.warning("Vision failed for %s scene %d: %s", video_path.name, scene_index, exc)
            return None

    with ThreadPoolExecutor(max_workers=settings.vision_workers) as pool:
        analyses = dict(zip(unique, pool.map(_analyze, unique)))
    for scene_index, original in duplicate_of.items():
        analyses[scene_index] = analyses.get(original)

    # 5. One node per window.
    scene_by_index = {scene.index: scene for scene in scenes}
    nodes: list[KnowledgeNode] = []
    for window, said in zip(windows, window_speech, strict=True):
        scene = scene_by_index[window.scene_index]
        analysis = analyses.get(scene.index)
        visual_failed = scene.index in keyframe_path and analysis is None
        transcript = " ".join(str(s["text"]).strip() for s in said).strip()
        visual = (analysis or {}).get("visual_summary") or None
        if not transcript and not visual:
            continue
        path = keyframe_path.get(scene.index)
        attributes: dict[str, Any] = {
            "speech_segments": [
                {
                    "start_seconds": float(s["start_time"]),
                    "end_seconds": float(s["end_time"]),
                    "text": str(s["text"]).strip(),
                    **({"confidence": s["confidence"]} if s.get("confidence") is not None else {}),
                }
                for s in said
            ],
            "speech_available": speech_available,
        }
        if analysis:
            attributes.update(
                image_type=analysis.get("image_type"),
                ocr_text=analysis.get("ocr_text") or None,
                ocr_blocks=analysis.get("ocr_blocks") or [],
                regions=analysis.get("regions") or [],
            )
        if scene.index in duplicate_of:
            attributes["keyframe_duplicate_of_scene"] = duplicate_of[scene.index]
        if visual_failed:
            attributes["visual_extraction_failed"] = True

        nodes.append(
            KnowledgeNode(
                content=transcript,
                transcript=transcript or None,
                visual_summary=visual,
                modality=MediaModality.VIDEO,
                timestamp=f"{_fmt(window.start)} - {_fmt(window.end)}",
                source=video_path.name,
                frame_path=derived_url(path) if path else None,
                entities=list((analysis or {}).get("entities") or []),
                entity_types=dict((analysis or {}).get("entity_types") or {}),
                confidence=window_confidence(said, bool(visual), visual_failed),
                provenance={
                    "start_seconds": round(window.start, 3),
                    "end_seconds": round(window.end, 3),
                    "scene_index": scene.index,
                    "scene_start_seconds": round(scene.start, 3),
                    "scene_end_seconds": round(scene.end, 3),
                    "keyframe_seconds": round(scene.keyframe_time, 3),
                },
                attributes=attributes,
            )
        )
    return nodes
