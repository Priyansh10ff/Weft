"""Shared Gemini access: one client, JSON responses, retries, file parts.

Every Gemini call in the pipeline goes through ``generate_json`` so retry
policy, model selection and response parsing live in one place.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from app.config import get_settings

_log = logging.getLogger(__name__)

# Above this size a file is sent through the Files API instead of inline.
_INLINE_LIMIT_BYTES = 18 * 1024 * 1024
_RETRYABLE_MARKERS = ("429", "500", "502", "503", "504", "RESOURCE_EXHAUSTED", "UNAVAILABLE",
                      "DEADLINE_EXCEEDED", "INTERNAL", "timed out", "Timeout", "Connection")

MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".mp3": "audio/mp3",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".pdf": "application/pdf",
}


class GeminiError(RuntimeError):
    """Gemini is unavailable, misconfigured, or returned unusable output."""


class GeminiNotConfigured(GeminiError):
    """No ``GEMINI_API_KEY``; callers should fall back to an offline path."""


_client: Any | None = None
_client_lock = threading.Lock()


def get_client() -> Any:
    global _client
    settings = get_settings()
    if not settings.gemini_api_key:
        raise GeminiNotConfigured("GEMINI_API_KEY is not configured.")
    with _client_lock:
        if _client is None:
            try:
                from google import genai
            except ImportError as exc:
                raise GeminiError("google-genai is not installed.") from exc
            _client = genai.Client(api_key=settings.gemini_api_key)
        return _client


def _is_retryable(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}"
    return any(marker in text for marker in _RETRYABLE_MARKERS)


def parse_json(text: str | None) -> Any:
    """Parse model JSON, tolerating a fenced ```json block."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[raw.find("\n") + 1 :] if "\n" in raw else raw
        raw = raw.rsplit("```", 1)[0]
    try:
        return json.loads(raw or "null")
    except json.JSONDecodeError as exc:
        raise GeminiError(f"Gemini returned invalid JSON: {exc}") from exc


def generate_json(
    contents: list[Any],
    *,
    system: str | None = None,
    model: str | None = None,
    client: Any | None = None,
    attempts: int = 3,
    temperature: float = 0.1,
) -> Any:
    """Call Gemini expecting a JSON body; retry transient failures with backoff."""
    active = client or get_client()
    try:
        from google.genai import types
    except ImportError as exc:
        raise GeminiError("google-genai is not installed.") from exc

    config = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        temperature=temperature,
    )
    delay = 1.0
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = active.models.generate_content(
                model=model or get_settings().gemini_model,
                contents=contents,
                config=config,
            )
            return parse_json(getattr(response, "text", None))
        except GeminiError:
            raise
        except Exception as exc:  # SDK exception types vary between releases
            last_exc = exc
            if attempt == attempts or not _is_retryable(exc):
                break
            _log.warning("Gemini call failed (attempt %d/%d): %s", attempt, attempts, exc)
            time.sleep(delay)
            delay *= 2
    raise GeminiError(f"Gemini request failed: {last_exc}") from last_exc


def file_part(path: Path, *, client: Any | None = None, timeout_seconds: float = 300.0) -> Any:
    """A content part for ``path``: inline bytes when small, Files API otherwise."""
    from google.genai import types

    mime = MIME_TYPES.get(path.suffix.lower())
    if mime is None:
        raise GeminiError(f"Unsupported file type for Gemini: {path.suffix}")
    if path.stat().st_size <= _INLINE_LIMIT_BYTES:
        return types.Part.from_bytes(data=path.read_bytes(), mime_type=mime)

    active = client or get_client()
    try:
        uploaded = active.files.upload(file=str(path), config={"mime_type": mime})
        deadline = time.monotonic() + timeout_seconds
        while str(getattr(getattr(uploaded, "state", None), "name", "ACTIVE")) == "PROCESSING":
            if time.monotonic() > deadline:
                raise GeminiError(f"Gemini file processing timed out for {path.name}")
            time.sleep(2)
            uploaded = active.files.get(name=uploaded.name)
    except GeminiError:
        raise
    except Exception as exc:
        raise GeminiError(f"Gemini file upload failed for {path.name}: {exc}") from exc
    if str(getattr(getattr(uploaded, "state", None), "name", "ACTIVE")) == "FAILED":
        raise GeminiError(f"Gemini could not process {path.name}")
    return uploaded


def reset_client() -> None:
    """Drop the cached client (tests)."""
    global _client
    with _client_lock:
        _client = None
