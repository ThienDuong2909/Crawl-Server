import json
import os
from pathlib import Path
from typing import Callable, Any
from urllib.parse import urlparse

import httpx
from PIL import Image, ImageFilter

from .parser import DouyinParser
from .schemas import DownloadResult


DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
ProgressCallback = Callable[[dict[str, Any]], None]


class DownloadService:
    def __init__(self, parser: DouyinParser | None = None, download_dir: str | Path | None = None, http_client=None):
        self.parser = parser or DouyinParser()
        self.download_dir = Path(download_dir or os.getenv("DOWNLOAD_DIR", "./download")).resolve()
        self.http_client = http_client

    async def download(self, url: str, progress_callback: ProgressCallback | None = None, raw_detail: dict[str, Any] | None = None) -> DownloadResult:
        parsed = await self.parser.parse_detail(url, raw_detail) if raw_detail is not None else await self.parser.parse(url)
        if parsed.type == "photo":
            return await self._download_photo_post(parsed, progress_callback)
        if parsed.video_data is None:
            raise ValueError("Thiếu dữ liệu video Douyin")
        source_url = parsed.video_data.best_no_watermark_url
        target_dir = self.download_dir / "douyin_video"
        target_dir.mkdir(parents=True, exist_ok=True)
        file_path = target_dir / f"douyin_{parsed.video_id}.mp4"
        self._emit(progress_callback, phase="parsed", video_id=parsed.video_id, progress=15)

        if file_path.exists() and file_path.stat().st_size > 0:
            size = file_path.stat().st_size
            self._emit(progress_callback, phase="completed", video_id=parsed.video_id, progress=100, bytes_written=size, total_bytes=size, cached=True)
            return DownloadResult(video_id=parsed.video_id, file_path=file_path, source_url=source_url, bytes_written=size)

        tmp_path = file_path.with_suffix(file_path.suffix + ".tmp")
        headers = {"User-Agent": DEFAULT_USER_AGENT, "Referer": "https://www.douyin.com/"}
        bytes_written = await self._stream_to_file(source_url, tmp_path, headers, progress_callback=progress_callback, video_id=parsed.video_id)
        tmp_path.replace(file_path)
        self._emit(progress_callback, phase="completed", video_id=parsed.video_id, progress=100, bytes_written=bytes_written)
        return DownloadResult(video_id=parsed.video_id, file_path=file_path, source_url=source_url, bytes_written=bytes_written)

    async def _download_photo_post(self, parsed, progress_callback: ProgressCallback | None) -> DownloadResult:
        photo_data = parsed.photo_data
        if photo_data is None or not photo_data.images:
            raise ValueError("Bài ảnh Douyin không có ảnh để tải")
        if len(photo_data.images) > 35:
            raise ValueError("TikTok chỉ hỗ trợ tối đa 35 ảnh trong một bài")
        album_dir = self.download_dir / "douyin_photo" / f"douyin_{parsed.video_id}"
        manifest_path = album_dir / "manifest.json"
        cached_image_paths: list[Path] = []
        if manifest_path.exists() and manifest_path.stat().st_size > 0:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            image_paths = [album_dir / name for name in manifest.get("image_files") or []]
            music_path = album_dir / manifest["music_file"] if manifest.get("music_file") else None
            images_complete = bool(image_paths) and all(path.exists() and path.stat().st_size > 0 for path in image_paths)
            if images_complete:
                cached_image_paths = image_paths
            music_complete = not photo_data.music_url or bool(music_path and music_path.exists() and music_path.stat().st_size > 0)
            if images_complete and music_complete:
                clean_paths, watermark_processing = await self._prepare_cached_photo_assets(
                    album_dir, image_paths, photo_data.images, manifest, headers={"User-Agent": DEFAULT_USER_AGENT, "Referer": "https://www.douyin.com/"}, video_id=parsed.video_id, progress_callback=progress_callback
                )
                tiktok_paths, tiktok_dimensions = self._ensure_tiktok_photo_assets(album_dir, clean_paths)
                updated_manifest = {
                    **manifest,
                    "clean_image_files": [path.name for path in clean_paths],
                    "watermark_processing": watermark_processing,
                    "tiktok_image_files": [path.name for path in tiktok_paths],
                    "tiktok_image_dimensions": tiktok_dimensions,
                }
                if updated_manifest != manifest:
                    self._write_manifest(manifest_path, updated_manifest)
                total = sum(path.stat().st_size for path in [*image_paths, *tiktok_paths]) + (music_path.stat().st_size if music_path and music_path.exists() else 0)
                self._emit(progress_callback, phase="completed", video_id=parsed.video_id, progress=100, bytes_written=total, cached=True, media_type="photo")
                return DownloadResult(type="photo", video_id=parsed.video_id, file_path=manifest_path, manifest_path=manifest_path, source_url=photo_data.images[0].url, bytes_written=total, image_paths=image_paths, music_path=music_path)

        album_dir.mkdir(parents=True, exist_ok=True)
        headers = {"User-Agent": DEFAULT_USER_AGENT, "Referer": "https://www.douyin.com/"}
        self._emit(progress_callback, phase="parsed", video_id=parsed.video_id, progress=10, media_type="photo", asset_count=len(photo_data.images))
        image_paths: list[Path] = list(cached_image_paths)
        total_bytes = sum(path.stat().st_size for path in image_paths)
        if not image_paths:
            for index, image in enumerate(photo_data.images, start=1):
                tmp_path = album_dir / f"{index:02d}.asset.tmp"
                written = await self._stream_to_file(image.url, tmp_path, headers, progress_callback=progress_callback, video_id=parsed.video_id)
                suffix = self._sniff_asset_suffix(tmp_path, kind="image", url=image.url)
                final_path = album_dir / f"{index:02d}{suffix}"
                tmp_path.replace(final_path)
                image_paths.append(final_path)
                total_bytes += written

        music_path = None
        if photo_data.music_url:
            tmp_path = album_dir / "music.asset.tmp"
            written = await self._stream_to_file(photo_data.music_url, tmp_path, headers, progress_callback=progress_callback, video_id=parsed.video_id)
            suffix = self._sniff_asset_suffix(tmp_path, kind="audio", url=photo_data.music_url)
            music_path = album_dir / f"music{suffix}"
            tmp_path.replace(music_path)
            total_bytes += written

        if not all(image.watermark_free for image in photo_data.images):
            raise ValueError("Không tìm thấy biến thể ảnh sạch watermark; đã chặn gửi TikTok để tránh đăng ảnh có watermark")
        clean_paths = image_paths
        watermark_processing = {"status": "clean", "method": "clean_source_url"}
        tiktok_paths, tiktok_dimensions = self._ensure_tiktok_photo_assets(album_dir, clean_paths)
        total_bytes += sum(path.stat().st_size for path in tiktok_paths)
        manifest = {
            "type": "photo",
            "platform": "douyin",
            "video_id": parsed.video_id,
            "description": parsed.desc or "",
            "image_files": [path.name for path in image_paths],
            "image_dimensions": [{"width": image.width, "height": image.height} for image in photo_data.images],
            "source_watermark_free": [image.watermark_free for image in photo_data.images],
            "clean_image_files": [path.name for path in clean_paths],
            "watermark_processing": watermark_processing,
            "tiktok_image_files": [path.name for path in tiktok_paths],
            "tiktok_image_dimensions": tiktok_dimensions,
            "music_file": music_path.name if music_path else None,
            "music_title": photo_data.music_title,
            "music_author": photo_data.music_author,
            "music_duration_sec": photo_data.music_duration_sec,
        }
        self._write_manifest(manifest_path, manifest)
        self._emit(progress_callback, phase="completed", video_id=parsed.video_id, progress=100, bytes_written=total_bytes, media_type="photo", asset_count=len(image_paths))
        return DownloadResult(type="photo", video_id=parsed.video_id, file_path=manifest_path, manifest_path=manifest_path, source_url=photo_data.images[0].url, bytes_written=total_bytes, image_paths=image_paths, music_path=music_path)

    async def _prepare_cached_photo_assets(
        self,
        album_dir: Path,
        image_paths: list[Path],
        photo_images: list[Any],
        manifest: dict[str, Any],
        *,
        headers: dict[str, str],
        video_id: str,
        progress_callback: ProgressCallback | None,
    ) -> tuple[list[Path], dict[str, str]]:
        listed_clean = [album_dir / name for name in manifest.get("clean_image_files") or []]
        if len(listed_clean) == len(image_paths) and all(path.exists() and path.stat().st_size > 0 for path in listed_clean):
            return listed_clean, manifest.get("watermark_processing") or {"status": "clean", "method": "clean_source_url"}
        source_flags = manifest.get("source_watermark_free")
        if source_flags == [True] * len(image_paths):
            return image_paths, {"status": "clean", "method": "clean_source_url"}
        if len(photo_images) != len(image_paths) or not all(image.watermark_free for image in photo_images):
            raise ValueError("Không tìm thấy biến thể ảnh sạch watermark; đã chặn gửi TikTok để tránh đăng ảnh có watermark")
        clean_paths: list[Path] = []
        for index, image in enumerate(photo_images, start=1):
            existing = next(album_dir.glob(f"clean_{index:02d}.*"), None)
            if existing and existing.stat().st_size > 0 and existing.suffix != ".tmp":
                clean_paths.append(existing)
                continue
            tmp_path = album_dir / f"clean_{index:02d}.asset.tmp"
            await self._stream_to_file(image.url, tmp_path, headers, progress_callback=progress_callback, video_id=video_id)
            suffix = self._sniff_asset_suffix(tmp_path, kind="image", url=image.url)
            final_path = album_dir / f"clean_{index:02d}{suffix}"
            tmp_path.replace(final_path)
            clean_paths.append(final_path)
        return clean_paths, {"status": "clean", "method": "clean_source_url"}

    @staticmethod
    def _write_manifest(manifest_path: Path, manifest: dict[str, Any]) -> None:
        manifest_tmp = manifest_path.with_suffix(".json.tmp")
        manifest_tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        manifest_tmp.replace(manifest_path)

    @staticmethod
    def _ensure_tiktok_photo_assets(album_dir: Path, image_paths: list[Path]) -> tuple[list[Path], list[dict[str, int]]]:
        output_paths: list[Path] = []
        dimensions: list[dict[str, int]] = []
        for index, source_path in enumerate(image_paths, start=1):
            target_path = album_dir / f"tiktok_{index:02d}.webp"
            with Image.open(source_path) as source:
                source.load()
                width, height = source.size
                reference_edge = width if height >= width else height
                scale = min(1.0, 1080 / reference_edge)
                target_size = (max(1, round(width * scale)), max(1, round(height * scale)))
                regenerate = (
                    source_path.name.startswith("clean_")
                    or not target_path.exists()
                    or target_path.stat().st_size <= 0
                    or target_path.stat().st_mtime_ns < source_path.stat().st_mtime_ns
                )
                if not regenerate:
                    try:
                        with Image.open(target_path) as cached:
                            regenerate = cached.size != target_size
                    except OSError:
                        regenerate = True
                if regenerate:
                    converted = source.convert("RGB")
                    if converted.size != target_size:
                        converted = converted.resize(target_size, Image.Resampling.LANCZOS)
                    converted = converted.filter(ImageFilter.UnsharpMask(radius=1.2, percent=45, threshold=3))
                    tmp_path = album_dir / f"tiktok_{index:02d}.webp.tmp"
                    converted.save(tmp_path, format="WEBP", quality=92, method=6)
                    tmp_path.replace(target_path)
                output_paths.append(target_path)
                dimensions.append({"width": target_size[0], "height": target_size[1]})
        return output_paths, dimensions

    @staticmethod
    def _sniff_asset_suffix(path: Path, *, kind: str, url: str) -> str:
        sample = path.read_bytes()[:16]
        if kind == "image":
            if sample.startswith(b"\xff\xd8\xff"):
                return ".jpg"
            if sample.startswith(b"\x89PNG\r\n\x1a\n"):
                return ".png"
            if sample.startswith(b"RIFF") and sample[8:12] == b"WEBP":
                return ".webp"
            suffix = Path(urlparse(url).path).suffix.lower()
            return suffix if suffix in {".jpg", ".jpeg", ".png", ".webp"} else ".webp"
        if sample.startswith(b"ID3") or sample[:2] in {b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"}:
            return ".mp3"
        if len(sample) >= 8 and sample[4:8] == b"ftyp":
            return ".m4a"
        suffix = Path(urlparse(url).path).suffix.lower()
        return suffix if suffix in {".mp3", ".m4a", ".aac"} else ".mp3"

    async def _stream_to_file(
        self,
        url: str,
        tmp_path: Path,
        headers: dict[str, str],
        progress_callback: ProgressCallback | None = None,
        video_id: str | None = None,
    ) -> int:
        client = self.http_client or httpx.AsyncClient(timeout=60, follow_redirects=True)
        close_client = self.http_client is None
        try:
            async with client.stream("GET", url, headers=headers, follow_redirects=True) as response:
                response.raise_for_status()
                total_bytes = self._safe_int(response.headers.get("content-length"))
                total = 0
                with tmp_path.open("wb") as fh:
                    async for chunk in response.aiter_bytes():
                        if not chunk:
                            continue
                        fh.write(chunk)
                        total += len(chunk)
                        progress = 20
                        if total_bytes:
                            progress = min(99, 20 + int((total / total_bytes) * 79))
                        self._emit(
                            progress_callback,
                            phase="downloading",
                            video_id=video_id,
                            progress=progress,
                            bytes_written=total,
                            total_bytes=total_bytes,
                        )
            if total <= 0:
                raise ValueError("File tải về rỗng")
            return total
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink()
            raise
        finally:
            if close_client:
                await client.aclose()

    def _emit(self, callback: ProgressCallback | None, **event) -> None:
        if callback:
            callback(event)

    def _safe_int(self, value: str | None) -> int | None:
        try:
            return int(value) if value else None
        except ValueError:
            return None
