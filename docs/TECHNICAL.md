# Weft: Technical Reference

How the system is built: architecture, stack, repository structure, data model, the ingestion pipeline, cross-modal linking, retrieval, answers, evaluation, the HTTP API, configuration, testing and known limitations.

Related: [PRODUCT.md](./PRODUCT.md) (overview) · [PRD.md](./PRD.md) (requirements) · [DESIGN.md](./DESIGN.md) (interface)

---

## 1. Architecture

```
          ┌─────────────────────────────────────────────┐
          │  Browser: landing page + workspace (/app)   │
          │  plain HTML, CSS, ES modules, hash routing  │
          └──────────────────────┬──────────────────────┘
                                 │ HTTP / JSON, multipart uploads
                                 ▼
┌──────────────────────────────────────────────────────────────────────┐
│ FastAPI (single process)                                             │
│                                                                      │
│  /upload/*  ──► jobs (worker pool) ──► pipeline                      │
│                                         ├─ processors: video, audio, │
│                                         │  image, pdf, json          │
│                                         ├─ speaker attribution       │
│                                         └─ entity extraction         │
│                                                  │                   │
│                                                  ▼                   │
│                                  ingestion (single write path)       │
│                                                  │                   │
│                                                  ▼                   │
│                                  linker (cross-modal + temporal)     │
│                                                                      │
│  /query     ──► retrieval ──► answer synthesizer                     │
│  /eval/*    ──► evaluation runner (isolated temp corpus)             │
│  /sources, /segments, /entities, /graph, /stats, /jobs               │
└────────┬───────────────────────────┬──────────────────────┬──────────┘
         │                           │                      │
         ▼                           ▼                      ▼
┌──────────────────┐     ┌──────────────────────┐   ┌──────────────────┐
│ SQLite (WAL)     │     │ ChromaDB             │   │ data/ on disk    │
│ system of record │     │ vector index keyed   │   │ uploads, derived │
│ + FTS5 keywords  │     │ by segment ID        │   │ frames, renders  │
└──────────────────┘     └──────────────────────┘   └──────────────────┘
         ▲
         │ external calls, only when keys are set
┌────────┴──────────────────────────────────────────┐
│ Gemini: vision, entities, speakers, answers       │
│ Groq Whisper: transcription (local faster-whisper │
│ fallback)                                         │
└───────────────────────────────────────────────────┘
```

- **One process, one port.** FastAPI serves the API, the website and the media files, and runs the background job workers.
- **SQLite is the system of record.** Every field shown after retrieval comes from SQLite. Chroma only maps embeddings to segment IDs and can be rebuilt with `python -m app.cli reindex`.
- **One write path.** Every processor returns `KnowledgeNode` objects. `services/ingestion.py` is the only code that turns them into records and writes them, in one transaction.
- **Degrades without keys.** Without Gemini, video keyframes and PDF pages are ingested without visual descriptions (speech and the text layer still are), entities come from patterns and answers are extractive. Standalone image uploads need Gemini and are refused with 422 without it. Without Groq, transcription uses local faster-whisper.

## 2. Stack

| Concern | Library |
|---|---|
| Runtime | Python 3.10+ |
| HTTP | FastAPI, Uvicorn, python-multipart |
| Models and validation | Pydantic 2 (`extra="forbid"` on request bodies) |
| System of record | SQLite (standard library) in WAL mode, FTS5 for keyword search |
| Vector index | ChromaDB 1.x, default embedding function (all-MiniLM-L6-v2, ONNX) |
| Vision, entities, speakers, answers | `google-genai` (Gemini, model from `GEMINI_MODEL`) |
| Transcription | `groq` (Whisper large-v3), `faster-whisper` fallback |
| Video | OpenCV (decoding, frame sampling, scene detection) |
| PDF | PyMuPDF (page renders, embedded images), pypdf (text layer) |
| Images | Pillow |
| Config | python-dotenv, environment variables |
| Frontend | Plain HTML, CSS and ES modules. No build step, no framework |
| Fonts | Geist and Geist Mono (Google Fonts) |
| Tests | pytest, httpx (FastAPI TestClient) |

