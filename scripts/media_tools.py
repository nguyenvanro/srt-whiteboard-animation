"""Shared media operations; no shell interpolation or system FFprobe required."""
from __future__ import annotations

import math
import os
import shutil
import subprocess
from pathlib import Path


def ffmpeg_exe() -> str:
    override = os.environ.get("IMAGEIO_FFMPEG_EXE")
    if override:
        return override
    system = shutil.which("ffmpeg")
    if system:
        return system
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError) as exc:
        raise RuntimeError("FFmpeg unavailable. Run python scripts/prepare_env.py") from exc


def run_ffmpeg(args: list[str], *, cwd: Path | None = None) -> None:
    result = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-nostdin", "-y", *args],
        cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode:
        raise RuntimeError("FFmpeg failed:\n" + result.stderr[-5000:])


def media_duration(path: Path, kind: str) -> float:
    import av
    with av.open(str(path)) as container:
        streams = getattr(container.streams, kind)
        if not streams:
            raise ValueError(f"No {kind} stream in {path}")
        stream = streams[0]
        if stream.duration is not None and stream.time_base is not None:
            seconds = float(stream.duration * stream.time_base)
        elif container.duration is not None:
            seconds = container.duration / av.time_base
        else:
            raise ValueError(f"Cannot determine {kind} duration: {path}")
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"Invalid {kind} duration: {path}")
    return seconds


def load_cues(path: Path, *, non_overlapping: bool = False) -> list[dict]:
    from parse_srt import parse_srt
    cues = parse_srt(path.read_text(encoding="utf-8-sig"))
    if not cues:
        raise ValueError(f"No valid SRT cues: {path}")
    previous_start = previous_end = 0
    for cue in cues:
        if cue["startMs"] < previous_start or cue["endMs"] <= cue["startMs"]:
            raise ValueError(f"Cue {cue['index']}: timestamps must increase and duration must be positive")
        if non_overlapping and cue["startMs"] < previous_end:
            raise ValueError(f"Cue {cue['index']}: overlapping speech cues are not supported")
        if not cue["text"].strip():
            raise ValueError(f"Cue {cue['index']}: empty text")
        previous_start, previous_end = cue["startMs"], cue["endMs"]
    return cues
