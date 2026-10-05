"""Background ingestion jobs backed by the SQLite ``jobs`` table.

Long videos take minutes (transcription, one vision call per scene, speaker
attribution), longer than an HTTP request should stay open.  With
``?background=true`` an upload is persisted, a job row is written and the
request returns ``202`` immediately; a small worker pool runs the pipeline
and records stage/progress so clients can poll ``GET /jobs/{id}``.

Because job state and the uploaded file are both on disk, jobs interrupted
by a restart are re-queued on startup.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from app.config import get_settings
from app.db.repository import KnowledgeRepository, get_repository
from app.schemas.knowledge import SourceAsset
from app.schemas.records import JobRecord, JobStatus
from app.services.ingestion import ingest_nodes
from app.services.pipeline import ProcessingFailed, run_pipeline
from app.services.vector_store import VectorStoreError

_log = logging.getLogger(__name__)


class JobManager:
    def __init__(self, repository: KnowledgeRepository | None = None, workers: int | None = None):
        self._repository = repository
        self._executor = ThreadPoolExecutor(
            max_workers=workers or get_settings().job_workers, thread_name_prefix="ingest"
        )
        self._futures: dict[str, Future[None]] = {}
        self._lock = threading.Lock()

    @property
    def repo(self) -> KnowledgeRepository:
        return self._repository or get_repository()

    def submit(self, asset: SourceAsset, file_path: Path) -> JobRecord:
        job = JobRecord(
            source_id=str(asset.source_id),
            filename=asset.filename,
            modality=asset.modality,
            file_path=str(file_path),
            content_type=asset.content_type,
        )
        self.repo.create_job(job)
        self._schedule(job)
        return job

    def _schedule(self, job: JobRecord) -> None:
        future = self._executor.submit(self._run, job)
        with self._lock:
            self._futures[job.id] = future
        future.add_done_callback(lambda _: self._forget(job.id))

    def _forget(self, job_id: str) -> None:
        with self._lock:
            self._futures.pop(job_id, None)

    def _run(self, job: JobRecord) -> None:
        repo = self.repo
        asset = SourceAsset(
            source_id=job.source_id,
            filename=job.filename,
            modality=job.modality,
            content_type=job.content_type,
        )

        def progress(stage: str, fraction: float) -> None:
            repo.update_job(job.id, stage=stage, progress=round(min(1.0, max(0.0, fraction)), 3))

        repo.update_job(job.id, status=JobStatus.RUNNING, stage="starting", progress=0.0)
        try:
            pipeline = run_pipeline(asset, Path(job.file_path), progress=progress)
            progress("indexing", 0.92)
            result = ingest_nodes(
                asset,
                pipeline.nodes,
                file_path=Path(job.file_path),
                source_attributes={"warnings": pipeline.warnings, "enrichment": pipeline.enrichment},
                repository=repo,
            )
        except (ProcessingFailed, VectorStoreError) as exc:
            repo.update_job(job.id, status=JobStatus.FAILED, stage="failed", error=str(exc))
            return
        except Exception as exc:  # never let a worker die silently
            _log.exception("Ingestion job %s crashed", job.id)
            repo.update_job(
                job.id, status=JobStatus.FAILED, stage="failed", error=f"{type(exc).__name__}: {exc}"
            )
            return
        repo.update_job(
            job.id,
            status=JobStatus.SUCCEEDED,
            stage="done",
            progress=1.0,
            warnings=pipeline.warnings,
            result={
                "source_id": result.source.id,
                "segments": len(result.segments),
                "entities": result.entity_count,
                "relations": result.relation_count,
            },
        )

    def resume_interrupted(self) -> int:
        """Re-queue jobs left queued/running by a previous process."""
        resumed = 0
        for job in self.repo.list_jobs([JobStatus.QUEUED, JobStatus.RUNNING], limit=1000):
            if job.id in self._futures:
                continue
            if not Path(job.file_path).is_file():
                self.repo.update_job(
                    job.id, status=JobStatus.FAILED, stage="failed",
                    error="Uploaded file is missing; it cannot be resumed.",
                )
                continue
            if self.repo.get_source(job.source_id) is not None:
                self.repo.update_job(job.id, status=JobStatus.SUCCEEDED, stage="done", progress=1.0)
                continue
            self.repo.update_job(job.id, status=JobStatus.QUEUED, stage="requeued", progress=0.0)
            self._schedule(job)
            resumed += 1
        return resumed

    def wait(self, job_id: str, timeout: float | None = None) -> None:
        with self._lock:
            future = self._futures.get(job_id)
        if future is not None:
            future.result(timeout=timeout)

    def shutdown(self, wait: bool = False) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=not wait)


_manager: JobManager | None = None
_manager_lock = threading.Lock()


def get_job_manager() -> JobManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = JobManager()
        return _manager


def reset_job_manager(manager: JobManager | None = None) -> None:
    global _manager
    with _manager_lock:
        if _manager is not None and _manager is not manager:
            _manager.shutdown(wait=False)
        _manager = manager