## 3. Repository structure

```
Weft/
├── main.py                         FastAPI app: routers, static mounts, /, /app, /health
├── requirements.txt
├── requirements-dev.txt
├── .env.example
├── EVALUATION.md                   Latest evaluation report (generated)
├── docs/                           PRODUCT, PRD, TECHNICAL, DESIGN
│
├── app/
│   ├── config.py                   Settings from the environment, read on every call
│   ├── cli.py                      stats, reindex, import-legacy, relink
│   ├── api/
│   │   ├── router.py               Includes every route module
│   │   └── routes/
│   │       ├── _upload.py          Shared upload handling: size limit, dedupe, background jobs
│   │       ├── video.py, audio.py, image.py, pdf.py, json_upload.py
│   │       ├── query.py            /query, /query/compare
│   │       ├── knowledge.py        stats, sources, segments, entities, timelines, graph, demo seed
│   │       ├── jobs.py             /jobs
│   │       └── evaluation.py       /eval/datasets, /eval/latest, /eval/run
│   ├── db/repository.py            SQLite schema, migrations, reads and writes, FTS5
│   ├── schemas/
│   │   ├── records.py              Source, Segment, Locator, Entity, Relation, Job
│   │   └── knowledge.py            API request and response models
│   ├── services/
│   │   ├── pipeline.py             File → nodes: processor, speakers, entities
│   │   ├── jobs.py                 Background worker pool, resume on startup
│   │   ├── scene_detector.py       Frame sampling, scene cuts, sentence-aligned windows
│   │   ├── video_processor.py      Transcript + scenes + keyframe vision → nodes
│   │   ├── audio_processor.py      Groq Whisper, faster-whisper fallback
│   │   ├── image_processor.py      Gemini vision: description, text blocks, regions, entities
│   │   ├── pdf_processor.py        Text layer + rendered page analysis per page
│   │   ├── json_processor.py       Records and plain text → nodes
│   │   ├── speaker_attribution.py  Speaker turns and names
│   │   ├── entity_extractor.py     Typed entities (Gemini, pattern fallback)
│   │   ├── gemini.py               Shared client, JSON responses, retries
│   │   ├── ingestion.py            Nodes → records, the single write path
│   │   ├── linker.py               Cross-modal and temporal links
│   │   ├── graph.py                Entity timelines, node neighbourhoods
│   │   ├── vector_store.py         Chroma collections keyed by segment ID
│   │   ├── retrieval.py            Hybrid, graph-aware retrieval
│   │   ├── answer_synthesizer.py   Grounded answer with citations
│   │   └── storage.py              Upload staging, derived directories
│   └── evaluation/
│       ├── dataset.py              Gold set format, evidence matching
│       ├── metrics.py              Recall, Complete, MRR, nDCG, precision, coverage
│       ├── runner.py               Isolated corpus, four systems, report
│       ├── __main__.py             python -m app.evaluation
│       └── datasets/               demo.json (questions), distractors.json (decoys)
│
├── static/
│   ├── index.html                  Landing page
│   ├── app.html                    Workspace shell
│   ├── css/                        weft.css (tokens, primitives), landing.css, app.css
│   ├── img/weft-mark.svg
│   └── js/
│       ├── landing.js
│       └── app/
│           ├── main.js             Hash router, status, shortcuts
│           ├── api.js              API client, uploads with progress
│           ├── ui.js               DOM helpers and small components
│           ├── evidence.js         Evidence drawer
│           └── views/              ask, library, source, entities, evaluate
│
├── test_data/
│   ├── seed_cross_modal_demo.py    Sample corpus loader (used by /demo/seed and the evaluation)
│   └── samples/                    Sample media
└── tests/                          pytest suites
```

### Website routes

