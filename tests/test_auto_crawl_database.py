import time

from douyin_nwm_tool.autocrawl import AutoCrawlConfig, AutoCrawlDatabase, VideoCandidate, AutoCrawlManager


class FakeProvider:
    async def fetch_channel_videos(self, channel, config):
        return [
            VideoCandidate(video_id="v_old", douyin_url="https://www.douyin.com/video/v_old", title="old", author_nickname="A", author_uid="u1", published_at=time.time() - 100000, view_count=999, like_count=99, duration_sec=10),
            VideoCandidate(video_id="v_low", douyin_url="https://www.douyin.com/video/v_low", title="low", author_nickname="A", author_uid="u1", published_at=time.time(), view_count=1, like_count=0, duration_sec=10),
            VideoCandidate(video_id="v_ok", douyin_url="https://www.douyin.com/video/v_ok", title="ok", author_nickname="A", author_uid="u1", published_at=time.time(), view_count=500, like_count=50, duration_sec=12),
            VideoCandidate(video_id="v_ok", douyin_url="https://www.douyin.com/video/v_ok", title="duplicate", author_nickname="A", author_uid="u1", published_at=time.time(), view_count=500, like_count=50, duration_sec=12),
        ]


class FakeDownloader:
    def __init__(self):
        self.urls = []

    async def download(self, url, progress_callback=None):
        from pathlib import Path
        from douyin_nwm_tool.schemas import DownloadResult
        self.urls.append(url)
        return DownloadResult(video_id=url.rsplit('/', 1)[-1], file_path=Path(f"/tmp/{url.rsplit('/', 1)[-1]}.mp4"), source_url="https://cdn.example/video.mp4", bytes_written=1234)


def test_autocrawl_schema_channel_crud_and_runtime_config(tmp_path):
    db = AutoCrawlDatabase(tmp_path / "crawl.db")
    channel = db.add_channel("https://www.douyin.com/user/abc", "Kenh A")

    assert channel["id"] > 0
    assert channel["status"] == "active"
    assert db.list_channels()[0]["display_name"] == "Kenh A"

    db.pause_channel(channel["id"])
    assert db.get_channel(channel["id"])["status"] == "paused"
    db.resume_channel(channel["id"])
    assert db.get_channel(channel["id"])["status"] == "active"

    db.set_config("min_view_count", "200")
    assert db.get_config("min_view_count") == "200"


def test_channel_schedule_and_video_star_delete_are_persistent(tmp_path):
    db = AutoCrawlDatabase(tmp_path / "crawl.db")
    uid = "MS4wLjABAAAANHPWGdxB_LxCRjTLpo8E_V0dNUTjTjmEpS17RcNgmqDzFeLikmrutalyuomaWdMe"
    channel = db.add_channel(
        f"https://www.douyin.com/user/{uid}",
        "Kênh tự động",
        douyin_uid=uid,
        interval_minutes=45,
        schedule_enabled=True,
    )
    assert channel["interval_minutes"] == 45
    assert channel["schedule_enabled"] == 1
    assert channel["next_run_at"] is not None

    db.update_channel_schedule(channel["id"], interval_minutes=90, schedule_enabled=False)
    updated = db.get_channel(channel["id"])
    assert updated["interval_minutes"] == 90
    assert updated["schedule_enabled"] == 0

    video = VideoCandidate(video_id="star_1", douyin_url="https://www.douyin.com/video/star_1", title="Star")
    db.record_video(channel["id"], video)
    db.set_video_starred("star_1", True)
    assert db.get_video("star_1")["is_starred"] == 1
    removed = db.delete_video("star_1")
    assert removed["video_id"] == "star_1"
    assert db.get_video("star_1") is None
    assert not db.list_queue()


class FakeAutoUploader:
    def __init__(self):
        self.video_ids = []

    async def upload(self, video):
        self.video_ids.append(video['video_id'])
        return {'id': f"job-{video['video_id']}", 'status': 'running', 'progress': 60}


def test_channel_auto_upload_soft_delete_and_video_upload_state(tmp_path):
    db = AutoCrawlDatabase(tmp_path / 'crawl.db')
    channel = db.add_channel('https://www.douyin.com/user/auto', 'Auto', auto_upload_enabled=True)
    assert channel['auto_upload_enabled'] == 1

    db.update_channel(channel['id'], auto_upload_enabled=0)
    assert db.get_channel(channel['id'])['auto_upload_enabled'] == 0
    db.update_channel(channel['id'], auto_upload_enabled=1)

    db.record_video(channel['id'], VideoCandidate(video_id='auto_1', douyin_url='https://www.douyin.com/video/auto_1', title='Auto video'))
    db.update_video_auto_upload('auto_1', status='running', job_id='job-auto_1', error=None)
    row = db.get_video('auto_1')
    assert row['auto_upload_status'] == 'running'
    assert row['auto_upload_job_id'] == 'job-auto_1'

    removed = db.delete_channel(channel['id'])
    assert removed['is_deleted'] == 1
    assert db.list_channels() == []
    assert db.get_video('auto_1') is not None


def test_auto_upload_runs_after_channel_video_download_completes(tmp_path):
    db = AutoCrawlDatabase(tmp_path / 'crawl.db')
    db.add_channel('https://www.douyin.com/user/abc', 'Kenh A', auto_upload_enabled=True)
    uploader = FakeAutoUploader()
    manager = AutoCrawlManager(db=db, provider=FakeProvider(), downloader=FakeDownloader(), auto_uploader=uploader, config=AutoCrawlConfig(max_video_age_hours=24, min_view_count=100, min_like_count=10, download_new_videos=True))

    result = manager.run_once_sync()

    assert result['status'] == 'completed'
    assert uploader.video_ids == ['v_ok']
    video = db.get_video('v_ok')
    assert video['auto_upload_status'] == 'running'
    assert video['auto_upload_job_id'] == 'job-v_ok'


def test_autocrawl_session_exposes_live_progress_counts(tmp_path):
    db = AutoCrawlDatabase(tmp_path / "crawl.db")
    session_id = db.create_session()
    db.update_session_progress(session_id, total_channels=4, success_count=1, fail_count=1, new_videos=3, skipped_videos=2)

    session = db.list_sessions()[0]
    assert session["status"] == "running"
    assert session["total_channels"] == 4
    assert session["success_count"] == 1
    assert session["fail_count"] == 1
    assert session["new_videos"] == 3
    assert session["skipped_videos"] == 2


def test_autocrawl_run_filters_dedupes_records_and_downloads(tmp_path):
    db = AutoCrawlDatabase(tmp_path / "crawl.db")
    db.add_channel("https://www.douyin.com/user/abc", "Kenh A")
    downloader = FakeDownloader()
    manager = AutoCrawlManager(
        db=db,
        provider=FakeProvider(),
        downloader=downloader,
        config=AutoCrawlConfig(max_video_age_hours=24, min_view_count=100, min_like_count=10, download_new_videos=True),
    )

    result = manager.run_once_sync()

    assert result["status"] == "completed"
    assert result["total_channels"] == 1
    assert result["new_videos"] == 1
    assert result["skipped_videos"] == 3
    videos = db.list_videos()
    assert [v["video_id"] for v in videos] == ["v_ok"]
    assert videos[0]["download_status"] == "completed"
    assert downloader.urls == ["https://www.douyin.com/video/v_ok"]
    session = db.list_sessions()[0]
    assert session["status"] == "completed"
    assert session["new_videos"] == 1
