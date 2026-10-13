"""Claude access: one client, JSON responses, streaming, image blocks.

Mirrors ``gemini.py`` so ``llm`` can route to either. Claude reads text and
images; it does not accept audio or video, so a ``MediaFile`` raises
``ClaudeError`` and speaker attribution is skipped when Claude is the provider.
Sampling parameters are left at their defaults (current models reject a
non-default temperature).
"""

from __future__ import annotations

import base64
import logging
import threading
from typing import Any, Iterator

from app.config import get_settings
from app.services.gemini import parse_json as _parse_json
from app.services.llm import Image, LLMError, LLMNotConfigured, MediaFile

_log = logging.getLogger(__name__)

_MAX_TOKENS = 4096
_JSON_RULE = "Respond with one JSON value only: no prose before or after it and no code fences."


class ClaudeError(LLMError):
    """Claude is unavailable, misconfigured, or returned unusable output."""


class ClaudeNotConfigured(ClaudeError, LLMNotConfigured):
    """No ``ANTHROPIC_API_KEY``."""


_client: Any | None = None
_client_lock = threading.Lock()


def get_client() -> Any:
    global _client
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise ClaudeNotConfigured("ANTHROPIC_API_KEY is not configured.")
    with _client_lock:
        if _client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise ClaudeError("anthropic is not installed.") from exc
            # The SDK retries 408/409/429/5xx and connection errors with backoff.
            _client = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=3, timeout=120.0)
        return _client


def convert(contents: list[Any], *, client: Any | None = None) -> list[dict[str, Any]]:
    """Provider-neutral content (see ``llm``) to Claude content blocks."""
    blocks: list[dict[str, Any]] = []
    for item in contents:
        if isinstance(item, Image):
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": item.mime_type,
                        "data": base64.standard_b64encode(item.data).decode("ascii"),
                    },
                }
            )
        elif isinstance(item, MediaFile):
            raise ClaudeError(f"Claude does not accept audio or video input ({item.path.name}).")
        elif isinstance(item, str):
            blocks.append({"type": "text", "text": item})
        else:
            raise ClaudeError(f"Unsupported content for Claude: {type(item).__name__}")
    return blocks


def _text(response: Any) -> str:
    return "".join(
        getattr(block, "text", "") for block in getattr(response, "content", []) or [] if getattr(block, "type", "") == "text"
    )


def generate_json(
    contents: list[dict[str, Any]],
    *,
    system: str | None = None,
    client: Any | None = None,
    attempts: int = 3,
) -> Any:
    """Call Claude expecting a JSON body. Retries are the SDK's."""
    active = client or get_client()
    instructions = f"{system}\n\n{_JSON_RULE}" if system else _JSON_RULE
    try:
        response = active.messages.create(
            model=get_settings().claude_model,
            max_tokens=_MAX_TOKENS,
            system=instructions,
            messages=[{"role": "user", "content": contents}],
        )
    except Exception as exc:  # anthropic.APIError and transport errors
        raise ClaudeError(f"Claude request failed: {exc}") from exc
    try:
        return _parse_json(_text(response))
    except LLMError as exc:
        raise ClaudeError(f"Claude returned invalid JSON: {exc}") from exc


def stream_text(
    contents: list[dict[str, Any]], *, system: str | None = None, client: Any | None = None
) -> Iterator[str]:
    """Yield text deltas; thinking blocks are not part of ``text_stream``."""
    active = client or get_client()
    kwargs: dict[str, Any] = {
        "model": get_settings().claude_model,
        "max_tokens": _MAX_TOKENS,
        "messages": [{"role": "user", "content": contents}],
    }
    if system:
        kwargs["system"] = system
    with active.messages.stream(**kwargs) as stream:
        for text in stream.text_stream:
            if text:
                yield text


def reset_client() -> None:
    """Drop the cached client (tests)."""
    global _client
    with _client_lock:
        _client = None