| Path | Page |
|---|---|
| `/` | Landing page |
| `/app#/ask` | Ask: question, answer with citations, ranked evidence, baseline comparison |
| `/app#/library` | Library: uploads with job progress, sources, sample data |
| `/app#/sources/{id}` | One source and its segments in order |
| `/app#/entities`, `/app#/entities/{id}` | Entities, and one entity's mentions and timeline |
| `/app#/evaluate` | Evaluation report and run |
| `/docs` | OpenAPI documentation |

## 4. Data model

All records are Pydantic models in `app/schemas/records.py` and rows in SQLite.

### Source
| Field | Notes |
|---|---|
| `id` | UUID |
| `filename`, `content_type`, `size_bytes` | As uploaded |
| `modality` | `video`, `audio`, `image`, `pdf`, `json` |
| `sha256` | Content hash, used to skip duplicate uploads |
| `storage_path` | Original file under `data/uploads/` |
| `status` | `indexed` or `index_failed` |
| `ingested_at` | Upload time |
| `attributes` | `recorded_at`, enrichment warnings, processor details |

### Segment
| Field | Notes |
|---|---|
| `id`, `source_id`, `modality` | |
| `kind` | `av_window` (video), `speech` (audio), `image`, `page` (PDF), `record` (JSON), `note` (text) |
| `ordinal` | Position within the source |
| `text` | What was said or written |
| `visual_summary` | What was shown |
| `locator` | `start_seconds`, `end_seconds`, `page_number`, `region` (normalised bounding box), `label` |
| `frame_path` | Keyframe or page render under `data/derived/{source_id}/` |
| `confidence` | 0 to 1, see below |
| `extractor` | e.g. `whisper+vision`, `pdf-text-layer+vision`, `structured-input` |
| `attributes` | Speech spans with timestamps and speakers, OCR text blocks and regions with boxes, keyframe hash |

Confidence starts from a prior per extractor and is adjusted by the extraction itself:

| Extractor | Prior |
|---|---|
| JSON records, plain text | 1.0 |
| PDF text layer + vision | 0.9 |
| Whisper (audio) | 0.85 |
| Whisper + vision (video) | 0.8 |
| Vision OCR (image) | 0.75 |
| PDF vision only | 0.7 |

Speech confidence uses Whisper's own scores: `exp(avg_logprob) × (1 − no_speech_prob)`. A failed vision call lowers a segment's confidence by 0.2. Entity mentions found by the pattern fallback count at 0.6 of a model-extracted mention.

### Entity
`id`, `name` (as first seen), `normalized_name` (deduplication key), `entity_type` (person, speaker, system, component, product, metric, organization, concept, event, location).

### Relation
`src_kind` / `src_id`, `dst_kind` / `dst_id` (source, segment or entity), `relation`, `confidence`, `attributes` (the reason: shared entities, similarity).

| Relation | Between | Created by |
|---|---|---|
| `mentions` | segment → entity | ingestion |
| `next` | segment → next segment in the same source | ingestion |
| `spoken_by` | speech segment → speaker entity | ingestion |
| `co_occurs` | windows of the same video scene | linker |
| `shows_same` | a slide or frame shown again later in the same recording | linker |
| `depicts` | a visual segment → a speech or text segment in another source about the same thing | linker |
| `corroborates` | two segments in different sources stating the same thing | linker |
| `same_as` | entity ↔ entity with different surface forms | linker |

### Tables
`sources`, `segments`, `entities`, `relations`, `jobs`, `schema_meta`, and the FTS5 table `segments_fts` (one row per segment: text, visual summary, OCR text, entity and speaker names). Indexes on source hash, segment source/time and modality, relation source and destination, job status.

## 5. Ingestion

Every upload goes through `_upload.py`: size check against `MAX_UPLOAD_MB` (413), content hash lookup (an existing identical source is returned unless `force=true`), staging under `data/uploads/`, then the pipeline either inline or as a background job (`background=true`, returns 202 with a job ID).

