"""Add narration and/or burned SRT subtitles to a rendered or merged MP4."""
from __future__ import annotations

import argparse
import asyncio
import math
import sys
import tempfile
from pathlib import Path

from generate_narration import DEFAULT_VOICE, generate_narration
from media_tools import load_cues, media_duration, run_ffmpeg


def add_finalization_args(parser: argparse.ArgumentParser) -> None:
    audio = parser.add_mutually_exclusive_group()
    audio.add_argument("--audio", type=Path, help="Existing narration file, starting at video time 0")
    audio.add_argument("--tts", action="store_true", help="Generate online Edge TTS narration from --srt")
    parser.add_argument("--srt", type=Path, help="UTF-8 subtitles, relative to this video's start")
    parser.add_argument("--burn-subtitles", action="store_true", help="Render --srt as visible captions")
    parser.add_argument("--voice", default=DEFAULT_VOICE)
    parser.add_argument("--rate", default="+0%")
    parser.add_argument("--max-speed", type=float, default=2.0)
    parser.add_argument("--font", default="Arial", help="Installed subtitle font family")
    parser.add_argument("--font-size", type=int, default=24, help="ASS subtitle font size")
    parser.add_argument("--subtitle-margin", type=int, default=24)


def validate_options(args) -> None:
    if (args.tts or args.burn_subtitles) and args.srt is None:
        raise ValueError("--tts and --burn-subtitles require --srt")
    if args.font_size <= 0 or args.subtitle_margin < 0:
        raise ValueError("font-size must be positive; subtitle-margin must be nonnegative")
    if any(c in args.font for c in ",:'\\[];\n\r"):
        raise ValueError("Font family contains unsupported filter characters")
    if not math.isfinite(args.max_speed) or args.max_speed < 1:
        raise ValueError("max-speed must be finite and >= 1")
    if args.srt:
        load_cues(args.srt, non_overlapping=args.tts)
    if args.audio:
        media_duration(args.audio, "audio")
    if getattr(args, "output", None):
        output = Path(args.output).resolve()
        for source in (args.audio, args.srt):
            if source and output == source.resolve():
                raise ValueError("Output must differ from audio and subtitle inputs")


def finalize_video(video: Path, output: Path, args) -> Path:
    validate_options(args)
    video, output = video.resolve(), output.resolve()
    if output.suffix.lower() != ".mp4":
        raise ValueError("Final output must use .mp4")
    if args.srt and output == args.srt.resolve():
        raise ValueError("Output must differ from subtitle input")
    if args.audio and output == args.audio.resolve():
        raise ValueError("Output must differ from audio input")
    video_duration = media_duration(video, "video")
    output.parent.mkdir(parents=True, exist_ok=True)
    audio = args.audio.resolve() if args.audio else None
    if args.tts:
        audio = output.with_suffix(".narration.wav")
        asyncio.run(generate_narration(args.srt, audio, voice=args.voice,
                                     rate=args.rate, max_speed=args.max_speed))
    duration = max(video_duration, media_duration(audio, "audio") if audio else 0)
    if args.burn_subtitles:
        duration = max(duration, max(c["endMs"] for c in load_cues(args.srt)) / 1000)
    with tempfile.TemporaryDirectory(prefix="finalize-", dir=output.parent) as folder:
        work = Path(folder)
        filters = []
        if duration > video_duration:
            filters.append(f"tpad=stop_mode=clone:stop_duration={duration-video_duration:.6f}")
        if args.burn_subtitles:
            # A fixed relative filename avoids FFmpeg filter escaping problems on Windows,
            # including drive letters, apostrophes, Unicode and spaces in user paths.
            (work / "captions.srt").write_text(args.srt.read_text(encoding="utf-8-sig"), encoding="utf-8")
            style = (f"FontName={args.font},FontSize={args.font_size},PrimaryColour=&H00FFFFFF,"
                     f"OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=0,"
                     f"Alignment=2,MarginV={args.subtitle_margin}")
            filters.append(f"subtitles=filename=captions.srt:force_style='{style}'")
        command = ["-i", str(video)]
        if audio:
            command += ["-i", str(audio)]
        command += ["-map", "0:v:0", "-map", "1:a:0" if audio else "0:a:0?"]
        if filters:
            command += ["-vf", ",".join(filters), "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p"]
        else:
            command += ["-c:v", "copy"]
        command += ["-c:a", "aac", "-b:a", "192k", "-af", "apad", "-t", f"{duration:.6f}",
                    "-movflags", "+faststart", str(work / "final.mp4")]
        run_ffmpeg(command, cwd=work)
        (work / "final.mp4").replace(output)
    return output


def finish_if_requested(video: Path, output: Path, args) -> Path:
    if args.audio or args.tts or args.burn_subtitles:
        return finalize_video(video, output, args)
    return video


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    add_finalization_args(parser)
    args = parser.parse_args(argv)
    if not (args.audio or args.tts or args.burn_subtitles):
        parser.error("Choose --audio, --tts, and/or --burn-subtitles")
    try:
        result = finalize_video(args.video, args.output, args)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"[err] {exc}", file=sys.stderr)
        return 1
    print(f"OUTPUT={result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
