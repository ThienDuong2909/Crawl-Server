import asyncio
import time

from douyin_nwm_tool.autocrawl import AutoCrawlConfig, AutoCrawlDatabase, AutoCrawlManager, VideoCandidate, public_video_row
from douyin_nwm_tool.schemas import DownloadResult


def test_failed_download_persists_operator_visible_error_on_video(tmp_path):
    db = AutoCrawlDatabase(tmp_path / "crawl.db")
    channel = db.add_channel("https://www.douyin.com/user/u-error", "Error channel", schedule_enabled=False)
    candidate = VideoCandidate(
        video_id="photo-post",
        douyin_url="https://www.douyin.com/video/photo-post",
        title="photo",
        published_at=time.time(),
        raw_data={"aweme_type": 68},
    )
    db.record_video(channel["id"], candidate)

    db.mark_video_failed(candidate.video_id, "Bước 1 chỉ hỗ trợ video Douyin, aweme_type=68")

    row = db.get_video(candidate.video_id)
    assert row["download_status"] == "failed"
    assert row["download_error"] == "Bước 1 chỉ hỗ trợ video Douyin, aweme_type=68"


def test_photo_post_is_downloaded_as_album_instead_of_skipped(tmp_path):
    class PhotoProvider:
        async def fetch_channel_videos(self, channel, config):
            return [VideoCandidate(
                video_id="photo-68",
                douyin_url="https://www.douyin.com/note/photo-68",
                title="photo post",
                published_at=time.time(),
                raw_data={"aweme_type": 68, "images": [{"url_list": ["https://img/1"]}]},
            )]

    class PhotoDownloader:
        async def download(self, url, progress_callback=None, raw_detail=None):
            assert raw_detail and raw_detail["aweme_type"] == 68
            album = tmp_path / "douyin_photo" / "douyin_photo-68"
            album.mkdir(parents=True)
            image = album / "01.webp"
            image.write_bytes(b"image")
            music = album / "music.mp3"
            music.write_bytes(b"music")
            manifest = album / "manifest.json"
            manifest.write_text('{"type":"photo","image_files":["01.webp"],"music_file":"music.mp3","music_title":"Nhạc gốc"}', encoding="utf-8")
            return DownloadResult(type="photo", video_id="photo-68", file_path=manifest, manifest_path=manifest, source_url="https://img/1", bytes_written=10, image_paths=[image], music_path=music)

    db = AutoCrawlDatabase(tmp_path / "crawl.db")
    db.add_channel("https://www.douyin.com/user/u-photo", "Photo channel", schedule_enabled=False)
    manager = AutoCrawlManager(
        db=db,
        provider=PhotoProvider(),
        downloader=PhotoDownloader(),
        config=AutoCrawlConfig(max_video_age_hours=None, download_new_videos=True),
    )

    result = asyncio.run(manager.run_once(download=True))

    assert result["new_videos"] == 1
    assert result["skipped_videos"] == 0
    row = db.get_video("photo-68")
    assert row["download_status"] == "completed"
    assert row["media_type"] == "photo"
    assert row["asset_count"] == 1
    assert row["music_path"].endswith("music.mp3")
    public = public_video_row(row)
    assert public["photo_files"] == ["01.webp"]
    assert public["photo_preview_urls"] == ["/upload-api/api/photos/photo-68/01.webp"]
    assert public["music_title"] == "Nhạc gốc"
    assert "local_path" not in public
