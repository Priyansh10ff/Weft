"""Top-level API router composition."""

from fastapi import APIRouter

from app.api.routes import audio, evaluation, image, jobs, json_upload, knowledge, pdf, query, video

api_router = APIRouter()
api_router.include_router(video.router)
api_router.include_router(audio.router)
api_router.include_router(image.router)
api_router.include_router(pdf.router)
api_router.include_router(json_upload.router)
api_router.include_router(query.router)
api_router.include_router(knowledge.router)
api_router.include_router(jobs.router)
api_router.include_router(evaluation.router)
