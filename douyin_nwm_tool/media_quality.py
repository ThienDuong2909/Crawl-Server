from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
from typing import Callable


@dataclass(frozen=True)
class VideoProbe:
    width: int
    height: int
    fps: float
    video_codec: str
    pixel_format: str


def should_enhance_video(probe: VideoProbe) -> bool:
    """Only upscale sources below TikTok's common 1080x1920 portrait target.

    Compatible full-HD or higher sources should be uploaded unchanged to avoid
    an unnecessary generation loss. Landscape videos use the short edge too.
    """
    short_edge = min(probe.width, probe.height)
    long_edge = max(probe.width, probe.height)
    return short_edge < 1080 or long_edge < 1920


def build_video_enhancement_command(source: Path, target: Path, probe: VideoProbe) -> list[str]:
    portrait = probe.height >= probe.width
    target_size = "1080:1920" if portrait else "1920:1080"
    filters = f"scale={target_size}:flags=lanczos,unsharp=5:5:0.30:5:5:0.0"
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source),
        "-vf", filters,
        "-c:v", "libx264", "-preset", "fast", "-crf", "19",
        "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", "-f", "mp4",
        str(target),
    ]


def probe_video(path: Path, *, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> VideoProbe:
    completed = runner(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,width,height,r_frame_rate,pix_fmt",
            "-of", "json", str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    stream = (json.loads(completed.stdout).get("streams") or [{}])[0]
    numerator, denominator = str(stream.get("r_frame_rate") or "0/1").split("/", 1)
    fps = float(numerator) / float(denominator or 1)
    return VideoProbe(
        width=int(stream.get("width") or 0),
        height=int(stream.get("height") or 0),
        fps=fps,
        video_codec=str(stream.get("codec_name") or ""),
        pixel_format=str(stream.get("pix_fmt") or ""),
    )


def ensure_tiktok_video_asset(
    source: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> Path:
    """Return source unchanged when compatible, otherwise create a verified HQ derivative."""
    probe = probe_video(source, runner=runner)
    if not should_enhance_video(probe):
        return source
    target_dir = source.parent
    target = target_dir / f"{source.stem}_tiktok_hq.mp4"
    if target.exists() and target.stat().st_size > 0 and target.stat().st_mtime_ns >= source.stat().st_mtime_ns:
        return target
    temporary = target.with_suffix(".mp4.tmp")
    runner(build_video_enhancement_command(source, temporary, probe), check=True)
    output_probe = probe_video(temporary, runner=runner)
    if min(output_probe.width, output_probe.height) < 1080 or max(output_probe.width, output_probe.height) < 1920:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("Enhanced video failed TikTok quality verification")
    temporary.replace(target)
    return target
