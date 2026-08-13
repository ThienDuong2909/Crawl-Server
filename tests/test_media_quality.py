from pathlib import Path

from douyin_nwm_tool.media_quality import (
    VideoProbe,
    build_video_enhancement_command,
    ensure_tiktok_video_asset,
    should_enhance_video,
)


def test_low_resolution_video_is_upscaled_and_mildly_sharpened_for_tiktok(tmp_path):
    source = tmp_path / "source.mp4"
    target = tmp_path / "enhanced.mp4"
    probe = VideoProbe(width=720, height=1280, fps=30.0, video_codec="h264", pixel_format="yuv420p")

    assert should_enhance_video(probe) is True
    command = build_video_enhancement_command(source, target, probe)

    assert command[0] == "ffmpeg"
    video_filter = command[command.index("-vf") + 1]
    assert "scale=1080:1920:flags=lanczos" in video_filter
    assert "unsharp=5:5:0.30:5:5:0.0" in video_filter
    assert command[command.index("-crf") + 1] == "19"
    assert command[-1] == str(target)


def test_full_hd_tiktok_compatible_video_is_not_reencoded():
    probe = VideoProbe(width=1080, height=1920, fps=30.0, video_codec="h265", pixel_format="yuv420p")

    assert should_enhance_video(probe) is False


def test_real_ffmpeg_enhancement_produces_verified_full_hd_video(tmp_path):
    import subprocess

    source = tmp_path / "low.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=360x640:rate=30",
            "-t", "0.2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
        ],
        check=True,
    )

    enhanced = ensure_tiktok_video_asset(source)

    assert enhanced != source
    assert enhanced.name == "low_tiktok_hq.mp4"
    assert enhanced.exists() and enhanced.stat().st_size > 0
