import json
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from douyin_nwm_tool.downloader import DownloadService
from douyin_nwm_tool.schemas import DouyinParseResult, PhotoData, PhotoImage, VideoData


class FakeParser:
    async def parse(self, url: str):
        assert url == "https://www.douyin.com/video/123"
        return DouyinParseResult(
            type="video",
            platform="douyin",
            video_id="123",
            desc="demo",
            video_data=VideoData(
                wm_video_url="https://media.example/wm.mp4",
                wm_video_url_HQ="https://media.example/wm_hq.mp4",
                nwm_video_url="https://media.example/nwm.mp4",
                nwm_video_url_HQ="https://media.example/nwm_hq.mp4",
            ),
        )


class FakeResponse:
    headers = {"content-type": "video/mp4", "content-length": "6"}

    def raise_for_status(self):
        return None

    async def aiter_bytes(self):
        yield b"abc"
        yield b"123"


class FakeStreamContext:
    async def __aenter__(self):
        return FakeResponse()

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeHttpClient:
    def stream(self, method, url, headers=None, follow_redirects=True):
        assert method == "GET"
        assert url == "https://media.example/nwm_hq.mp4"
        assert follow_redirects is True
        return FakeStreamContext()


@pytest.mark.asyncio
async def test_download_saves_no_watermark_video_atomically(tmp_path: Path):
    service = DownloadService(parser=FakeParser(), download_dir=tmp_path, http_client=FakeHttpClient())

    result = await service.download("https://www.douyin.com/video/123")

    assert result.video_id == "123"
    assert result.file_path == tmp_path / "douyin_video" / "douyin_123.mp4"
    assert result.file_path.read_bytes() == b"abc123"
    assert not result.file_path.with_suffix(".mp4.tmp").exists()


@pytest.mark.asyncio
async def test_download_photo_post_saves_ordered_images_music_and_manifest(tmp_path: Path):
    class PhotoParser:
        async def parse(self, url: str):
            return DouyinParseResult(
                type="photo",
                video_id="photo-68",
                desc="四张照片",
                photo_data=PhotoData(
                    images=[
                        PhotoImage(url="https://media.example/01.webp", width=1080, height=1440),
                        PhotoImage(url="https://media.example/02.webp", width=1440, height=1080),
                    ],
                    music_url="https://media.example/music.mp3",
                    music_title="凌风(宿命版)",
                    music_duration_sec=20,
                ),
            )

    def webp_bytes(size, color):
        output = BytesIO()
        Image.new("RGB", size, color).save(output, format="WEBP", quality=90)
        return output.getvalue()

    source_one = webp_bytes((1200, 1600), "red")
    source_two = webp_bytes((1600, 1200), "blue")
    payloads = {
        "https://media.example/01.webp": ("image/webp", source_one),
        "https://media.example/02.webp": ("image/webp", source_two),
        "https://media.example/music.mp3": ("audio/mpeg", b"music-bytes"),
    }

    class Response:
        def __init__(self, content_type, body):
            self.headers = {"content-type": content_type, "content-length": str(len(body))}
            self.body = body
        def raise_for_status(self):
            return None
        async def aiter_bytes(self):
            yield self.body

    class Stream:
        def __init__(self, response):
            self.response = response
        async def __aenter__(self):
            return self.response
        async def __aexit__(self, exc_type, exc, tb):
            return False

    class Client:
        def stream(self, method, url, headers=None, follow_redirects=True):
            content_type, body = payloads[url]
            return Stream(Response(content_type, body))

    result = await DownloadService(parser=PhotoParser(), download_dir=tmp_path, http_client=Client()).download(
        "https://www.douyin.com/note/photo-68"
    )

    album_dir = tmp_path / "douyin_photo" / "douyin_photo-68"
    assert result.type == "photo"
    assert result.file_path == album_dir / "manifest.json"
    assert [path.name for path in result.image_paths] == ["01.webp", "02.webp"]
    assert [path.read_bytes() for path in result.image_paths] == [source_one, source_two]
    assert result.music_path == album_dir / "music.mp3"
    assert result.music_path.read_bytes() == b"music-bytes"
    manifest = json.loads(result.file_path.read_text(encoding="utf-8"))
    assert manifest["image_files"] == ["01.webp", "02.webp"]
    assert manifest["tiktok_image_files"] == ["tiktok_01.webp", "tiktok_02.webp"]
    assert manifest["tiktok_image_dimensions"] == [
        {"width": 1080, "height": 1440},
        {"width": 1440, "height": 1080},
    ]
    with Image.open(album_dir / "tiktok_01.webp") as first:
        assert first.size == (1080, 1440)
    with Image.open(album_dir / "tiktok_02.webp") as second:
        assert second.size == (1440, 1080)
    with Image.open(BytesIO(source_one)) as original, Image.open(album_dir / "tiktok_01.webp") as enhanced:
        baseline = original.convert("RGB").resize((1080, 1440), Image.Resampling.LANCZOS)
        assert enhanced.convert("RGB").tobytes() != baseline.tobytes()
    assert manifest["music_title"] == "凌风(宿命版)"
    assert manifest["music_file"] == "music.mp3"


