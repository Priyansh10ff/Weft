# Evaluation

Dataset **demo**: 24 questions over a 31-segment corpus (demo+distractors), top-5 retrieval, embedding `default`, generated 2026-10-08T09:58:37Z.

Questions over the bundled sample corpus (checkout-timeout incident and onboarding redesign). Evidence is deliberately split across video speech, video slides, PDF pages, screenshots and JSON records; several answers exist only in what was shown on screen.

| System | Recall@5 | Complete@5 | MRR | nDCG@5 | Modality coverage | p50 latency |
| --- | --- | --- | --- | --- | --- | --- |
| Text-only RAG (baseline) | 78% | 71% | 0.79 | 0.67 | 79% | 562.7 ms |
| Dense, text + visual | 92% | 83% | 0.86 | 0.81 | 91% | 560.6 ms |
| Hybrid (dense + keyword + entity) | 94% | 83% | 0.86 | 0.84 | 94% | 587.6 ms |
| Weft (hybrid + decomposition + graph) | 95% | 88% | 0.86 | 0.85 | 95% | 624.8 ms |

**Weft vs text-only RAG:** Recall@5 +17 pts, Complete@5 +17 pts, MRR +0.07, modality coverage +16 pts.

## Complete@5 by question type

| Type | n | Text-only RAG (baseline) | Dense, text + visual | Hybrid (dense + keyword + entity) | Weft (hybrid + decomposition + graph) |
| --- | --- | --- | --- | --- | --- |
| cross_modal | 4 | 50% | 75% | 50% | 25% |
| exact_identifier | 6 | 100% | 100% | 100% | 100% |
| multi_part | 2 | 0% | 0% | 0% | 100% |
| text | 6 | 100% | 83% | 100% | 100% |
| visual_only | 6 | 50% | 100% | 100% | 100% |

Metrics: **Recall@k** share of required evidence retrieved; **Complete@k** share of questions where *all* required evidence (often spread over several modalities) was retrieved; **MRR** reciprocal rank of the first relevant hit; **nDCG@k** ranking quality; **modality coverage** share of the modalities a correct answer needs that appear among the retrieved required evidence.

Regenerate with `python -m app.evaluation run --markdown EVALUATION.md`.
