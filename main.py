"""FastAPI entry point for the multimodal ingestion service."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.router import api_router
from app.services.storage import storage_root


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Resume background ingestion jobs a previous process left unfinished."""
    from app.services.jobs import get_job_manager

    manager = get_job_manager()
    try:
        resumed = manager.resume_interrupted()
        if resumed:
            logging.getLogger(__name__).info("Resumed %d interrupted ingestion job(s)", resumed)
    except Exception:  # a broken job row must not keep the API down
        logging.getLogger(__name__).exception("Could not resume interrupted jobs")
    yield
    manager.shutdown(wait=False)


app = FastAPI(
    title="Multimodal Data Management Pipeline",
    version="0.2.0",
    description="Turns video, audio, images, PDFs and JSON into structured, linked, retrievable knowledge.",
    lifespan=lifespan,
)

# The dashboard is static HTML/CSS/JS served same-origin, but CORS is kept
# open for local demo convenience (e.g. hitting the API from a notebook).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_frames_directory = Path.cwd() / "data" / "frames"
_frames_directory.mkdir(parents=True, exist_ok=True)
app.mount("/frames", StaticFiles(directory=str(_frames_directory)), name="frames")

# Standalone image uploads are persisted under data/uploads/{source_id}/...
# and referenced by the same relative URL scheme, so they need their own
# mount to be viewable in the dashboard.
_uploads_directory = Path.cwd() / "data" / "uploads"
_uploads_directory.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(_uploads_directory)), name="uploads")

# Per-source derived artifacts (video keyframes, PDF page renders) live under
# data/derived/{source_id}/..., so uploads never overwrite each other's frames.
_derived_directory = storage_root() / "derived"
_derived_directory.mkdir(parents=True, exist_ok=True)
app.mount("/derived", StaticFiles(directory=str(_derived_directory)), name="derived")

_static_directory = Path(__file__).parent / "static"
if _static_directory.is_dir():
    app.mount("/static", StaticFiles(directory=str(_static_directory)), name="static")

app.include_router(api_router)


@app.get("/", tags=["system"], include_in_schema=False)
async def dashboard() -> FileResponse:
    """Serve the terminal-brutalist dashboard as the app's root page."""
    return FileResponse(str(_static_directory / "index.html"))


@app.get("/health", tags=["system"])
async def health_check() -> dict[str, str]:
    """Small readiness endpoint for local development and deployment probes."""
    return {"status": "ok"}
