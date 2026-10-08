# Weft: Product Requirements Document

| | |
|---|---|
| **Product** | Weft, multimodal data pipeline for RAG |
| **Owner** | Priyansh Dugar |
| **Status** | v0.2, runs locally. v0.3 planned |
| **Last updated** | October 2026 |

Related: [PRODUCT.md](./PRODUCT.md) (overview) · [TECHNICAL.md](./TECHNICAL.md) (implementation) · [DESIGN.md](./DESIGN.md) (interface)

---

## 1. Problem

RAG systems are usually built over text: transcripts, document text and chat logs. In real teams a large part of the knowledge is not text. It is the slide on screen during an incident review, the diagram on page 3 of a spec, the dashboard screenshot that proves a fix worked. A text-only pipeline never sees it, so it either cannot answer or answers without evidence.

Even when every format is indexed, the evidence for one answer is often split across them. The explanation is in a recording, the exact setting is in a PDF and the result is in a screenshot. Retrieving each one separately does not connect them, and an answer without a precise source cannot be checked.

Weft ingests every format into one evidence store with exact locations, links related evidence across formats and time, retrieves across all of it, and measures the result against a text-only baseline.

## 2. Goals

| # | Goal | How we know |
|---|---|---|
| G1 | Every format a team produces can be ingested without manual preprocessing | Video, audio, image, PDF and JSON uploads each produce located segments (tests per processor) |
| G2 | Every retrieved piece of evidence can be traced to its exact location | Each segment has a locator (time range, page, region or record) and the UI opens it there |
| G3 | Evidence that belongs together is linked across formats | Linker tests, plus links visible on segments and used in retrieval |
| G4 | Retrieval beats text-only RAG on questions whose evidence is not only text | Evaluation on the gold set: Complete@5 and modality coverage above the text-only baseline |
| G5 | The system runs offline for grading and CI | All tests pass with no API keys. Answers fall back to an extractive summary |

## 3. Non-goals

- Multi-user accounts, permissions or a hosted service
- Live meeting capture or streaming transcription
- Editing, redacting or exporting the original files
- Training or fine-tuning models
- A general-purpose chatbot that answers without evidence

## 4. Users

| Persona | Description | Main needs |
|---|---|---|
| **Riya, backend engineer** | Owns checkout. Incident reviews are recorded, postmortems are PDFs, results are Grafana screenshots | "What did we change and how do we know it worked?", with the clip, the page and the chart |
| **Arjun, product manager** | Runs design reviews and A/B tests. Specs, walkthrough videos, survey exports | Why a flow was redesigned, what users said, what the numbers showed |
| **Meera, researcher** | Works through recorded interviews and scanned reports | Answers she can cite to a timestamp or page |
| **Dev, RAG developer** | Building retrieval for his own product | A reference pipeline and an evaluation he can run on his data |

## 5. User stories and acceptance criteria

### Ingestion

**I1. As a user, I can upload a video and get searchable, located evidence.**
- MP4 is accepted. The audio is transcribed with timestamps and a confidence per segment.
- The video is cut into scenes where the picture changes, not into fixed chunks. A scene longer than 30 seconds is split at a sentence boundary.
- Each scene keeps its speech and a description of the keyframe, with text blocks and regions located on the frame.
- A keyframe that repeats an earlier one (the same slide again) reuses its analysis and is linked to it.

**I2. As a user, I can upload audio** (MP3, WAV, M4A, AAC, FLAC, OGG) and get timestamped speech segments with speakers labelled where Gemini is configured.

**I3. As a user, I can upload an image** (PNG, JPEG) and get a description, its text as located blocks, visual regions and typed entities.

**I4. As a user, I can upload a PDF** and get one segment per page with the exact text layer, plus a description of the rendered page and its embedded images.

**I5. As a user, I can upload JSON or plain text.** A JSON array of records becomes one segment per record, using `text` / `content` / `transcript`, and an optional `locator`, `source`, `entities` and `visual_summary`.

