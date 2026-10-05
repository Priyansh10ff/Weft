"""Entity extraction, speaker attribution and the typed graph they produce."""

from __future__ import annotations

from app.schemas.knowledge import KnowledgeNode, MediaModality, SourceAsset
from app.schemas.records import RelationType
from app.services.entity_extractor import extract_entities, heuristic_entities
from app.services.ingestion import ingest_nodes
from app.services.pipeline import attach_entities, attach_speakers, audio_nodes
from app.services.speaker_attribution import attribute_speakers


def test_heuristic_entities():
    found = dict(
        heuristic_entities(
            "So the checkout-service kept timing out because the Payments API retried. "
            "p99 latency fell after PostgreSQL got read replicas, see ticket #4471."
        )
    )
    assert found["checkout-service"] == "system"
    assert found["PostgreSQL"] == "product"
    assert found["p99 latency"] == "metric"
    assert found["ticket #4471"] == "event"
    assert "Payments API" in found
    assert not any(name.lower().startswith("so ") for name in found)


def test_llm_entities_batched(fake_gemini, monkeypatch):
    monkeypatch.setenv("ENTITY_BATCH_SIZE", "2")

    def respond(contents):
        return {"items": [{"i": 0, "entities": [{"name": "Kafka", "type": "system"},
                                                 {"name": "kafka", "type": "system"},
                                                 {"name": "Bob", "type": "wizard"}]}]}

    client = fake_gemini(respond)
    result, method = extract_entities(["a", "b", "c"], client=client)
    assert method == "llm"
    assert len(client.calls) == 2
    assert result[0] == [("Kafka", "system"), ("Bob", "concept")]
    assert result[1] == [] and result[2] == [("Kafka", "system"), ("Bob", "concept")]


def test_extraction_falls_back_without_key():
    result, method = extract_entities(["The checkout-service failed"])
    assert method == "heuristic" and ("checkout-service", "system") in result[0]


def test_speaker_attribution_maps_labels_and_names(fake_gemini, tmp_path):
    media = tmp_path / "standup.mp3"
    media.write_bytes(b"ID3fake")

    def respond(contents):
        return {
            "speakers": [{"label": "Speaker 1", "name": "Priya", "evidence": "I'm Priya"},
                         {"label": "Speaker 2", "name": None}],
            "segments": [{"i": 0, "speaker": "Speaker 1"}, {"i": 1, "speaker": "Speaker 2"},
                         {"i": 9, "speaker": "Speaker 1"}],
        }

    speech = [
        {"start_time": 0, "end_time": 2, "text": "Hi, I'm Priya from infra."},
        {"start_time": 2, "end_time": 4, "text": "Thanks."},
        {"start_time": 4, "end_time": 6, "text": "(inaudible)"},
    ]
    labels = attribute_speakers(media, speech, client=fake_gemini(respond))
    assert labels[0] == {"label": "Speaker 1", "name": "Priya", "confidence": 0.8}
    assert labels[1]["name"] is None and labels[1]["confidence"] == 0.7
    assert labels[2] is None


def test_speakers_and_entities_become_graph_edges(fake_gemini, tmp_path, repo, store, monkeypatch):
    media = tmp_path / "standup.mp3"
    media.write_bytes(b"ID3fake")
    nodes = audio_nodes(
        [
            {"start_time": 0, "end_time": 3, "text": "I'm Priya. We moved reads to the read-replica.", "confidence": 0.9},
            {"start_time": 3, "end_time": 5, "text": "Nice, the p99 latency dropped.", "confidence": 0.8},
        ],
        "standup.mp3",
    )

    def respond(contents):
        return {"speakers": [{"label": "Speaker 1", "name": "Priya"}, {"label": "Speaker 2", "name": None}],
                "segments": [{"i": 0, "speaker": "Speaker 1"}, {"i": 1, "speaker": "Speaker 2"}]}

    assert attach_speakers(nodes, media, MediaModality.AUDIO, client=fake_gemini(respond)) == 2
    assert attach_entities(nodes) == "heuristic"

    result = ingest_nodes(SourceAsset(filename="standup.mp3", modality=MediaModality.AUDIO), nodes,
                          repository=repo, vector_store=store)
    first, second = result.segments
    assert first.confidence == 0.9  # Whisper's own value, not the prior
    spoken = repo.relations_for(first.id, RelationType.SPOKEN_BY)
    priya = repo.get_entity(spoken[0][0].dst_id)
    assert (priya.name, priya.entity_type) == ("Priya", "person")
    other = repo.get_entity(repo.relations_for(second.id, RelationType.SPOKEN_BY)[0][0].dst_id)
    assert other.name == "Speaker 2 (standup.mp3)" and other.entity_type == "speaker"

    mentions = repo.entities_for_segments([first.id, second.id])
    names = {e.name: (e.entity_type, c) for e, c in mentions[first.id] + mentions[second.id]}
    assert names["read-replica"][0] == "system"
    assert names["p99 latency"] == ("metric", 0.48)  # heuristic mention: 0.8 * 0.6


def test_video_speech_segments_get_speakers(fake_gemini, tmp_path):
    media = tmp_path / "talk.mp4"
    media.write_bytes(b"fake")
    node = KnowledgeNode(
        modality=MediaModality.VIDEO,
        transcript="a b",
        attributes={"speech_segments": [
            {"start_seconds": 0, "end_seconds": 1, "text": "a"},
            {"start_seconds": 1, "end_seconds": 2, "text": "b"},
        ]},
    )

    def respond(contents):
        return {"speakers": [{"label": "Speaker 1", "name": "Ravi"}],
                "segments": [{"i": 0, "speaker": "Speaker 1"}, {"i": 1, "speaker": "Speaker 1"}]}

    attach_speakers([node], media, MediaModality.VIDEO, client=fake_gemini(respond))
    assert [s["speaker"] for s in node.attributes["speech_segments"]] == ["Ravi", "Ravi"]
    assert node.attributes["speakers"] == [{"label": "Speaker 1", "name": "Ravi", "confidence": 0.8}]


def test_entity_type_upgrades_from_concept(repo):
    assert repo.upsert_entity("Kafka").entity_type == "concept"
    assert repo.upsert_entity("kafka", "system").entity_type == "system"
    assert repo.upsert_entity("Kafka", "concept").entity_type == "system"
