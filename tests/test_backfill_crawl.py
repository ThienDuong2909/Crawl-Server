import asyncio
import time
from datetime import datetime, time as clock_time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo


def test_backfill_uses_fixed_vietnam_slots_8_11_19():
    from douyin_nwm_tool.autocrawl import next_backfill_slot_at

    tz = ZoneInfo("Asia/Ho_Chi_Minh")
    day = datetime(2026, 8, 17, tzinfo=tz)
    assert next_backfill_slot_at(day.replace(hour=7, minute=59).timestamp()) == day.replace(hour=8, minute=0).timestamp()
    assert next_backfill_slot_at(day.replace(hour=8, minute=1).timestamp()) == day.replace(hour=11, minute=0).timestamp()
    assert next_backfill_slot_at(day.replace(hour=11, minute=1).timestamp()) == day.replace(hour=19, minute=0).timestamp()
    assert next_backfill_slot_at(day.replace(hour=19, minute=1).timestamp()) == (day + timedelta(days=1)).replace(hour=8, minute=0).timestamp()


def test_scheduler_runs_backfill_instead_of_normal_crawl_until_completed(tmp_path):
    from douyin_nwm_tool.autocrawl import AutoCrawlDatabase, AutoCrawlScheduler

    db = AutoCrawlDatabase(tmp_path / "scheduler-backfill.db")
    channel = db.add_channel(
        "https://www.douyin.com/user/scheduler-history",
        "Scheduler history",
        schedule_enabled=True,
        tiktok_account_id="secondary-account",
    )
    db.update_channel(channel["id"], backfill_next_run_at=time.time() - 1)

    class FakeManager:
        def __init__(self):
            self.db = db
            self.backfill_calls = []
            self.normal_calls = []

        async def run_backfill_batch(self, channel_id):
            self.backfill_calls.append(channel_id)
            return {"status": "active", "processed": 3, "remaining": 2}

        async def run_once(self, channel_id=None, download=True):
            self.normal_calls.append(channel_id)
            return {"status": "completed"}

    manager = FakeManager()
    result = asyncio.run(AutoCrawlScheduler(manager).run_due_channels())

    assert result == {"processed": 1, "completed": 1, "failed": 0}
    assert manager.backfill_calls == [channel["id"]]
    assert manager.normal_calls == []
    assert db.get_channel(channel["id"])["last_run_at"] is None


def test_backfill_processes_only_three_oldest_per_day_then_enables_recurring_schedule(tmp_path):
    from douyin_nwm_tool.autocrawl import AutoCrawlConfig, AutoCrawlDatabase, AutoCrawlManager, VideoCandidate

    now = time.time()
    candidates = [
        VideoCandidate(str(index), f"https://www.douyin.com/video/{index}", title=f"Video {index}", published_at=now - days * 86400)
        for index, days in [(1, 1), (5, 80), (3, 30), (4, 60), (2, 10)]
    ]

    class FakeProvider:
        async def fetch_channel_history(self, channel, cutoff_at):
            return candidates

    class FakeDownloader:
        async def download(self, url, **kwargs):
            video_id = url.rsplit("/", 1)[-1]
            path = tmp_path / f"{video_id}.mp4"
            path.write_bytes(b"video")
            return SimpleNamespace(file_path=path, bytes_written=5, type="video", image_paths=[], music_path=None)

    class FakeUploader:
        def __init__(self):
            self.uploaded = []

        async def upload(self, video):
            self.uploaded.append(video["video_id"])
            return {"id": f"job-{video['video_id']}", "status": "awaiting_user_review"}

    class FakeReporter:
        def __init__(self):
            self.events = []

        async def report(self, channel, event):
            self.events.append((channel["id"], event["kind"], event["processed"], event["remaining"]))
            return {"status": "sent"}

    db = AutoCrawlDatabase(tmp_path / "daily-backfill.db")
    channel = db.add_channel(
        "https://www.douyin.com/user/history-batch",
        "History batch",
        schedule_enabled=True,
        auto_upload_enabled=False,
        tiktok_account_id="secondary-account",
    )
    uploader = FakeUploader()
    reporter = FakeReporter()
    manager = AutoCrawlManager(
        db=db,
        provider=FakeProvider(),
        downloader=FakeDownloader(),
        auto_uploader=uploader,
        progress_reporter=reporter,
        config=AutoCrawlConfig(max_video_age_hours=72),
    )

    db.update_channel(channel["id"], backfill_next_run_at=now - 1)
    first = asyncio.run(manager.run_backfill_batch(channel["id"], now=now))
    after_first = db.get_channel(channel["id"])
    assert first["processed"] == 1
    assert uploader.uploaded == ["5"]
    assert after_first["backfill_status"] == "active"
    assert after_first["backfill_processed"] == 1
    assert after_first["schedule_enabled"] == 0
    assert after_first["next_run_at"] is None
    assert after_first["backfill_next_run_at"] > now
    assert reporter.events == [(channel["id"], "backfill_progress", 1, 4)]

    deferred = asyncio.run(manager.run_backfill_batch(channel["id"], now=now + 60))
    assert deferred["status"] == "deferred"
    assert deferred["processed"] == 0
    assert uploader.uploaded == ["5"]

    for index, video_id in enumerate(["4", "3", "2", "1"], start=1):
        db.update_channel(channel["id"], backfill_next_run_at=now - 1)
        result = asyncio.run(manager.run_backfill_batch(channel["id"], now=now + index))
        assert result["processed"] == 1
        assert uploader.uploaded[-1] == video_id
    completed = db.get_channel(channel["id"])
    assert completed["backfill_status"] == "completed"
    assert completed["backfill_processed"] == 5
    assert completed["backfill_completed_at"] is not None
    assert completed["backfill_next_run_at"] is None
    assert completed["schedule_enabled"] == 1
    assert reporter.events[-1] == (channel["id"], "backfill_completed", 1, 0)
    assert completed["last_notification_status"] == "sent"


