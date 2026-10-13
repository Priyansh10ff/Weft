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
from app.api.security import HardeningMiddleware
from app.config import get_settings
from app.services.storage import storage_root

_settings = get_settings()
logging.basicConfig(
    level=getattr(logging, _settings.log_level, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


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
    title="Weft",
    version="0.3.0",
    description="Turns video, audio, images, PDFs and JSON into structured, linked, retrievable knowledge.",
    lifespan=lifespan,
)

app.add_middleware(HardeningMiddleware)
# Added after the hardening middleware so it wraps it: a 401 still carries CORS headers.
# The website is served from this origin, so no CORS is needed for it.
# CORS_ORIGINS (comma-separated, or "*") opens the API to other origins.
if _settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(_settings.cors_origins),
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type", "X-API-Key", "Authorization", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )

_storage = storage_root()
for _name in ("frames", "uploads", "derived"):
    (_storage / _name).mkdir(parents=True, exist_ok=True)

# Legacy flat frame directory (pre-Phase-2 uploads).
app.mount("/frames", StaticFiles(directory=str(_storage / "frames")), name="frames")
# Original uploads, data/uploads/{source_id}/{filename}: images, and the
# video/audio the evidence viewer seeks into.
app.mount("/uploads", StaticFiles(directory=str(_storage / "uploads")), name="uploads")
# Per-source derived artifacts (keyframes, page renders).
app.mount("/derived", StaticFiles(directory=str(_storage / "derived")), name="derived")

_static_directory = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_static_directory)), name="static")

app.include_router(api_router)


@app.get("/", include_in_schema=False)
async def landing() -> FileResponse:
    """Public landing page."""
    return FileResponse(str(_static_directory / "index.html"))


@app.get("/app", include_in_schema=False)
async def workspace() -> FileResponse:
    """The Weft workspace (single-page app, hash-routed)."""
    return FileResponse(str(_static_directory / "app.html"))


@app.get("/health", tags=["system"])
async def health_check() -> dict[str, object]:
    """Readiness probe plus which providers are configured (never the keys)."""
    settings = get_settings()
    return {
        "status": "ok",
        "version": app.version,
        "providers": {
            "llm": settings.llm_provider,
            "vision": settings.llm_enabled,
            "transcription": bool(settings.groq_api_key),
            "speakers": settings.llm_provider == "gemini" and settings.speaker_attribution,
            "entities": "llm" if settings.llm_enabled else "heuristic",
        },
        "demo_enabled": settings.demo_enabled,
        "auth_required": bool(settings.api_key),
    }
