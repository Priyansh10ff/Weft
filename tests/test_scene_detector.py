"""Scene detection, windowing and speech assignment."""

from __future__ import annotations

from pathlib import Path

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

from app.services import scene_detector as sd  # noqa: E402


def _slide(text: str, accent: tuple[int, int, int]) -> "np.ndarray":
    frame = np.full((360, 640, 3), 245, dtype=np.uint8)
    cv2.rectangle(frame, (0, 0), (640, 50), accent, -1)
    for line, y in enumerate(range(110, 330, 45)):
        cv2.putText(frame, f"{text} line {line}", (40, y), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (20, 20, 20), 2)
    return frame


def make_slide_video(path: Path, slides: list[tuple[str, tuple[int, int, int]]], seconds: int = 4, fps: int = 10) -> Path:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (640, 360))
    for text, accent in slides:
        frame = _slide(text, accent)
        for _ in range(seconds * fps):
            writer.write(frame)
    writer.release()
    return path


SLIDES = [
    ("Problem: DB load", (200, 60, 60)),
    ("Fix: read replicas", (60, 160, 60)),
    ("Results", (60, 60, 200)),
    ("Problem: DB load", (200, 60, 60)),  # same slide shown again
]


def test_detects_slide_changes_and_duplicate_keyframes(tmp_path):
    video = make_slide_video(tmp_path / "talk.mp4", SLIDES)
    _, duration, sigs = sd.sample_video(video, 0.5)
    assert duration == pytest.approx(16.0, abs=0.2)
    scenes = sd.detect_scenes(sigs, duration, threshold=0.04, min_scene_seconds=2.0)
    assert [round(s.start) for s in scenes] == [0, 4, 8, 12]
    assert all(s.start <= s.keyframe_time <= s.end for s in scenes)

    by_index = {s.frame_index: s for s in sigs}
    hashes = [by_index[s.keyframe_index].dhash for s in scenes]
    assert sd.hamming(hashes[0], hashes[3]) <= 6  # repeated slide
    assert sd.hamming(hashes[0], hashes[1]) > 6


def test_static_video_is_one_scene(tmp_path):
    video = make_slide_video(tmp_path / "static.mp4", [SLIDES[0]], seconds=6)
    _, duration, sigs = sd.sample_video(video, 0.5)
    assert len(sd.detect_scenes(sigs, duration, threshold=0.04, min_scene_seconds=2.0)) == 1


def test_short_transition_is_not_a_scene(tmp_path):
    path = tmp_path / "flash.mp4"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (640, 360))
    a, b, c = _slide("A", (200, 0, 0)), _slide("B", (0, 200, 0)), _slide("C", (0, 0, 200))
    for frame, n in ((a, 40), (b, 5), (c, 40)):  # B flashes for 0.5s
        for _ in range(n):
            writer.write(frame)
    writer.release()
    _, duration, sigs = sd.sample_video(path, 0.5)
    scenes = sd.detect_scenes(sigs, duration, threshold=0.04, min_scene_seconds=2.0)
    assert len(scenes) == 2


def _speech(*spans):
    return [{"start_time": s, "end_time": e, "text": f"t{s}"} for s, e in spans]


def test_long_scene_splits_at_speech_boundaries():
    scene = sd.Scene(0, 0.0, 90.0, 89.0, 0)
    speech = _speech(*[(t, t + 9.5) for t in range(0, 90, 10)])
    windows = sd.plan_windows([scene], speech, max_window_seconds=30)
    assert windows[0].start == 0.0 and windows[-1].end == 90.0
    assert all(w.end - w.start <= 30 for w in windows)
    # Every cut except the scene end lands on the end of a speech segment.
    ends = {s["end_time"] for s in speech}
    assert all(w.end in ends for w in windows[:-1])


def test_long_silent_scene_splits_evenly():
    windows = sd.plan_windows([sd.Scene(0, 0.0, 100.0, 99.0, 0)], [], max_window_seconds=30)
    assert len(windows) >= 3 and all(w.end - w.start <= 45 for w in windows)
    assert windows[-1].end == 100.0


def test_short_scene_is_one_window():
    assert sd.plan_windows([sd.Scene(0, 0.0, 12.0, 11.0, 0)], [], max_window_seconds=30) == [
        sd.Window(0, 0.0, 12.0)
    ]


def test_speech_is_assigned_once_by_midpoint():
    windows = [sd.Window(0, 0.0, 10.0), sd.Window(1, 10.0, 20.0)]
    speech = _speech((8.0, 11.0), (9.0, 13.0), (15.0, 25.0))
    assigned = sd.assign_speech(windows, speech)
    assert [len(a) for a in assigned] == [1, 2]
    assert sum(len(a) for a in assigned) == len(speech)
