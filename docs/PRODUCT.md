# Weft

Ask a question across your videos, recordings, PDFs, screenshots and records, and get an answer that points to the exact moment, page or record it came from.

Requirements in [PRD.md](./PRD.md) · Implementation in [TECHNICAL.md](./TECHNICAL.md) · Interface in [DESIGN.md](./DESIGN.md)

---

## What it is

Weft is a multimodal data pipeline for retrieval-augmented generation (RAG). It ingests video, audio, images, PDFs and JSON, breaks each file into small pieces of evidence, and records where every piece came from: a time range in a recording, a page in a PDF, a region of a screenshot, a record in a JSON file.

It then links those pieces to each other across files and formats. The slide that was on screen is linked to what was said over it. A chart in a screenshot is linked to the sentence in a postmortem that states the same number. The same system, metric or person mentioned in a video, a document and a ticket becomes one entity.

When you ask a question, Weft searches all of it at once, follows those links, and answers with citations you can open: the video starts at the right second, the PDF opens on the right page, the matching text on a slide is highlighted.

## Why it exists

- **Most knowledge in a team is not in text.** Decisions are made in meetings, architectures are drawn on slides, results are shown as dashboards and screenshots. A text-only RAG system reads transcripts and document text and never sees any of it.
- **The answer is often split across formats.** "How did we fix the checkout timeout, and how do we know it worked?" needs the explanation from a recording, the policy from a postmortem and the latency drop from a dashboard screenshot. Searching each format separately does not connect them.
- **An answer without a source cannot be trusted.** A summary that says "the team switched to exponential backoff" is only useful if you can see who said it, when, and what was on screen at the time.
- **Exact identifiers get lost in embeddings.** `max_retries=5`, `ticket #4471` or a version number look alike to a semantic search. They need exact matching alongside it.
- **"Better than text RAG" should be measured, not claimed.** Weft ships with a gold question set and an evaluation that compares it with a text-only baseline on the same corpus.

## Who it is for

**Primary: engineering and product teams**
Teams whose decisions live in incident reviews, design reviews, stand-ups, specs and dashboards, and who need to find what was decided, by whom, and on what evidence.

**Secondary: researchers and analysts**
People working through recorded interviews, lectures, scanned reports and screenshots, who need answers they can cite back to the original.

**Also: builders of RAG systems**
Developers who want a reference for multimodal ingestion, provenance and cross-modal linking, with an evaluation harness they can point at their own data.

**Who it is not for**
Weft is not a hosted SaaS, a video editor, a general chatbot or a replacement for a document management system. It does not edit or redistribute your files.

## How it works

1. **Upload files** to the Library: MP4 video, MP3/WAV/M4A audio, PNG/JPEG images, PDFs, JSON or plain text. Large files run as background jobs with live progress.
2. **Weft breaks each file into evidence.** A video is cut into scenes where the picture changes, and each scene keeps the speech said over it and a description of what was shown. A PDF becomes one piece per page. An image keeps its text blocks and their positions. A JSON file becomes one piece per record.
3. **It names the things in it.** People, systems, components, metrics and concepts are extracted, and speakers in recordings are labelled. Different spellings of the same thing become one entity.
4. **It links evidence across files.** Pieces that show or state the same thing are connected, with the reason recorded (shared entities, similarity, same slide shown again).
5. **Ask a question.** Weft splits multi-part questions, searches by meaning, by exact keyword and by entity, follows the links, and ranks the evidence.
6. **Read the answer with its sources.** Every claim cites a numbered piece of evidence. Opening it plays the video from that second, opens the page, or highlights the matching region of an image.
7. **Check the numbers.** The Evaluate page runs the gold question set and shows how Weft compares with text-only RAG, question by question.

## Core features

**Ingestion**
- Five input types: video, audio, image, PDF and JSON or text
- Scene detection for video instead of fixed-length chunks, with long scenes split at sentence boundaries
- Speech transcription with timestamps and per-segment confidence
- Vision analysis of keyframes, images and PDF pages: description, text blocks and visual regions with bounding boxes
- Duplicate keyframes (the same slide shown again) reuse their analysis
- Background jobs with stage and progress, resumed after a restart
- Duplicate uploads detected by content hash

**Knowledge model**
- Every piece of evidence keeps its exact location, the extractor that produced it and a confidence score
- Typed entities and speaker attribution
- Typed links between evidence: shown at the same time, the same slide again, a visual that illustrates a statement, two sources that agree, consecutive pieces
- Timelines of every mention of an entity across files

**Retrieval and answers**
- Hybrid search: semantic, keyword (catches exact identifiers) and entity matching, fused into one ranking
- Multi-part questions split into sub-questions so every part gets evidence
- Evidence pulled in through cross-modal links
- Each result explains why it was returned and where exactly the match is
- Grounded answers with numbered citations, or an extractive summary when no model key is configured

**Website**
- Landing page and a workspace with Ask, Library, Entities and Evaluate
- Evidence drawer that plays video from the cited second and shows the cited page or image region
- Side-by-side comparison with a text-only baseline for any question
- One-click sample data

**Evaluation**
- 24 questions with known evidence, plus look-alike decoy files
- Four systems compared on the same corpus, from text-only RAG to full Weft
- Recall, completeness, rank and modality coverage, overall and per question type

## Principles

- **Every answer points to its source.** If Weft cannot cite it, it says so.
- **What was shown counts as much as what was said.** Visual content is evidence, not decoration.
- **The database is the record, the index is disposable.** All evidence lives in SQLite. The vector index can be rebuilt from it at any time.
- **Confidence is explicit.** Machine-extracted content carries a confidence score, and weak extraction ranks lower.
- **Measure before claiming.** Retrieval changes are judged by the evaluation, not by a single good-looking query.

## Out of scope

- Hosting, accounts and multi-user access control (Weft runs locally for one user or team)
- Live meeting capture or real-time transcription
- Editing, redacting or exporting the original files
- Languages other than those the configured models support
- Fine-tuning or training models

## Tech at a glance

| Layer | Choice |
|---|---|
| API and server | Python, FastAPI, Uvicorn |
| System of record | SQLite (WAL) with FTS5 keyword search |
| Vector index | ChromaDB with the MiniLM-L6 embedding model |
| Vision, entities, speakers, answers | Google Gemini |
| Transcription | Groq Whisper, with local faster-whisper as fallback |
| Video and documents | OpenCV, PyMuPDF, pypdf, Pillow |
| Frontend | Plain HTML, CSS and ES modules served by FastAPI |
