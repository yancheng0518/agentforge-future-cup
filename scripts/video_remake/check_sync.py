"""Read-only, timestamp-aware narration and burnt-caption verification.

No listening assessment is claimed. Uses 10ms amplitude-envelope correlation
against original speech MP3s and 30fps pixel matching of the final burnt captions.
Reports AAC frame overlaps separately: concatenating decoded samples without PTS
correction can produce a false cumulative drift when AAC priming packets overlap.
Requires only Pillow and imageio-ffmpeg; writes JSON and logs to --output-dir.
This is a measurement tool, not an automatic quality PASS certificate. Successful
exit means measurements were produced; callers must interpret the reported limits.
"""
from __future__ import annotations

import argparse
import array
import bisect
import hashlib
import json
import math
import re
import subprocess
from pathlib import Path

import imageio_ffmpeg
from PIL import Image, ImageChops, ImageStat

ROOT = Path(__file__).resolve().parents[2]
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def invoke(args: list[str], *, info: bool = False) -> subprocess.CompletedProcess:
    result = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "info" if info else "error", *args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
    return result


def audio_timestamps(video: Path, output: Path) -> dict:
    result = invoke(["-i", str(video), "-vn", "-af", "ashowinfo", "-f", "null", "-"], info=True)
    log = result.stderr.decode("utf-8", errors="replace")
    (output / "audio-pts.log").write_text(log, encoding="utf-8")
    frames = []
    pattern = r"n:(\d+) pts:(-?\d+) pts_time:([0-9.-]+).*rate:(\d+) nb_samples:(\d+)"
    for line in log.splitlines():
        match = re.search(pattern, line) if "ashowinfo" in line else None
        if match:
            n, pts, sec, rate, count = match.groups()
            frames.append({"n": int(n), "pts": int(pts), "time": float(sec),
                           "rate": int(rate), "samples": int(count)})
    if not frames:
        raise ValueError("No decoded audio frames found")
    discontinuities = []
    for previous, current in zip(frames, frames[1:]):
        delta = current["pts"] - previous["pts"] - previous["samples"]
        if delta:
            discontinuities.append({"frame": current["n"], "time": current["time"],
                                    "delta_ms": delta / current["rate"] * 1000})
    return {"frame_count": len(frames), "discontinuities": discontinuities,
            "overlap_total_ms": -sum(min(0, x["delta_ms"]) for x in discontinuities),
            "gap_total_ms": sum(max(0, x["delta_ms"]) for x in discontinuities),
            "decoded_samples_seconds": sum(x["samples"] / x["rate"] for x in frames),
            "last_frame_end_seconds": frames[-1]["time"] + frames[-1]["samples"] / frames[-1]["rate"]}


def audio_envelope(path: Path, *, use_timestamps: bool = False) -> list[float]:
    args = ["-i", str(path), "-vn", "-ac", "1"]
    if use_timestamps:
        # Small negative AAC PTS jumps must be handled, rather than silently
        # counting every decoded priming sample as additional wall-clock time.
        args += ["-af", "aresample=8000:async=1:min_hard_comp=0.001:first_pts=0"]
    args += ["-ar", "8000", "-f", "s16le", "pipe:1"]
    samples = array.array("h")
    samples.frombytes(invoke(args).stdout)
    return [sum(abs(x) for x in samples[i:i + 80]) / 80
            for i in range(0, len(samples) - 79, 80)]


def correlate(final: list[float], reference: list[float], expected: float) -> dict:
    begin, end = 12, len(reference) - 12  # Exclude 120ms at each fade boundary.
    vector = reference[begin:end]
    mean = sum(vector) / len(vector)
    vector = [x - mean for x in vector]
    norm = sum(x * x for x in vector)
    base = round(expected * 100) + begin
    best = (-2.0, 0)
    for delta in range(-100, 151):
        offset = base + delta
        if offset < 0:
            continue
        other = final[offset:offset + len(vector)]
        if len(other) != len(vector):
            continue
        average = sum(other) / len(other)
        other = [x - average for x in other]
        denominator = math.sqrt(norm * sum(x * x for x in other))
        score = sum(x * y for x, y in zip(vector, other)) / denominator if denominator else 0
        if score > best[0]:
            best = (score, delta)
    return {"audio_offset_ms": best[1] * 10, "envelope_correlation": best[0]}


