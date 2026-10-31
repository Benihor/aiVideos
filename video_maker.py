"""Turn a script and optional pictures/stock media into a short mp4 via ffmpeg."""

from __future__ import annotations

import os
import re
import subprocess
import wave
from pathlib import Path

import imageio_ffmpeg
import numpy as np
from PIL import Image, ImageDraw, ImageFont

W, H = 1280, 720
FPS = 24
FONT_DIR = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
PAPER = (243, 236, 223)
CAPTION = (255, 248, 240)
ACCENT = (196, 85, 42)

PALETTES = [
    ((36, 24, 20), (92, 48, 32), (212, 122, 74)),
    ((22, 32, 34), (38, 72, 74), (118, 168, 150)),
    ((32, 24, 40), (72, 42, 78), (176, 146, 196)),
    ((28, 32, 22), (58, 72, 40), (168, 164, 92)),
    ((40, 22, 28), (96, 40, 48), (214, 132, 122)),
]


def _font(filename: str, size: int) -> ImageFont.ImageFont:
    path = FONT_DIR / filename
    if path.exists():
        return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def chunk_words(text: str, max_words: int = 10) -> list[str]:
    words = text.split()
    if not words:
        return []
    if len(words) <= max_words:
        return [" ".join(words)]
    return [" ".join(words[i : i + max_words]) for i in range(0, len(words), max_words)]


