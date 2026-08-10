import asyncio
import time

from douyin_nwm_tool.autocrawl import AutoCrawlConfig, AutoCrawlDatabase, AutoCrawlManager, VideoCandidate
from douyin_nwm_tool.schemas import DownloadResult


def test_deleting_channel_stops_in_progress_crawl_before_more_videos_download(tmp_path):
    db = AutoCrawlDatabase(tmp_path / "deleted-during-crawl.db")
    channel = db.add_channel("https://www.douyin.com/user/delete-me", "Delete me", schedule_enabled=True)

    class Provider:
        async def fetch_channel_videos(self, channel, config):
            return [
                VideoCandidate(video_id="delete-1", douyin_url="https://www.douyin.com/video/delete-1", published_at=time.time(), raw_data={"aweme_type": 0}),
                VideoCandidate(video_id="delete-2", douyin_url="https://www.douyin.com/video/delete-2", published_at=time.time(), raw_data={"aweme_type": 0}),
            ]

    class DeleteDuringFirstDownload:
        def __init__(self):
            self.calls = []

        async def download(self, url, progress_callback=None):
            video_id = url.rsplit("/", 1)[-1]
            self.calls.append(video_id)
            db.delete_channel(channel["id"])
            if progress_callback:
                progress_callback({"phase": "downloading", "progress": 25})
            path = tmp_path / f"{video_id}.mp4"
            path.write_bytes(b"must-not-complete")
            return DownloadResult(video_id=video_id, file_path=path, source_url=url, bytes_written=17)

    downloader = DeleteDuringFirstDownload()
    manager = AutoCrawlManager(db=db, provider=Provider(), downloader=downloader, config=AutoCrawlConfig())

    asyncio.run(manager.run_once(channel_id=channel["id"], download=True))

    deleted = db.get_channel(channel["id"])
    assert deleted["is_deleted"] == 1
    assert deleted["status"] == "paused"
    assert deleted["schedule_enabled"] == 0
    assert deleted["next_run_at"] is None
    assert downloader.calls == ["delete-1"]
    assert db.get_video("delete-2") is None
