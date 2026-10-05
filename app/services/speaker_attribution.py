"""Who said what: speaker turns and names for transcribed speech.

Whisper produces *what* was said and *when*, not *who*.  The recording and
its numbered transcript segments are given to Gemini, which labels each
segment with a consistent speaker and resolves a real name when the
recording itself reveals it (a self-introduction, being addressed by name,
an on-screen caption).  Names are never guessed; unresolved speakers keep a
per-recording label such as "Speaker 2".

This is model-based diarization: good enough to answer "who explained it"
in a meeting recording, and recorded with a lower confidence than the
transcript itself.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.services import gemini

_log = logging.getLogger(__name__)

SPEAKER_CONFIDENCE = 0.7
NAMED_SPEAKER_CONFIDENCE = 0.8

_SYSTEM = """You perform speaker diarization on a recording, using its
transcript as a guide. The transcript is split into numbered segments with
start/end times in seconds.

1. Identify the distinct speakers by voice. Label them "Speaker 1",
   "Speaker 2", ... in order of first appearance.
2. Assign every segment exactly one speaker label.
3. Give a speaker's real name ONLY if the recording itself states it: the
   speaker introduces themselves, is addressed by name, or a name caption is
   shown on screen for them. Otherwise name is null. Never guess.

Return JSON:
{"speakers": [{"label": str, "name": str | null, "evidence": str | null}],
 "segments": [{"i": int, "speaker": str}]}"""


def attribute_speakers(
    media_path: Path,
    speech: list[dict[str, Any]],
    *,
    client: Any | None = None,
) -> list[dict[str, Any] | None]:
    """Return one ``{"label", "name", "confidence"}`` (or ``None``) per speech segment.

    Raises ``gemini.GeminiError`` on failure; callers treat speakers as optional.
    """
    if not speech:
        return []
    numbered = [
        {
            "i": i,
            "start": round(float(seg["start_time"]), 2),
            "end": round(float(seg["end_time"]), 2),
            "text": str(seg["text"])[:500],
        }
        for i, seg in enumerate(speech)
    ]
    part = gemini.file_part(media_path, client=client)
    raw = gemini.generate_json(
        [part, f"Transcript segments:\n{json.dumps(numbered, ensure_ascii=False)}"],
        system=_SYSTEM,
        client=client,
    )
    if not isinstance(raw, dict):
        raise gemini.GeminiError("Speaker attribution returned a non-object response.")

    names: dict[str, str | None] = {}
    for speaker in raw.get("speakers") or []:
        if isinstance(speaker, dict) and speaker.get("label"):
            name = speaker.get("name")
            names[str(speaker["label"]).strip()] = str(name).strip() if name else None

    result: list[dict[str, Any] | None] = [None] * len(speech)
    for item in raw.get("segments") or []:
        try:
            index = int(item.get("i"))
        except (TypeError, ValueError, AttributeError):
            continue
        label = str(item.get("speaker") or "").strip()
        if not label or not 0 <= index < len(speech):
            continue
        name = names.get(label)
        result[index] = {
            "label": label,
            "name": name,
            "confidence": NAMED_SPEAKER_CONFIDENCE if name else SPEAKER_CONFIDENCE,
        }
    return result
