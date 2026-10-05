"""Non-destructive presentation-readiness checks for the Gradient-Rush stack."""

from __future__ import annotations

import ast
import importlib
import os
import gc
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4


ROOT = Path(__file__).resolve().parent


class CheckFailure(Exception):
    """Raised when one audit area does not meet its contract."""


class Audit:
    def __init__(self) -> None:
        self.results: list[tuple[str, str, str]] = []

    def run(self, name: str, check: Callable[[], str]) -> None:
        try:
            detail = check()
        except Exception as exc:  # Keep the report complete if one area fails.
            self.results.append((name, "FAIL", f"{type(exc).__name__}: {exc}"))
        else:
            self.results.append((name, "PASS", detail))

    def passed(self) -> bool:
        return all(status == "PASS" for _, status, _ in self.results)


def check_environment() -> str:
    packages = {
        "google-generativeai": "google.generativeai",
        "groq": "groq",
        "chromadb": "chromadb",
        "fastapi": "fastapi",
        "streamlit": "streamlit",
        "cv2": "cv2",
        "pypdf": "pypdf",
    }
    imported: list[str] = []
    for display_name, module_name in packages.items():
        importlib.import_module(module_name)
        imported.append(display_name)

    completed = subprocess.run(
        [sys.executable, "-m", "compileall", "."],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise CheckFailure(completed.stdout + completed.stderr)
    return f"imports: {', '.join(imported)}; compileall: ok"


def check_vector_store() -> str:
    from app.db.repository import KnowledgeRepository
    from app.schemas.knowledge import KnowledgeNode, MediaModality, SourceAsset, TemporalLocation
    from app.services import retrieval
    from app.services.ingestion import ingest_nodes
    from app.services.vector_store import VectorStore

    temporary_directory = Path(tempfile.mkdtemp(prefix="gradient-rush-store-"))
    repo = None
    store = None
    try:
        store = VectorStore(
            persistence_path=temporary_directory / "chroma",
            collection_name=f"audit_{uuid4().hex}",
        )
        repo = KnowledgeRepository(temporary_directory / "knowledge.db")
        node = KnowledgeNode(
            content="The database architecture uses a distributed SQL cluster.",
            transcript="The database architecture uses a distributed SQL cluster.",
            visual_summary="A diagram shows three database nodes connected in a cluster.",
            timestamp=TemporalLocation(start_seconds=12, end_seconds=20),
            modality=MediaModality.VIDEO,
            source="audit.mp4",
            frame_path="/frames/audit.jpg",
            entities=["SQL cluster"],
        )
        asset = SourceAsset(filename="audit.mp4", modality=MediaModality.VIDEO)
        ingest_nodes(asset, [node], repository=repo, vector_store=store)
        multimodal = retrieval.search(
            "database architecture", 1, vector_store=store, repository=repo
        )
        baseline = retrieval.search(
            "database architecture", 1, text_only=True, vector_store=store, repository=repo
        )
        if not multimodal or not baseline:
            raise CheckFailure("Chroma did not return both multimodal and baseline results")
        segment = multimodal[0]["segment"]
        if not segment.visual_summary:
            raise CheckFailure("visual summary was not retained in SQLite")
        if segment.locator.start_seconds != 12 or segment.locator.end_seconds != 20:
            raise CheckFailure(f"locator was not preserved: {segment.locator}")
        if multimodal[0]["metadata"]["entities"] != ["SQL cluster"]:
            raise CheckFailure("entity mention was not linked to the segment")
        if baseline[0]["transcript"] != node.transcript:
            raise CheckFailure("text-only baseline transcript was not retained")
        if baseline[0]["metadata"]["visual_summary"] is not None:
            raise CheckFailure("text-only baseline leaked visual evidence")
    finally:
        if repo is not None:
            repo.close()
        del store
        gc.collect()
        shutil.rmtree(temporary_directory, ignore_errors=True)
    return "SQLite records + Chroma index, hydration, entities, text-only baseline: ok"


def check_media_processors() -> str:
    from app.services.audio_processor import AudioProcessingError, process_audio
    from app.services.image_processor import ImageProcessingError, process_image
    from app.services.pdf_processor import PdfProcessingError, process_pdf
    from app.services.video_processor import MediaProcessingError, process_video

    missing_path = ROOT / "data" / "audit-file-that-does-not-exist"
    expected_errors = (
        (process_audio, (str(missing_path),), AudioProcessingError),
        (process_image, (str(missing_path),), ImageProcessingError),
        (process_pdf, (str(missing_path),), PdfProcessingError),
        (process_video, (str(missing_path),), MediaProcessingError),
    )
    for processor, arguments, expected_error in expected_errors:
        try:
            processor(*arguments)
        except expected_error:
            pass
        else:
            raise CheckFailure(f"{processor.__name__} did not reject a missing file")

    with tempfile.TemporaryDirectory(prefix="gradient-rush-media-") as temporary_directory:
        empty_path = Path(temporary_directory) / "empty.bin"
        empty_path.write_bytes(b"")
        empty_checks = (
            (process_audio, (str(empty_path),), AudioProcessingError),
            (process_image, (str(empty_path),), ImageProcessingError),
            (process_pdf, (str(empty_path),), PdfProcessingError),
            (process_video, (str(empty_path),), MediaProcessingError),
        )
        for processor, arguments, expected_error in empty_checks:
            try:
                processor(*arguments)
            except expected_error:
                pass
            except Exception as exc:
                raise CheckFailure(
                    f"{processor.__name__} leaked {type(exc).__name__} for empty input"
                ) from exc
            else:
                raise CheckFailure(f"{processor.__name__} accepted empty input")

    import app.services.audio_processor as audio_module

    fake_segments = types.SimpleNamespace(
        start=1.0,
        end=2.5,
        text="Local fallback transcript",
    )

    class FakeWhisperModel:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def transcribe(self, *args: Any, **kwargs: Any) -> tuple[list[Any], None]:
            return [fake_segments], None

    original_module = sys.modules.get("faster_whisper")
    sys.modules["faster_whisper"] = types.SimpleNamespace(WhisperModel=FakeWhisperModel)
    original_key = os.environ.pop("GROQ_API_KEY", None)
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav") as audio_file:
            audio_file.write(b"not-real-audio")
            audio_file.flush()
            fallback = audio_module.process_audio(audio_file.name)
    finally:
        if original_key is not None:
            os.environ["GROQ_API_KEY"] = original_key
        if original_module is None:
            sys.modules.pop("faster_whisper", None)
        else:
            sys.modules["faster_whisper"] = original_module
    if fallback[0]["text"] != "Local fallback transcript":
        raise CheckFailure("faster-whisper fallback did not return a transcript")
    return "missing/empty media handling and local ASR fallback: ok"


def check_fastapi_contracts() -> str:
    from main import app

    routes = {path for path in app.openapi().get("paths", {})}
    required_routes = {
        "/upload/video",
        "/upload/audio",
        "/upload/image",
        "/upload/pdf",
        "/query",
        "/query/compare",
        "/health",
    }
    missing_routes = required_routes - routes
    if missing_routes:
        raise CheckFailure(f"missing routes: {sorted(missing_routes)}")
    frames_mounts = [route for route in app.routes if getattr(route, "path", None) == "/frames"]
    if not frames_mounts:
        raise CheckFailure("/frames static mount is not registered")
    frames_directory = ROOT / "data" / "frames"
    if not frames_directory.is_dir():
        raise CheckFailure("data/frames directory does not exist")
    return f"{len(required_routes)} required routes and /frames mount: ok"


def check_frontend_contract() -> str:
    frontend_path = ROOT / "frontend.py"
    source = frontend_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(frontend_path))
    constants = {
        node.targets[0].id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    if constants.get("BACKEND_URL") != "http://localhost:8000":
        raise CheckFailure("frontend backend URL is not http://localhost:8000")
    for endpoint in ("/upload/video", "/upload/pdf", "/upload/image", "/query/compare"):
        if endpoint not in source:
            raise CheckFailure(f"frontend does not target {endpoint}")
    if '"file": (' not in source or 'json={"query": query, "limit": 3}' not in source:
        raise CheckFailure("frontend request payloads do not match backend contracts")
    from app.schemas.knowledge import KnowledgeQueryRequest

    request = KnowledgeQueryRequest(query="database architecture", limit=3)
    if request.model_dump() != {"query": "database architecture", "limit": 3}:
        raise CheckFailure("query payload failed Pydantic schema validation")
    return "backend URL, upload multipart field, and compare JSON payload: ok"


def print_report(audit: Audit) -> None:
    print("\nGradient-Rush pipeline self-test")
    print("=" * 86)
    print(f"{'Component':<38} {'Status':<8} Details")
    print("-" * 86)
    for component, status, details in audit.results:
        print(f"{component:<38} {status:<8} {details}")
    print("=" * 86)
    print("Overall: PASS" if audit.passed() else "Overall: FAIL")


def main() -> int:
    audit = Audit()
    audit.run("1. Environment & imports", check_environment)
    audit.run("2. Database & schema", check_vector_store)
    audit.run("3. Media processors", check_media_processors)
    audit.run("4. FastAPI routes & app", check_fastapi_contracts)
    audit.run("5. Frontend integration", check_frontend_contract)
    print_report(audit)
    return 0 if audit.passed() else 1


if __name__ == "__main__":
    raise SystemExit(main())