def group_to_max(parts: list[str], limit: int) -> list[str]:
    parts = [part for part in parts if part]
    if len(parts) <= limit:
        return parts
    groups: list[list[str]] = [[] for _ in range(limit)]
    for index, part in enumerate(parts):
        groups[min(limit - 1, index * limit // len(parts))].append(part)
    return [" ".join(group) for group in groups if group]


def _chunk_size_for_count(word_count: int) -> int | None:
    """Fixed chunk size from total script length. None means long-script subject mode."""
    if word_count < 30:
        return 3
    if word_count < 70:
        return 7
    if word_count <= 120:
        return 10
    return None


def _split_long_by_subject(text: str, max_scenes: int = 25) -> list[str]:
    """For scripts above 120 words: prefer subject/clause cuts, else every 20 words."""
    cleaned = clean_text(text)
    pieces = [clean_text(piece) for piece in re.split(r"(?<=[.!?])\s+", cleaned)]
    pieces = [piece for piece in pieces if piece] or [cleaned]

    units: list[str] = []
    for piece in pieces:
        soft = [
            clean_text(part)
            for part in re.split(
                r"\s*(?:\bthen\b|\bwhile\b|\bbefore\b|\bafter\b|\buntil\b|\band\b|;)\s+",
                piece,
                flags=re.IGNORECASE,
            )
            if clean_text(part)
        ]
        for part in soft or [piece]:
            units.extend(chunk_words(part, max_words=20))
    return group_to_max(units, max_scenes)


def split_scenes(text: str, max_words: int = 10, max_scenes: int = 25) -> list[str]:
    """
    Cut photos by total word count:
      <30  -> every 3 words
      30-69 -> every 7 words
      70-120 -> every 10 words
      >120 -> subject/clause cuts, else every 20 words
    Cap at 25 shots. Speech stays one continuous take; photos follow word timing.
    """
    cleaned = clean_text(text)
    if not cleaned:
        return []
    words = cleaned.split()
    size = _chunk_size_for_count(len(words))
    if size is None:
        return _split_long_by_subject(cleaned, max_scenes=max_scenes)
    return group_to_max(chunk_words(cleaned, max_words=size), max_scenes)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    words = text.split()
    if not words:
        return []
    lines = []
    current = words[0]
    for word in words[1:]:
        trial = f"{current} {word}"
        if draw.textlength(trial, font=font) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _layout(text: str, face: str, sizes: list[int], max_width: int, max_height: int, max_lines: int):
    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    for size in sizes:
        chosen = _font(face, size)
        lines = _wrap(probe, text, chosen, max_width)
        line_height = int(size * 1.28)
        if len(lines) <= max_lines and line_height * len(lines) <= max_height:
            return chosen, lines, line_height
    chosen = _font(face, sizes[-1])
    lines = _wrap(probe, text, chosen, max_width)[:max_lines]
    return chosen, lines, int(sizes[-1] * 1.28)


def _ffmpeg() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


def _run_ffmpeg(args: list[str]) -> None:
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "ffmpeg failed").strip().splitlines()
        raise RuntimeError(detail[-1] if detail else "ffmpeg failed")


def _voiceover(text: str, wav_path: Path) -> None:
    script_path = wav_path.with_suffix(".txt")
    script_path.write_text(text, encoding="utf-8-sig")
    command = r"""
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synth.Rate = 0
$synth.SetOutputToWaveFile($env:AIVIDEO_WAV)
$spoken = Get-Content -Raw -Encoding UTF8 $env:AIVIDEO_TXT
$synth.Speak($spoken)
$synth.Dispose()
"""
    env = os.environ.copy()
    env["AIVIDEO_WAV"] = str(wav_path)
    env["AIVIDEO_TXT"] = str(script_path)
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    finally:
        script_path.unlink(missing_ok=True)
    if result.returncode != 0 or not wav_path.exists() or wav_path.stat().st_size < 128:
        detail = (result.stderr or result.stdout or "Voiceover failed").strip()
        raise RuntimeError(detail)


def _wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate() or 1
        return handle.getnframes() / float(rate)


def _durations(scenes: list[str], audio_seconds: float | None) -> list[float]:
    """Photo lengths follow word counts so cuts stay aligned to the speech flow."""
    weights = [max(len(scene.split()), 1) for scene in scenes]
    total = float(sum(weights))
    if audio_seconds is None:
        return [max(0.9, 0.55 * weight) for weight in weights]
    durations = [audio_seconds * weight / total for weight in weights]
    if durations:
        durations[-1] = max(0.2, audio_seconds - sum(durations[:-1]))
    return durations


def _save_background(path: Path, index: int) -> None:
    top, bottom, accent = PALETTES[index % len(PALETTES)]
    yy = np.linspace(0, 1, H)[:, None]
    xx = np.linspace(0, 1, W)[None, :]
    blend = np.clip(yy * 0.78 + xx * 0.22, 0, 1)
    gradient = np.array(top) * (1 - blend[..., None]) + np.array(bottom) * blend[..., None]
    pixels = np.clip(gradient, 0, 255).astype(np.uint8)
    image = Image.fromarray(pixels, "RGB")
    draw = ImageDraw.Draw(image)
    radius = 220
    cx = 360 + (index % 3) * 180
    cy = 240
    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=accent)
    image.save(path, format="JPEG", quality=88)


def _caption_png(
    path: Path,
    *,
    text: str,
    title: str,
    index: int,
    total: int,
    pictured: bool,
    attribution: str | None,
    show_text: bool = True,
) -> None:
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    if not show_text:
        # Keep a transparent overlay so the ffmpeg path stays unchanged.
        overlay.save(path, format="PNG")
        return

    draw = ImageDraw.Draw(overlay)
    label_font = _font("segoeui.ttf", 22)
    credit_font = _font("segoeui.ttf", 16)
    count = f"{index + 1}  /  {total}"
    count_width = draw.textlength(count, font=label_font)

    if pictured:
        font, lines, line_height = _layout(
            text, "segoeuib.ttf", [36, 30, 26, 22], W - 200, 180, 4
        )
        pad_x, pad_y = 28, 18
        block_h = line_height * len(lines)
        max_line = max((draw.textlength(line, font=font) for line in lines), default=0)
        box_w = max_line + pad_x * 2
        box_h = block_h + pad_y * 2
        box_x = (W - box_w) / 2
        box_y = H - 40 - box_h
        draw.rounded_rectangle(
            (box_x, box_y, box_x + box_w, box_y + box_h),
            radius=16,
            fill=(18, 16, 14, 168),
        )
        y = box_y + pad_y
        for line in lines:
            width = draw.textlength(line, font=font)
            x = (W - width) / 2
            draw.text((x, y), line, font=font, fill=(*CAPTION, 250))
            y += line_height
    else:
        font, lines, line_height = _layout(
            text, "georgiab.ttf", [56, 46, 38, 30], W - 200, 420, 7
        )
        block = line_height * len(lines)
        y = (H - block) / 2
        draw.rectangle((W / 2 - 36, y - 28, W / 2 + 36, y - 24), fill=(*ACCENT, 230))
        for line in lines:
            width = draw.textlength(line, font=font)
            draw.text(((W - width) / 2, y), line, font=font, fill=(*PAPER, 250))
            y += line_height

    if title:
        shown = title
        max_title = W - 140 - count_width
        while shown and draw.textlength(shown + "…", font=label_font) > max_title:
            shown = shown[:-1].rstrip()
        if shown != title:
            shown = (shown + "…") if shown else ""
        draw.text((56, 36), shown, font=label_font, fill=(*PAPER, 220))
    draw.text((W - 56 - count_width, 36), count, font=label_font, fill=(*ACCENT, 240))

    # Keep the client video clean: no on-screen stock credit line.
    _ = attribution
    _ = credit_font
    overlay.save(path, format="PNG")


