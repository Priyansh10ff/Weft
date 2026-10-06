"""Grounded answer synthesis over multimodal retrieval evidence.

The retrieval endpoint alone (embed query -> nearest neighbours) proves
*search* works. It does not prove the system actually *connects* evidence
across modalities to answer a question. This module closes that gap: it
takes the top-k ``KnowledgeNode`` hits -- which may span video transcript,
video frame descriptions, image OCR/visual summaries, and PDF page text/
diagram summaries -- and asks an LLM to synthesize one grounded answer that
explicitly cites which modality and locator each piece of evidence came
from, and to say plainly when the evidence does not support an answer.

If no LLM key is configured (offline grading / CI), ``synthesize_answer``
falls back to a deterministic extractive summary so the endpoint never
breaks, while still surfacing per-source evidence in ``answer.sources``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any


class AnswerSynthesisError(RuntimeError):
    """Raised only for truly unexpected failures; callers should prefer the
    deterministic fallback over raising, so this is rarely used."""


@dataclass
class SynthesizedAnswer:
    answer: str
    grounded: bool
    sources: list[dict[str, Any]] = field(default_factory=list)
    method: str = "llm"


def _fmt_seconds(value: Any) -> str:
    try:
        total = max(0, int(float(value)))
    except (TypeError, ValueError):
        return "?"
    return f"{total // 60:02d}:{total % 60:02d}"


def _evidence_block(hits: list[dict[str, Any]]) -> str:
    """Render each hit as a numbered, modality-tagged evidence card.

    Besides text and visuals, a card carries who spoke, the exact sentence
    that matched, the OCR text that matched, and, for evidence pulled in
    through the graph, which card it is linked to and why.
    """
    index_of = {hit.get("id"): i for i, hit in enumerate(hits, start=1)}
    lines = []
    for i, hit in enumerate(hits, start=1):
        metadata = hit.get("metadata") or {}
        modality = metadata.get("modality") or "unknown"
        source = hit.get("source") or metadata.get("source") or "unknown source"
        locator = hit.get("timestamp") or metadata.get("timestamp") or "n/a"
        transcript = (hit.get("transcript") or metadata.get("transcript") or "").strip()
        visual = (metadata.get("visual_summary") or "").strip()
        parts = [f"[Evidence {i}] modality={modality} source={source} locator={locator}"]
        if metadata.get("speakers"):
            parts.append(f"  speakers: {', '.join(metadata['speakers'])}")
        if transcript:
            parts.append(f"  text/transcript: {transcript[:1500]}")
        if visual:
            parts.append(f"  visual_summary: {visual[:1200]}")
        span = hit.get("matched_span")
        if span and span.get("text"):
            who = f" ({span['speaker']})" if span.get("speaker") else ""
            parts.append(
                f"  best matching moment: {_fmt_seconds(span.get('start_seconds'))}"
                f"-{_fmt_seconds(span.get('end_seconds'))}{who}: {span['text']}"
            )
        regions = hit.get("matched_regions") or []
        if regions:
            parts.append("  matching text on screen/page: " + " | ".join(r["text"] for r in regions))
        via = hit.get("via")
        if via:
            linked = index_of.get(via.get("from_segment_id"))
            target = f"Evidence {linked}" if linked else "another card"
            shared = ", ".join(via.get("shared_entities") or [])
            parts.append(
                f"  linked to {target} by '{via.get('relation')}'" + (f" (shared: {shared})" if shared else "")
            )
        lines.append("\n".join(parts))
    return "\n\n".join(lines)


def _fallback_answer(query: str, hits: list[dict[str, Any]]) -> SynthesizedAnswer:
    """Deterministic, no-LLM extractive answer used when no API key is set.

    Still cross-modal: it stitches together transcript/text evidence and
    visual-summary evidence from the *same* top hits so the connection
    between modalities is visible even without generation.
    """
    if not hits:
        return SynthesizedAnswer(
            answer=(
                "No indexed evidence matched this query closely enough to answer. "
                "Try rephrasing, or upload source material covering this topic."
            ),
            grounded=False,
            sources=[],
            method="fallback-empty",
        )

    sentences: list[str] = []
    sources: list[dict[str, Any]] = []
    for i, hit in enumerate(hits, start=1):
        metadata = hit.get("metadata") or {}
        modality = metadata.get("modality") or "unknown"
        source = hit.get("source") or metadata.get("source") or "unknown source"
        locator = hit.get("timestamp") or metadata.get("timestamp") or "n/a"
        transcript = (hit.get("transcript") or metadata.get("transcript") or "").strip()
        visual = (metadata.get("visual_summary") or "").strip()

        span = hit.get("matched_span") or {}
        if span.get("text"):
            # Quote the sentence that matched, with its own timestamp.
            locator = f"{_fmt_seconds(span.get('start_seconds'))}"
            transcript = span["text"]
        speakers = metadata.get("speakers") or []
        fragment_parts = []
        if transcript:
            fragment_parts.append(transcript + (f" (said by {', '.join(speakers)})" if speakers else ""))
        if visual:
            fragment_parts.append(f"(shown visually: {visual})")
        if fragment_parts:
            sentences.append(
                f"According to {modality} evidence from '{source}' at {locator}: "
                + " ".join(fragment_parts)
            )
        sources.append(
            {
                "evidence_index": i,
                "cited": True,
                "modality": modality,
                "source": source,
                "locator": locator,
                "similarity_score": hit.get("similarity_score"),
            }
        )

    body = " ".join(sentences) if sentences else "Relevant items were found but contained no extractable text."
    answer = f"Based on {len(hits)} retrieved item(s) across modalities: {body}"
    return SynthesizedAnswer(answer=answer, grounded=bool(sentences), sources=sources, method="fallback-extractive")


def _gemini_client() -> Any | None:
    from app.services import gemini

    try:
        return gemini.get_client()
    except gemini.GeminiError:
        return None


_SYSTEM_PROMPT = (
    "You are the answer layer of a multimodal retrieval system. You are given a user "
    "question and a numbered list of evidence cards. Each card is tagged with its modality "
    "(video, image, pdf, audio), its source file, and a locator (timestamp or page). Some "
    "cards have spoken/OCR/page text ('text/transcript'); some have a description of what is "
    "visually shown ('visual_summary'); some have both.\n\n"
    "Your job: answer the question using ONLY the evidence given. Actively connect evidence "
    "across modalities when it strengthens the answer -- e.g. if the transcript from one card "
    "says a concept was explained and the visual_summary of a nearby card shows the diagram of "
    "that same concept, say so explicitly and cite both. Do not just restate one card in "
    "isolation if others corroborate or complete it.\n\n"
    "Cards may name speakers, the exact matching moment inside a recording, matching text "
    "on a screen or page, and links between cards (e.g. a diagram that 'depicts' what was "
    "said). Use these to answer who/when/where parts precisely: name the speaker, give the "
    "timestamp or page where something was shown.\n\n"
    "Rules:\n"
    "- Every factual claim must be traceable to at least one evidence card; cite cards inline "
    "like (Evidence 2).\n"
    "- If the evidence does not actually answer the question, say so plainly instead of "
    "guessing.\n"
    "- Be concise: 2-5 sentences.\n"
    "- Return strict JSON: {\"answer\": string, \"grounded\": boolean, "
    "\"cited_evidence\": [int, ...]}"
)


def synthesize_answer(
    query: str,
    hits: list[dict[str, Any]],
    *,
    client: Any | None = None,
    subqueries: list[str] | None = None,
) -> SynthesizedAnswer:
    """Produce one grounded, cross-modal answer from retrieval hits.

    Never raises for missing credentials or provider errors -- degrades to
    the deterministic extractive fallback so ``/query`` stays reliable.
    """
    if not hits:
        return _fallback_answer(query, hits)

    active_client = client or _gemini_client()
    if active_client is None:
        return _fallback_answer(query, hits)

    evidence_text = _evidence_block(hits)
    parts = ""
    if subqueries:
        parts = "The question has these parts; answer each:\n" + "\n".join(f"- {q}" for q in subqueries) + "\n\n"
    prompt = (
        f"Question: {query}\n\n{parts}Evidence:\n{evidence_text}\n\n"
        "Respond with the JSON object described in your instructions."
    )

    try:
        from app.services import gemini

        parsed = gemini.generate_json([prompt], system=_SYSTEM_PROMPT, client=active_client)
        if not isinstance(parsed, dict):
            return _fallback_answer(query, hits)
        answer_text = str(parsed.get("answer", "")).strip()
        if not answer_text:
            return _fallback_answer(query, hits)
        cited = parsed.get("cited_evidence") or []
        cited_indices = {int(c) for c in cited if isinstance(c, (int, float, str)) and str(c).strip().lstrip("-").isdigit()}

        sources = []
        for i, hit in enumerate(hits, start=1):
            metadata = hit.get("metadata") or {}
            sources.append(
                {
                    "evidence_index": i,
                    "cited": i in cited_indices if cited_indices else True,
                    "modality": metadata.get("modality") or "unknown",
                    "source": hit.get("source") or metadata.get("source") or "unknown source",
                    "locator": hit.get("timestamp") or metadata.get("timestamp") or "n/a",
                    "similarity_score": hit.get("similarity_score"),
                }
            )

        return SynthesizedAnswer(
            answer=answer_text,
            grounded=bool(parsed.get("grounded", True)),
            sources=sources,
            method="llm",
        )
    except Exception:
        # Any provider/parse failure -> never break the query endpoint.
        return _fallback_answer(query, hits)