def test_history_provider_paginates_until_90_day_cutoff_and_returns_oldest_first():
    from douyin_nwm_tool.autocrawl import DouyinChannelProvider

    now = time.time()

    def item(video_id, days_old):
        return {
            "aweme_id": video_id,
            "create_time": now - days_old * 24 * 3600,
            "video": {"duration": 10_000},
        }

    class FakeCrawler:
        async def fetch_user_post_videos(self, sec_user_id, cursor, count):
            if cursor == 0:
                return {"aweme_list": [item("new", 1), item("middle", 30)], "has_more": 1, "max_cursor": 20}
            return {"aweme_list": [item("old", 89), item("too-old", 91)], "has_more": 1, "max_cursor": 40}

    provider = DouyinChannelProvider(crawler=FakeCrawler())
    videos = asyncio.run(provider.fetch_channel_history(
        {"profile_url": "https://www.douyin.com/user/history", "douyin_uid": "uid"},
        cutoff_at=now - 90 * 24 * 3600,
    ))

    assert [video.video_id for video in videos] == ["old", "middle", "new"]


def test_non_default_account_channel_starts_persisted_backfill_without_recurring_schedule(tmp_path):
    from douyin_nwm_tool.autocrawl import AutoCrawlDatabase

    db = AutoCrawlDatabase(tmp_path / "backfill.db")
    channel = db.add_channel(
        "https://www.douyin.com/user/history",
        "History",
        "history",
        interval_minutes=120,
        schedule_enabled=True,
        auto_upload_enabled=True,
        tiktok_account_id="secondary-account",
    )

    assert channel["backfill_status"] == "pending"
    assert channel["backfill_cutoff_at"] <= time.time() - 89 * 24 * 3600
    assert channel["backfill_next_run_at"] is not None
    assert channel["recurring_schedule_enabled"] == 1
    assert channel["schedule_enabled"] == 0
    assert channel["next_run_at"] is None
    assert db.list_due_channels(now=time.time() + 1) == []


def test_default_account_channel_keeps_normal_recurring_schedule(tmp_path):
    from douyin_nwm_tool.autocrawl import AutoCrawlDatabase

    db = AutoCrawlDatabase(tmp_path / "normal.db")
    channel = db.add_channel(
        "https://www.douyin.com/user/normal",
        "Normal",
        schedule_enabled=True,
        tiktok_account_id="main_tiktok",
    )

    assert channel["backfill_status"] == "not_required"
    assert channel["schedule_enabled"] == 1
    assert channel["recurring_schedule_enabled"] == 1
    assert channel["next_run_at"] is not None
