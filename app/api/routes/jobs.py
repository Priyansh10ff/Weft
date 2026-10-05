"""Status of background ingestion jobs."""

from anyio import to_thread
from fastapi import APIRouter, HTTPException, Query, status

from app.db.repository import get_repository
from app.schemas.records import JobRecord, JobStatus

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("", response_model=list[JobRecord])
async def list_jobs(
    status_filter: JobStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[JobRecord]:
    statuses = [status_filter] if status_filter else None
    return await to_thread.run_sync(lambda: get_repository().list_jobs(statuses, limit))


@router.get("/{job_id}", response_model=JobRecord)
async def get_job(job_id: str) -> JobRecord:
    job = await to_thread.run_sync(lambda: get_repository().get_job(job_id))
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Job {job_id} not found")
    return job
