"""Claude as the single provider: selection, JSON calls, images, streaming."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.config import get_settings
from app.services import llm


class FakeClaude:
    """Stands in for ``anthropic.Anthropic``; ``responder(content_blocks) -> dict | str``."""

    def __init__(self, responder):
        outer = self
        self.calls: list[dict] = []

        class _Stream:
            def __init__(self, text):
                self.text_stream = iter([text[i : i + 12] for i in range(0, len(text), 12)])

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class _Messages:
            def create(self, **kwargs):
                outer.calls.append(kwargs)
                body = responder(kwargs["messages"][0]["content"])
                text = body if isinstance(body, str) else json.dumps(body)
                return SimpleNamespace(content=[SimpleNamespace(type="thinking", thinking="..."), SimpleNamespace(type="text", text=text)])

            def stream(self, **kwargs):
                outer.calls.append(kwargs)
                return _Stream(responder(kwargs["messages"][0]["content"]))

        self.messages = _Messages()


@pytest.mark.parametrize(
    ("setting", "gemini", "claude", "expected"),
    [
        ("auto", "", "", None),
        ("auto", "g", "", "gemini"),
        ("auto", "g", "c", "claude"),
        ("gemini", "g", "c", "gemini"),
        ("claude", "g", "", None),
        ("claude", "", "c", "claude"),
    ],
)
def test_one_provider_is_selected(monkeypatch, setting, gemini, claude, expected):
    monkeypatch.setenv("LLM_PROVIDER", setting)
    monkeypatch.setenv("GEMINI_API_KEY", gemini)
    monkeypatch.setenv("ANTHROPIC_API_KEY", claude)
    assert get_settings().llm_provider == expected
    assert llm.provider() == expected


def test_no_provider_raises_not_configured():
    with pytest.raises(llm.LLMNotConfigured):
        llm.generate_json(["hi"])


def test_claude_json_skips_thinking_and_strips_fences():
    client = FakeClaude(lambda blocks: '```json\n{"ok": true}\n```')
    assert llm.generate_json(["question"], system="be brief", client=client) == {"ok": True}
    call = client.calls[0]
    assert call["model"] == get_settings().claude_model
    assert "temperature" not in call
    assert call["system"].startswith("be brief") and "JSON" in call["system"]
    assert call["messages"][0]["content"] == [{"type": "text", "text": "question"}]


def test_claude_entities(monkeypatch):
    from app.services.entity_extractor import extract_entities

    client = FakeClaude(lambda blocks: {"items": [{"i": 0, "entities": [{"name": "Payments API", "type": "system"}]}]})
    entities, method = extract_entities(["The Payments API timed out."], client=client)
    assert method == "llm"
    assert entities == [[("Payments API", "system")]]


def test_claude_reads_images_as_base64_blocks(tmp_path):
    from app.services.image_processor import analyze_image

    image = tmp_path / "frame.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    client = FakeClaude(lambda blocks: {"image_type": "diagram", "visual_summary": "A retry loop.", "ocr_text": "retry"})
    result = analyze_image(image, client=client)
    assert result["visual_summary"] == "A retry loop."
    blocks = client.calls[0]["messages"][0]["content"]
    assert blocks[1]["type"] == "image"
    assert blocks[1]["source"]["media_type"] == "image/png"


def test_claude_refuses_audio_so_speakers_are_skipped(tmp_path):
    from app.services.speaker_attribution import attribute_speakers

    media = tmp_path / "call.mp3"
    media.write_bytes(b"ID3")
    client = FakeClaude(lambda blocks: {})
    with pytest.raises(llm.LLMError):
        attribute_speakers(media, [{"start_time": 0, "end_time": 1, "text": "hello"}], client=client)
    assert llm.supports_media("claude") is False
    assert llm.supports_media("gemini") is True


def test_claude_streams_a_cited_answer():
    from app.services.answer_synthesizer import stream_answer

    hits = [{"source": "notes.pdf", "text": "Backoff fixed the timeouts.", "metadata": {"modality": "pdf"}}]
    client = FakeClaude(lambda blocks: "Retries now back off, which fixed the timeouts (Evidence 1).")
    events = list(stream_answer("How was it fixed?", hits, client=client))
    deltas = [v for k, v in events if k == "delta"]
    answer = events[-1][1]
    assert len(deltas) > 1
    assert answer.method == "llm" and answer.grounded
    assert answer.sources[0]["cited"] is True


def test_health_names_the_provider(monkeypatch):
    from fastapi.testclient import TestClient

    from main import app

    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-claude")
    body = TestClient(app).get("/health").json()
    assert body["providers"]["llm"] == "claude"
    assert body["providers"]["vision"] is True
    assert body["providers"]["speakers"] is False
    assert "secret-claude" not in json.dumps(body)