### Pipeline (`services/pipeline.py`)
1. Run the modality's processor. Derived files go to `data/derived/{source_id}/`.
2. Attribute speech to speakers (audio and video, needs Gemini).
3. Extract typed entities from spoken and written text.
4. Hand the nodes to `ingestion.py`, which writes the source, segments, entities, `mentions`, `next` and `spoken_by` edges in one transaction, indexes the segments in Chroma and refreshes FTS5.
5. Run the linker for the new source.

Enrichment failures (steps 2 and 3) are stored as warnings on the source and never fail the upload.

### Video (`video_processor.py`, `scene_detector.py`)
1. Transcribe the audio track into timestamped speech segments with confidence.
2. Sample a frame every `SCENE_SAMPLE_SECONDS` (0.5 s) and compare it with the current scene's anchor frame. A new scene starts when the changed-pixel fraction exceeds `SCENE_CUT_THRESHOLD` (0.04) and the change lasts at least `SCENE_MIN_SECONDS` (2 s).
3. Split scenes longer than `WINDOW_MAX_SECONDS` (30 s) into windows at sentence boundaries, so no sentence is cut.
4. Save one keyframe per scene and describe it with Gemini (description, text blocks with boxes, regions, entities). A keyframe within `KEYFRAME_DUP_HAMMING` (6) bits of an earlier one by perceptual hash reuses that analysis. Vision calls run on `VISION_WORKERS` (4) threads.
5. Emit one segment per window with its speech, its speech spans, the keyframe and its analysis.

### Audio (`audio_processor.py`)
Groq Whisper large-v3 when `GROQ_API_KEY` is set, otherwise local faster-whisper. One segment per speech span with timestamps and confidence.

### Image (`image_processor.py`)
One Gemini call returns a description, the OCR text, text blocks and visual regions with normalised bounding boxes, and typed entities. PNG and JPEG only.

### PDF (`pdf_processor.py`)
One segment per page. The exact text layer comes from the PDF. With Gemini, the rendered page is analysed for diagrams, charts and text blocks, and embedded images are saved.

### JSON and text (`json_processor.py`)
A JSON array of records (or one object) becomes one segment per record, using `text`, `content` or `transcript`, plus optional `visual_summary`, `locator`, `source` and `entities`. A `.txt` file becomes one note.

### Jobs (`services/jobs.py`)
A `JOB_WORKERS` (2) thread pool runs queued jobs and records `stage`, `progress`, warnings and the result in the `jobs` table. Jobs that were running when the server stopped are re-queued on startup.

## 6. Cross-modal and temporal linking (`services/linker.py`)

After a source is ingested, each of its segments is compared with the rest of the knowledge base using two signals:

1. **Shared entities** after `same_as` resolution (plurals and separators normalised).
2. **Embedding similarity** between the two segments' full documents, from the vector index (`CANDIDATES_FROM_INDEX` = 8 nearest).

| Condition | Threshold |
|---|---|
| Enough shared entities | `MIN_SHARED_ENTITIES` = 2 |
| One shared entity plus moderate similarity | `SIMILARITY_WITH_ENTITY` = 0.42 |
| Similarity alone | `SIMILARITY_ALONE` = 0.62 |
| Links kept per segment | `MAX_LINKS_PER_SEGMENT` = 5 |

Visual to text across sources becomes `depicts`. Text to text or visual to visual across sources becomes `corroborates`. Within a recording, windows of one scene get `co_occurs` and repeated keyframes get `shows_same`. Each edge stores its shared entities and similarity. `POST /graph/relink` or `python -m app.cli relink` recomputes every link.

## 7. Retrieval (`services/retrieval.py`)

`retrieve(query, limit, hybrid, expand, decompose_query, modalities)` returns ranked hits and a description of what it did.

