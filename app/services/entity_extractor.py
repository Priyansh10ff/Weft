"""Typed entity extraction for text evidence (speech, page text, records).

Vision already returns entities for images, frames and page renders; this
module covers what was *said* or *written*, so a concept explained aloud and
the same concept drawn on a slide end up linked to the same entity.

Gemini does the extraction in batches.  Without an API key (or if the call
fails) a conservative pattern-based extractor is used instead, and the
result is marked so its mentions carry lower confidence.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.config import get_settings
from app.services import gemini
from app.services.image_processor import ENTITY_TYPES

_log = logging.getLogger(__name__)

_MAX_CHARS_PER_ITEM = 2000

_SYSTEM = f"""You extract named entities from short evidence snippets for a
knowledge graph. For each snippet return the specific people, organizations,
systems, services, components, metrics, products, locations, events and key
technical concepts it mentions. Use the canonical surface form (e.g.
"payments API", "p99 latency", "circuit breaker"). Skip generic words
("the team", "the issue", "this slide"). Types must be one of {list(ENTITY_TYPES)}.
Return JSON: {{"items": [{{"i": <snippet index>, "entities": [{{"name": str, "type": str}}]}}]}}"""

Entity = tuple[str, str]  # (name, type)


# --------------------------------------------------------------------- fallback

_STOP_LEADS = {
    "a", "an", "and", "as", "at", "but", "for", "here", "here's", "i", "if", "in", "it", "so",
    "the", "then", "this", "that", "these", "those", "we", "our", "you", "your", "next", "now",
    "and", "or", "of", "on", "to", "with", "what", "when", "why", "how", "after", "before",
}
_STOP_TRAILS = _STOP_LEADS | {"page", "section", "slide", "figure", "table", "summary"}
_ACRONYM_STOP = {"I", "OK", "AM", "PM", "TV", "US", "UK", "OR", "AND", "THE", "A"}

_SLUG = re.compile(r"\b[a-z][a-z0-9]+(?:[-_][a-z0-9]+)+\b")
_CAMEL = re.compile(r"\b[A-Z][a-z]+[A-Z][A-Za-z0-9]*\b")
_ACRONYM = re.compile(r"\b[A-Z][A-Z0-9]{1,6}s?\b")
_METRIC = re.compile(r"\b(?:p\d{2,3}(?:\s+latency)?|error rate|latency|throughput|uptime|completion rate)\b", re.I)
_TITLE_PHRASE = re.compile(r"\b(?:[A-Z][a-z0-9]+)(?:[ \t]+(?:[A-Z][a-z0-9]+|[A-Z]{2,})){1,3}\b")
_TICKET = re.compile(r"(?:ticket|incident|issue)\s*#\s*\d+", re.I)


def heuristic_entities(text: str, limit: int = 15) -> list[Entity]:
    """Cheap, high-precision patterns; recall is deliberately modest."""
    found: dict[str, str] = {}

    def add(name: str, kind: str) -> None:
        name = name.strip(" .,:;")
        key = name.casefold()
        if len(name) < 2 or key in (k.casefold() for k in found):
            return
        found[name] = kind

    for match in _SLUG.finditer(text):
        add(match.group(0), "system")
    for match in _CAMEL.finditer(text):
        add(match.group(0), "product")
    for match in _METRIC.finditer(text):
        add(match.group(0).lower(), "metric")
    for match in _TICKET.finditer(text):
        add(match.group(0), "event")
    for match in _TITLE_PHRASE.finditer(text):
        words = match.group(0).split()
        while words and words[0].casefold() in _STOP_LEADS:
            words = words[1:]
        while words and words[-1].casefold() in _STOP_TRAILS:
            words = words[:-1]
        if len(words) >= 2:
            add(" ".join(words), "concept")
    for match in _ACRONYM.finditer(text):
        token = match.group(0)
        if token not in _ACRONYM_STOP:
            add(token, "concept")
    return list(found.items())[:limit]


# -------------------------------------------------------------------------- llm


def _llm_batch(texts: list[str], client: Any | None) -> list[list[Entity]]:
    items = [{"i": i, "text": t[:_MAX_CHARS_PER_ITEM]} for i, t in enumerate(texts)]
    import json

    raw = gemini.generate_json(
        [f"Snippets:\n{json.dumps(items, ensure_ascii=False)}"], system=_SYSTEM, client=client
    )
    result: list[list[Entity]] = [[] for _ in texts]
    for item in (raw or {}).get("items", []) if isinstance(raw, dict) else []:
        try:
            index = int(item.get("i"))
        except (TypeError, ValueError):
            continue
        if not 0 <= index < len(texts):
            continue
        seen: set[str] = set()
        for entity in item.get("entities") or []:
            if not isinstance(entity, dict):
                continue
            name = str(entity.get("name") or "").strip()
            kind = str(entity.get("type") or "concept").strip().lower()
            if not name or name.casefold() in seen:
                continue
            seen.add(name.casefold())
            result[index].append((name, kind if kind in ENTITY_TYPES else "concept"))
    return result


def extract_entities(
    texts: list[str], *, client: Any | None = None
) -> tuple[list[list[Entity]], str]:
    """Return per-text entities and the method used (``"llm"`` or ``"heuristic"``)."""
    settings = get_settings()
    if not texts:
        return [], "none"
    if client is not None or settings.gemini_enabled:
        try:
            out: list[list[Entity]] = []
            size = settings.entity_batch_size
            for start in range(0, len(texts), size):
                out.extend(_llm_batch(texts[start : start + size], client))
            return out, "llm"
        except gemini.GeminiError as exc:
            _log.warning("LLM entity extraction failed, using heuristics: %s", exc)
    return [heuristic_entities(t) for t in texts], "heuristic"