def _credits_png(path: Path, lines: list[str]) -> None:
    image = Image.new("RGB", (W, H), (18, 16, 14))
    draw = ImageDraw.Draw(image)
    title_font = _font("georgiab.ttf", 36)
    body = _font("segoeui.ttf", 20)
    heading = "Media credits"
    draw.text(((W - draw.textlength(heading, font=title_font)) / 2, 48), heading, font=title_font, fill=PAPER)
    y = 120
    for line in lines[:12]:
        shown = line
        while shown and draw.textlength(shown, font=body) > W - 120:
            shown = shown[:-1]
        if shown != line and shown:
            shown = shown[:-1] + "…"
        draw.text((60, y), shown, font=body, fill=(220, 214, 205))
        y += 34
    image.save(path, format="PNG")


def _motion_crop(scene_index: int, duration: float) -> tuple[str, str, int, int]:
    """Return (x_expr, y_expr, scale_w, scale_h) for varied pan / boom motion."""
    d = max(duration, 0.1)
    mode = scene_index % 8
    # Boom shots start more zoomed-in on a larger plate.
    if mode == 0:
        return f"(iw-ow)*t/{d:.3f}", f"(ih-oh)*0.25", 1680, 945
    if mode == 1:
        return f"(iw-ow)*(1-t/{d:.3f})", f"(ih-oh)*0.55", 1680, 945
    if mode == 2:
        return f"(iw-ow)*0.5", f"(ih-oh)*t/{d:.3f}", 1680, 945
    if mode == 3:
        return f"(iw-ow)*0.35", f"(ih-oh)*(1-t/{d:.3f})", 1680, 945
    if mode == 4:
        return f"(iw-ow)*t/{d:.3f}", f"(ih-oh)*(1-t/{d:.3f})", 1760, 990
    if mode == 5:
        return f"(iw-ow)*(1-t/{d:.3f})", f"(ih-oh)*t/{d:.3f}", 1760, 990
    if mode == 6:
        # Punch-in boom: race toward a corner, then settle.
        return (
            f"(iw-ow)*min(1\\,t/{d:.3f}*1.35)",
            f"(ih-oh)*min(1\\,t/{d:.3f}*0.9)",
            1900,
            1069,
        )
    # Punch-out feel: start deep, ease toward center-left.
    return (
        f"(iw-ow)*(1-min(1\\,t/{d:.3f}*1.2))*0.85",
        f"(ih-oh)*(0.55-0.25*min(1\\,t/{d:.3f}))",
        1900,
        1069,
    )