1. **Decompose.** The question is split at clause boundaries that start a new question ("…, who explained it, and where was it shown?"). Parts under 3 words merge back. A later part that shares no content word with the first part gets the first part's topic appended, so "what were the results?" stays on topic.
2. **Search three ways** for the full question and each part, 30 candidates each:
   - **dense:** cosine similarity over text + visual descriptions (Chroma `segments` collection)
   - **keyword:** BM25 over `segments_fts` (catches `max_retries=5`, `ticket #4471`)
   - **entity:** segments that mention an entity named in the question, singular and plural, including `same_as` variants
3. **Fuse** each run with reciprocal rank fusion (k = 60, weights dense 1.0, keyword 1.0, entity 0.8). Then fuse the runs: the full question has weight 1, and the parts share `SUBQUERY_SHARE` = 0.5 of a vote, so a thin part cannot outvote the question.
4. **Use the graph.** The top `EXPANSION_SEEDS` = 5 hits are seeds. A candidate linked to a seed by `depicts`, `co_occurs`, `shows_same` or `corroborates` gains `LINK_BOOST` = 0.05 of the seed's decayed score (decay 0.75, 0.7, 0.6, 0.6 times the link's confidence). A segment found only through a link enters with the decayed score and records `via`: the relation, the seed and the shared entities.
5. **Weight by confidence:** `score × (0.6 + 0.4 × confidence)`.
6. **Select:**
   - at most 2 hits per source when `limit` ≤ 5, otherwise 3
   - each part's best hit gets a slot first, if it also ranks in the full question's top `COVERAGE_POOL` = 12 candidates (this keeps decoys out)
   - the rest fill by score
7. **Explain.** Each hit carries `score`, `signals` (dense, keyword and entity ranks, keyword score, matched entities, link boosts), `via`, `matched_span` (the best sentence in a long window, with its own timestamp and speaker) and `matched_regions` (the OCR blocks that match, with boxes).

The text-only baseline (`search_text_only`) uses the `segments_text_only` collection, which embeds text without any visual description.

## 8. Answers (`services/answer_synthesizer.py`)

The top hits are formatted as numbered evidence blocks (source, modality, locator, speaker, text, what was shown, matched span) and sent to Gemini with the sub-questions. The model must cite evidence by number and say when the evidence does not support an answer. Without a key, or if the call fails, an extractive summary of the top evidence is returned with `method: "extractive"`, and `answer.sources` still lists each cited piece.

## 9. Evaluation (`app/evaluation/`)

| Part | Detail |
|---|---|
| Gold set | `datasets/demo.json`: 24 questions. Each lists `required` evidence (source plus page, time range, label or the whole file), `also_relevant` evidence and `expected_terms` |
| Question types | `cross_modal` (4), `multi_part` (2), `visual_only` (6), `exact_identifier` (6), `text` (6) |
| Distractors | `datasets/distractors.json`: look-alike sources (another postmortem with `max_retries=3`, an older onboarding spec, unrelated dashboards, tickets and recordings). 31 segments in total |
| Matching | A hit matches a reference when the filename matches and the page, label or time range overlaps |
| Isolation | Each run builds a temporary SQLite database and Chroma index, loads the sample corpus and the distractors, and deletes them after |
| Systems | `text_rag` (text-only embeddings), `dense_multimodal` (text + visual embeddings), `hybrid` (dense + keyword + entity, no decomposition or graph), `weft` (full retrieval) |
| Metrics | Recall@k, Complete@k (every required piece found), MRR, nDCG@k, precision@k, modality coverage, p50 latency. With `--answers`, answer term coverage for the baseline and Weft |

```bash
python -m app.evaluation run --k 5 --markdown EVALUATION.md
python -m app.evaluation run --k 3 --systems text_rag weft
python -m app.evaluation run --answers
python -m app.evaluation list
```

Reports are saved to `data/eval/latest.json`. Reports made with the hashing embedding used in tests are labelled as proxies in the UI.

### Current results (MiniLM embeddings)

