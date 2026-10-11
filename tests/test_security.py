"""HTTP hardening: headers, request IDs, the optional API key, upload checks."""

from __future__ import annotations

import io
import json

import pytest
from fastapi.testclient import TestClient

from app.api.routes._upload import content_matches
from app.schemas.knowledge import MediaModality
from main import app


@pytest.fixture()
def client(repo, store, tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_STORAGE_ROOT", str(tmp_path / "media"))
    monkeypatch.delenv("WEFT_API_KEY", raising=False)
    return TestClient(app)


def _json_upload(client, headers=None):
    body = json.dumps([{"text": "Checkout timed out", "locator": "t-1"}]).encode()
    return client.post("/upload/json", headers=headers or {},
                       files={"file": ("t.json", io.BytesIO(body), "application/json")})


def test_security_headers_and_request_id(client):
    page = client.get("/app")
    assert page.headers["x-content-type-options"] == "nosniff"
    assert page.headers["x-frame-options"] == "DENY"
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert len(page.headers["x-request-id"]) == 16
    assert client.get("/health", headers={"X-Request-ID": "abc123"}).headers["x-request-id"] == "abc123"
    # Swagger UI loads from a CDN, so the docs keep working without the CSP.
    assert "content-security-policy" not in client.get("/docs").headers


def test_api_key_guards_writes_but_not_reads(client, monkeypatch):
    monkeypatch.setenv("WEFT_API_KEY", "s3cret-key")
    assert client.get("/health").json()["auth_required"] is True
    assert _json_upload(client).status_code == 401
    assert client.post("/query", json={"query": "checkout"}).status_code == 401
    assert _json_upload(client, {"X-API-Key": "wrong"}).status_code == 401
    assert _json_upload(client, {"X-API-Key": "s3cret-key"}).status_code == 200
    assert _json_upload(client, {"Authorization": "Bearer s3cret-key"}).json()["status"] == "deduplicated"
    assert client.get("/stats").status_code == 200
    assert client.get("/sources").status_code == 200


def test_no_api_key_means_open(client):
    assert client.get("/health").json()["auth_required"] is False
    assert _json_upload(client).status_code == 200


def test_upload_content_must_match_its_type(client):
    fake_pdf = client.post("/upload/pdf", files={"file": ("report.pdf", io.BytesIO(b"not a pdf at all"), "application/pdf")})
    assert fake_pdf.status_code == 415
    fake_png = client.post("/upload/image", files={"file": ("shot.png", io.BytesIO(b"GIF89a..."), "image/png")})
    assert fake_png.status_code == 415
    binary_json = client.post("/upload/json", files={"file": ("x.json", io.BytesIO(b"\x00\x01\x02"), "application/json")})
    assert binary_json.status_code == 415


def test_upload_limit_is_enforced_while_streaming(client, monkeypatch):
    monkeypatch.setenv("MAX_UPLOAD_MB", "1")
    big = b"[" + b'"x",' * 400_000 + b'"x"]'
    response = client.post("/upload/json", files={"file": ("big.json", io.BytesIO(big), "application/json")})
    assert response.status_code == 413


@pytest.mark.parametrize(
    ("modality", "head", "ok"),
    [
        (MediaModality.PDF, b"%PDF-1.7\n", True),
        (MediaModality.IMAGE, b"\x89PNG\r\n\x1a\n....", True),
        (MediaModality.IMAGE, b"\xff\xd8\xff\xe0JFIF", True),
        (MediaModality.VIDEO, b"\x00\x00\x00\x18ftypmp42", True),
        (MediaModality.VIDEO, b"RIFF....AVI ", False),
        (MediaModality.AUDIO, b"ID3\x04\x00", True),
        (MediaModality.AUDIO, b"RIFF\x00\x00\x00\x00WAVEfmt ", True),
        (MediaModality.AUDIO, b"\xff\xfb\x90\x00", True),
        (MediaModality.AUDIO, b"%PDF-1.4", False),
        (MediaModality.JSON, "café notes".encode(), True),
    ],
)
def test_content_signatures(tmp_path, modality, head, ok):
    path = tmp_path / "f.bin"
    path.write_bytes(head)
    assert content_matches(path, modality) is ok
