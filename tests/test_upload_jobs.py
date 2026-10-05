"""Upload handling: background jobs, hash dedupe, limits, resume after restart."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.schemas.knowledge import MediaModality
from app.schemas.records import JobRecord, JobStatus
from app.services.jobs import JobManager, reset_job_manager
from main import app

RECORDS = [{"text": "Checkout timed out", "locator": "t-1", "entities": ["checkout-service"]}]


@pytest.fixture()
def manager(repo):
    jm = JobManager(repository=repo, workers=1)
    reset_job_manager(jm)
    yield jm
    reset_job_manager(None)
    jm.shutdown(wait=True)


@pytest.fixture()
def client(repo, store, manager):
    return TestClient(app)


def _post(client, payload=RECORDS, name="tickets.json", **params):
    body = json.dumps(payload).encode() if not isinstance(payload, bytes) else payload
    return client.post(
        "/upload/json", params=params,
        files={"file": (name, io.BytesIO(body), "application/json")},
    )


def test_background_upload_runs_as_job(client, manager, repo):
    response = _post(client, background="true")
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "queued" and body["job_id"]

    manager.wait(body["job_id"], timeout=30)
    job = client.get(f"/jobs/{body['job_id']}").json()
    assert job["status"] == "succeeded" and job["progress"] == 1.0
    assert job["result"]["segments"] == 1 and job["result"]["source_id"] == body["source_id"]
    assert repo.get_source(body["source_id"]).attributes["enrichment"]["entity_method"] == "heuristic"
    assert any(j["id"] == body["job_id"] for j in client.get("/jobs").json())
    assert client.get("/jobs/missing").status_code == 404


def test_failed_job_records_error(client, manager):
    response = _post(client, payload=b"[]", background="true")
    manager.wait(response.json()["job_id"], timeout=30)
    job = client.get(f"/jobs/{response.json()['job_id']}").json()
    assert job["status"] == "failed" and "no object records" in job["error"]


def test_identical_upload_is_deduplicated(client, repo):
    first = _post(client).json()
    second = _post(client, name="copy.json").json()
    assert second["status"] == "deduplicated"
    assert second["source_id"] == first["source_id"] and second["processed_nodes"] == 1
    assert repo.stats().sources == 1

    forced = _post(client, force="true").json()
    assert forced["status"] == "completed" and forced["source_id"] != first["source_id"]
    assert repo.stats().sources == 2


def test_upload_validation(client, monkeypatch):
    assert _post(client, name="tickets.exe").status_code == 415
    assert _post(client, payload=b"").status_code == 400
    monkeypatch.setenv("MAX_UPLOAD_MB", "1")
    assert _post(client, payload=b"x" * (1024 * 1024 + 1), name="big.txt").status_code == 413
    assert _post(client, payload=b"[1, 2]").status_code == 422


def test_resume_interrupted_jobs(repo, store, tmp_path):
    upload = tmp_path / "pending.json"
    upload.write_text(json.dumps(RECORDS))
    stale = JobRecord(source_id="11111111-1111-1111-1111-111111111111", filename="pending.json",
                      modality=MediaModality.JSON, file_path=str(upload), status=JobStatus.RUNNING)
    gone = JobRecord(source_id="22222222-2222-2222-2222-222222222222", filename="gone.json",
                     modality=MediaModality.JSON, file_path=str(tmp_path / "missing.json"))
    repo.create_job(stale)
    repo.create_job(gone)

    jm = JobManager(repository=repo, workers=1)
    try:
        assert jm.resume_interrupted() == 1
        jm.wait(stale.id, timeout=30)
    finally:
        jm.shutdown(wait=True)
    assert repo.get_job(stale.id).status is JobStatus.SUCCEEDED
    assert repo.get_source(stale.source_id) is not None
    failed = repo.get_job(gone.id)
    assert failed.status is JobStatus.FAILED and "missing" in failed.error