| System | Recall@3 | Complete@3 | MRR@3 | Recall@5 | Complete@5 | MRR@5 |
|---|---|---|---|---|---|---|
| Text-only RAG | 68% | 58% | 0.77 | 78% | 71% | 0.79 |
| Dense, text + visual | 79% | 67% | 0.85 | 92% | 83% | 0.86 |
| Hybrid | 82% | 71% | 0.85 | 94% | 83% | 0.86 |
| **Weft** | **87%** | **79%** | **0.86** | **95%** | **88%** | **0.86** |

Complete@5 by type: visual-only 100% (text-only 50%), exact identifier 100%, multi-part 100% (all others 0%), text 100%, cross-modal 25% (hybrid 50%, dense 75%). The full report is in [EVALUATION.md](../EVALUATION.md).

## 10. HTTP API

JSON in and out except uploads (multipart, field `file`). Errors return `{ "detail": ... }` with the status code. Full schemas at `/docs`.

### Uploads
| Method | Path | Purpose |
|---|---|---|
| POST | `/upload/video` | MP4 |
| POST | `/upload/audio` | MP3, WAV, M4A, AAC, FLAC, OGG |
| POST | `/upload/image` | PNG, JPEG |
| POST | `/upload/pdf` | PDF |
| POST | `/upload/json` | JSON or plain text |

Query parameters on every upload: `background` (return 202 with a job), `force` (re-ingest a duplicate), `recorded_at` (ISO time the recording or document was made, used for timelines). 413 when the file is too large, 422 when it can't be processed.

### Query
| Method | Path | Purpose |
|---|---|---|
| POST | `/query` | `{ query, limit, hybrid, expand, decompose, modalities }` → answer, hits with explanations, sub-questions, strategy |
| POST | `/query/compare` | Weft results next to the text-only baseline for the same question |

### Knowledge
| Method | Path | Purpose |
|---|---|---|
| GET | `/stats` | Counts of sources, segments, entities and relations by modality and type |
| GET | `/sources` | Sources with segment counts and media URLs |
| GET | `/sources/{id}` | One source with all its segments |
| DELETE | `/sources/{id}` | Remove a source, its segments, links and index entries |
| GET | `/segments/{id}` | One segment with its source, entities and every link |
| GET | `/entities?q=&limit=` | Entities with mention, source and modality counts |
| GET | `/entities/{id}` | Every segment mentioning an entity |
| GET | `/entities/{id}/timeline` | Mentions of an entity and its aliases ordered by `recorded_at` and position |
| GET | `/graph/{node_id}?depth=1` | Neighbourhood of a segment or entity: nodes and typed, weighted edges |
| POST | `/graph/relink` | Recompute all links |
| POST | `/demo/seed` | Load the sample corpus (disabled with `ENABLE_DEMO=false`) |

### Jobs, evaluation, system
| Method | Path | Purpose |
|---|---|---|
| GET | `/jobs`, `/jobs/{id}` | Background jobs: status, stage, progress, warnings, result |
| GET | `/eval/datasets` | Available gold sets and systems |
| GET | `/eval/latest` | Latest report (404 if none) |
| POST | `/eval/run` | `{ dataset, k, systems }`, runs and saves a report |
| GET | `/health` | `{ status, version, providers, demo_enabled }`. Providers say which models are configured, never the keys |

Static mounts: `/static` (website), `/uploads`, `/derived` and `/frames` (media and derived artefacts).

## 11. Configuration

All settings come from environment variables, or `.env` in the project root. `app/config.py` reads them on every call, so tests can override them.

