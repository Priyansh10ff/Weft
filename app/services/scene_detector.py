"""Content-aware video segmentation.

Instead of cutting a recording into fixed 3-second chunks, frames are sampled
at a fine interval, compared with their predecessor, and a new *scene* starts
where the picture changes substantially (a new slide, a cut to a diagram, a
screen-share switching windows).  Long scenes are then split into *windows*
at speech-segment boundaries so a two-minute explanation over one slide does
not become one oversized chunk, and no sentence is cut in half.

The numeric parts are pure functions over plain data so they can be tested
without a video file.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np


@dataclass
class FrameSignature:
    time: float
    frame_index: int
    thumb: np.ndarray  # 160x90 grayscale, float in [0, 1]
    dhash: int


@dataclass(frozen=True)
class Scene:
    index: int
    start: float
    end: float
    keyframe_time: float
    keyframe_index: int


@dataclass(frozen=True)
class Window:
    scene_index: int
    start: float
    end: float


def dhash(gray: np.ndarray) -> int:
    """64-bit difference hash of a grayscale image."""
    import cv2

    small = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    return int("".join("1" if b else "0" for b in bits), 2)


def hamming(a: int, b: int) -> int:
    return int(bin(a ^ b).count("1"))


_THUMB_SIZE = (160, 90)
# Per-pixel intensity change that counts as "changed" (filters compression noise).
_PIXEL_DELTA = 0.08


def signature(frame_bgr: np.ndarray, time: float, frame_index: int) -> FrameSignature:
    import cv2

    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    thumb = cv2.resize(gray, _THUMB_SIZE, interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
    return FrameSignature(time, frame_index, thumb, dhash(gray))


def frame_distance(a: FrameSignature, b: FrameSignature) -> float:
    """Fraction of thumbnail pixels whose intensity changed noticeably.

    Measured on a 160x90 thumbnail so a text-only slide change (same
    template, new bullet points) still registers: on real slide decks a
    change moves ~5% of pixels, while compression noise moves ~0%.
    """
    return float(np.mean(np.abs(a.thumb - b.thumb) > _PIXEL_DELTA))


def detect_scenes(
    signatures: list[FrameSignature],
    duration: float,
    *,
    threshold: float,
    min_scene_seconds: float,
) -> list[Scene]:
    """Group sampled frames into scenes separated by large visual changes."""
    if not signatures:
        return [Scene(0, 0.0, max(0.0, duration), 0.0, 0)] if duration > 0 else []

    # Each frame is compared with the scene's *anchor* frame, not just its
    # predecessor, so gradual change (scrolling, a diagram drawn stroke by
    # stroke) still triggers a cut once it adds up.  A change that comes
    # less than ``min_scene_seconds`` after the last cut means the previous
    # cut landed on a transition (a flash, a fade, an animation): the cut is
    # moved forward instead of opening another tiny scene.
    boundaries = [0]  # indices into ``signatures`` where a scene starts
    anchor = 0
    for i in range(1, len(signatures)):
        if frame_distance(signatures[anchor], signatures[i]) < threshold:
            continue
        anchor = i
        if signatures[i].time - signatures[boundaries[-1]].time < min_scene_seconds:
            if len(boundaries) > 1:
                boundaries[-1] = i
            continue
        boundaries.append(i)

    scenes: list[Scene] = []
    for n, first in enumerate(boundaries):
        last = (boundaries[n + 1] if n + 1 < len(boundaries) else len(signatures)) - 1
        start = 0.0 if n == 0 else signatures[first].time
        end = signatures[boundaries[n + 1]].time if n + 1 < len(boundaries) else duration
        end = max(end, start)
        # Prefer a late frame: slides with build animations are fullest at the end.
        target = end - min(1.0, (end - start) / 4.0)
        key = min(range(first, last + 1), key=lambda i: abs(signatures[i].time - target))
        scenes.append(Scene(n, start, end, signatures[key].time, signatures[key].frame_index))

    # Absorb a too-short trailing scene into its predecessor.
    if len(scenes) > 1 and scenes[-1].end - scenes[-1].start < min_scene_seconds:
        tail = scenes.pop()
        prev = scenes.pop()
        scenes.append(Scene(prev.index, prev.start, tail.end, prev.keyframe_time, prev.keyframe_index))
    return scenes


def plan_windows(
    scenes: list[Scene],
    speech: list[dict[str, Any]],
    *,
    max_window_seconds: float,
) -> list[Window]:
    """Split scenes longer than ``max_window_seconds`` at speech boundaries."""
    windows: list[Window] = []
    for scene in scenes:
        length = scene.end - scene.start
        if length <= max_window_seconds:
            windows.append(Window(scene.index, scene.start, scene.end))
            continue

        # Candidate cut points: ends of speech segments inside the scene, so
        # a sentence is never split across windows.
        cuts = sorted(
            {
                float(seg["end_time"])
                for seg in speech
                if scene.start < float(seg["end_time"]) < scene.end
            }
        )
        start = scene.start
        while scene.end - start > max_window_seconds:
            limit = start + max_window_seconds
            usable = [c for c in cuts if start + max_window_seconds / 3 <= c <= limit]
            boundary = usable[-1] if usable else limit
            windows.append(Window(scene.index, start, boundary))
            start = boundary
        windows.append(Window(scene.index, start, scene.end))
    return windows


def assign_speech(
    windows: list[Window], speech: list[dict[str, Any]]
) -> list[list[dict[str, Any]]]:
    """Assign every speech segment to exactly one window (by its midpoint).

    Prevents the same sentence being indexed twice when it straddles a
    window boundary.
    """
    assigned: list[list[dict[str, Any]]] = [[] for _ in windows]
    for seg in speech:
        mid = (float(seg["start_time"]) + float(seg["end_time"])) / 2.0
        for i, window in enumerate(windows):
            is_last = i == len(windows) - 1
            if window.start <= mid < window.end or (is_last and mid >= window.start):
                assigned[i].append(seg)
                break
        else:
            if windows:
                assigned[0 if mid < windows[0].start else -1].append(seg)
    return assigned


def sample_video(path: Path, sample_seconds: float) -> tuple[float, float, list[FrameSignature]]:
    """Decode ``path`` once, keeping a signature every ``sample_seconds``.

    Returns ``(fps, duration, signatures)``.
    """
    import cv2

    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        capture.release()
        raise ValueError(f"Unable to open video: {path.name}")
    try:
        fps = float(capture.get(cv2.CAP_PROP_FPS)) or 0.0
        frame_count = float(capture.get(cv2.CAP_PROP_FRAME_COUNT)) or 0.0
        if fps <= 0:
            fps = 25.0
        step = max(1, int(round(fps * sample_seconds)))
        signatures: list[FrameSignature] = []
        index = 0
        while capture.grab():
            if index % step == 0:
                ok, frame = capture.retrieve()
                if ok and frame is not None:
                    signatures.append(signature(frame, index / fps, index))
            index += 1
        duration = (frame_count or index) / fps
        return fps, max(duration, index / fps), signatures
    finally:
        capture.release()


def read_frame(path: Path, frame_index: int) -> np.ndarray | None:
    import cv2

    capture = cv2.VideoCapture(str(path))
    try:
        capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        ok, frame = capture.read()
        return frame if ok else None
    finally:
        capture.release()
