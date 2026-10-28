"""Generate Vietnamese (or another Edge voice) narration aligned to SRT cues.

Only this step sends subtitle text to the online Microsoft Edge TTS service.
The output is mono 24 kHz PCM WAV, including all SRT gaps and initial silence.
"""
from __future__ import annotations

import argparse
import asyncio
import html
import math
import re
import sys
import tempfile
import wave
from pathlib import Path

from media_tools import load_cues, run_ffmpeg

SAMPLE_RATE = 24000
DEFAULT_VOICE = "vi-VN-HoaiMyNeural"


async def synthesize(text: str, voice: str, rate: str, output: Path) -> None:
    try:
        import edge_tts
    except ImportError as exc:
        raise RuntimeError("Install edge-tts with python scripts/prepare_env.py") from exc
    for attempt in range(3):
        try:
            # A Communicate instance is single-use; recreate it on every retry.
            await edge_tts.Communicate(text, voice=voice, rate=rate).save(str(output))
            return
        except Exception as exc:
            if attempt == 2:
                raise RuntimeError(f"Edge TTS failed after 3 attempts; check network access and voice name: {exc}") from exc
            print(f"  Edge TTS temporarily failed; retry {attempt + 1}/2", file=sys.stderr)
            await asyncio.sleep(attempt + 1)


def tempo_filter(speed: float) -> str:
    factors = []
    while speed > 2:
        factors.append("atempo=2")
        speed /= 2
    factors.append(f"atempo={speed:.9f}")
    return ",".join(factors)


def write_silence(writer: wave.Wave_write, samples: int) -> None:
    while samples > 0:
        count = min(samples, SAMPLE_RATE)
        writer.writeframesraw(bytes(count * 2))
        samples -= count


async def generate_narration(
    srt: Path, output: Path, *, voice: str = DEFAULT_VOICE,
    rate: str = "+0%", max_speed: float = 2.0,
) -> Path:
    if output.suffix.lower() != ".wav":
        raise ValueError("Narration output must use the .wav extension")
    if not math.isfinite(max_speed) or max_speed < 1:
        raise ValueError("max-speed must be finite and >= 1")
    if not re.fullmatch(r"[+-]\d+%", rate):
        raise ValueError("rate must look like +0%, +10%, or -10%")
    cues = load_cues(srt, non_overlapping=True)
    output = output.resolve()
    if output == srt.resolve():
        raise ValueError("Output must differ from the subtitle input")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="narration-", dir=output.parent) as folder:
        work = Path(folder)
        staged = work / "narration.wav"
        with wave.open(str(staged), "wb") as writer:
            writer.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
            cursor = 0
            for cue in cues:
                text = html.unescape(re.sub(r"<[^>]+>|\{\\[^}]*\}", "", cue["text"])).strip()
                if not text:
                    raise ValueError(f"Cue {cue['index']}: no spoken text after removing markup")
                print(f"TTS {cue['index']}/{len(cues)}: {text[:70]}")
                mp3, pcm, fitted = work / "cue.mp3", work / "cue.wav", work / "fitted.wav"
                await synthesize(text, voice, rate, mp3)
                run_ffmpeg(["-i", str(mp3), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
                            "-c:a", "pcm_s16le", str(pcm)])
                start = round(cue["startMs"] * SAMPLE_RATE / 1000)
                end = round(cue["endMs"] * SAMPLE_RATE / 1000)
                budget = end - start
                with wave.open(str(pcm), "rb") as clip:
                    spoken_samples = clip.getnframes()
                speed = max(1.0, spoken_samples / budget)
                if speed > max_speed + 1e-6:
                    raise ValueError(
                        f"Cue {cue['index']} needs {speed:.2f}x speed (limit {max_speed:.2f}x). "
                        "Shorten the text, extend this cue, or increase --max-speed. No speech was cut."
                    )
                if speed > 1:
                    print(f"  Fit cue to {cue['durMs']}ms at {speed:.2f}x")
                # Fit without truncation, then pad to the exact SRT sample count.
                # A second pass compensates for atempo's small output-length variance.
                for _ in range(3):
                    run_ffmpeg(["-i", str(pcm), "-af", tempo_filter(speed), str(fitted)])
                    with wave.open(str(fitted), "rb") as clip:
                        count = clip.getnframes()
                    if count <= budget:
                        break
                    speed *= count / budget * 1.005
                    if speed > max_speed + 1e-6:
                        raise ValueError(f"Cue {cue['index']}: fitting exceeds --max-speed; extend the cue")
                else:
                    raise ValueError(f"Cue {cue['index']}: could not fit speech without cutting it")
                write_silence(writer, start - cursor)
                with wave.open(str(fitted), "rb") as clip:
                    while data := clip.readframes(SAMPLE_RATE):
                        writer.writeframesraw(data)
                write_silence(writer, budget - count)
                cursor = end
        staged.replace(output)
    return output


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Generate SRT-aligned narration using online Edge TTS")
    parser.add_argument("srt", type=Path)
    parser.add_argument("output", type=Path, help="Output PCM .wav")
    parser.add_argument("--voice", default=DEFAULT_VOICE, help="e.g. vi-VN-HoaiMyNeural or vi-VN-NamMinhNeural")
    parser.add_argument("--rate", default="+0%", help="Use --rate=-10%% for a slower voice")
    parser.add_argument("--max-speed", type=float, default=2.0)
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(generate_narration(args.srt, args.output, voice=args.voice,
                             rate=args.rate, max_speed=args.max_speed))
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"[err] {exc}", file=sys.stderr)
        return 1
    print(f"OUTPUT={result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
