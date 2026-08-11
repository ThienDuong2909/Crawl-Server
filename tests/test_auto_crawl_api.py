from fastapi.testclient import TestClient


def test_autocrawl_api_add_channel_run_and_status(monkeypatch, tmp_path):
    from douyin_nwm_tool import main
    from douyin_nwm_tool.autocrawl import AutoCrawlDatabase, AutoCrawlManager, AutoCrawlConfig, VideoCandidate

    class FakeProvider:
        async def fetch_channel_videos(self, channel, config):
            return [VideoCandidate(video_id="api_1", douyin_url="https://www.douyin.com/video/api_1", title="api", published_at=1893456000, view_count=1000, like_count=100, duration_sec=9)]

    class FakeDownloader:
        async def download(self, url, progress_callback=None):
            from pathlib import Path
            from douyin_nwm_tool.schemas import DownloadResult
            return DownloadResult(video_id="api_1", file_path=Path("/tmp/api_1.mp4"), source_url="https://cdn.example/api_1.mp4", bytes_written=10)

    db = AutoCrawlDatabase(tmp_path / "api-crawl.db")
    main.autocrawl_manager = AutoCrawlManager(db=db, provider=FakeProvider(), downloader=FakeDownloader(), config=AutoCrawlConfig(min_view_count=10, min_like_count=1))
    client = TestClient(main.app)

    create = client.post("/api/crawl/channels", json={"profile_url":"https://www.douyin.com/user/test", "display_name":"Test Channel"})
    assert create.status_code == 200
    assert create.json()["display_name"] == "Test Channel"
    assert create.json()["initial_crawl_started"] is True
    assert db.list_sessions()[0]["new_videos"] == 1

    run = client.post("/api/crawl/run", json={"download": True})
    assert run.status_code == 200
    body = run.json()
    assert body["status"] == "completed"
    assert body["new_videos"] == 0

    assert client.get("/api/crawl/status").json()["channels"]["active"] == 1
    videos = client.get("/api/crawl/videos").json()["items"]
    assert videos[0]["video_id"] == "api_1"
    assert "local_path" not in videos[0]
    sessions = client.get("/api/crawl/sessions").json()["items"]
    assert sessions[0]["new_videos"] == 0
    assert sessions[1]["new_videos"] == 1


def test_autocrawl_api_rejects_duplicate_channel_and_bad_url(tmp_path):
    from douyin_nwm_tool import main
    from douyin_nwm_tool.autocrawl import AutoCrawlDatabase, AutoCrawlManager

    class EmptyProvider:
        async def fetch_channel_videos(self, channel, config):
            return []

    main.autocrawl_manager = AutoCrawlManager(db=AutoCrawlDatabase(tmp_path / "api-crawl.db"), provider=EmptyProvider())
    client = TestClient(main.app)

    bad = client.post("/api/crawl/channels", json={"profile_url":"https://example.com/not-douyin", "display_name":"Bad"})
    assert bad.status_code == 400

    ok = client.post("/api/crawl/channels", json={"profile_url":"https://www.douyin.com/user/abc", "display_name":"A"})
    assert ok.status_code == 200
    dup = client.post("/api/crawl/channels", json={"profile_url":"https://www.douyin.com/user/abc", "display_name":"A2"})
    assert dup.status_code == 409


def test_autocrawl_api_accepts_user_id_and_manages_channel_schedule_and_video_actions(tmp_path):
    from douyin_nwm_tool import main
    from douyin_nwm_tool.autocrawl import AutoCrawlDatabase, AutoCrawlManager, VideoCandidate

    db = AutoCrawlDatabase(tmp_path / "api-actions.db")

    class EmptyProvider:
        async def fetch_channel_videos(self, channel, config):
            return []

    main.autocrawl_manager = AutoCrawlManager(db=db, provider=EmptyProvider())
    client = TestClient(main.app)
    uid = "MS4wLjABAAAANHPWGdxB_LxCRjTLpo8E_V0dNUTjTjmEpS17RcNgmqDzFeLikmrutalyuomaWdMe"

    created = client.post("/api/crawl/channels", json={
        "user_id": uid,
        "display_name": "Creator A",
        "interval_minutes": 30,
        "schedule_enabled": True,
        "auto_upload_enabled": True,
        "translate_enabled": True,
    })
    assert created.status_code == 200
    channel = created.json()
    assert channel["profile_url"] == f"https://www.douyin.com/user/{uid}"
    assert channel["interval_minutes"] == 30
    assert channel["schedule_enabled"] == 1
    assert channel["auto_upload_enabled"] == 1
    assert channel["translate_enabled"] == 1

    updated = client.patch(f"/api/crawl/channels/{channel['id']}", json={
        "status": "active",
        "interval_minutes": 75,
        "schedule_enabled": False,
        "auto_upload_enabled": False,
        "translate_enabled": False,
    })
    assert updated.status_code == 200
    assert updated.json()["interval_minutes"] == 75
    assert updated.json()["schedule_enabled"] == 0
    assert updated.json()["auto_upload_enabled"] == 0
    assert updated.json()["translate_enabled"] == 0

    db.record_video(channel["id"], VideoCandidate(video_id="action_1", douyin_url="https://www.douyin.com/video/action_1", title="Action video"))
    starred = client.patch("/api/crawl/videos/action_1", json={"is_starred": True})
    assert starred.status_code == 200
    assert starred.json()["is_starred"] == 1

    deleted = client.delete("/api/crawl/videos/action_1")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert client.get("/api/crawl/videos").json()["items"] == []

    removed_channel = client.delete(f"/api/crawl/channels/{channel['id']}")
    assert removed_channel.status_code == 200
    assert removed_channel.json()["deleted"] is True
    assert client.get('/api/crawl/channels').json()["items"] == []


def test_autocrawl_api_paginates_tables_with_maximum_eight_rows(tmp_path):
    from douyin_nwm_tool import main
    from douyin_nwm_tool.autocrawl import AutoCrawlDatabase, AutoCrawlManager, VideoCandidate

    db = AutoCrawlDatabase(tmp_path / "api-pagination.db")
    for index in range(10):
        channel = db.add_channel(
            f"https://www.douyin.com/user/page-{index}",
            f"Page {index}",
        )
        db.record_video(
            channel["id"],
            VideoCandidate(
                video_id=f"api-page-{index}",
                douyin_url=f"https://www.douyin.com/video/api-page-{index}",
            ),
        )
    main.autocrawl_manager = AutoCrawlManager(db=db)
    client = TestClient(main.app)

    channels = client.get("/api/crawl/channels?page=1&page_size=99").json()
    videos = client.get("/api/crawl/videos?page=2&page_size=8").json()

    assert len(channels["items"]) == 8
    assert channels["pagination"]["page_size"] == 8
    assert channels["pagination"]["total"] == 10
    assert len(videos["items"]) == 2
    assert videos["pagination"]["total_pages"] == 2
