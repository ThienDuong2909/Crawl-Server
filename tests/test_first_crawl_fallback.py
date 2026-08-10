import asyncio
import time

from douyin_nwm_tool.autocrawl import AutoCrawlConfig, AutoCrawlDatabase, AutoCrawlManager, VideoCandidate
from douyin_nwm_tool.schemas import DownloadResult


class OldVideoProvider:
    async def fetch_channel_videos(self, channel, config):
        now = time.time()
        return [
            VideoCandidate(video_id="old-7d", douyin_url="https://www.douyin.com/video/old-7d", title="7 days", published_at=now-7*86400, raw_data={"aweme_type": 0}),
            VideoCandidate(video_id="old-4d", douyin_url="https://www.douyin.com/video/old-4d", title="4 days", published_at=now-4*86400, raw_data={"aweme_type": 0}),
            VideoCandidate(video_id="old-6d", douyin_url="https://www.douyin.com/video/old-6d", title="6 days", published_at=now-6*86400, raw_data={"aweme_type": 4}),
            VideoCandidate(video_id="old-5d", douyin_url="https://www.douyin.com/video/old-5d", title="5 days", published_at=now-5*86400, raw_data={"aweme_type": 0}),
            VideoCandidate(video_id="old-8d", douyin_url="https://www.douyin.com/video/old-8d", title="8 days", published_at=now-8*86400, raw_data={"aweme_type": 0}),
        ]


class RecordingDownloader:
    def __init__(self, output_dir):
        self.output_dir = output_dir
        self.video_ids = []

    async def download(self, url, progress_callback=None):
        video_id = url.rsplit("/", 1)[-1]
        self.video_ids.append(video_id)
        path = self.output_dir / f"{video_id}.mp4"
        path.write_bytes(b"video")
        return DownloadResult(video_id=video_id, file_path=path, source_url="https://cdn.example/video.mp4", bytes_written=5)


def test_first_crawl_falls_back_to_three_newest_videos_when_none_are_within_72_hours(tmp_path):
    db = AutoCrawlDatabase(tmp_path / "crawl.db")
    channel = db.add_channel("https://www.douyin.com/user/old-only", "Old only", schedule_enabled=False)
    downloader = RecordingDownloader(tmp_path)
    manager = AutoCrawlManager(
        db=db,
        provider=OldVideoProvider(),
        downloader=downloader,
        config=AutoCrawlConfig(max_video_age_hours=72, download_new_videos=True),
    )

    result = asyncio.run(manager.run_once(channel_id=channel["id"], download=True))

    assert result["new_videos"] == 3
    assert result["skipped_videos"] == 2
    assert downloader.video_ids == ["old-4d", "old-5d", "old-6d"]
    assert {row["video_id"] for row in db.list_videos()} == {"old-4d", "old-5d", "old-6d"}


def test_later_crawls_keep_the_72_hour_filter_instead_of_repeating_fallback(tmp_path):
    class OneOldVideoProvider:
        async def fetch_channel_videos(self, channel, config):
            return [VideoCandidate(
                video_id="newly-seen-but-old",
                douyin_url="https://www.douyin.com/video/newly-seen-but-old",
                title="older than 72 hours",
                published_at=time.time() - 4 * 86400,
                raw_data={"aweme_type": 0},
            )]

    db = AutoCrawlDatabase(tmp_path / "crawl.db")
    channel = db.add_channel("https://www.douyin.com/user/repeat-old", "Repeat old", schedule_enabled=False)
    downloader = RecordingDownloader(tmp_path)
    manager = AutoCrawlManager(
        db=db,
        provider=OldVideoProvider(),
        downloader=downloader,
        config=AutoCrawlConfig(max_video_age_hours=72, download_new_videos=True),
    )
    first = asyncio.run(manager.run_once(channel_id=channel["id"], download=True))
    assert first["new_videos"] == 3

    manager.provider = OneOldVideoProvider()
    second = asyncio.run(manager.run_once(channel_id=channel["id"], download=True))

    assert second["new_videos"] == 0
    assert second["skipped_videos"] == 1
    assert db.get_video("newly-seen-but-old") is None
