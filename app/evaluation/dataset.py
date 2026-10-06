"""Gold question sets and evidence matching.

A question lists the evidence a correct answer *requires* and evidence that
is *also relevant*. Evidence is referenced by source filename plus an
optional locator, so the same gold set works whatever segment boundaries
the pipeline produced:

* ``"Page 3"``             matches a segment on page 3,
* ``"00:12-00:25"``        matches a segment whose time range overlaps it,
* ``"00:12"``              matches a segment containing that moment,
* ``"ticket-4471"``        matches a segment with that label,
* no locator               matches any segment of that source.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

DATASET_DIR = Path(__file__).resolve().parent / "datasets"

_CLOCK = r"(\d{1,2}(?::\d{2}){1,2})"
_RANGE = re.compile(rf"^\s*{_CLOCK}\s*(?:-|–|to)\s*{_CLOCK}\s*$")
_POINT = re.compile(rf"^\s*{_CLOCK}\s*$")
_PAGE = re.compile(r"^\s*page\s*(\d+)\s*$", re.IGNORECASE)


def _seconds(text: str) -> float:
    total = 0.0
    for part in text.split(":"):
        total = total * 60 + float(part)
    return total


class EvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    locator: str | None = None

    def key(self) -> str:
        return f"{self.source}#{self.locator or '*'}"

    def matches(self, filename: str | None, locator: dict[str, Any] | None) -> bool:
        if not filename or filename.casefold() != self.source.casefold():
            return False
        if not self.locator:
            return True
        loc = locator or {}
        if match := _PAGE.match(self.locator):
            return loc.get("page_number") == int(match[1])
        start, end = loc.get("start_seconds"), loc.get("end_seconds")
        if match := _RANGE.match(self.locator):
            lo, hi = _seconds(match[1]), _seconds(match[2])
            if start is None:
                return False
            seg_end = end if end is not None else start
            overlap = min(hi, seg_end) - max(lo, start)
            return overlap >= min(1.0, (hi - lo) / 2) or (lo <= start < hi)
        if match := _POINT.match(self.locator):
            t = _seconds(match[1])
            return start is not None and start <= t <= (end if end is not None else start)
        return (loc.get("label") or "").casefold() == self.locator.casefold()


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    type: str
    question: str
    required: list[EvidenceRef] = Field(min_length=1)
    also_relevant: list[EvidenceRef] = Field(default_factory=list)
    expected_terms: list[str] = Field(default_factory=list)


class Dataset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    corpus: str = Field(default="demo", description="'demo' seeds the bundled sample corpus; 'current' uses the live library.")
    distractors: str | None = Field(default=None, description="Name of a distractor pack mixed into the demo corpus.")
    questions: list[Question]


def load_dataset(name_or_path: str) -> Dataset:
    path = Path(name_or_path)
    if not path.suffix:
        path = DATASET_DIR / f"{name_or_path}.json"
    if not path.is_file():
        raise FileNotFoundError(f"No evaluation dataset at {path}")
    return Dataset.model_validate(json.loads(path.read_text(encoding="utf-8")))


def available_datasets() -> list[str]:
    return sorted(p.stem for p in DATASET_DIR.glob("*.json") if p.stem != "distractors")


def load_distractors(name: str) -> list[dict[str, Any]]:
    path = DATASET_DIR / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))["sources"]