@pytest.mark.asyncio
async def test_cached_watermarked_photo_is_preserved_and_clean_variant_feeds_tiktok(tmp_path: Path):
    class PhotoParser:
        async def parse(self, url: str):
            return DouyinParseResult(
                type="photo",
                video_id="photo-clean",
                photo_data=PhotoData(images=[PhotoImage(
                    url="https://media.example/clean.webp",
                    width=1200,
                    height=1600,
                    watermark_free=True,
                )]),
            )

    def webp_bytes(color):
        output = BytesIO()
        Image.new("RGB", (1200, 1600), color).save(output, format="WEBP", quality=90)
        return output.getvalue()

    old_source = webp_bytes("red")
    old_tiktok_buffer = BytesIO()
    Image.new("RGB", (1080, 1440), "red").save(old_tiktok_buffer, format="WEBP", quality=90)
    old_tiktok = old_tiktok_buffer.getvalue()
    clean_source = webp_bytes("green")
    album = tmp_path / "douyin_photo" / "douyin_photo-clean"
    album.mkdir(parents=True)
    (album / "01.webp").write_bytes(old_source)
    (album / "tiktok_01.webp").write_bytes(old_tiktok)
    (album / "manifest.json").write_text(json.dumps({
        "type": "photo",
        "video_id": "photo-clean",
        "image_files": ["01.webp"],
        "music_file": None,
    }), encoding="utf-8")

    class Response:
        headers = {"content-type": "image/webp", "content-length": str(len(clean_source))}
        def raise_for_status(self):
            return None
        async def aiter_bytes(self):
            yield clean_source

    class Stream:
        async def __aenter__(self):
            return Response()
        async def __aexit__(self, exc_type, exc, tb):
            return False

    class Client:
        def stream(self, method, url, headers=None, follow_redirects=True):
            assert url == "https://media.example/clean.webp"
            return Stream()

    await DownloadService(parser=PhotoParser(), download_dir=tmp_path, http_client=Client()).download(
        "https://www.douyin.com/note/photo-clean"
    )

    assert (album / "01.webp").read_bytes() == old_source
    assert (album / "clean_01.webp").read_bytes() == clean_source
    manifest = json.loads((album / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["clean_image_files"] == ["clean_01.webp"]
    assert manifest["watermark_processing"]["status"] == "clean"
    assert manifest["watermark_processing"]["method"] == "clean_source_url"
    with Image.open(album / "tiktok_01.webp") as delivered:
        red, green, blue = delivered.convert("RGB").getpixel((delivered.width // 2, delivered.height // 2))
        assert green > red and green > blue


@pytest.mark.asyncio
async def test_download_reports_byte_progress(tmp_path: Path):
    service = DownloadService(parser=FakeParser(), download_dir=tmp_path, http_client=FakeHttpClient())
    events = []

    await service.download("https://www.douyin.com/video/123", progress_callback=events.append)

    assert events[0]["phase"] == "parsed"
    assert any(e["phase"] == "downloading" and e["bytes_written"] == 3 and e["total_bytes"] == 6 for e in events)
    assert any(e["phase"] == "downloading" and e["bytes_written"] == 6 and e["total_bytes"] == 6 for e in events)
    assert events[-1]["phase"] == "completed"
