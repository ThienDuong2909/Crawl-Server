from fastapi.testclient import TestClient

from douyin_nwm_tool.autocrawl import AutoCrawlConfig, AutoCrawlDatabase, AutoCrawlManager, VideoCandidate
from douyin_nwm_tool.schemas import DownloadResult


def test_adding_channel_runs_first_crawl_immediately_then_schedules_next_interval(monkeypatch, tmp_path):
    from douyin_nwm_tool import main

    class Provider:
        async def fetch_channel_videos(self, channel, config):
            return [VideoCandidate(
                video_id="initial-video",
                douyin_url="https://www.douyin.com/video/initial-video",
                title="Initial video",
                raw_data={"aweme_type": 0},
            )]

    class Downloader:
        async def download(self, url, progress_callback=None):
            path = tmp_path / "initial-video.mp4"
            path.write_bytes(b"video")
            return DownloadResult(video_id="initial-video", file_path=path, source_url=url, bytes_written=5)

    db = AutoCrawlDatabase(tmp_path / "initial-crawl.db")
    manager = AutoCrawlManager(
        db=db,
        provider=Provider(),
        downloader=Downloader(),
        config=AutoCrawlConfig(download_new_videos=False),
    )
    monkeypatch.setattr(main, "autocrawl_manager", manager)
    monkeypatch.setattr(main.autocrawl_scheduler, "manager", manager)
    monkeypatch.setattr(main.autocrawl_scheduler, "start", lambda *args, **kwargs: main.autocrawl_scheduler.status())
    client = TestClient(main.app)

    response = client.post("/api/crawl/channels", json={
        "user_id": "initial-user",
        "display_name": "Initial user",
        "interval_minutes": 30,
        "schedule_enabled": True,
    })

    assert response.status_code == 200
    assert response.json()["initial_crawl_started"] is True
    sessions = db.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["status"] == "completed"
    assert sessions[0]["new_videos"] == 1
    channel = db.list_channels()[0]
    assert channel["last_scraped_at"] is not None
    assert channel["last_run_at"] is not None
    assert 1795 <= channel["next_run_at"] - channel["last_run_at"] <= 1805
