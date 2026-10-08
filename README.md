# Weft

Multimodal data pipeline for RAG. Weft ingests video, audio, images, PDFs and JSON, keeps the exact location of every piece of evidence, links what was said to what was shown across files, and answers questions with citations you can open at the right second, page or region.

![Python](https://img.shields.io/badge/python-3.10%2B-3776AB) ![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688) ![SQLite](https://img.shields.io/badge/SQLite-FTS5-003B57) ![ChromaDB](https://img.shields.io/badge/ChromaDB-1.x-5A3FC0)

[Product](docs/PRODUCT.md) · [Requirements](docs/PRD.md) · [Technical reference](docs/TECHNICAL.md) · [Design system](docs/DESIGN.md) · [Evaluation report](EVALUATION.md)

---

## Why

Text-only RAG reads transcripts and document text. It never sees the slide on screen, the diagram on page 3 or the dashboard that proves a fix worked, and it can't connect evidence that is split across a recording, a PDF and a screenshot. Weft treats every format as evidence, links it, and shows exactly where each answer came from.

## What it does

| Stage | What happens |
|---|---|
| **Ingest** | Video is cut into scenes where the picture changes, with the speech said over each scene and a description of what was shown. Audio is transcribed with timestamps. PDFs become one segment per page. Images keep their text blocks and regions with bounding boxes. JSON becomes one segment per record |
| **Understand** | Typed entities (people, systems, metrics, concepts) and speaker labels. Variants of the same name resolve to one entity |
| **Link** | `co_occurs`, `shows_same`, `depicts`, `corroborates`, `same_as` and `next` edges across files and time, each with the reason it exists |
| **Retrieve** | Multi-part questions are split. Semantic, keyword (BM25, for exact identifiers like `max_retries=5`) and entity search are fused. Linked evidence is ranked up. Every hit says why it was returned and where the match is |
| **Answer** | Gemini answers from the retrieved evidence only, with numbered citations. Without a key, an extractive summary |
| **Evaluate** | A gold question set with known evidence and decoy files, scored for text-only RAG, dense, hybrid and full Weft |

## Results

Gold set of 24 questions over the sample corpus plus hard distractors (31 segments), MiniLM embeddings.

| System | Recall@5 | Complete@5 | MRR | Modality coverage |
|---|---|---|---|---|
| Text-only RAG (baseline) | 78% | 71% | 0.79 | 79% |
| Dense, text + visual | 92% | 83% | 0.86 | 91% |
| Hybrid (dense + keyword + entity) | 94% | 83% | 0.86 | 94% |
| **Weft** (hybrid + decomposition + graph) | **95%** | **88%** | **0.86** | **95%** |

Complete@5 means every piece of required evidence was in the top 5. Weft reaches 100% on visual-only, exact-identifier and multi-part questions. Cross-modal questions are its weakest type at 25% (hybrid 50%), which is the main open item. Full breakdown in [EVALUATION.md](EVALUATION.md) and [TECHNICAL.md §9](docs/TECHNICAL.md#9-evaluation-appevaluation).

## Quickstart

Requires Python 3.10 or newer.

```bash
git clone https://github.com/Priyansh10ff/Weft.git
cd Weft
python -m venv .venv
source .venv/bin/activate              # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env                   # Windows: copy .env.example .env
uvicorn main:app --reload
```

Open [http://127.0.0.1:8000/app](http://127.0.0.1:8000/app) and choose **Load sample data**, then ask: *How did the team fix the checkout timeout issue, and how do we know it worked?*

The first start downloads the MiniLM embedding model (about 80 MB).

### API keys

Both are optional. Everything runs without them, with reduced features.

| Key | Enables | Without it |
|---|---|---|
| `GEMINI_API_KEY` ([get one](https://aistudio.google.com/apikey)) | Vision for keyframes, images and PDF pages, typed entities, speaker labels, written answers | No visual descriptions, image uploads refused, pattern-based entities, extractive answers |
| `GROQ_API_KEY` ([get one](https://console.groq.com/keys)) | Fast Whisper transcription | Local faster-whisper (slower, downloads a model) |

The sidebar in the workspace and `GET /health` show which providers are active. Every other setting is listed in [TECHNICAL.md §11](docs/TECHNICAL.md#11-configuration).

## Using it

**Website.** `/` is the landing page. `/app` is the workspace:

- **Ask:** a question, an answer with citations, ranked evidence and a comparison with text-only RAG
- **Library:** upload files and follow their jobs
- **Entities:** every mention of a thing across files, over time
- **Evaluate:** run the gold set and inspect each question

**API.** Upload a file, then ask:

```bash
curl -F "file=@incident-review.mp4" "http://127.0.0.1:8000/upload/video?background=true"
curl -X POST http://127.0.0.1:8000/query -H "Content-Type: application/json" \
     -d '{"query": "What retry policy replaced the old one, and where was it shown?", "limit": 5}'
```

Interactive docs at [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs). The full endpoint list is in [TECHNICAL.md §10](docs/TECHNICAL.md#10-http-api).

**CLI.**

```bash
python -m app.evaluation run --k 5 --markdown EVALUATION.md   # evaluate and write the report
python -m app.cli stats                                       # counts by table, modality, relation
python -m app.cli reindex                                     # rebuild the vector index from SQLite
python -m app.cli relink                                      # recompute cross-modal links
```

## Architecture

```
upload ─► processor (video · audio · image · pdf · json) ─► speakers + entities
       ─► ingestion: SQLite (system of record) + Chroma (index) + FTS5 (keywords)
       ─► linker: co_occurs · shows_same · depicts · corroborates · same_as

question ─► decompose ─► dense + BM25 + entity ─► rank fusion ─► graph boost
         ─► confidence weighting + coverage ─► cited evidence ─► grounded answer
```

FastAPI serves the API, the website and the media from one process. SQLite holds every record. The Chroma index is keyed by segment ID and can be rebuilt at any time. Details in [TECHNICAL.md](docs/TECHNICAL.md).

## Project layout

```
main.py               FastAPI app and website routes
app/api/              HTTP routes
app/services/         processors, pipeline, ingestion, linker, retrieval, answers
app/db/               SQLite repository
app/schemas/          records and API models
app/evaluation/       gold set, metrics, runner, CLI
static/               landing page and workspace (HTML, CSS, ES modules)
test_data/            sample corpus and loader
tests/                pytest suites
docs/                 product, requirements, technical and design docs
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest tests
```

Tests need no network or API keys: Gemini and Groq are disabled and a deterministic embedding replaces MiniLM.

## Documentation

| Document | Covers |
|---|---|
| [PRODUCT.md](docs/PRODUCT.md) | What Weft is, why it exists, who it's for, how it works |
| [PRD.md](docs/PRD.md) | Goals, user stories, requirements, metrics, roadmap, risks |
| [TECHNICAL.md](docs/TECHNICAL.md) | Architecture, data model, pipeline, linking, retrieval, evaluation, API, configuration |
| [DESIGN.md](docs/DESIGN.md) | Colour tokens, type, layout, components, states |
