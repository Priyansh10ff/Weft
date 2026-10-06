"""Retrieval metrics over ranked evidence lists (binary relevance)."""

from __future__ import annotations

import math


def recall_at_k(found_required: set[int], required_count: int) -> float:
    return len(found_required) / required_count if required_count else 0.0


def complete_at_k(found_required: set[int], required_count: int) -> float:
    """1.0 only when *every* required piece of evidence was retrieved."""
    return 1.0 if required_count and len(found_required) == required_count else 0.0


def reciprocal_rank(relevant_ranks: list[int]) -> float:
    """1 / rank of the first relevant hit (ranks are 1-based)."""
    return 1.0 / min(relevant_ranks) if relevant_ranks else 0.0


def ndcg_at_k(relevant_flags: list[bool], ideal_relevant: int, k: int) -> float:
    dcg = sum(1.0 / math.log2(i + 2) for i, rel in enumerate(relevant_flags[:k]) if rel)
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(ideal_relevant, k)))
    return dcg / ideal if ideal else 0.0


def precision_at_k(relevant_flags: list[bool], k: int) -> float:
    return sum(relevant_flags[:k]) / k if k else 0.0


def term_coverage(answer: str, terms: list[str]) -> float | None:
    if not terms:
        return None
    text = answer.casefold()
    return sum(1 for t in terms if t.casefold() in text) / len(terms)


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0