| Key | Default | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | | Vision, entities, speakers, answers |
| `GEMINI_MODEL` | `gemini-3.6-flash` | Gemini model for every call |
| `GROQ_API_KEY` | | Whisper transcription. Without it, local faster-whisper |
| `MEDIA_STORAGE_ROOT` | `./data` | Uploads, derived files, evaluation reports |
| `KNOWLEDGE_DB_PATH` | `data/knowledge.db` | SQLite database |
| `CHROMA_PATH` | `./chroma_db` | Vector index |
| `SCENE_SAMPLE_SECONDS` | `0.5` | Frame sampling interval |
| `SCENE_CUT_THRESHOLD` | `0.04` | Changed-pixel fraction that starts a scene |
| `SCENE_MIN_SECONDS` | `2` | Shorter changes are treated as transitions |
| `WINDOW_MAX_SECONDS` | `30` | Long scenes split at sentence boundaries |
| `KEYFRAME_DUP_HAMMING` | `6` | Perceptual-hash distance for repeated keyframes |
| `VISION_WORKERS` | `4` | Parallel vision calls per video |
| `ENABLE_ENTITY_EXTRACTION` | `true` | Typed entity extraction |
| `ENABLE_SPEAKER_ATTRIBUTION` | `true` | Speaker labels for speech |
| `ENTITY_BATCH_SIZE` | `25` | Segments per entity extraction call |
| `JOB_WORKERS` | `2` | Background ingestion workers |
| `MAX_UPLOAD_MB` | `500` | Upload size limit |
| `ENABLE_DEMO` | `true` | Allow `POST /demo/seed` |

## 12. Local development

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env                 # add GEMINI_API_KEY and GROQ_API_KEY (optional)
uvicorn main:app --reload            # http://127.0.0.1:8000
```

Open `http://127.0.0.1:8000/app` and choose **Load sample data** in the Library, or call `POST /demo/seed`. The first run downloads the MiniLM embedding model (about 80 MB) into `~/.cache/chroma`.

| Command | Does |
|---|---|
| `python -m app.cli stats` | Counts per table, modality and relation |
| `python -m app.cli reindex` | Rebuild the Chroma index from SQLite |
| `python -m app.cli relink` | Recompute cross-modal and temporal links |
| `python -m app.cli import-legacy` | Move pre-SQLite Chroma data into SQLite |
| `python -m app.evaluation run` | Run the gold set (see section 9) |
| `pytest tests` | All tests |

## 13. Testing

- **Runner:** pytest. API tests use FastAPI's TestClient against the real app with a temporary database and index.
- **No network, no keys.** Tests use a deterministic hashing embedding instead of MiniLM and stub every Gemini and Groq call.
- **Suites:**
  - `test_scene_detector`: scene cuts, windows, sentence alignment
  - `test_extraction`, `test_enrichment`: processors, entity extraction, speaker attribution
  - `test_knowledge_store`: repository, transactions, FTS5
  - `test_chroma_index`: index writes, searches, rebuilds
  - `test_upload_jobs`: uploads, deduplication, size limit, background jobs and resume
  - `test_linker`: each link type, thresholds, `same_as`
  - `test_retrieval`: decomposition, fusion, exact identifiers, expansion, spans and regions
  - `test_knowledge_api`: every knowledge endpoint
  - `test_evaluation`: evidence matching, metrics, dataset validity, end-to-end ranking, the eval API
- **Retrieval changes** are checked with `python -m app.evaluation run` at k = 3 and k = 5 with the real embedding model before they are kept.

## 14. Known limitations

| Limitation | Effect | Planned fix |
|---|---|---|
| Vision needs Gemini | Without a key, image uploads are refused and slides and pages have no description or OCR, so visual evidence can't be found | Local OCR fallback |
| Cross-modal Complete@5 is 25% vs 50% for hybrid | One cross-modal question in four loses a required piece to a related ticket | Modality-aware selection, better linker coverage |
| Small gold set on sample data | Numbers may not transfer to other corpora | Second gold set from real recordings, evaluation on user corpora |
| Speaker names come from a model | Possible misattribution | Names only when the recording reveals them, lower confidence |
| No authentication | Anyone who can reach the port can read and upload | Run locally, or put it behind a reverse proxy with auth. API keys planned |
| Single process, SQLite, in-process workers | Not built for many concurrent users | Server database and a job queue for shared deployments |
| No graph view in the UI | Links are visible per segment, not as a graph | Graph view (PRD FR22) |
| Fonts from Google Fonts | The website needs network for its fonts, and falls back to system fonts offline | Self-host the fonts |
