"""Seed a deliberately cross-modal knowledge base for demoing /query.

This does NOT call Whisper/Gemini and needs no API keys -- it feeds
`KnowledgeNode` records through the same ingestion path the upload API uses
(SQLite records + Chroma index), so `uvicorn main:app` (or
`test_pipeline.py`) can query them immediately.

The scenario is designed so that no single modality alone answers the demo
question -- the evidence is deliberately split:

  - video transcript (spoken) names the failure mode but not the fix
  - a video frame's visual_summary shows the retry/backoff diagram that IS
    the fix, at a different timestamp than the transcript segment
  - a PDF page gives the numeric backoff policy (base delay, max retries)
  - a screenshot image shows the resulting dashboard graph after the fix
    shipped, with OCR text of the annotation

Demo question: "How did the team fix the checkout service's timeout
failures, and what does the fix look like in production?"

A text-only / single-modality system can find *one* of these fragments.
Answering it fully requires combining the spoken explanation (video audio),
the diagram (video frame / visual_summary), the numeric policy (PDF page
text), and the after-the-fact proof (image OCR + visual_summary) -- i.e.
genuine cross-modal retrieval, not just OCR-to-text-to-vector-search.

This scenario mirrors the real sample files bundled under
``test_data/samples/`` (which you can upload through the actual API — see
``test_data/upload_samples.py``); this script instead ingests equivalent
pre-extracted knowledge so the demo works instantly, with no API keys and no
running server.

Run:
    python test_data/seed_cross_modal_demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from uuid import NAMESPACE_URL, uuid5  # noqa: E402

from app.db.repository import KnowledgeRepository, get_repository  # noqa: E402
from app.schemas.knowledge import KnowledgeNode, MediaModality, SourceAsset  # noqa: E402
from app.services.ingestion import IngestResult, delete_source, ingest_nodes, locator_from_node  # noqa: E402
from app.services.vector_store import VectorStore  # noqa: E402


def build_demo_nodes() -> list[KnowledgeNode]:
    return [
        # --- Video: the incident review, spoken explanation ---
        KnowledgeNode(
            transcript=(
                "So the checkout service kept timing out under load because every "
                "downstream call to the payments API retried immediately with no "
                "backoff, which just hammered the payments API harder while it was "
                "already struggling."
            ),
            modality=MediaModality.VIDEO,
            timestamp="00:00 - 00:12",
            source="incident-review.mp4",
            frame_path=None,
            entities=["checkout service", "payments API", "timeout"],
            provenance={"kind": "video_transcript_segment"},
        ),
        # --- Video: a later frame showing the fix diagram on the slide ---
        KnowledgeNode(
            transcript="And here's the diagram of what we changed.",
            visual_summary=(
                "A slide titled 'Exponential backoff + jitter' showing the checkout "
                "service calling the payments API, with a retry loop annotated: "
                "base delay 200ms, multiplier 2x, max 5 retries, random jitter added "
                "to each wait. A circuit breaker box sits after the retry loop."
            ),
            modality=MediaModality.VIDEO,
            timestamp="00:12 - 00:25",
            source="incident-review.mp4",
            frame_path=None,
            entities=["exponential backoff", "jitter", "circuit breaker", "checkout service", "payments API"],
            provenance={"kind": "video_frame_summary"},
        ),
        # --- PDF: the written postmortem with the exact numeric policy ---
        KnowledgeNode(
            content=(
                "Remediation: checkout-service now wraps payments-api calls in an "
                "exponential backoff retry policy: base_delay_ms=200, "
                "multiplier=2.0, max_retries=5, jitter=full. A circuit breaker "
                "trips after 10 consecutive failures within 30s and opens for 60s."
            ),
            transcript=(
                "Remediation: checkout-service now wraps payments-api calls in an "
                "exponential backoff retry policy: base_delay_ms=200, "
                "multiplier=2.0, max_retries=5, jitter=full. A circuit breaker "
                "trips after 10 consecutive failures within 30s and opens for 60s."
            ),
            visual_summary=(
                "Page 3 of the postmortem PDF, section 'Remediation', containing a "
                "small table of the retry policy parameters next to a sequence "
                "diagram of checkout-service, the retry wrapper, and payments-api."
            ),
            modality=MediaModality.PDF,
            timestamp="Page 3",
            source="checkout-timeout-postmortem.pdf",
            frame_path=None,
            entities=["exponential backoff", "circuit breaker", "checkout-service", "payments API", "jitter"],
            provenance={"kind": "pdf_page"},
        ),
        # --- Image: the dashboard screenshot proving it worked, with OCR ---
        KnowledgeNode(
            content="p99 latency (checkout->payments) | before: 4200ms | after: 310ms",
            transcript="p99 latency (checkout->payments) | before: 4200ms | after: 310ms",
            visual_summary=(
                "A Grafana dashboard screenshot showing the checkout-to-payments "
                "p99 latency graph dropping sharply at the deploy marker, with an "
                "annotation 'backoff+jitter deployed' at the drop point, and error "
                "rate falling from 18% to under 1%."
            ),
            modality=MediaModality.IMAGE,
            source="grafana-after-fix.png",
            frame_path=None,
            attributes={
                "image_type": "screenshot",
                "ocr_blocks": [
                    {"text": "Grafana Dashboard: p99 latency (checkout->payments)", "box": {"x": 0.264, "y": 0.078, "width": 0.306, "height": 0.03}},
                    {"text": "before: 4200ms  after: 310ms  error rate 18% -> 0.8%", "box": {"x": 0.266, "y": 0.138, "width": 0.289, "height": 0.026}},
                    {"text": "base_delay=200ms x2 max=5", "box": {"x": 0.354, "y": 0.3, "width": 0.178, "height": 0.028}},
                    {"text": "Checkout", "box": {"x": 0.046, "y": 0.44, "width": 0.198, "height": 0.116}},
                    {"text": "Retry+Jitter", "box": {"x": 0.356, "y": 0.44, "width": 0.198, "height": 0.116}},
                    {"text": "Payments API", "box": {"x": 0.666, "y": 0.44, "width": 0.241, "height": 0.116}},
                ],
                "regions": [
                    {"label": "p99 latency panel", "description": "Latency graph for checkout -> payments", "box": {"x": 0.264, "y": 0.196, "width": 0.639, "height": 0.07}},
                    {"label": "Retry path", "description": "Checkout -> Retry+Jitter -> Payments API", "box": {"x": 0.031, "y": 0.436, "width": 0.894, "height": 0.13}},
                ],
            },
            entities=["Grafana", "p99 latency", "checkout-service", "payments API", "jitter"],
            provenance={"kind": "standalone_image"},
        ),
        # --- Distractor from an unrelated source, so retrieval must actually
        #     discriminate rather than just returning "everything recent". ---
        KnowledgeNode(
            transcript=(
                "Next up in the all-hands: the design team walked through the new "
                "onboarding illustrations for the mobile app."
            ),
            modality=MediaModality.VIDEO,
            timestamp="15:00 - 15:20",
            source="all-hands-q3.mp4",
            frame_path=None,
            entities=["onboarding", "illustrations"],
            provenance={"kind": "video_transcript_segment"},
        ),
        # --- JSON: structured support tickets confirming customer impact,
        #     mirroring the /upload/json input modality. ---
        KnowledgeNode(
            content=(
                "Ticket #4471: Customer reported checkout failing repeatedly with a "
                "generic timeout error during the incident window, before the "
                "backoff/circuit-breaker fix shipped."
            ),
            transcript=(
                "Ticket #4471: Customer reported checkout failing repeatedly with a "
                "generic timeout error during the incident window, before the "
                "backoff/circuit-breaker fix shipped."
            ),
            modality=MediaModality.JSON,
            timestamp="ticket-4471",
            source="support-tickets.json",
            entities=["checkout service", "timeout", "circuit breaker"],
            provenance={"kind": "json_record"},
        ),
        KnowledgeNode(
            content=(
                "Ticket #4502: Customer confirms checkout now completes normally "
                "after the latest deploy; no more timeout errors during checkout."
            ),
            transcript=(
                "Ticket #4502: Customer confirms checkout now completes normally "
                "after the latest deploy; no more timeout errors during checkout."
            ),
            modality=MediaModality.JSON,
            timestamp="ticket-4502",
            source="support-tickets.json",
            entities=["checkout service", "resolved"],
            provenance={"kind": "json_record"},
        ),
        # ==============================================================
        # Scenario 2: onboarding permission-flow redesign. Mirrors
        # test_data/samples/onboarding-walkthrough.mp4,
        # onboarding-ab-results.png, onboarding-design-spec.pdf, and
        # user-feedback-survey.json. Query:
        #   "Why did we redesign the onboarding permissions flow, and did
        #    it actually improve completion rate?"
        # ==============================================================
        KnowledgeNode(
            transcript=(
                "Our funnel data showed most users dropped off on the permissions "
                "screen because we asked for location, notifications, and contacts "
                "all at once."
            ),
            modality=MediaModality.VIDEO,
            timestamp="00:00 - 00:11",
            source="onboarding-walkthrough.mp4",
            frame_path=None,
            entities=["onboarding", "permissions", "funnel"],
            provenance={"kind": "video_transcript_segment"},
        ),
        KnowledgeNode(
            transcript="Here's the new flow we shipped.",
            visual_summary=(
                "A slide titled 'Fix: Split Permission Screens' showing three "
                "separate screens: 1) Location with a reason shown first, "
                "2) Notifications with a reason shown first, 3) Contacts access "
                "deferred until the user taps 'Invite a friend'."
            ),
            modality=MediaModality.VIDEO,
            timestamp="00:11 - 00:26",
            source="onboarding-walkthrough.mp4",
            frame_path=None,
            entities=["permission screens", "deferred contacts access", "onboarding", "permissions"],
            provenance={"kind": "video_frame_summary"},
        ),
        KnowledgeNode(
            content=(
                "Success Metrics: raise permission-screen completion rate from "
                "39% to 65%+, and reduce unnecessary contacts-permission prompts "
                "by at least 80%. Result: completion rate rose to 74%, and "
                "contacts permission requests dropped 90% versus the old flow."
            ),
            transcript=(
                "Success Metrics: raise permission-screen completion rate from "
                "39% to 65%+, and reduce unnecessary contacts-permission prompts "
                "by at least 80%. Result: completion rate rose to 74%, and "
                "contacts permission requests dropped 90% versus the old flow."
            ),
            visual_summary=(
                "Pages 3-4 of the design spec PDF, 'Success Metrics' and "
                "'Result', with the target completion rate next to the actual "
                "A/B test outcome."
            ),
            modality=MediaModality.PDF,
            timestamp="Page 3",
            source="onboarding-design-spec.pdf",
            frame_path=None,
            entities=["completion rate", "A/B test", "permissions", "contacts permission"],
            provenance={"kind": "pdf_page"},
        ),
        KnowledgeNode(
            content=(
                "Metric: permission screen completion rate. Before (single "
                "screen, 3 asks): completion = 39%. After (split screens, "
                "deferred contacts): completion = 74%. Contacts permission "
                "requests dropped 90% (now deferred)."
            ),
            transcript=(
                "Metric: permission screen completion rate. Before (single "
                "screen, 3 asks): completion = 39%. After (split screens, "
                "deferred contacts): completion = 74%. Contacts permission "
                "requests dropped 90% (now deferred)."
            ),
            visual_summary=(
                "A results screenshot showing an A/B test comparison table: "
                "'Before' (single combined permission screen, 39% completion) "
                "versus 'After' (split screens with deferred contacts access, "
                "74% completion)."
            ),
            modality=MediaModality.IMAGE,
            source="onboarding-ab-results.png",
            frame_path=None,
            attributes={
                "image_type": "table",
                "ocr_blocks": [
                    {"text": "Onboarding A/B Test Results", "box": {"x": 0.042, "y": 0.06, "width": 0.14, "height": 0.028}},
                    {"text": "Metric: permission screen completion rate", "box": {"x": 0.042, "y": 0.16, "width": 0.203, "height": 0.028}},
                    {"text": "Before (single screen, 3 asks):", "box": {"x": 0.0625, "y": 0.3, "width": 0.142, "height": 0.028}},
                    {"text": "completion = 39%", "box": {"x": 0.0625, "y": 0.36, "width": 0.088, "height": 0.028}},
                    {"text": "After (split screens, deferred contacts):", "box": {"x": 0.542, "y": 0.3, "width": 0.182, "height": 0.028}},
                    {"text": "completion = 74%", "box": {"x": 0.542, "y": 0.36, "width": 0.088, "height": 0.028}},
                    {"text": "Contacts permission requests dropped 90% (now deferred)", "box": {"x": 0.042, "y": 0.598, "width": 0.28, "height": 0.03}},
                ],
                "regions": [
                    {"label": "Before", "description": "Single combined permission screen", "box": {"x": 0.042, "y": 0.26, "width": 0.4375, "height": 0.26}},
                    {"label": "After", "description": "Split screens with deferred contacts access", "box": {"x": 0.521, "y": 0.26, "width": 0.4375, "height": 0.26}},
                ],
            },
            entities=["A/B test", "completion rate", "contacts permission", "permissions"],
            provenance={"kind": "standalone_image"},
        ),
        KnowledgeNode(
            content=(
                "Survey response #244: liked that it explained why it needed "
                "location before asking; didn't feel like the app was asking "
                "for everything at once anymore. Collected after the redesign "
                "shipped."
            ),
            transcript=(
                "Survey response #244: liked that it explained why it needed "
                "location before asking; didn't feel like the app was asking "
                "for everything at once anymore. Collected after the redesign "
                "shipped."
            ),
            modality=MediaModality.JSON,
            timestamp="response-244",
            source="user-feedback-survey.json",
            entities=["onboarding", "permissions", "positive"],
            provenance={"kind": "json_record"},
        ),
    ]


SAMPLES = Path(__file__).resolve().parent / "samples"

# When each sample "happened", so entity timelines read like a real project:
# the incident (March) and the onboarding redesign (March-April).
DEMO_RECORDED_AT = {
    "support-tickets.json": "2026-03-09T10:00:00+00:00",
    "incident-review.mp4": "2026-03-11T15:00:00+00:00",
    "checkout-timeout-postmortem.pdf": "2026-03-12T09:30:00+00:00",
    "grafana-after-fix.png": "2026-03-14T18:20:00+00:00",
    "all-hands-q3.mp4": "2026-03-20T16:00:00+00:00",
    "onboarding-design-spec.pdf": "2026-03-28T11:00:00+00:00",
    "onboarding-walkthrough.mp4": "2026-04-02T14:00:00+00:00",
    "onboarding-ab-results.png": "2026-04-20T09:00:00+00:00",
    "user-feedback-survey.json": "2026-04-25T12:00:00+00:00",
}

# The slide images baked into each sample video, in order of appearance.
VIDEO_FRAMES = {
    "incident-review.mp4": ["frame_problem.png", "frame_fix_diagram.png"],
    "onboarding-walkthrough.mp4": ["frame_onboarding_problem.png", "frame_onboarding_fix.png"],
}


def _render_pdf_page(pdf: Path, page_number: int, out_dir: Path) -> Path | None:
    try:
        import pymupdf as fitz  # type: ignore[import-untyped]
    except ImportError:
        try:
            import fitz  # type: ignore[import-untyped,no-redef]
        except ImportError:
            return None
    try:
        with fitz.open(str(pdf)) as doc:
            if not 1 <= page_number <= doc.page_count:
                return None
            target = out_dir / f"page_{page_number:04d}.jpg"
            doc[page_number - 1].get_pixmap(dpi=110).save(str(target))
            return target
    except Exception:
        return None


def _attach_sample_media(
    source_id: str, filename: str, modality: MediaModality, nodes: list[KnowledgeNode]
) -> str | None:
    """Copy the real sample file (and its frames/pages) into storage.

    Gives the demo playable media and real thumbnails. Returns the storage
    key of the original file, or ``None`` when no sample file exists.
    """
    import shutil

    from app.services.storage import derivative_directory, derived_url, storage_root

    sample = SAMPLES / filename
    storage_path = None
    if sample.is_file():
        upload_dir = storage_root() / "uploads" / source_id
        upload_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(sample, upload_dir / filename)
        storage_path = f"uploads/{source_id}/{filename}"

    if modality is MediaModality.VIDEO:
        frames = VIDEO_FRAMES.get(filename, [])
        out = derivative_directory(source_id, "frames")
        for node, frame_name in zip(nodes, frames):
            if (SAMPLES / frame_name).is_file():
                shutil.copyfile(SAMPLES / frame_name, out / frame_name)
                node.frame_path = derived_url(out / frame_name)
    elif modality is MediaModality.IMAGE and storage_path:
        for node in nodes:
            node.frame_path = "/" + storage_path
    elif modality is MediaModality.PDF and sample.is_file():
        out = derivative_directory(source_id, "pages")
        for node in nodes:
            page = locator_from_node(node).page_number
            rendered = _render_pdf_page(sample, page, out) if page else None
            if rendered is not None:
                node.frame_path = derived_url(rendered)
    return storage_path


def seed_demo(
    repository: KnowledgeRepository | None = None,
    vector_store: VectorStore | None = None,
) -> list[IngestResult]:
    """Ingest the demo nodes as one source per (file, modality).

    Source IDs are deterministic, so re-seeding replaces the previous demo
    data instead of duplicating it.
    """
    groups: dict[tuple[str, MediaModality], list[KnowledgeNode]] = {}
    for node in build_demo_nodes():
        groups.setdefault((node.source or "unknown", MediaModality(node.modality)), []).append(node)

    repo = repository or get_repository()
    results = []
    for (filename, modality), nodes in groups.items():
        source_id = uuid5(NAMESPACE_URL, f"gradient-rush-demo/{modality.value}/{filename}")
        if repo.get_source(str(source_id)) is not None:
            delete_source(str(source_id), repository=repo, vector_store=vector_store)
        asset = SourceAsset(source_id=source_id, filename=filename, modality=modality)
        storage_path = _attach_sample_media(str(source_id), filename, modality, nodes)
        result = ingest_nodes(
            asset,
            nodes,
            source_attributes={"demo_seed": True, "recorded_at": DEMO_RECORDED_AT.get(filename)},
            repository=repo,
            vector_store=vector_store,
        )
        if storage_path:
            # No content hash on purpose: uploading the real sample later must
            # run the real pipeline instead of being deduplicated against
            # this hand-written demo knowledge.
            result.source.storage_path = storage_path
            result.source.size_bytes = (SAMPLES / filename).stat().st_size
            repo.upsert_source(result.source)
        results.append(result)
    return results


def main() -> None:
    results = seed_demo()
    segments = sum(len(r.segments) for r in results)
    relations = sum(r.relation_count for r in results)
    print(
        f"Seeded {len(results)} sources / {segments} segments / {relations} relations "
        "into the knowledge store."
    )
    print(
        "Try: curl -X POST http://127.0.0.1:8000/query -H 'Content-Type: application/json' "
        "-d '{\"query\": \"How did the team fix the checkout timeout issue and how do we "
        "know it worked?\", \"limit\": 5}'"
    )


if __name__ == "__main__":
    main()