def measure_captions(video: Path, build: dict, audio: dict[str, dict], output: Path) -> list[dict]:
    width, height, fps = 480, 16, 30
    decoded = invoke(["-i", str(video), "-an", "-vf",
                      "crop=1920:64:0:944,scale=480:16:flags=lanczos,format=gray,showinfo",
                      "-fps_mode", "passthrough", "-f", "rawvideo", "pipe:1"], info=True)
    raw = decoded.stdout
    log = decoded.stderr.decode("utf-8", errors="replace")
    (output / "video-pts.log").write_text(log, encoding="utf-8")
    times = [float(match.group(1)) for line in log.splitlines()
             if "showinfo" in line and (match := re.search(r"\bn:\s*\d+\s+pts:\s*-?\d+\s+pts_time:([0-9.-]+)", line))]
    size = width * height
    frames = [Image.frombytes("L", (width, height), raw[i:i + size])
              for i in range(0, len(raw) - size + 1, size)]
    if len(frames) != len(times):
        raise ValueError(f"Video frame / PTS mismatch: {len(frames)} != {len(times)}")
    rows = []
    for record in build["segments"]:
        segment_id = record["segment"]["id"]
        for index, cue in enumerate(record["captions"]):
            expected_start = record["start"] + cue["start"]
            expected_end = record["start"] + cue["end"]
            midpoint = (expected_start + expected_end) / 2
            reference = frames[min(len(frames) - 1, bisect.bisect_left(times, midpoint))]
            blank = Image.new("L", reference.size, reference.getpixel((0, 0)))
            contrast = ImageStat.Stat(ImageChops.difference(reference, blank)).mean[0]
            if contrast < .5:
                rows.append({"segment": segment_id, "index": index, "text": cue["text"],
                             "error": "No visible text in expected midpoint frame"})
                continue
            hits = []
            for frame_index in range(bisect.bisect_left(times, expected_start - .5),
                                     bisect.bisect_right(times, expected_end + .5)):
                difference = ImageStat.Stat(ImageChops.difference(reference, frames[frame_index])).mean[0]
                if difference / contrast < .2:
                    hits.append(frame_index)
            if not hits:
                rows.append({"segment": segment_id, "index": index, "text": cue["text"],
                             "error": "Caption template not found"})
                continue
            observed_start = times[min(hits)]
            observed_end = times[max(hits) + 1] if max(hits) + 1 < len(times) else times[-1] + 1 / fps
            estimated_word_start = expected_start + audio[segment_id]["audio_offset_ms"] / 1000
            rows.append({"segment": segment_id, "index": index, "text": cue["text"],
                         "expected_start": expected_start, "expected_end": expected_end,
                         "observed_start": observed_start, "observed_end": observed_end,
                         "burnt_minus_srt_start_ms": (observed_start - expected_start) * 1000,
                         "burnt_minus_srt_end_ms": (observed_end - expected_end) * 1000,
                         "burnt_minus_estimated_spoken_word_start_ms": (observed_start - estimated_word_start) * 1000,
                         "audio_correlation": audio[segment_id]["envelope_correlation"]})
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path, default=ROOT / "submission/演示视频-复核修正版-r2-20261116.mp4")
    parser.add_argument("--build", type=Path)
    parser.add_argument("--output-dir", type=Path,
                        default=ROOT / "submission/video-work/remake-20261116/recheck-final/sync")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    video = args.video.resolve()
    build = json.loads((args.build or video.with_suffix(".build.json")).read_text(encoding="utf-8"))
    actual_sha = hashlib.sha256(video.read_bytes()).hexdigest()
    if actual_sha != build["sha256"]:
        raise ValueError("Video SHA does not match supplied build manifest")
    timestamps = audio_timestamps(video, output)
    envelope = audio_envelope(video, use_timestamps=True)
    matches = {}
    for record in build["segments"]:
        identifier = record["segment"]["id"]
        reference = audio_envelope(ROOT / record["segment"]["audio"])
        matches[identifier] = {"edit_start": record["start"], **correlate(envelope, reference, record["start"])}
    captions = measure_captions(video, build, matches, output)
    successful = [row for row in captions if "error" not in row]
    errors = [row for row in captions if "error" in row]
    report = {
        "video_sha256": actual_sha, "method": "PTS-corrected 10ms audio envelope correlation; 30fps burnt-caption pixel matching",
        "limitations": "Not a listening assessment. Caption/audio estimate resolution about 43ms (one frame plus one audio envelope bin). Independent phoneme alignment not performed.",
        "audio_timestamps": timestamps, "audio_alignment": matches, "caption_measurements": captions,
        "summary": {
            "caption_count": len(captions), "caption_measurement_errors": len(errors),
            "min_audio_correlation": min(item["envelope_correlation"] for item in matches.values()),
            "max_abs_caption_vs_speech_ms": max(abs(row["burnt_minus_estimated_spoken_word_start_ms"]) for row in successful),
            "min_caption_vs_speech_ms": min(row["burnt_minus_estimated_spoken_word_start_ms"] for row in successful),
            "max_caption_vs_speech_ms": max(row["burnt_minus_estimated_spoken_word_start_ms"] for row in successful),
        },
    }
    (output / "sync-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"sha256": actual_sha, "summary": report["summary"],
                      "aac_discontinuities": len(timestamps["discontinuities"]),
                      "aac_overlap_total_ms": timestamps["overlap_total_ms"]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
