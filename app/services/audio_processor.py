"""Free-tier Groq Whisper transcription with a local faster-whisper fallback."""

import logging
import math
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)


class AudioProcessingError(RuntimeError):
    """Raised when an input audio file cannot be transcribed."""


def _response_value(value: object, field: str, default: Any = None) -> Any:
    """Read a field from either an SDK response object or a dictionary."""
    if isinstance(value, Mapping):
        return value.get(field, default)
    return getattr(value, field, default)


def _load_environment() -> None:
    """Load local .env values when python-dotenv is available."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def _groq_client() -> Any:
    """Create a Groq client after GEMINI-independent environment loading."""
    try:
        from groq import Groq
    except ImportError as exc:
        raise AudioProcessingError("Groq support requires the groq package.") from exc
    try:
        return Groq(api_key=os.environ["GROQ_API_KEY"])
    except (KeyError, ValueError) as exc:
        raise AudioProcessingError("GROQ_API_KEY is not configured.") from exc


def segment_confidence(avg_logprob: object, no_speech_prob: object) -> float | None:
    """Whisper's own certainty for a segment, in ``[0, 1]``.

    ``exp(avg_logprob)`` is the geometric-mean token probability; it is
    discounted by the probability that the span is not speech at all.
    Returns ``None`` when the provider did not report the statistics.
    """
    try:
        logprob = float(avg_logprob)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    try:
        no_speech = float(no_speech_prob)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        no_speech = 0.0
    value = math.exp(min(0.0, logprob)) * (1.0 - min(1.0, max(0.0, no_speech)))
    return round(min(1.0, max(0.0, value)), 4)


def _normalize_segments(response: object) -> list[dict[str, float | str]]:
    """Normalize Groq's verbose JSON response into the public segment shape."""
    transcripts: list[dict[str, float | str]] = []
    for raw_segment in _response_value(response, "segments", []) or []:
        try:
            start_time = float(_response_value(raw_segment, "start"))
            end_time = float(_response_value(raw_segment, "end"))
        except (TypeError, ValueError) as exc:
            raise AudioProcessingError(
                "Whisper returned a segment without valid start/end timestamps."
            ) from exc
        if end_time < start_time:
            raise AudioProcessingError("Whisper returned a segment with an invalid time range.")
        text = str(_response_value(raw_segment, "text", "")).strip()
        if text:
            entry: dict[str, float | str] = {
                "start_time": start_time,
                "end_time": end_time,
                "text": text,
            }
            confidence = segment_confidence(
                _response_value(raw_segment, "avg_logprob"),
                _response_value(raw_segment, "no_speech_prob"),
            )
            if confidence is not None:
                entry["confidence"] = confidence
            transcripts.append(entry)
    return transcripts


def _transcribe_with_groq(path: Path, client: Any) -> list[dict[str, float | str]]:
    """Request timestamped segments from Groq's hosted Whisper model."""
    try:
        with path.open("rb") as audio_file:
            response = client.audio.transcriptions.create(
                file=(path.name, audio_file.read()),
                model="whisper-large-v3",
                response_format="verbose_json",
                timestamp_granularities=["segment"],
            )
    except Exception as exc:  # SDK/provider exceptions vary between releases.
        # Log the full traceback so Groq failures are never silently swallowed.
        _log.exception("Groq Whisper transcription failed for %s: %s", path.name, exc)
        raise AudioProcessingError("Groq Whisper transcription failed.") from exc
    return _normalize_segments(response)


def _transcribe_locally(path: Path) -> list[dict[str, float | str]]:
    """Use CPU faster-whisper when no Groq API key has been configured."""
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise AudioProcessingError(
            "Install faster-whisper for local transcription or set GROQ_API_KEY."
        ) from exc
    try:
        model = WhisperModel(
            os.getenv("FASTER_WHISPER_MODEL", "base"),
            device="cpu",
            compute_type="int8",
        )
        segments, _ = model.transcribe(str(path), vad_filter=True)
        results: list[dict[str, float | str]] = []
        for segment in segments:
            text = str(segment.text).strip()
            if not text:
                continue
            entry: dict[str, float | str] = {
                "start_time": float(segment.start),
                "end_time": float(segment.end),
                "text": text,
            }
            confidence = segment_confidence(
                getattr(segment, "avg_logprob", None), getattr(segment, "no_speech_prob", None)
            )
            if confidence is not None:
                entry["confidence"] = confidence
            results.append(entry)
        return results
    except Exception as exc:
        raise AudioProcessingError("Local faster-whisper transcription failed.") from exc


def process_audio(
    file_path: str, *, client: Any | None = None
) -> list[dict[str, float | str]]:
    """Transcribe audio/video with Groq, falling back to local faster-whisper.

    A supplied ``client`` is treated as a Groq-compatible client, which keeps
    the function straightforward to test without network access.
    """
    path = Path(file_path)
    if not path.is_file():
        raise AudioProcessingError(f"Audio source does not exist: {path}")
    _load_environment()
    if client is not None:
        return _transcribe_with_groq(path, client)
    if os.getenv("GROQ_API_KEY"):
        return _transcribe_with_groq(path, _groq_client())
    return _transcribe_locally(path)
