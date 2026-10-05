"""HTTP contract for upload -> structured records -> inspection/query."""

from __future__ import annotations

import io
import json

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture()
def client(repo, store, tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_STORAGE_ROOT", str(tmp_path / "media"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    return TestClient(app)


def _upload_tickets(client: TestClient) -> dict:
    records = [
        {"text": "Checkout timed out repeatedly", "locator": "ticket-1", "entities": ["checkout-service"]},
        {"text": "Checkout works after the backoff fix", "locator": "ticket-2", "entities": ["Checkout Service", "backoff"]},
    ]
    response = client.post(
        "/upload/json",
        files={"file": ("tickets.json", io.BytesIO(json.dumps(records).encode()), "application/json")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_upload_returns_structured_counts(client):
    body = _upload_tickets(client)
    assert body["processed_nodes"] == 2
    assert body["source_id"]
    assert body["entity_count"] == 2  # checkout-service == Checkout Service
    assert body["relation_count"] == 3


def test_inspection_endpoints(client):
    body = _upload_tickets(client)
    stats = client.get("/stats").json()
    assert stats["segments"] == 2 and stats["segments_by_modality"] == {"json": 2}

    sources = client.get("/sources").json()
    assert sources[0]["segment_count"] == 2
    assert sources[0]["source"]["sha256"]

    detail = client.get(f"/sources/{body['source_id']}").json()
    segment = detail["segments"][1]
    assert segment["locator"]["label"] == "ticket-2"
    assert segment["confidence"] == 1.0

    seg = client.get(f"/segments/{segment['id']}").json()
    assert {m["entity"]["name"] for m in seg["entities"]} == {"checkout-service", "backoff"}
    assert all(link["relation"]["relation"] == "mentions" for link in seg["links"])

    entities = client.get("/entities", params={"q": "checkout"}).json()
    assert entities[0]["mention_count"] == 2
    entity = client.get(f"/entities/{entities[0]['entity']['id']}").json()
    assert len(entity["segments"]) == 2

    assert client.get("/segments/nope").status_code == 404
    assert client.get("/sources/nope").status_code == 404


def test_query_returns_segment_provenance(client):
    _upload_tickets(client)
    response = client.post("/query", json={"query": "checkout backoff fix", "limit": 3})
    assert response.status_code == 200, response.text
    result = response.json()["results"][0]
    assert result["segment_id"] and result["source_id"]
    assert result["modality"] == "json"
    assert result["timestamp"] == "ticket-2"
    assert "backoff" in result["entities"]
    assert result["confidence"] == 1.0


def test_delete_source_endpoint(client):
    body = _upload_tickets(client)
    response = client.delete(f"/sources/{body['source_id']}")
    assert response.status_code == 200 and response.json()["deleted_segments"] == 2
    assert client.get("/stats").json()["segments"] == 0
    assert client.delete(f"/sources/{body['source_id']}").status_code == 404
