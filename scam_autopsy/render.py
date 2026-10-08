"""Original, asset-free Scam Autopsy video illustrations and Kokoro narration.

The cloud renderer only needs Pillow, NumPy, Kokoro, huggingface_hub and FFmpeg.
Preview mode deliberately does not import the speech stack or download a model.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from PIL import Image, ImageDraw, ImageFont


WIDTH, HEIGHT, FPS = 720, 1280, 24
SAMPLE_RATE = 24_000
NAVY = (10, 19, 38)
PANEL = (19, 35, 56)
WHITE = (247, 249, 248)
MUTED = (161, 181, 193)
CORAL = (255, 105, 99)
MINT = (105, 234, 194)
GOLD = (255, 205, 107)
MODEL_REPO = "hexgrad/Kokoro-82M"
MODEL_REVISION = "f3ff3571791e39611d31c381e3a41a3af07b4987"
VOICE_NAME = "af_heart"


@dataclass
class Scene:
    narration: str
    heading: str
    visual: str
    items: list[str]
    label: str
    duration: float = 6.0
    cues: list[tuple[float, float, str]] = field(default_factory=list)


def load_case(path: Path) -> dict[str, Any]:
    case = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(case, dict):
        raise ValueError("Case must be a JSON object")
    for key in ("id", "title", "hook", "description"):
        if not isinstance(case.get(key), str) or not case[key].strip():
            raise ValueError(f"Case needs a nonempty {key}")
    if case.get("format") not in ("short", "long"):
        raise ValueError("format must be 'short' or 'long'")
    urls = case.get("source_urls")
    if not isinstance(urls, list) or not urls or any(
        not isinstance(url, str) or urlparse(url).scheme != "https" or not urlparse(url).netloc
        for url in urls
    ):
        raise ValueError("source_urls must contain HTTPS source links")
    raw_scenes = case.get("scenes")
    if not isinstance(raw_scenes, list) or not raw_scenes:
        raise ValueError("Case needs scenes")
    scenes = []
    for index, raw in enumerate(raw_scenes, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"Scene {index} must be an object")
        for key in ("narration", "heading", "visual", "label"):
            if not isinstance(raw.get(key), str) or not raw[key].strip():
                raise ValueError(f"Scene {index} needs a nonempty {key}")
        if not isinstance(raw.get("items"), list) or any(
            not isinstance(item, str) for item in raw["items"]
        ):
            raise ValueError(f"Scene {index} needs string items")
        scenes.append(Scene(**{key: raw[key] for key in ("narration", "heading", "visual", "items", "label")}))
    if case["format"] == "short" and len(scenes) != 7:
        raise ValueError("Shorts need exactly seven distinct scenes")
    case["scenes"] = scenes
    return case


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    env = os.environ.get("SCAM_AUTOPSY_FONT_BOLD" if bold else "SCAM_AUTOPSY_FONT")
    names = [
        env,
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    for name in names:
        if name and Path(name).exists():
            return ImageFont.truetype(name, size)
    return ImageFont.load_default(size=size)


def _fit_lines(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int, max_lines: int = 3) -> list[str]:
    words = text.split()
    lines: list[str] = []
    line = ""
    for word in words:
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        while draw.textlength(lines[-1] + "…", font=font) > max_width and len(lines[-1]) > 1:
            lines[-1] = lines[-1][:-1]
        lines[-1] += "…"
    return lines


def _text(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, *, size: int, fill: tuple[int, int, int], bold: bool = False, max_width: int = 560, max_lines: int = 3, spacing: int = 8) -> int:
    font = _font(size, bold)
    y = xy[1]
    for line in _fit_lines(draw, text, font, max_width, max_lines):
        draw.text((xy[0], y), line, font=font, fill=fill, stroke_width=0)
        y += size + spacing
    return y


def _ease(value: float) -> float:
    value = max(0.0, min(1.0, value))
    return value * value * (3 - 2 * value)


def _background(accent: tuple[int, int, int]) -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), NAVY)
    draw = ImageDraw.Draw(image)
    for y in range(HEIGHT):
        mix = y / HEIGHT
        tone = (int(10 + 7 * mix), int(19 + 9 * mix), int(38 + 11 * mix))
        draw.line((0, y, WIDTH, y), fill=tone)
    for x in range(50, WIDTH, 68):
        draw.line((x, 120, x, 1010), fill=(23, 39, 58), width=1)
    for y in range(135, 1010, 68):
        draw.line((50, y, 645, y), fill=(23, 39, 58), width=1)
    draw.ellipse((470, 325, 825, 680), outline=tuple(max(30, c // 3) for c in accent), width=2)
    draw.ellipse((-130, 740, 220, 1090), outline=(33, 55, 70), width=2)
    return image


def _rounded(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], fill: tuple[int, int, int], radius: int = 24, outline: tuple[int, int, int] | None = None, width: int = 2) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def _kind(visual: str) -> str:
    value = visual.lower()
    if any(word in value for word in ("phone", "message", "text", "sms", "chat", "dm", "whatsapp")):
        return "phone"
    if any(word in value for word in ("warning", "red flag", "alarm", "danger", "alert")):
        return "warning"
    if any(word in value for word in ("flow", "chain", "step", "route", "sequence")):
        return "flow"
    if any(word in value for word in ("payment", "bank", "transfer", "invoice", "money", "upi")):
        return "payment"
    if any(word in value for word in ("profile", "imperson", "account", "identity", "avatar")):
        return "profile"
    if any(word in value for word in ("source", "record", "receipt", "evidence", "document")):
        return "receipt"
    return "cards"


def _draw_phone(draw: ImageDraw.ImageDraw, scene: Scene, t: float, accent: tuple[int, int, int]) -> None:
    slide = int((1 - _ease(t / 0.55)) * 70)
    x, y = 195, 355 + slide
    _rounded(draw, (x, y, x + 330, y + 500), (6, 13, 28), 42, accent, 5)
    _rounded(draw, (x + 15, y + 20, x + 315, y + 476), (24, 42, 62), 28)
    _rounded(draw, (x + 119, y + 10, x + 211, y + 25), (6, 13, 28), 8)
    draw.ellipse((x + 35, y + 47, x + 73, y + 85), fill=accent)
    _text(draw, (x + 88, y + 48), "UNKNOWN CONTACT", size=17, fill=WHITE, bold=True, max_width=210, max_lines=1)
    draw.line((x + 28, y + 105, x + 302, y + 105), fill=(62, 83, 101), width=2)
    messages = scene.items[:3]
    for i, item in enumerate(messages):
        reveal = _ease((t - 0.45 - i * 0.55) / 0.38)
        if reveal <= 0:
            continue
        yy = y + 135 + i * 99
        width = int(266 * reveal)
        _rounded(draw, (x + 33, yy, x + 33 + width, yy + 78), PANEL if i % 2 == 0 else (28, 77, 76), 18)
        if reveal > 0.72:
            _text(draw, (x + 47, yy + 13), item, size=21, fill=WHITE, max_width=235, max_lines=2, spacing=2)
    draw.ellipse((x + 151, y + 484, x + 178, y + 511), fill=accent)


def _draw_warning(draw: ImageDraw.ImageDraw, scene: Scene, t: float, accent: tuple[int, int, int]) -> None:
    pulse = 1 + 0.035 * math.sin(t * 5)
    cx, cy = 360, 585
    radius = int(175 * pulse * _ease(t / 0.48))
    if radius:
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=(49, 35, 51), outline=CORAL, width=4)
    triangle = [(360, 438), (500, 690), (220, 690)]
    draw.polygon(triangle, fill=CORAL)
    draw.polygon([(360, 475), (470, 670), (250, 670)], fill=(50, 35, 52))
    _text(draw, (336, 516), "!", size=125, fill=CORAL, bold=True, max_width=80, max_lines=1)
    for i, item in enumerate(scene.items[:2]):
        if t < 1.1 + i * 0.55:
            continue
        yy = 743 + i * 59
        _rounded(draw, (95, yy, 625, yy + 46), PANEL, 14, (82, 70, 79))
        _text(draw, (115, yy + 10), item, size=21, fill=WHITE, max_width=490, max_lines=1)


def _draw_flow(draw: ImageDraw.ImageDraw, scene: Scene, t: float, accent: tuple[int, int, int]) -> None:
    labels = scene.items[:3]
    positions = {1: [(305, 535)], 2: [(155, 535), (425, 535)], 3: [(115, 503), (290, 593), (465, 503)]}[len(labels)] if labels else []
    for i, label in enumerate(labels):
        reveal = _ease((t - i * 0.55) / 0.5)
        if reveal <= 0:
            continue
        x, y = positions[i]
        size = int(108 * reveal)
        draw.ellipse((x, y, x + size, y + size), fill=(24, 58, 67), outline=accent, width=4)
        _text(draw, (x + 41, y + 29), str(i + 1), size=38, fill=accent, bold=True, max_width=50, max_lines=1)
        _text(draw, (x - 20, y + 126), label, size=21, fill=WHITE, bold=True, max_width=150, max_lines=2)
        if i < len(labels) - 1 and t > 0.7 + i * 0.55:
            next_x, next_y = positions[i + 1]
            end = next_x - 23
            mid_y = (y + next_y) // 2 + 54
            draw.line((x + 110, y + 54, end, mid_y), fill=CORAL, width=6)
            draw.polygon([(end + 8, mid_y), (end - 9, mid_y - 9), (end - 9, mid_y + 9)], fill=CORAL)


def _draw_payment(draw: ImageDraw.ImageDraw, scene: Scene, t: float, accent: tuple[int, int, int]) -> None:
    shift = int((1 - _ease(t / 0.55)) * 60)
    _rounded(draw, (113, 418 + shift, 607, 797 + shift), (27, 45, 62), 27, (76, 109, 117), 3)
    _text(draw, (145, 449 + shift), "PAYMENT REQUEST", size=22, fill=MUTED, bold=True, max_width=410)
    draw.line((145, 497 + shift, 575, 497 + shift), fill=(79, 104, 116), width=2)
    for i, item in enumerate(scene.items[:3]):
        if t < 0.45 + i * 0.55:
            continue
        yy = 530 + i * 68 + shift
        draw.ellipse((147, yy + 6, 172, yy + 31), outline=accent, width=3)
        _text(draw, (192, yy), item, size=24, fill=WHITE, max_width=360, max_lines=1)
    _rounded(draw, (144, 744 + shift, 575, 785 + shift), (70, 43, 49), 12)
    _text(draw, (260, 750 + shift), "HOLD TO VERIFY", size=20, fill=CORAL, bold=True, max_width=290, max_lines=1)


def _draw_profile(draw: ImageDraw.ImageDraw, scene: Scene, t: float, accent: tuple[int, int, int]) -> None:
    _rounded(draw, (115, 409, 605, 818), PANEL, 29, (71, 104, 111), 3)
    draw.ellipse((255, 448, 465, 658), fill=(44, 79, 86), outline=accent, width=4)
    draw.ellipse((329, 487, 391, 549), fill=accent)
    draw.arc((290, 526, 430, 646), 185, 355, fill=accent, width=27)
    _text(draw, (160, 675), "LOOKS FAMILIAR?", size=33, fill=WHITE, bold=True, max_width=400, max_lines=1)
    if t > 0.7:
        draw.line((185, 740, 530, 740), fill=CORAL, width=5)
        _text(draw, (180, 754), scene.items[0] if scene.items else "Verify independently", size=22, fill=MINT, max_width=380, max_lines=2)


def _draw_receipt(draw: ImageDraw.ImageDraw, scene: Scene, t: float, accent: tuple[int, int, int]) -> None:
    slide = int((1 - _ease(t / 0.6)) * 80)
    _rounded(draw, (149, 400 + slide, 570, 839 + slide), WHITE, 12, accent, 3)
    _rounded(draw, (178, 429 + slide, 542, 472 + slide), NAVY, 8)
    _text(draw, (195, 437 + slide), "SOURCE RECORD", size=19, fill=MINT, bold=True, max_width=320, max_lines=1)
    for i, item in enumerate(scene.items[:3]):
        if t < 0.55 + i * 0.53:
            continue
        yy = 505 + slide + i * 101
        draw.ellipse((180, yy + 4, 205, yy + 29), fill=CORAL if i == 0 else MINT)
        _text(draw, (220, yy), item, size=23, fill=NAVY, bold=True, max_width=316, max_lines=2)
        draw.line((180, yy + 77, 535, yy + 77), fill=(205, 214, 215), width=2)


def _draw_cards(draw: ImageDraw.ImageDraw, scene: Scene, t: float, accent: tuple[int, int, int]) -> None:
    items = scene.items[:3]
    for i, item in enumerate(items):
        reveal = _ease((t - i * 0.45) / 0.45)
        if reveal <= 0:
            continue
        yy = 432 + i * 135
        x = int(75 + (1 - reveal) * 110)
        _rounded(draw, (x, yy, 645, yy + 108), PANEL, 20, accent if i == 0 else (65, 93, 101))
        _rounded(draw, (x + 19, yy + 23, x + 65, yy + 69), accent if i == 0 else (38, 72, 77), 12)
        _text(draw, (x + 31, yy + 28), str(i + 1), size=23, fill=NAVY, bold=True, max_width=28, max_lines=1)
        _text(draw, (x + 83, yy + 25), item, size=25, fill=WHITE, bold=True, max_width=440, max_lines=2)


_ART = {
    "phone": _draw_phone,
    "warning": _draw_warning,
    "flow": _draw_flow,
    "payment": _draw_payment,
    "profile": _draw_profile,
    "receipt": _draw_receipt,
    "cards": _draw_cards,
}


def _source_name(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _caption(scene: Scene, local_time: float) -> str:
    speech_time = local_time - 0.25
    if speech_time < 0:
        return ""
    cues = scene.cues
    if not cues:
        words = scene.narration.split()
        count = min(len(words) - 1, max(0, int(speech_time / max(0.1, scene.duration - 0.55) * len(words))))
        return " ".join(words[(count // 6) * 6:(count // 6 + 1) * 6])
    current = 0
    for i, (start, _, _) in enumerate(cues):
        if speech_time >= start:
            current = i
        else:
            break
    if speech_time > cues[-1][1] + 0.2:
        return ""
    start = (current // 6) * 6
    return " ".join(text for _, _, text in cues[start:start + 6])


def render_frame(case: dict[str, Any], scene_index: int, local_time: float, background: Image.Image | None = None) -> Image.Image:
    scenes: list[Scene] = case["scenes"]
    scene = scenes[scene_index]
    accent = (MINT, CORAL, GOLD)[scene_index % 3]
    image = (background or _background(accent)).copy()
    draw = ImageDraw.Draw(image)
    # The top 120 px, bottom 250 px, and right 100 px stay free of key text.
    _text(draw, (68, 131), "SCAM / AUTOPSY", size=24, fill=MINT, bold=True, max_width=340, max_lines=1)
    _text(draw, (529, 133), f"{scene_index + 1:02d} / {len(scenes):02d}", size=23, fill=MUTED, bold=True, max_width=115, max_lines=1)
    draw.line((68, 179, 620, 179), fill=(78, 106, 115), width=2)
    segment_width = 552 / len(scenes)
    for i in range(len(scenes)):
        x0 = 68 + int(i * segment_width)
        x1 = 68 + int((i + 1) * segment_width) - 8
        color = accent if i < scene_index else (58, 80, 92)
        if i == scene_index:
            x1 = x0 + int((x1 - x0) * max(0.03, min(1.0, local_time / scene.duration)))
        draw.line((x0, 191, x1, 191), fill=color, width=5)
    _text(draw, (68, 219), scene.label.upper(), size=21, fill=accent, bold=True, max_width=550, max_lines=1)
    heading_y = 253 + int((1 - _ease(local_time / 0.45)) * 18)
    _text(draw, (68, heading_y), scene.heading, size=51, fill=WHITE, bold=True, max_width=540, max_lines=2, spacing=4)
    _ART[_kind(scene.visual)](draw, scene, local_time, accent)
    sources = list(dict.fromkeys(_source_name(url) for url in case["source_urls"]))
    source = " · ".join(sources[:2])
    _rounded(draw, (68, 861, 620, 900), (29, 54, 70), 12)
    _text(draw, (85, 870), f"SOURCES  {source}", size=18, fill=MINT, bold=True, max_width=510, max_lines=1)
    caption = _caption(scene, local_time)
    _rounded(draw, (62, 925, 622, 1021), (5, 12, 27), 21, (68, 96, 108), 2)
    if caption:
        _text(draw, (87, 939), caption, size=30, fill=WHITE, bold=True, max_width=507, max_lines=2, spacing=1)
    return image


def _kokoro_pipeline() -> Any:
    """Resolve model bytes at a fixed commit, then load local paths on CPU."""
    from huggingface_hub import hf_hub_download
    from kokoro import KModel, KPipeline

    files = {name: hf_hub_download(repo_id=MODEL_REPO, filename=name, revision=MODEL_REVISION) for name in (
        "config.json", "kokoro-v1_0.pth", f"voices/{VOICE_NAME}.pt"
    )}
    model = KModel(repo_id=MODEL_REPO, config=files["config.json"], model=files["kokoro-v1_0.pth"]).to("cpu").eval()
    pipeline = KPipeline(lang_code="a", repo_id=MODEL_REPO, model=model, device="cpu")
    return pipeline, files[f"voices/{VOICE_NAME}.pt"]


def synthesize_scenes(scenes: list[Scene], wav_path: Path) -> None:
    """Synthesize every scene independently so every cut follows its own audio."""
    import numpy as np

    pipeline, voice_path = _kokoro_pipeline()
    segments: list[np.ndarray] = []
    for index, scene in enumerate(scenes, 1):
        chunks: list[np.ndarray] = []
        cues: list[tuple[float, float, str]] = []
        cursor = 0.0
        for result in pipeline(scene.narration, voice=voice_path, speed=1.08):
            if result.audio is None:
                continue
            audio = result.audio.detach().cpu().numpy().astype(np.float32).reshape(-1)
            chunks.append(audio)
            for token in result.tokens or []:
                text = str(getattr(token, "text", "")).strip()
                start = getattr(token, "start_ts", None)
                end = getattr(token, "end_ts", None)
                if text and start is not None and end is not None:
                    if re.fullmatch(r"[^\w]+", text) and cues:
                        prior = cues[-1]
                        cues[-1] = (prior[0], max(prior[1], cursor + float(end)), prior[2] + text)
                    else:
                        cues.append((cursor + float(start), cursor + float(end), text))
            cursor += len(audio) / SAMPLE_RATE
        if not chunks:
            raise RuntimeError(f"Kokoro produced no audio for scene {index}")
        speech = np.concatenate(chunks)
        # A soft peak limit prevents clipping without changing scene timing.
        peak = float(np.max(np.abs(speech)))
        if peak > 0.91:
            speech = speech * (0.91 / peak)
        fade = min(len(speech) // 2, int(0.012 * SAMPLE_RATE))
        if fade:
            speech[:fade] *= np.linspace(0, 1, fade, dtype=np.float32)
            speech[-fade:] *= np.linspace(1, 0, fade, dtype=np.float32)
        before = np.zeros(int(0.25 * SAMPLE_RATE), dtype=np.float32)
        after = np.zeros(int(0.30 * SAMPLE_RATE), dtype=np.float32)
        segment = np.concatenate((before, speech, after))
        scene.duration = len(segment) / SAMPLE_RATE
        scene.cues = cues
        segments.append(segment)
    total = sum(scene.duration for scene in scenes)
    if total < 35 and len(scenes) == 7:
        pad = (35 - total) / len(scenes)
        for i, segment in enumerate(segments):
            extra = np.zeros(math.ceil(pad * SAMPLE_RATE), dtype=np.float32)
            segments[i] = np.concatenate((segment, extra))
            scenes[i].duration = len(segments[i]) / SAMPLE_RATE
    all_audio = np.concatenate(segments)
    with wave.open(str(wav_path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes((np.clip(all_audio, -1, 1) * 32767).astype("<i2").tobytes())


def _probe(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    )
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not video or not audio:
        raise RuntimeError("Rendered file is missing video or audio")
    duration = float(data["format"]["duration"])
    if video.get("codec_name") != "h264" or audio.get("codec_name") != "aac":
        raise RuntimeError("Rendered codecs are not H.264 and AAC")
    if video.get("pix_fmt") != "yuv420p" or int(video["width"]) != WIDTH or int(video["height"]) != HEIGHT:
        raise RuntimeError("Rendered dimensions or pixel format are wrong")
    if abs(duration - float(audio.get("duration", duration))) > 0.35:
        raise RuntimeError("Audio and video duration differ")
    return {
        "file": str(path), "duration_seconds": round(duration, 3), "width": WIDTH, "height": HEIGHT,
        "fps": video.get("avg_frame_rate"), "video_codec": video["codec_name"],
        "audio_codec": audio["codec_name"], "pixel_format": video["pix_fmt"],
        "video_duration_seconds": round(float(video.get("duration", duration)), 3),
        "audio_duration_seconds": round(float(audio.get("duration", duration)), 3),
        "audio_sample_rate": audio.get("sample_rate"), "bytes": path.stat().st_size,
    }


def render_video(case: dict[str, Any], output: Path) -> dict[str, Any]:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("FFmpeg and FFprobe must be installed")
    output.parent.mkdir(parents=True, exist_ok=True)
    scenes: list[Scene] = case["scenes"]
    with tempfile.TemporaryDirectory(prefix="scam-autopsy-render-") as directory:
        audio_path = Path(directory) / "narration.wav"
        synthesize_scenes(scenes, audio_path)
        total = sum(scene.duration for scene in scenes)
        if case["format"] == "short" and total > 55:
            raise ValueError(f"Narration is {total:.1f}s; shorten the script to stay within 55s")
        frames = [max(1, round(scene.duration * FPS)) for scene in scenes]
        expected_duration = sum(frames) / FPS
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
            "-s:v", f"{WIDTH}x{HEIGHT}", "-r", str(FPS), "-i", "pipe:0", "-i", str(audio_path),
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-pix_fmt", "yuv420p", "-r", str(FPS), "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
            "-t", f"{expected_duration:.4f}", "-movflags", "+faststart", str(output),
        ]
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            assert process.stdin is not None
            for index, scene in enumerate(scenes):
                background = _background((MINT, CORAL, GOLD)[index % 3])
                for frame in range(frames[index]):
                    image = render_frame(case, index, frame / FPS, background)
                    process.stdin.write(image.tobytes())
            process.stdin.close()
            stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
            if process.wait() != 0:
                raise RuntimeError(f"FFmpeg failed: {stderr[-2000:]}")
        except Exception:
            if process.poll() is None:
                process.kill()
            process.wait()
            raise
        finally:
            if process.stderr is not None:
                process.stderr.close()
    receipt = _probe(output)
    if abs(receipt["duration_seconds"] - expected_duration) > 0.25:
        raise RuntimeError("Output duration does not match the scene timeline")
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", type=Path, help="Case JSON file")
    parser.add_argument("--output", type=Path, help="Output MP4, or PNG with --preview")
    parser.add_argument("--preview", action="store_true", help="Render a still without TTS/model downloads")
    args = parser.parse_args(argv)
    case = load_case(args.case)
    if args.preview:
        output = args.output or args.case.with_name(f"{case['id']}-preview.png")
        if output.suffix.lower() != ".png":
            parser.error("Preview output must be a PNG")
        output.parent.mkdir(parents=True, exist_ok=True)
        render_frame(case, 0, 2.1).save(output)
        print(json.dumps({"preview": str(output), "width": WIDTH, "height": HEIGHT}))
    else:
        output = args.output or args.case.with_name(f"{case['id']}.mp4")
        if output.suffix.lower() != ".mp4":
            parser.error("Video output must be an MP4")
        print(json.dumps(render_video(case, output)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
