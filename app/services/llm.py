"""One door to the language model, whichever provider is configured.

Weft runs on exactly one provider at a time, chosen by ``LLM_PROVIDER``
(``auto``, ``gemini`` or ``claude``; see ``Settings.llm_provider``). Callers
build provider-neutral content (strings, :class:`Image`, :class:`MediaFile`)
and call :func:`generate_json` or :func:`stream_text`; this module converts
the content and routes the call to ``gemini.py`` or ``claude.py``.

A client passed explicitly (tests, or a caller reusing one) decides the
backend by its shape: an Anthropic client has ``.messages``, a Gemini client
has ``.models``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from app.config import get_settings


class LLMError(RuntimeError):
    """The model is unavailable, misconfigured, or returned unusable output."""


class LLMNotConfigured(LLMError):
    """No provider key; callers should fall back to an offline path."""


@dataclass(frozen=True)
class Image:
    """Inline image bytes (PNG, JPEG or WebP)."""

    data: bytes
    mime_type: str


@dataclass(frozen=True)
class MediaFile:
    """An audio or video file. Only Gemini accepts these."""

    path: Path


NAMES = {"gemini": "Gemini", "claude": "Claude"}


def provider() -> str | None:
    """The active provider, or ``None`` when no key is configured."""
    return get_settings().llm_provider


def display_name(name: str | None = None) -> str | None:
    return NAMES.get(name or provider() or "")


def supports_media(name: str | None = None) -> bool:
    """Whether the provider can listen to audio/video (speaker attribution)."""
    return (name or provider()) == "gemini"


def _backend(client: Any | None) -> Any:
    if client is not None:
        name = "claude" if hasattr(client, "messages") else "gemini"
    else:
        name = provider()
    if name is None:
        raise LLMNotConfigured("No language model is configured (set GEMINI_API_KEY or ANTHROPIC_API_KEY).")
    if name == "claude":
        from app.services import claude

        return claude
    from app.services import gemini

    return gemini


def get_client() -> Any:
    return _backend(None).get_client()


def generate_json(
    contents: list[Any],
    *,
    system: str | None = None,
    client: Any | None = None,
    attempts: int = 3,
) -> Any:
    """Ask for a JSON body; ``contents`` mixes strings, ``Image`` and ``MediaFile``."""
    backend = _backend(client)
    active = client or backend.get_client()
    return backend.generate_json(backend.convert(contents, client=active), system=system, client=active, attempts=attempts)


def stream_text(contents: list[Any], *, system: str | None = None, client: Any | None = None) -> Iterator[str]:
    """Yield plain-text chunks as the model writes."""
    backend = _backend(client)
    active = client or backend.get_client()
    yield from backend.stream_text(backend.convert(contents, client=active), system=system, client=active)


def reset_clients() -> None:
    from app.services import claude, gemini

    gemini.reset_client()
    claude.reset_client()