**I6. As a user, long uploads don't block me.** With background mode the upload returns at once and a job reports its stage and progress. Jobs interrupted by a restart are resumed.

**I7. As a user, uploading the same file twice does nothing** unless I force it. Files over the size limit are refused with a clear message.

**I8. As a user, I can delete a source**, which removes its segments, links and index entries.

### Knowledge model

**K1. Every segment records** its source, modality, locator, text, visual description, extractor and confidence.

**K2. Entities are typed** (person, speaker, system, component, product, metric, organization, concept, event, location), and surface variants of the same entity are resolved as one.

**K3. Segments are linked** by `next` (order in a source), `co_occurs` (same scene), `shows_same` (repeated slide), `depicts` (a visual illustrating a statement in another source), `corroborates` (two sources stating the same thing) and `spoken_by` (speaker). Every link records why it exists.

**K4. As a user, I can see an entity's timeline**: every mention across files, ordered by recording time and position.

### Retrieval and answers

**Q1. As a user, I can ask a question and get ranked evidence from every format.**
- Semantic, keyword and entity search are combined.
- An exact identifier in the question (`max_retries=5`, `ticket #4471`) finds the segment that contains it.

**Q2. Multi-part questions are answered completely.** "What was the problem, what replaced it, and what were the results?" is split into parts, and each part's best evidence is kept in the results.

**Q3. Linked evidence is used.** A segment linked to a top result (for example a chart that confirms a statement) is ranked up, and the result says which link brought it in.

**Q4. Each result explains itself**: which searches matched it, the sentence or text region that matched, its speaker, and its confidence.

**Q5. The answer cites its evidence** with numbered references and says plainly when the evidence does not support an answer. Without a Gemini key, an extractive summary is returned.

**Q6. As a user, I can compare** Weft's results with a text-only baseline for the same question.

**Q7. As a user, I can open any citation** at its location: video from the cited second, PDF at the page, image with the region highlighted.

### Evaluation

**E1. As a developer, I can run the evaluation from the CLI or the website** and get Recall@k, Complete@k, MRR, nDCG@k, precision@k, modality coverage and latency for four systems.

**E2. Results are broken down by question type** (cross-modal, multi-part, visual-only, exact identifier, text) and per question, showing which required evidence each system found or missed.

**E3. The evaluation never touches my library.** It builds its own temporary corpus every run.

## 6. Functional requirements

| ID | Requirement | Priority | Status |
|---|---|---|---|
| FR1 | Upload endpoints for video, audio, image, PDF and JSON/text | Must | Done |
| FR2 | Scene-based video segmentation with sentence-aligned windows | Must | Done |
| FR3 | Transcription with timestamps and confidence (Groq Whisper, local fallback) | Must | Done |
| FR4 | Vision analysis with located text blocks and regions | Must | Done |
| FR5 | Page-wise PDF extraction (text layer + rendered page) | Must | Done |
| FR6 | Background jobs with progress and resume | Should | Done |
| FR7 | Content-hash deduplication and upload size limit | Should | Done |
| FR8 | SQLite system of record, rebuildable vector index | Must | Done |
| FR9 | Typed entity extraction (LLM, with pattern fallback) and alias resolution | Must | Done |
| FR10 | Speaker attribution for speech | Should | Done |
| FR11 | Cross-modal and temporal linking with recorded reasons | Must | Done |
| FR12 | Entity timelines and graph neighbourhood API | Should | Done |
| FR13 | Hybrid retrieval: dense + BM25 + entity, rank fusion | Must | Done |
| FR14 | Query decomposition with per-part coverage | Should | Done |
| FR15 | Link-based reinforcement and expansion of results | Should | Done |
| FR16 | Matched span and matched regions on every result | Should | Done |
| FR17 | Grounded answer with citations, offline fallback | Must | Done |
| FR18 | Text-only baseline comparison endpoint | Must | Done |
| FR19 | Gold-set evaluation: CLI, API and Evaluate page | Must | Done |
| FR20 | Website: landing, Ask, Library, source view, Entities, Evaluate | Must | Done |
| FR21 | Sample data loader | Should | Done |
| FR22 | Interactive graph view of a segment or entity | Should | **Open** (v0.3) |
| FR23 | Streaming answers | Could | **Open** (v0.3) |
| FR24 | Docker image and CI pipeline | Should | **Open** (v0.3) |
| FR25 | API authentication for shared deployments | Could | Open (later) |
| FR26 | Evaluation on a user's own corpus with their own gold set | Could | Open (later) |

