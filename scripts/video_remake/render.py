"""Render the AgentForge narrated demo, without modifying the original video.

Usage (from the repository root)::

    python scripts/video_remake/render.py --config path/to/edit.json
    python scripts/video_remake/render.py --config path/to/edit.json --only 0,1

Dependencies: Pillow, imageio-ffmpeg. The separate speech script uses edge-tts.
All paths in the edit JSON are relative to the repository root, not the JSON.
The default output is submission/演示视频-复核修正版-r2-20261116.mp4.

Edit format::

    {"title": "AgentForge", "segments": [{
      "audio": "submission/video-work/.../audio/001.mp3",
      "text": "A short spoken sentence, also used for captions.",
      "chapter": "01", "kicker": "需求与交付", "title": "从一句需求开始",
      "source": "真实界面 · 历史运行回放", "kind": "image",
      "media": "submission/video-work/.../shot.png", "crop": [x,y,w,h],
      "detail": "Optional explanation for a card.",
      "stats": [{"value": "14", "label": "Docker 验收通过"}]
    }]}

kind is image, video, or card. Media and crop are optional for cards. Videos
support media_start (seconds) and media_rate (default 1); footage is NEVER looped.
If narration exceeds video length, only its last frame is held. duration may set
a minimum segment length. subtitle_lines may supply strings or {text,start,end}
objects; timings are local seconds. Otherwise captions split on punctuation and
use character-weighted timing. This is editorial timing, not forced alignment.
tail defaults to 0.30 s. zoom defaults to 0.012 for images and 0 for cards/videos.
flow optionally supplies 2–5 brief labels on a card. no_subtitle disables captions.
subtitle_cues is an alias for timed subtitle_lines. pause_after is an alias for tail.
For TTS word-boundary timing, set top-level captions_json and narration_manifest;
each segment's id matches the narration manifest and caption segment identifier.
The renderer converts global caption times to segment-local times before editing.
Audio gain_db defaults to +3 dB; use 0 for an already-normalized recording.

Outputs alongside the MP4: UTF-8 SRT, contact sheet, build JSON with source hashes,
FFmpeg version, complete timing manifest, video hash, and full decode check.
Intermediate clips are kept under submission/video-work/remake-20261116/render/.
Each cache key includes source file hashes, all segment settings and this script.
Final video assembly is video-only. Every narration segment is padded/trimmed to
exactly frame_count * 1600 PCM samples at 48 kHz; joined PCM is AAC-encoded ONCE.
This avoids relying on per-segment AAC packet padding when assigning video times.
--reuse-build previous.build.json reuses unchanged verified visual clips; source
hashes, segment settings and exact frame counts must match. Existing clips stay.
An --only preview is written to a separate preview file and never replaces final.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
import wave
from pathlib import Path
from typing import Any

import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / "submission/video-work/remake-20261116/render"
DEFAULT_OUTPUT = ROOT / "submission/演示视频-复核修正版-r2-20261116.mp4"
ORIGINAL = ROOT / "submission/演示视频.mp4"
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
WIDTH, HEIGHT, FPS = 1920, 1080, 30
PX, PY, PW, PH = 72, 218, 1776, 704
BG = "#f5f4ef"
INK = "#182a28"
MUTED = "#63716c"
GREEN = "#247565"
BLUE = "#376a9c"
FONT_REG = Path("C:/Windows/Fonts/msyh.ttc")
FONT_BOLD = Path("C:/Windows/Fonts/msyhbd.ttc")


def run(args: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(args, cwd=cwd, capture_output=True, encoding="utf-8", errors="replace")
    if check and result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {result.stderr[-6000:]}")
    return result


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def path_for(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def media_duration(path: Path) -> float:
    result = run([FFMPEG, "-hide_banner", "-i", str(path)], check=False)
    match = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    if not match:
        raise ValueError(f"Cannot read duration: {path}")
    return int(match[1]) * 3600 + int(match[2]) * 60 + float(match[3])


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_BOLD if bold else FONT_REG), size)


def wrap(text: str, face: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    result: list[str] = []
    for paragraph in text.split("\n"):
        line = ""
        for char in paragraph:
            if line and face.getlength(line + char) > max_width:
                result.append(line)
                line = char
            else:
                line += char
        result.append(line)
    return result


def multiline(draw: ImageDraw.ImageDraw, text: str, xy: tuple[int, int], *, size: int,
              width: int, fill: str = INK, bold: bool = False, spacing: int = 15,
              max_lines: int | None = None) -> int:
    face = font(size, bold)
    lines = wrap(text, face, width)
    if max_lines is not None and len(lines) > max_lines:
        raise ValueError(f"Text exceeds {max_lines} lines: {text}")
    x, y = xy
    for line in lines:
        draw.text((x, y), line, font=face, fill=fill, anchor="lt")
        y += size + spacing
    return y


def pill(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str,
         *, fill: str = "#e5ede6", color: str = GREEN, size: int = 22) -> None:
    face = font(size)
    x, y = xy
    width = math.ceil(face.getlength(text)) + 32
    draw.rounded_rectangle((x, y, x + width, y + 42), radius=21, fill=fill)
    draw.text((x + 16, y + 8), text, font=face, fill=color, anchor="lt")


def make_chrome(segment: dict, index: int, total: int, dest: Path) -> None:
    image = Image.new("RGBA", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    # A modest inset border gives the recording a surface without a fake browser.
    draw.rounded_rectangle((PX - 2, PY - 2, PX + PW + 2, PY + PH + 2), radius=20, fill="#d9dfd6")
    draw.rounded_rectangle((PX, PY, PX + PW, PY + PH), radius=18, fill=(0, 0, 0, 0))
    draw.text((PX, 48), "AGENTFORGE", font=font(22, True), fill=GREEN, anchor="lt")
    kicker = str(segment.get("kicker", "多智能体软件开发工作台"))
    draw.text((PX + 208, 48), kicker, font=font(22), fill=MUTED, anchor="lt")
    chapter = str(segment.get("chapter", "01"))
    draw.text((WIDTH - PX, 48), f"{chapter.zfill(2)}", font=font(22, True), fill=GREEN, anchor="rt")
    title = str(segment.get("title", "从需求到可运行的软件"))
    face_size = 48
    while font(face_size, True).getlength(title) > PW and face_size > 36:
        face_size -= 2
    if font(face_size, True).getlength(title) > PW:
        raise ValueError(f"Header title is too long: {title}")
    draw.text((PX, 99), title, font=font(face_size, True), fill=INK, anchor="lt")
    source = str(segment.get("source", ""))
    if source:
        pill(draw, (PX, 164), source, size=20)
    draw.line((PX, 1051, WIDTH - PX, 1051), fill="#dce2d9", width=3)
    draw.line((PX, 1051, PX + round(PW * (index + 1) / total), 1051), fill=GREEN, width=3)
    draw.text((PX, 1022), "奶龙队  /  未来杯 · 赛题一", font=font(17), fill=MUTED, anchor="lt")
    draw.text((WIDTH - PX, 1022), f"{index + 1:02d} / {total:02d}", font=font(17), fill=MUTED, anchor="rt")
    image.save(dest)


def make_card(segment: dict, dest: Path) -> None:
    image = Image.new("RGB", (PW, PH), "#edf1e8")
    draw = ImageDraw.Draw(image)
    draw.ellipse((1430, -280, 2080, 370), fill="#e4ebe0")
    draw.ellipse((1490, -200, 1970, 280), outline="#c9d8c6", width=2)
    draw.line((72, 65, 150, 65), fill=GREEN, width=6)
    headline = str(segment.get("headline", segment.get("title", "AgentForge")))
    end_y = multiline(draw, headline, (72, 110), size=72, bold=True, width=PW - 144, max_lines=2)
    detail = str(segment.get("detail", ""))
    if detail:
        end_y = multiline(draw, detail, (74, end_y + 25), size=30, width=PW - 160,
                          fill=MUTED, spacing=15, max_lines=3)
    stats = segment.get("stats", [])
    if stats:
        if len(stats) > 4:
            raise ValueError("At most four card stats are supported")
        box_y = max(392, end_y + 30)
        if box_y + 200 > PH - 32:
            raise ValueError("Card detail and stats do not fit")
        gap = 28
        box_w = (PW - 144 - gap * (len(stats) - 1)) / len(stats)
        for i, stat in enumerate(stats):
            x = int(72 + i * (box_w + gap))
            draw.rounded_rectangle((x, box_y, x + box_w, box_y + 196), radius=18, fill="#fbfcf8")
            value = str(stat["value"])
            val_size = 63
            while font(val_size, True).getlength(value) > box_w - 44 and val_size > 32:
                val_size -= 2
            draw.text((x + 25, box_y + 24), value, font=font(val_size, True), fill=GREEN, anchor="lt")
            multiline(draw, str(stat["label"]), (x + 25, box_y + 118), size=26,
                      width=int(box_w - 44), fill=MUTED, spacing=9, max_lines=2)
    elif segment.get("flow"):
        flow = segment["flow"]
        if not 2 <= len(flow) <= 5:
            raise ValueError("flow must contain 2–5 labels")
        gap = 60
        box_w = (PW - 144 - gap * (len(flow) - 1)) / len(flow)
        y = max(450, end_y + 45)
        if y + 128 > PH - 40:
            raise ValueError("Card flow does not fit")
        for i, label in enumerate(flow):
            x = int(72 + i * (box_w + gap))
            draw.rounded_rectangle((x, y, x + box_w, y + 128), radius=18, fill="#fbfcf8")
            draw.text((x + 22, y + 20), f"0{i + 1}", font=font(18, True), fill=GREEN, anchor="lt")
            multiline(draw, str(label), (x + 22, y + 54), size=27, width=int(box_w - 44), max_lines=1)
            if i + 1 < len(flow):
                mid = int(x + box_w + gap / 2)
                draw.line((mid - 15, y + 64, mid + 15, y + 64), fill=GREEN, width=2)
                draw.line((mid + 8, y + 58, mid + 15, y + 64, mid + 8, y + 70), fill=GREEN, width=2)
    image.save(dest)


def make_image(segment: dict, dest: Path) -> None:
    image = Image.open(path_for(segment["media"])).convert("RGB")
    if segment.get("crop"):
        x, y, w, h = segment["crop"]
        if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > image.width or y + h > image.height:
            raise ValueError(f"Crop exceeds image bounds: {segment['crop']} in {image.size}")
        image = image.crop((x, y, x + w, y + h))
    image.thumbnail((PW, PH), Image.Resampling.LANCZOS)
    # thumbnail will not enlarge; a deliberately cropped detail should enlarge.
    scale = min(PW / image.width, PH / image.height)
    image = image.resize((round(image.width * scale), round(image.height * scale)), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (PW, PH), "#ffffff")
    canvas.paste(image, ((PW - image.width) // 2, (PH - image.height) // 2))
    canvas.save(dest)


def caption_lines(text: str) -> list[str]:
    chunks = re.findall(r"[^，。！？；：,!?;:]+[，。！？；：,!?;:]?", text)
    combined: list[str] = []
    current = ""
    face = font(40)
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        if current and face.getlength(current + chunk) > 1600:
            combined.extend(wrap(current, face, 1600))
            current = chunk
        else:
            current += chunk
    if current:
        combined.extend(wrap(current, face, 1600))
    return combined


def captions(segment: dict, duration: float) -> list[dict]:
    if segment.get("no_subtitle"):
        return []
    supplied = segment.get("subtitle_cues", segment.get("subtitle_lines"))
    if supplied and isinstance(supplied[0], dict):
        result = supplied
    else:
        lines = supplied or caption_lines(segment.get("text", ""))
        if not lines:
            return []
        weights = [max(1, len(re.sub(r"[，。！？；：,!?;:\s]", "", line))) for line in lines]
        total = sum(weights)
        start = 0.0
        result = []
        for line, weight in zip(lines, weights):
            end = start + duration * weight / total
            result.append({"text": line, "start": start, "end": end})
            start = end
    for item in result:
        if font(40).getlength(item["text"]) > 1700:
            raise ValueError(f"Subtitle too wide: {item['text']}")
        if not 0 <= item["start"] < item["end"] <= duration + 0.1:
            raise ValueError(f"Subtitle timing out of bounds: {item}")
    return result


def render_segment(segment: dict, index: int, total: int, args: argparse.Namespace) -> dict:
    kind = segment.get("kind", "image")
    if kind not in ("image", "video", "card"):
        raise ValueError(f"Unknown segment kind: {kind}")
    audio = path_for(segment["audio"])
    if not audio.is_file():
        raise FileNotFoundError(audio)
    audio_seconds = media_duration(audio)
    tail = float(segment.get("tail", segment.get("pause_after", 0.30)))
    duration = max(audio_seconds + tail, float(segment.get("duration", 0)))
    frame_count = math.ceil(duration * FPS)
    duration = frame_count / FPS
    source_hashes = {segment["audio"]: sha(audio)}
    if kind in ("image", "video"):
        media = path_for(segment["media"])
        source_hashes[segment["media"]] = sha(media)
    key = hashlib.sha256(json.dumps({"segment": segment, "index": index, "total": total,
            "sources": source_hashes, "script": sha(Path(__file__)), "fps": FPS,
            "ffmpeg": FFMPEG, "preset": args.preset, "crf": args.crf,
            "font_regular": sha(FONT_REG), "font_bold": sha(FONT_BOLD)},
            sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
    caption_list = captions(segment, audio_seconds)
    previous = getattr(args, "reuse_records", {}).get(index)
    if previous and not args.no_cache:
        previous_clip = path_for(previous["clip"])
        if (previous.get("segment") == segment and previous.get("source_hashes") == source_hashes
                and previous.get("captions") == caption_list
                and abs(previous["duration"] - duration) < 0.00001 and previous_clip.is_file()):
            record = json.loads(json.dumps(previous, ensure_ascii=False))
            record["frame_count"] = frame_count
            record["reused_from_build"] = str(args.reuse_build)
            print(f"[{index+1}/{total}] reuse visual {duration:.2f}s {segment.get('title', '')}", flush=True)
            return record
    directory = WORK / f"{index:03d}-{key}"
    directory.mkdir(parents=True, exist_ok=True)
    clip = directory / "clip.mp4"
    record = {"index": index, "kind": kind, "duration": duration, "audio_duration": audio_seconds,
              "source_hashes": source_hashes, "segment": segment, "captions": caption_list,
              "clip": str(clip.relative_to(ROOT)).replace("\\", "/"), "cache_key": key,
              "frame_count": frame_count}
    if clip.exists() and (directory / "complete.json").exists() and not args.no_cache:
        print(f"[{index+1}/{total}] cached {duration:.2f}s {segment.get('title', '')}", flush=True)
        return record
    print(f"[{index+1}/{total}] render {duration:.2f}s {segment.get('title', '')}", flush=True)
    make_chrome(segment, index, total, directory / "chrome.png")
    shutil.copyfile(FONT_REG, directory / "font.ttc")
    command = [FFMPEG, "-y", "-hide_banner", "-loglevel", "warning"]
    if kind == "video":
        if float(segment.get("media_rate", 1)) <= 0:
            raise ValueError("media_rate must be positive")
        command.extend(["-ss", str(segment.get("media_start", 0)), "-i", str(media)])
        operations = []
        if segment.get("crop"):
            x, y, w, h = segment["crop"]
            operations.append(f"crop={w}:{h}:{x}:{y}")
        operations.extend([f"setpts=(PTS-STARTPTS)/{float(segment.get('media_rate', 1))}", f"fps={FPS}",
            f"scale={PW}:{PH}:force_original_aspect_ratio=decrease",
            f"pad={PW}:{PH}:(ow-iw)/2:(oh-ih)/2:color=white", "setsar=1",
            f"tpad=stop_mode=clone:stop_duration={duration:.4f}"])
        media_filters = ",".join(operations)
    else:
        if kind == "card":
            make_card(segment, directory / "media.png")
        else:
            make_image(segment, directory / "media.png")
        command.extend(["-loop", "1", "-framerate", str(FPS), "-i", "media.png"])
        zoom = float(segment.get("zoom", 0.012 if kind == "image" else 0))
        if zoom:
            media_filters = (f"zoompan=z='1+{zoom:.6f}*on/{max(1,frame_count-1)}':"
                f"x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=1:s={PW}x{PH}:fps={FPS}")
        else:
            media_filters = "null"
    command.extend(["-loop", "1", "-framerate", str(FPS), "-i", "chrome.png"])
    filters = [f"[0:v]{media_filters},pad={WIDTH}:{HEIGHT}:{PX}:{PY}:color={BG}[scene]",
               "[scene][1:v]overlay=0:0:shortest=1[framed]"]
    caption_filters = []
    for n, item in enumerate(caption_list):
        (directory / f"caption-{n}.txt").write_text(item["text"], encoding="utf-8")
        caption_filters.append(f"drawtext=fontfile=font.ttc:textfile=caption-{n}.txt:expansion=none:"
            f"fontsize=40:fontcolor={INK}:x=(w-text_w)/2:y=958:"
            f"enable='gte(t,{item['start']:.4f})*lt(t,{item['end']:.4f})'")
    # Keep captions completely still and sharp; only the screenshot region moves.
    caption_filters.extend([f"fade=t=in:st=0:d=0.16:color={BG}", "format=yuv420p"])
    filters.append("[framed]" + ",".join(caption_filters) + "[v]")
    command.extend(["-filter_complex", ";".join(filters), "-map", "[v]", "-an",
        "-frames:v", str(frame_count), "-c:v", "libx264", "-preset", args.preset,
        "-crf", str(args.crf), "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.1",
        "-r", str(FPS),
        "-movflags", "+faststart", "clip.mp4"])
    result = run(command, cwd=directory, check=False)
    (directory / "ffmpeg.log").write_text(result.stderr, encoding="utf-8")
    if result.returncode:
        raise RuntimeError(f"Segment {index} failed: {result.stderr[-6000:]}")
    (directory / "complete.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def video_frame_count(path: Path) -> int:
    """Count actual H.264 frame packets, without inferring from container duration."""
    result = run([FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(path),
                  "-map", "0:v:0", "-an", "-c:v", "copy", "-progress", "pipe:1", "-f", "null", "-"])
    counts = re.findall(r"^frame=(\d+)\s*$", result.stdout, flags=re.MULTILINE)
    if not counts:
        raise ValueError(f"Unable to count video frames: {path}")
    return int(counts[-1])


def assemble_exact(records: list[dict], output: Path) -> dict:
    """Use the video frame clock for both PCM and captions; encode AAC only once."""
    fingerprints = [{"clip": sha(path_for(r["clip"])), "frames": r["frame_count"],
                     "audio": r["source_hashes"][r["segment"]["audio"]],
                     "gain": r["segment"].get("gain_db", 3),
                     "audio_duration": r["audio_duration"]} for r in records]
    key = hashlib.sha256(json.dumps({"items": fingerprints, "algorithm": "pcm48k-frame-clock-v1"},
                                   sort_keys=True).encode("utf-8")).hexdigest()[:20]
    directory = WORK / f"assembly-{key}"
    directory.mkdir(parents=True, exist_ok=True)
    audio_parts: list[Path] = []
    concat_lines = ["ffconcat version 1.0"]
    total_frames = 0
    for order, record in enumerate(records):
        frame_count = video_frame_count(path_for(record["clip"]))
        if frame_count != record["frame_count"]:
            raise ValueError(f"Segment {record['index']} frame count mismatch: {frame_count} != {record['frame_count']}")
        record["verified_video_frames"] = frame_count
        record["start_frame"] = total_frames
        record["start"] = total_frames / FPS
        total_frames += frame_count
        record["end_frame"] = total_frames
        record["end"] = total_frames / FPS
        record["duration"] = frame_count / FPS
        video_only = directory / f"{order:03d}-video.mp4"
        if not video_only.exists():
            run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", str(path_for(record["clip"])),
                 "-map", "0:v:0", "-an", "-c:v", "copy", "-video_track_timescale", "15360", str(video_only)])
        if video_frame_count(video_only) != frame_count:
            raise ValueError(f"Visual remux changed the frame count for {record['index']}")
        concat_lines.extend([f"file '{video_only.name}'", f"duration {frame_count / FPS:.12f}"])
        samples = frame_count * (48000 // FPS)
        pcm = directory / f"{order:03d}-audio.wav"
        if not pcm.exists():
            segment = record["segment"]
            audio_filter = (f"aresample=48000,asetpts=PTS-STARTPTS,volume={float(segment.get('gain_db',3))}dB,"
                            f"apad,atrim=end_sample={samples},"
                            "afade=t=in:st=0:d=0.025,afade=t=out:st="
                            f"{max(0,record['audio_duration']-0.04):.4f}:d=0.04")
            run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", str(path_for(segment["audio"])),
                 "-map", "0:a:0", "-af", audio_filter, "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(pcm)])
        with wave.open(str(pcm), "rb") as source:
            if (source.getframerate(), source.getnchannels(), source.getsampwidth(), source.getnframes()) != (48000,2,2,samples):
                raise ValueError(f"PCM sample count/format mismatch for segment {record['index']}")
        record["audio_samples"] = samples
        record["audio_start_sample"] = record["start_frame"] * (48000 // FPS)
        audio_parts.append(pcm)
    concat = directory / "video.ffconcat"
    concat.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")
    joined = directory / "narration.wav"
    with wave.open(str(joined), "wb") as target:
        target.setnchannels(2)
        target.setsampwidth(2)
        target.setframerate(48000)
        for pcm in audio_parts:
            with wave.open(str(pcm), "rb") as source:
                while data := source.readframes(48000):
                    target.writeframesraw(data)
    with wave.open(str(joined), "rb") as source:
        if source.getnframes() != total_frames * (48000 // FPS):
            raise ValueError("Joined narration does not match the exact video frame clock")
    result = run([FFMPEG, "-y", "-hide_banner", "-loglevel", "warning", "-f", "concat", "-safe", "0",
         "-i", str(concat), "-i", str(joined), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
         "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart",
         str(output)], check=False)
    (directory / "mux.log").write_text(result.stderr, encoding="utf-8")
    if result.returncode or "non-monotonic" in result.stderr.lower() or "non monoton" in result.stderr.lower():
        raise RuntimeError(f"Exact assembly mux failed or reported timestamp warnings: {result.stderr}")
    actual_frames = video_frame_count(output)
    if actual_frames != total_frames:
        raise ValueError(f"Final frame count mismatch: {actual_frames} != {total_frames}")
    return {"method": "video-only concat; frame-exact PCM; one final AAC encode", "aac_encoding_passes": 1,
            "sample_rate": 48000, "samples_per_video_frame": 48000 // FPS,
            "video_frames": total_frames, "audio_pcm_samples": total_frames * (48000 // FPS),
            "directory": str(directory.relative_to(ROOT)).replace("\\", "/"),
            "joined_pcm_sha256": sha(joined), "mux_stderr": result.stderr}


def timestamp(seconds: float) -> str:
    ms = round(seconds * 1000)
    hours, ms = divmod(ms, 3600000)
    minutes, ms = divmod(ms, 60000)
    secs, ms = divmod(ms, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{ms:03}"


def contact_sheet(records: list[dict], output: Path) -> None:
    # One midpoint per edit, rather than selected attractive frames only.
    columns = 4
    tile_w, tile_h, label_h = 480, 270, 52
    sheet = Image.new("RGB", (columns * tile_w, math.ceil(len(records) / columns) * (tile_h + label_h)), BG)
    draw = ImageDraw.Draw(sheet)
    thumbs = WORK / "contact"
    thumbs.mkdir(exist_ok=True)
    for i, record in enumerate(records):
        thumb = thumbs / f"{i:03d}.jpg"
        run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-ss", str(min(2,record["duration"] / 2)),
             "-i", str(ROOT / record["clip"]), "-frames:v", "1", "-vf", f"scale={tile_w}:{tile_h}", str(thumb)])
        x, y = (i % columns) * tile_w, (i // columns) * (tile_h + label_h)
        sheet.paste(Image.open(thumb), (x, y))
        title = record["segment"].get("title", "")
        draw.text((x + 10, y + tile_h + 5), f"{i+1:02d}  {record['duration']:.1f}s  {title[:20]}",
                  font=font(15), fill=INK)
    sheet.save(output, quality=92)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--only", help="Comma-separated zero-based segment indexes; writes separate preview")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--reuse-build", type=Path, help="Reuse unchanged visual clips from a prior build manifest")
    parser.add_argument("--preset", default="medium")
    parser.add_argument("--crf", type=int, default=19)
    parser.add_argument("--skip-decode-check", action="store_true", help="For previews only")
    args = parser.parse_args()
    args.reuse_records = {}
    if args.reuse_build:
        previous_build = json.loads(args.reuse_build.read_text(encoding="utf-8-sig"))
        args.reuse_records = {item["index"]: item for item in previous_build["segments"]}
    output = args.output.resolve()
    if output == ORIGINAL.resolve():
        raise ValueError("The original demonstration video is immutable")
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    segments = config["segments"]
    if not segments:
        raise ValueError("The edit must contain at least one segment")
    if config.get("captions_json"):
        cue_list = json.loads(path_for(config["captions_json"]).read_text(encoding="utf-8-sig"))
        narration = {}
        if config.get("narration_manifest"):
            narration = {item["id"]: item for item in json.loads(
                path_for(config["narration_manifest"]).read_text(encoding="utf-8-sig"))["segments"]}
        for segment in segments:
            speech = narration.get(segment.get("id"), {})
            start = segment.get("narration_start", speech.get("start"))
            if start is None:
                raise ValueError("Global captions require narration_start or a narration_manifest entry")
            segment["subtitle_cues"] = [{"text": cue["text"], "start": max(0, cue["start"] - start),
                "end": cue["end"] - start} for cue in cue_list if cue["segment"] == segment["id"]]
            if not segment["subtitle_cues"]:
                raise ValueError(f"No word-boundary captions found for {segment['id']}")
            if "pause_after" in speech and "tail" not in segment and "pause_after" not in segment:
                segment["pause_after"] = speech["pause_after"]
    selected = set(int(index) for index in args.only.split(",")) if args.only else set(range(len(segments)))
    if any(index < 0 or index >= len(segments) for index in selected):
        raise ValueError("Preview index is out of bounds")
    if args.only:
        output = output.with_stem(output.stem + "-preview-" + args.only.replace(",", "-"))
    if args.skip_decode_check and not args.only:
        raise ValueError("Final videos require the full decode check")
    output.parent.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    original_sha_before = sha(ORIGINAL) if ORIGINAL.exists() else None
    records = [render_segment(segment, i, len(segments), args) for i, segment in enumerate(segments) if i in selected]
    print("Assembling exact PCM audio against the verified video frame clock", flush=True)
    assembly = assemble_exact(records, output)
    clock = assembly["video_frames"] / FPS
    srt: list[str] = []
    for record in records:
        for line in record["captions"]:
            srt.append(f"{len(srt)+1}\n{timestamp(record['start']+line['start'])} --> {timestamp(record['start']+line['end'])}\n{line['text']}\n")
    output.with_suffix(".srt").write_text("\n".join(srt), encoding="utf-8-sig")
    print("Creating contact sheet", flush=True)
    contact_sheet(records, output.with_suffix(".contact.jpg"))
    probe = run([FFMPEG, "-hide_banner", "-i", str(output)], check=False)
    decode = None
    if not args.skip_decode_check:
        print("Decoding every final audio/video frame", flush=True)
        decode = run([FFMPEG, "-hide_banner", "-v", "error", "-i", str(output), "-f", "null", "-"], check=False)
        if decode.returncode or decode.stderr.strip():
            raise RuntimeError(f"Final decode failed: {decode.stderr}")
    original_sha_after = sha(ORIGINAL) if ORIGINAL.exists() else None
    if original_sha_before != original_sha_after:
        raise RuntimeError("Original demonstration video changed during rendering")
    build = {"title": config.get("title", "AgentForge"), "output": str(output),
             "duration_from_edit": clock, "duration_probed": media_duration(output),
             "bytes": output.stat().st_size, "sha256": sha(output),
             "original_unchanged": original_sha_before == original_sha_after,
             "original_sha256": original_sha_before,
             "format": {"width": WIDTH, "height": HEIGHT, "fps": FPS, "video": "H.264", "audio": "AAC 48kHz stereo"},
             "ffmpeg": run([FFMPEG, "-version"]).stdout.splitlines()[0],
             "full_decode_passed": decode.returncode == 0 if decode else None,
             "probe": probe.stderr, "render_script_sha256": sha(Path(__file__)),
             "config_sha256": sha(args.config), "assembly": assembly, "segments": records}
    output.with_suffix(".build.json").write_text(json.dumps(build, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: build[k] for k in ("output", "duration_probed", "bytes", "sha256", "full_decode_passed")},
                     ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
