"""Page-wise PDF extraction: exact text layer + vision analysis of each page.

Every page becomes one node carrying the PDF's own text (exact), and, when a
renderer is available, a description of the rendered page with OCR text
blocks and visual regions located by bounding box, so a diagram on page 3
can be traced to where on the page it is.  Page renders and embedded images
are written to this source's own derived directory.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.schemas.knowledge import KnowledgeNode, MediaModality
from app.services.image_processor import ImageProcessingError, analyze_image
from app.services.storage import derivative_directory, derived_url

_log = logging.getLogger(__name__)


class PdfProcessingError(RuntimeError):
    """Raised when a PDF cannot be opened or read page by page."""


def _persist_page_image(
    image: object, page_number: int, image_index: int, directory: Path
) -> Path | None:
    """Persist an embedded pypdf image into the source's derived directory."""
    data = getattr(image, "data", None)
    if not isinstance(data, bytes):
        return None
    suffix = Path(str(getattr(image, "name", ""))).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png"}:
        return None
    path = directory / f"page_{page_number:04d}_image_{image_index:03d}{suffix}"
    path.write_bytes(data)
    return path


def _render_page(fitz_page: Any, page_number: int, directory: Path) -> Path | None:
    """Render one page to JPEG with PyMuPDF; ``None`` if rendering fails."""
    try:
        pixmap = fitz_page.get_pixmap(dpi=150)
        path = directory / f"page_{page_number:04d}.jpg"
        pixmap.save(str(path))
        return path
    except Exception as exc:
        _log.warning("Could not render PDF page %d: %s", page_number, exc)
        return None


def _open_renderer(pdf_path: Path) -> Any:
    try:
        import pymupdf as fitz  # type: ignore[import-untyped]
    except ImportError:
        try:
            import fitz  # type: ignore[import-untyped,no-redef]
        except ImportError:
            return None
    try:
        return fitz.open(str(pdf_path))
    except Exception as exc:
        _log.warning("PyMuPDF failed to open %s, continuing without renders: %s", pdf_path.name, exc)
        return None


def process_pdf(
    file_path: str,
    *,
    output_dir: Path | None = None,
    client: Any | None = None,
    describe: Any = analyze_image,
) -> list[KnowledgeNode]:
    """Extract one KnowledgeNode per PDF page with text, visuals and regions."""
    pdf_path = Path(file_path)
    if not pdf_path.is_file():
        raise PdfProcessingError(f"PDF source does not exist: {pdf_path}")
    directory = output_dir or derivative_directory(f"adhoc-{uuid4()}", "pages")
    directory.mkdir(parents=True, exist_ok=True)

    try:
        from pypdf import PdfReader

        reader = PdfReader(str(pdf_path))
        pages = list(reader.pages)
    except ImportError as exc:
        raise PdfProcessingError("pypdf is required to process PDF files.") from exc
    except Exception as exc:
        raise PdfProcessingError(f"Unable to open PDF: {pdf_path.name}") from exc

    renderer = _open_renderer(pdf_path)
    nodes: list[KnowledgeNode] = []
    try:
        for page_number, page in enumerate(pages, start=1):
            try:
                page_text = (page.extract_text() or "").strip()
                embedded_images = list(getattr(page, "images", []))
            except Exception as exc:
                raise PdfProcessingError(f"Unable to extract PDF page {page_number}.") from exc

            analyses: list[dict[str, Any]] = []
            frame_path: Path | None = None
            failed = False

            # Preferred: analyze the whole rendered page (layout, charts, diagrams).
            if renderer is not None:
                rendered = _render_page(renderer[page_number - 1], page_number, directory)
                if rendered is not None:
                    frame_path = rendered
                    try:
                        analyses.append(describe(rendered, client=client))
                    except ImageProcessingError as exc:
                        _log.warning("Vision failed for page %d of %s: %s", page_number, pdf_path.name, exc)
                        failed = True

            # Fallback: analyze embedded images individually.
            if renderer is None:
                for image_index, image in enumerate(embedded_images, start=1):
                    extracted = _persist_page_image(image, page_number, image_index, directory)
                    if extracted is None:
                        continue
                    frame_path = frame_path or extracted
                    try:
                        analyses.append(describe(extracted, client=client))
                    except ImageProcessingError as exc:
                        _log.warning(
                            "Vision failed for image %d on page %d of %s: %s",
                            image_index, page_number, pdf_path.name, exc,
                        )
                        failed = True

            entities: list[str] = []
            entity_types: dict[str, str] = {}
            for analysis in analyses:
                for name in analysis.get("entities", []):
                    if name not in entity_types:
                        entities.append(name)
                        entity_types[name] = analysis.get("entity_types", {}).get(name, "concept")
            visual_summary = "\n".join(a["visual_summary"] for a in analyses if a.get("visual_summary"))

            attributes: dict[str, Any] = {
                "embedded_image_count": len(embedded_images),
                "has_text_layer": bool(page_text),
            }
            if analyses:
                first = analyses[0]
                attributes.update(
                    image_type=first.get("image_type"),
                    ocr_blocks=first.get("ocr_blocks") or [],
                    regions=first.get("regions") or [],
                )
                # Scanned pages have no text layer: fall back to vision OCR.
                if not page_text and first.get("ocr_text"):
                    page_text = first["ocr_text"]
                    attributes["text_from_ocr"] = True
            if failed:
                attributes["visual_extraction_failed"] = True

            nodes.append(
                KnowledgeNode(
                    content=page_text,
                    transcript=page_text or None,
                    visual_summary=visual_summary or None,
                    timestamp=f"Page {page_number}",
                    frame_path=derived_url(frame_path) if frame_path else None,
                    modality=MediaModality.PDF,
                    source=pdf_path.name,
                    entities=entities,
                    entity_types=entity_types,
                    provenance={"page_number": page_number},
                    attributes=attributes,
                )
            )
    finally:
        if renderer is not None:
            try:
                renderer.close()
            except Exception:
                pass
    return nodes