def _render_image_clip(
    *,
    ffmpeg: str,
    image_path: Path,
    caption_path: Path,
    dest: Path,
    duration: float,
    scene_index: int,
) -> None:
    x_expr, y_expr, sw, sh = _motion_crop(scene_index, duration)
    fade = min(0.18, duration / 4)
    vf = (
        f"scale={sw}:{sh}:force_original_aspect_ratio=increase,"
        f"crop={sw}:{sh},"
        f"crop={W}:{H}:x='{x_expr}':y='{y_expr}',"
        f"fade=t=in:st=0:d={fade:.3f},"
        f"fps={FPS},"
        f"format=yuv420p"
    )
    _run_ffmpeg(
        [
            ffmpeg,
            "-y",
            "-loop",
            "1",
            "-i",
            str(image_path),
            "-i",
            str(caption_path),
            "-filter_complex",
            f"[0:v]{vf}[base];[base][1:v]overlay=0:0:format=auto,format=yuv420p[out]",
            "-map",
            "[out]",
            "-t",
            f"{duration:.3f}",
            "-r",
            str(FPS),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-an",
            str(dest),
        ]
    )
    if not dest.exists():
        raise RuntimeError(f"Missing clip for shot {scene_index + 1}")


def _render_video_clip(
    *,
    ffmpeg: str,
    video_path: Path,
    caption_path: Path,
    dest: Path,
    duration: float,
) -> None:
    fade = min(0.18, duration / 4)
    vf = (
        f"scale={W}:{H}:force_original_aspect_ratio=increase,"
        f"crop={W}:{H},"
        f"fade=t=in:st=0:d={fade:.3f},"
        f"fps={FPS},"
        f"format=yuv420p"
    )
    _run_ffmpeg(
        [
            ffmpeg,
            "-y",
            "-stream_loop",
            "-1",
            "-i",
            str(video_path),
            "-i",
            str(caption_path),
            "-filter_complex",
            f"[0:v]{vf}[base];[base][1:v]overlay=0:0:format=auto,format=yuv420p[out]",
            "-map",
            "[out]",
            "-t",
            f"{duration:.3f}",
            "-r",
            str(FPS),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-an",
            str(dest),
        ]
    )


def _render_still_clip(
    *,
    ffmpeg: str,
    image_path: Path,
    dest: Path,
    duration: float,
) -> None:
    _run_ffmpeg(
        [
            ffmpeg,
            "-y",
            "-loop",
            "1",
            "-i",
            str(image_path),
            "-vf",
            f"scale={W}:{H},fps={FPS},format=yuv420p",
            "-t",
            f"{duration:.3f}",
            "-r",
            str(FPS),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-an",
            str(dest),
        ]
    )


def _concat_clips(ffmpeg: str, clips: list[Path], dest: Path) -> None:
    list_path = dest.with_name("concat.txt")
    lines = []
    for clip in clips:
        # ffmpeg concat demuxer needs escaped single quotes on Windows paths.
        safe = str(clip).replace("\\", "/").replace("'", r"'\''")
        lines.append(f"file '{safe}'")
    list_path.write_text("\n".join(lines), encoding="utf-8")
    try:
        _run_ffmpeg(
            [
                ffmpeg,
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_path),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(dest),
            ]
        )
    finally:
        list_path.unlink(missing_ok=True)


def _mux(video_path: Path, audio_path: Path | None, dest: Path) -> None:
    ffmpeg = _ffmpeg()
    command = [ffmpeg, "-y", "-i", str(video_path)]
    if audio_path is not None:
        command += [
            "-i",
            str(audio_path),
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-shortest",
        ]
    else:
        command += ["-c", "copy"]
    command += ["-movflags", "+faststart", str(dest)]
    _run_ffmpeg(command)


def _mux_scene(video_path: Path, audio_path: Path | None, dest: Path) -> None:
    """Attach one spoken segment to its matching picture clip."""
    if audio_path is None:
        if video_path.resolve() == dest.resolve():
            return
        _run_ffmpeg([_ffmpeg(), "-y", "-i", str(video_path), "-c", "copy", str(dest)])
        return
    _mux(video_path, audio_path, dest)


def _silent_wav(path: Path, seconds: float = 2.5) -> None:
    frames = max(1, int(22050 * seconds))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(22050)
        handle.writeframes(b"\x00\x00" * frames)


