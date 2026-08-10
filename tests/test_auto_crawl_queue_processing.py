from douyin_nwm_tool.autocrawl import AutoCrawlConfig, AutoCrawlDatabase, AutoCrawlManager, VideoCandidate


class QueueFakeDownloader:
    def __init__(self):
        self.urls = []

    async def download(self, url, progress_callback=None):
        from pathlib import Path
        from douyin_nwm_tool.schemas import DownloadResult
        self.urls.append(url)
        return DownloadResult(video_id=url.rsplit('/', 1)[-1], file_path=Path(f"/tmp/{url.rsplit('/', 1)[-1]}.mp4"), source_url="https://cdn.example/video.mp4", bytes_written=2048)


def test_autocrawl_can_process_existing_pending_download_queue(tmp_path):
    db = AutoCrawlDatabase(tmp_path / "queue.db")
    channel = db.add_channel("https://www.douyin.com/user/abc", "Kenh Queue")
    db.record_video(channel["id"], VideoCandidate(video_id="q1", douyin_url="https://www.douyin.com/video/q1", title="queued"), status="pending")
    downloader = QueueFakeDownloader()
    manager = AutoCrawlManager(db=db, downloader=downloader, config=AutoCrawlConfig())

    result = manager.process_queue_sync()

    assert result == {"processed": 1, "completed": 1, "failed": 0}
    assert downloader.urls == ["https://www.douyin.com/video/q1"]
    assert db.get_video("q1")["download_status"] == "completed"
    assert db.list_queue(status="done")[0]["video_id"] == "q1"
