"""Runtime settings, read from the environment (and ``.env`` when present).

Values are read on every ``get_settings()`` call rather than frozen at import
time, so tests and CLI tools can override them with environment variables.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - python-dotenv is a hard dependency
    load_dotenv = None  # type: ignore[assignment]

if load_dotenv is not None:
    load_dotenv()


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str | None
    gemini_model: str
    anthropic_api_key: str | None
    claude_model: str
    llm_provider_setting: str
    groq_api_key: str | None

    # Video segmentation
    scene_sample_seconds: float
    scene_cut_threshold: float
    scene_min_seconds: float
    window_max_seconds: float
    keyframe_dup_hamming: int
    vision_workers: int

    # Enrichment
    entity_extraction: bool
    speaker_attribution: bool
    entity_batch_size: int

    # Jobs
    job_workers: int
    max_upload_mb: int

    # Website
    demo_enabled: bool

    # Security and operations
    api_key: str | None
    cors_origins: tuple[str, ...]
    log_level: str

    @property
    def llm_provider(self) -> str | None:
        """The one provider in use: ``"claude"``, ``"gemini"`` or ``None``.

        ``LLM_PROVIDER=claude`` or ``gemini`` picks that provider (``None`` if
        its key is missing). ``auto`` (the default) prefers Claude when its key
        is set, then Gemini.
        """
        choice = self.llm_provider_setting
        if choice == "claude":
            return "claude" if self.anthropic_api_key else None
        if choice == "gemini":
            return "gemini" if self.gemini_api_key else None
        if self.anthropic_api_key:
            return "claude"
        if self.gemini_api_key:
            return "gemini"
        return None

    @property
    def llm_enabled(self) -> bool:
        return self.llm_provider is not None

    @property
    def gemini_enabled(self) -> bool:
        """Gemini is the active provider."""
        return self.llm_provider == "gemini"


def get_settings() -> Settings:
    return Settings(
        gemini_api_key=os.getenv("GEMINI_API_KEY") or None,
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.6-flash"),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
        claude_model=os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5"),
        llm_provider_setting=(os.getenv("LLM_PROVIDER") or "auto").strip().lower(),
        groq_api_key=os.getenv("GROQ_API_KEY") or None,
        scene_sample_seconds=_float("SCENE_SAMPLE_SECONDS", 0.5),
        scene_cut_threshold=_float("SCENE_CUT_THRESHOLD", 0.04),
        scene_min_seconds=_float("SCENE_MIN_SECONDS", 2.0),
        window_max_seconds=_float("WINDOW_MAX_SECONDS", 30.0),
        keyframe_dup_hamming=_int("KEYFRAME_DUP_HAMMING", 6),
        vision_workers=max(1, _int("VISION_WORKERS", 4)),
        entity_extraction=_flag("ENABLE_ENTITY_EXTRACTION", True),
        speaker_attribution=_flag("ENABLE_SPEAKER_ATTRIBUTION", True),
        entity_batch_size=max(1, _int("ENTITY_BATCH_SIZE", 25)),
        job_workers=max(1, _int("JOB_WORKERS", 2)),
        max_upload_mb=max(1, _int("MAX_UPLOAD_MB", 500)),
        demo_enabled=_flag("ENABLE_DEMO", True),
        api_key=(os.getenv("WEFT_API_KEY") or "").strip() or None,
        cors_origins=tuple(o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()),
        log_level=(os.getenv("LOG_LEVEL") or "INFO").upper(),
    )