def make_video(
    *,
    text: str,
    title: str,
    image_paths: list[Path] | None = None,
    scene_media: list[dict] | None = None,
    voice: bool,
    captions: bool = True,
    dest_mp4: Path,
    on_progress,
) -> dict:
    scenes = split_scenes(text)
    if not scenes:
        raise ValueError("Write what the video should say.")

    on_progress(f"Planning {len(scenes)} shot{'s' if len(scenes) != 1 else ''}…")
    image_paths = image_paths or []
    scene_media = scene_media or []
    work = dest_mp4.parent / "work"
    work.mkdir(parents=True, exist_ok=True)
    ffmpeg = _ffmpeg()

    media_paths: list[Path] = []
    media_kinds: list[str] = []
    attributions: list[str | None] = []

    for index, _scene in enumerate(scenes):
        media = scene_media[index] if index < len(scene_media) else None
        if media and media.get("path"):
            path = Path(media["path"])
            attributions.append(media.get("attribution"))
            if media.get("media_type") == "video" or path.suffix.lower() in {".mp4", ".mov", ".webm", ".mkv"}:
                media_paths.append(path)
                media_kinds.append("video")
            else:
                media_paths.append(path)
                media_kinds.append("image")
        elif image_paths:
            media_paths.append(image_paths[index % len(image_paths)])
            media_kinds.append("image")
            attributions.append(None)
        else:
            bg = work / f"bg_{index}.jpg"
            _save_background(bg, index)
            media_paths.append(bg)
            media_kinds.append("image")
            attributions.append(None)

    # One continuous narration — photo cuts follow timing, speech never restarts per shot.
    spoken: Path | None = None
    audio_seconds: float | None = None
    wav_path = work / "voice_full.wav"
    if voice:
        on_progress("Recording the voiceover…")
        # Full script in one take so speech stays smooth across photo cuts.
        spoken_script = clean_text(text)
        try:
            _voiceover(spoken_script, wav_path)
            audio_seconds = _wav_seconds(wav_path)
            spoken = wav_path
        except (OSError, RuntimeError, wave.Error):
            wav_path.unlink(missing_ok=True)
            spoken = None
            audio_seconds = None
            on_progress("Voiceover skipped. Building the picture track…")

    durations = _durations(scenes, audio_seconds)
    clips: list[Path] = []

    for index, scene in enumerate(scenes):
        on_progress(f"Encoding shot {index + 1} of {len(scenes)}…")
        caption = work / f"caption_{index}.png"
        has_real_media = bool(
            scene_media and index < len(scene_media) and scene_media[index].get("path")
        ) or bool(image_paths)
        _caption_png(
            caption,
            text=scene,
            title=title,
            index=index,
            total=len(scenes),
            pictured=has_real_media,
            attribution=attributions[index],
            show_text=captions,
        )

        clip = work / f"scene_{index}.mp4"
        if media_kinds[index] == "video":
            _render_video_clip(
                ffmpeg=ffmpeg,
                video_path=media_paths[index],
                caption_path=caption,
                dest=clip,
                duration=durations[index],
            )
        else:
            _render_image_clip(
                ffmpeg=ffmpeg,
                image_path=media_paths[index],
                caption_path=caption,
                dest=clip,
                duration=durations[index],
                scene_index=index,
            )
        clips.append(clip)

    # Credits stay in attributions.json only — not burned into the client video.
    credit_lines = [line for line in attributions if line]

    on_progress("Stitching the video…")
    silent = work / "silent.mp4"
    _concat_clips(ffmpeg, clips, silent)
    try:
        _mux(silent, spoken, dest_mp4)
    finally:
        if spoken is not None:
            spoken.unlink(missing_ok=True)
        silent.unlink(missing_ok=True)

    return {
        "scenes": len(scenes),
        "pictures": sum(1 for kind in media_kinds if kind == "image"),
        "stock": sum(1 for item in scene_media if item),
        "voice": spoken is not None or (voice and audio_seconds is not None),
        "captions": bool(captions),
        "attributions": credit_lines,
    }
