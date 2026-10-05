"""Phase 2 extraction: video windows, PDF pages, vision normalization, confidence."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.schemas.knowledge import MediaModality, SourceAsset
from app.services.audio_processor import segment_confidence
from app.services.image_processor import ImageProcessingError, normalize_analysis
from app.services.ingestion import ingest_nodes

cv2 = pytest.importorskip("cv2")
from tests.test_scene_detector import SLIDES, make_slide_video  # noqa: E402


def _vision(path: Path, client=None) -> dict:
    """Fake vision: describe a keyframe by its file name."""
    return {
        "image_type": "slide",
        "visual_summary": f"slide shown in {path.name}",
        "ocr_text": "Fix: read replicas",
        "ocr_blocks": [{"text": "Fix: read replicas", "box": {"x": 0.1, "y": 0.1, "width": 0.5, "height": 0.1}}],
        "regions": [],
        "entities": ["read replicas", "Postgres"],
        "entity_types": {"read replicas": "component", "Postgres": "system"},
    }


def _speech(path: str, client=None) -> list[dict]:
    return [
        {"start_time": 0.5, "end_time": 3.5, "text": "Database load is the problem.", "confidence": 0.9},
        {"start_time": 4.5, "end_time": 7.5, "text": "We added read replicas.", "confidence": 0.8},
        {"start_time": 13.0, "end_time": 15.5, "text": "Back to the problem slide.", "confidence": 0.7},
    ]


def test_video_windows_follow_scenes(tmp_path):
    from app.services.video_processor import process_video

    video = make_slide_video(tmp_path / "talk.mp4", SLIDES)
    out = tmp_path / "media" / "derived" / "src" / "frames"
    calls: list[str] = []

    def vision(path, client=None):
        calls.append(path.name)
        return _vision(path)

    nodes = process_video(str(video), output_dir=out, transcribe=_speech, describe=vision)

    assert [n.provenance["scene_index"] for n in nodes] == [0, 1, 2, 3]
    assert len(calls) == 3  # slide 1 reappears as scene 3 and reuses its analysis
    assert nodes[3].attributes["keyframe_duplicate_of_scene"] == 0
    assert nodes[0].transcript == "Database load is the problem."
    assert nodes[1].transcript == "We added read replicas."
    assert nodes[2].transcript is None and nodes[2].visual_summary  # silent slide still indexed
    assert nodes[0].frame_path.startswith("/derived/src/frames/scene_000_")
    assert (out / Path(nodes[0].frame_path).name).is_file()
    assert nodes[0].entity_types["Postgres"] == "system"
    assert nodes[0].attributes["ocr_blocks"][0]["box"]["width"] == 0.5
    # speech 0.9 (weighted) averaged with vision prior 0.75
    assert nodes[0].confidence == pytest.approx((0.9 + 0.75) / 2)
    segs = nodes[1].attributes["speech_segments"]
    assert segs[0]["start_seconds"] == 4.5 and segs[0]["confidence"] == 0.8


def test_video_without_audio_is_indexed_visually(tmp_path):
    from app.services.audio_processor import AudioProcessingError
    from app.services.video_processor import process_video

    def no_audio(path, client=None):
        raise AudioProcessingError("no audio track")

    video = make_slide_video(tmp_path / "silent.mp4", SLIDES[:2])
    nodes = process_video(str(video), output_dir=tmp_path / "f", transcribe=no_audio, describe=_vision)
    assert len(nodes) == 2
    assert all(n.attributes["speech_available"] is False for n in nodes)


def test_failed_vision_keeps_speech_and_lowers_confidence(tmp_path, repo, store):
    from app.services.video_processor import process_video

    def broken(path, client=None):
        raise ImageProcessingError("quota")

    video = make_slide_video(tmp_path / "talk.mp4", SLIDES[:2])
    nodes = process_video(str(video), output_dir=tmp_path / "f", transcribe=_speech, describe=broken)
    assert nodes[0].visual_summary is None
    assert nodes[0].attributes["visual_extraction_failed"] is True
    assert nodes[0].confidence == pytest.approx(0.9 * 0.9)

    result = ingest_nodes(
        SourceAsset(filename="talk.mp4", modality=MediaModality.VIDEO), nodes,
        repository=repo, vector_store=store,
    )
    assert result.segments[0].attributes["visual_extraction_failed"] is True
    assert result.segments[0].locator.start_seconds == 0.0


def _make_pdf(path: Path, pages: list[str]) -> Path:
    fitz = pytest.importorskip("pymupdf")
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        if text:
            page.insert_text((72, 72), text)
    doc.save(str(path))
    doc.close()
    return path


def test_pdf_pages_text_vision_and_ocr_fallback(tmp_path, repo, store):
    from app.services.pdf_processor import process_pdf

    pdf = _make_pdf(tmp_path / "design.pdf", ["Remediation: add read replicas.", ""])

    def vision(path, client=None):
        result = _vision(path)
        result["ocr_text"] = "Scanned page text" if path.name == "page_0002.jpg" else "x"
        return result

    nodes = process_pdf(str(pdf), output_dir=tmp_path / "media" / "derived" / "s" / "pages", describe=vision)
    assert [n.provenance["page_number"] for n in nodes] == [1, 2]
    assert "Remediation" in nodes[0].transcript
    assert nodes[0].attributes["has_text_layer"] is True
    assert nodes[1].transcript == "Scanned page text"
    assert nodes[1].attributes["text_from_ocr"] is True
    assert nodes[0].frame_path == "/derived/s/pages/page_0001.jpg"

    result = ingest_nodes(
        SourceAsset(filename="design.pdf", modality=MediaModality.PDF), nodes,
        repository=repo, vector_store=store,
    )
    first, second = result.segments
    assert first.extractor == "pdf-text-layer+vision" and first.confidence == 0.9
    assert second.extractor == "pdf-vision-only" and second.confidence == 0.7


def test_normalize_analysis_converts_boxes_and_types():
    out = normalize_analysis(
        {
            "image_type": "diagram",
            "visual_summary": " arch ",
            "text_blocks": [{"text": "API", "box_2d": [100, 200, 300, 600]}, {"text": "bad", "box_2d": [5, 5, 1, 1]}],
            "regions": [{"label": "cache", "box_2d": [0, 0, 1000, 500]}],
            "entities": [{"name": "Redis", "type": "system"}, {"name": "Redis", "type": "x"}, "Kafka",
                         {"name": "Ana", "type": "alien"}],
        }
    )
    assert out["ocr_blocks"][0]["box"] == {"x": 0.2, "y": 0.1, "width": 0.4, "height": 0.2}
    assert out["ocr_blocks"][1]["box"] is None
    assert out["regions"][0]["box"]["width"] == 0.5
    assert out["entities"] == ["Redis", "Kafka", "Ana"]
    assert out["entity_types"] == {"Redis": "system", "Kafka": "concept", "Ana": "concept"}
    with pytest.raises(ImageProcessingError):
        normalize_analysis(["not", "an", "object"])


def test_whisper_confidence():
    assert segment_confidence(0.0, 0.0) == 1.0
    assert segment_confidence(-0.2, 0.1) == pytest.approx(0.7369, abs=1e-4)
    assert segment_confidence(None, 0.1) is None
    assert segment_confidence(-0.1, None) == pytest.approx(0.9048, abs=1e-4)
