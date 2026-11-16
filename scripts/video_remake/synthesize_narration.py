"""Reproduce the approved Xiaoxiao narration and word-timed source metadata.

Usage (from repo root):
  python scripts/video_remake/synthesize_narration.py

Requires edge-tts and imageio-ffmpeg. The public narration text is sent to the
Microsoft Edge speech service. No project credentials or environment file is read.
Outputs are media work files, excluded from Git by submission/video-work/.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import subprocess
import sys
import wave
from pathlib import Path

import edge_tts
import imageio_ffmpeg

REPO = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE = Path(__file__).with_name("narration.json")
DEFAULT_OUTPUT = REPO / "submission/video-work/remake-20261116/audio"


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def duration(path: Path) -> float:
    """Measure fully decoded audio, not the MP3 bitrate estimate."""
    result = subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-i", str(path),
         "-f", "s16le", "-ac", "1", "-ar", "24000", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
    return len(result.stdout) / (24000 * 2)


def subtitle_cues(segment: dict) -> list[dict]:
    """Prefer clause breaks and use service word timestamps for caption timing."""
    text = segment["text"]
    words = []
    search_from = 0
    for item in segment["boundaries"]:
        if item.get("type") != "WordBoundary":
            continue
        position = text.find(item["text"], search_from)
        if position < 0:
            raise ValueError(f"Cannot align subtitle: {segment['id']} {item['text']!r}")
        words.append({**item, "begin": position, "end": position + len(item["text"])})
        search_from = position + len(item["text"])
    cues = []
    start_word = 0
    for index, item in enumerate(words):
        end_char = words[index + 1]["begin"] if index + 1 < len(words) else len(text)
        start_char = words[start_word]["begin"]
        length = end_char - start_char
        punctuation = text[item["end"]:end_char]
        next_length = (words[index + 1]["end"] - start_char) if index + 1 < len(words) else 0
        at_break = bool(re.search(r"[，；：。？！]", punctuation)) and length >= 11
        if at_break or next_length > 27 or index == len(words) - 1:
            cue_text = text[start_char:end_char].strip().strip("，；：。")
            start = segment["start"] + words[start_word]["offset"] / 1e7
            end = segment["start"] + (item["offset"] + item["duration"]) / 1e7 + 0.12
            cues.append({"segment": segment["id"], "chapter": segment["chapter"],
                         "start": round(start, 3), "end": round(end, 3), "text": cue_text})
            start_word = index + 1
    return cues


def srt_time(seconds: float) -> str:
    value = int(round(seconds * 1000))
    hours, value = divmod(value, 3600000)
    minutes, value = divmod(value, 60000)
    seconds, millis = divmod(value, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def export_assembled_media(segments: list[dict], output: Path) -> None:
    cues = [cue for segment in segments for cue in subtitle_cues(segment)]
    for current, following in zip(cues, cues[1:]):
        current["end"] = min(current["end"], round(following["start"] - 0.01, 3))
    write_json(output / "captions.json", cues)
    srt = "\n\n".join(
        f"{index}\n{srt_time(cue['start'])} --> {srt_time(cue['end'])}\n{cue['text']}"
        for index, cue in enumerate(cues, start=1)
    )
    (output / "captions.srt").write_text(srt + "\n", encoding="utf-8")
    with wave.open(str(output / "narration.wav"), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(24000)
        for segment in segments:
            result = subprocess.run(
                [imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-i", str(REPO / segment["audio"]),
                 "-f", "s16le", "-ac", "1", "-ar", "24000", "pipe:1"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            if result.returncode:
                raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
            writer.writeframes(result.stdout)
            writer.writeframes(bytes(int(round(segment["pause_after"] * 24000)) * 2))


async def create_segment(text: str, voice: str, rate: str, stem: Path) -> dict:
    fingerprint = hashlib.sha256((voice + "\n" + rate + "\n" + text).encode()).hexdigest()
    audio_path = stem.with_suffix(".mp3")
    metadata_path = stem.with_suffix(".json")
    if metadata_path.exists() and audio_path.exists():
        existing = json.loads(metadata_path.read_text(encoding="utf-8"))
        if existing.get("fingerprint") == fingerprint:
            return existing
    errors = []
    for attempt in range(1, 4):
        try:
            boundaries = []
            audio_bytes = bytearray()
            communicator = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")
            async for chunk in communicator.stream():
                if chunk["type"] == "audio":
                    audio_bytes.extend(chunk["data"])
                else:
                    boundaries.append(chunk)
            if not audio_bytes:
                raise RuntimeError("Speech service returned no audio")
            audio_path.write_bytes(audio_bytes)
            result = {
                "fingerprint": fingerprint, "voice": voice, "rate": rate, "text": text,
                "audio": audio_path.relative_to(REPO).as_posix(),
                "duration": duration(audio_path), "boundary_time_unit": "100ns",
                "boundaries": boundaries, "attempts": attempt, "previous_errors": errors,
                "sha256": hashlib.sha256(audio_bytes).hexdigest(),
            }
            write_json(metadata_path, result)
            return result
        except Exception as error:
            errors.append({"attempt": attempt, "type": type(error).__name__, "message": str(error)})
            write_json(stem.with_suffix(".errors.json"), errors)
            if attempt == 3:
                raise
            await asyncio.sleep(attempt * 2)
    raise AssertionError("Unreachable")


async def main(source_path: Path, output: Path) -> None:
    source = json.loads(source_path.read_text(encoding="utf-8"))
    output.mkdir(parents=True, exist_ok=True)
    segments = []
    cursor = 0.0
    for chapter in source["chapters"]:
        for index, text in enumerate(chapter["segments"], start=1):
            key = f"{chapter['id']}-{index:02d}"
            metadata = await create_segment(text, source["voice"], source["rate"], output / key)
            segment = {
                "id": key, "chapter": chapter["id"], "chapter_title": chapter["title"],
                **metadata, "start": round(cursor, 6),
            }
            is_last = index == len(chapter["segments"])
            segment["pause_after"] = source["chapter_gap_seconds"] if is_last else source["gap_seconds"]
            cursor += metadata["duration"] + segment["pause_after"]
            segments.append(segment)
            print(f"{key}: {metadata['duration']:.3f}s {text}", flush=True)
    result = {
        "source": source_path.relative_to(REPO).as_posix(), "voice": source["voice"],
        "rate": source["rate"], "duration_including_pauses": round(cursor, 6),
        "spoken_duration": round(sum(x["duration"] for x in segments), 6),
        "segment_count": len(segments), "segments": segments,
    }
    write_json(output / "manifest.json", result)
    export_assembled_media(segments, output)
    print(json.dumps({key: val for key, val in result.items() if key != "segments"}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    asyncio.run(main(args.source.resolve(), args.output.resolve()))