## 7. Non-functional requirements

| Area | Requirement |
|---|---|
| **Provenance** | Every segment has a locator, an extractor and a confidence. Every link stores its reason. Answers cite segments by number |
| **Integrity** | A source, its segments, entities and links are committed in one SQLite transaction. The vector index is keyed by segment ID and can be rebuilt from SQLite |
| **Resilience** | Enrichment failures (speakers, entities) never fail an upload. They are stored as warnings on the source. Missing API keys degrade video, PDF, entity and answer features without failing them. Image uploads need Gemini |
| **Privacy** | Files and derived artefacts stay on local disk. Only content sent to Gemini or Groq leaves the machine, and only when their keys are set. `/health` reports which providers are on, never the keys |
| **Performance** | Retrieval p50 under 1 s on the sample corpus on a laptop. Long uploads run in the background. Repeated keyframes skip the vision call |
| **Quality** | Weft must score at or above text-only RAG on every overall metric of the gold set, and changes to retrieval are judged on the evaluation |
| **Accessibility** | Keyboard navigation, visible focus, labelled controls, readable contrast on the dark theme, layouts from 360 px wide |
| **Maintainability** | Configuration comes only from environment variables. Tests run without network or keys. No secrets in the repo |

## 8. Success metrics

Measured on the bundled gold set with the MiniLM embedding model, top 5.

| Metric | Target | Current |
|---|---|---|
| Complete@5 (all required evidence found) | Above text-only RAG | 88% vs 71% |
| Recall@5 | Above text-only RAG | 95% vs 78% |
| Modality coverage | Above text-only RAG | 95% vs 79% |
| Visual-only questions, Complete@5 | 100% | 100% |
| Exact-identifier questions, Complete@5 | 100% | 100% |
| Cross-modal questions, Complete@5 | At or above hybrid | 25% vs 50% (below target) |
| Retrieval p50 latency | Under 1 s | about 0.6 s |

## 9. Release plan

| Version | Scope |
|---|---|
| **v0.2 (current)** | Everything marked Done above |
| **v0.3** | Graph view (FR22), streaming answers (FR23), Docker image and CI (FR24), cross-modal retrieval on a par with hybrid |
| **Later** | API authentication (FR25), evaluation on user corpora (FR26), larger embedding models, a second gold set from real recordings |

## 10. Risks and open questions

| Risk | Impact | Mitigation |
|---|---|---|
| Vision and transcription quality depend on external models | Wrong descriptions or transcripts become wrong evidence | Confidence on every segment, confidence-weighted ranking, citations so the user can check |
| No API keys configured | No vision (image uploads refused), transcription by local model only, extractive answers | Video, audio, PDF and JSON still ingest. `/health` and the workspace show which providers are on |
| The gold set is small (24 questions) and built on sample data | Tuning can overfit, numbers may not transfer to real data | Hard distractors, results at k=3 and k=5, changes kept only when they hold at both. FR26 for user corpora |
| Cross-modal questions at top 5 still trail hybrid by one question | The headline use case is not yet the strongest | Tracked in section 8. Next: modality-aware selection and better linker coverage |
| Speaker names are inferred by a model | A misattributed quote | Names are used only when the recording reveals them. Otherwise "Speaker 2". Lower confidence than the transcript |
| Single-process SQLite and in-process job workers | Not built for many concurrent users | Fine for local and team use. A shared deployment would need a server database and a job queue |
