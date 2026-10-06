"""Evaluation API: run the gold question set and read the latest report."""

from __future__ import annotations

from typing import Any

from anyio import to_thread
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from app.evaluation.dataset import available_datasets
from app.evaluation.runner import SYSTEMS, evaluate, load_latest_report, save_report
from app.services.vector_store import VectorStoreError

router = APIRouter(prefix="/eval", tags=["evaluation"])


class EvalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset: str = Field(default="demo", pattern=r"^[A-Za-z0-9_-]+$")
    k: int = Field(default=5, ge=1, le=20)
    systems: list[str] | None = None


@router.get("/datasets")
async def datasets() -> dict[str, Any]:
    return {"datasets": available_datasets(), "systems": SYSTEMS}


@router.get("/latest")
async def latest() -> dict[str, Any]:
    report = await to_thread.run_sync(load_latest_report)
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No evaluation has been run yet.")
    return report


@router.post("/run")
async def run(request: EvalRequest) -> dict[str, Any]:
    """Run the evaluation (isolated demo corpus) and save it as the latest report."""
    if request.dataset not in available_datasets():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown dataset {request.dataset}")

    def go() -> dict[str, Any]:
        report = evaluate(request.dataset, k=request.k, systems=request.systems)
        save_report(report)
        return report

    try:
        return await to_thread.run_sync(go)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except VectorStoreError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
