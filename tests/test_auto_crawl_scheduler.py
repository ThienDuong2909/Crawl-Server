from fastapi.testclient import TestClient
import asyncio
import time


def test_per_channel_scheduler_runs_only_due_jobs_and_reschedules(tmp_path):
    from douyin_nwm_tool.autocrawl import AutoCrawlDatabase, AutoCrawlScheduler

    db = AutoCrawlDatabase(tmp_path / "per-channel.db")
    due = db.add_channel("https://www.douyin.com/user/due", "Due", interval_minutes=30, schedule_enabled=True)
    disabled = db.add_channel("https://www.douyin.com/user/off", "Off", interval_minutes=10, schedule_enabled=False)
    db.update_channel(due["id"], next_run_at=time.time() - 1)

    class FakeManager:
        def __init__(self):
            self.db = db
            self.calls = []

        async def run_once(self, channel_id=None, download=True):
            self.calls.append(channel_id)
            await asyncio.sleep(0.02)
            return {"status": "completed", "channel_id": channel_id}

    manager = FakeManager()
    scheduler = AutoCrawlScheduler(manager, interval_minutes=60)
    result = asyncio.run(scheduler.run_due_channels())

    assert manager.calls == [due["id"]]
    assert result["processed"] == 1
    assert db.get_channel(due["id"])["last_run_at"] is not None
    assert db.get_channel(due["id"])["next_run_at"] > time.time()
    assert db.get_channel(disabled["id"])["last_run_at"] is None


def test_scheduler_processes_all_overdue_channels_in_parallel(tmp_path):
    from douyin_nwm_tool.autocrawl import AutoCrawlDatabase, AutoCrawlScheduler

    db = AutoCrawlDatabase(tmp_path / "paced-overdue.db")
    first = db.add_channel("https://www.douyin.com/user/first", "First", schedule_enabled=True)
    second = db.add_channel("https://www.douyin.com/user/second", "Second", schedule_enabled=True)
    past = time.time() - 10
    db.update_channel(first["id"], next_run_at=past - 1)
    db.update_channel(second["id"], next_run_at=past)

    class FakeManager:
        def __init__(self):
            self.db = db
            self.calls = []

        async def run_once(self, channel_id=None, download=True):
            self.calls.append(channel_id)
            await asyncio.sleep(0.02)
            return {"status": "completed", "channel_id": channel_id}

    manager = FakeManager()
    scheduler = AutoCrawlScheduler(manager)

    result = asyncio.run(scheduler.run_due_channels())

    assert result == {"processed": 2, "completed": 2, "failed": 0}
    assert set(manager.calls) == {first["id"], second["id"]}
    assert db.get_channel(first["id"])["last_run_at"] is not None
    assert db.get_channel(second["id"])["last_run_at"] is not None


def test_scheduler_retries_due_error_channel_but_never_runs_paused_channel(tmp_path):
    from douyin_nwm_tool.autocrawl import AutoCrawlDatabase

    db = AutoCrawlDatabase(tmp_path / "recover-error-channel.db")
    retryable = db.add_channel(
        "https://www.douyin.com/user/retryable",
        "Retryable",
        interval_minutes=60,
        schedule_enabled=True,
    )
    paused = db.add_channel(
        "https://www.douyin.com/user/paused",
        "Paused",
        interval_minutes=60,
        schedule_enabled=True,
    )
    now = time.time()
    db.update_channel(
        retryable["id"],
        status="error",
        error_count=1,
        error_message="HTTP trạng thái 403",
        next_run_at=now - 10,
    )
    db.update_channel(paused["id"], status="paused", next_run_at=now - 10)

    due_ids = [row["id"] for row in db.list_due_channels(now=now)]

    assert retryable["id"] in due_ids
    assert paused["id"] not in due_ids


def test_recurring_channel_stays_retryable_after_repeated_provider_errors(tmp_path):
    from douyin_nwm_tool.autocrawl import AutoCrawlConfig, AutoCrawlDatabase, AutoCrawlManager

    db = AutoCrawlDatabase(tmp_path / "repeated-errors.db")
    channel = db.add_channel(
        "https://www.douyin.com/user/flaky",
        "Flaky",
        interval_minutes=1,
        schedule_enabled=True,
    )

    class AlwaysForbiddenProvider:
        async def fetch_channel_videos(self, channel, config):
            raise RuntimeError("HTTP trạng thái 403")

    manager = AutoCrawlManager(
        db=db,
        provider=AlwaysForbiddenProvider(),
        config=AutoCrawlConfig(max_channel_retries=3),
    )

    for _ in range(4):
        result = asyncio.run(manager.run_once(channel_id=channel["id"], download=False))
        assert result["status"] == "failed"

    failed_channel = db.get_channel(channel["id"])
    assert failed_channel["status"] == "error"
    db.update_channel(channel["id"], next_run_at=time.time() - 1)
    assert [row["id"] for row in db.list_due_channels()] == [channel["id"]]


def test_autocrawl_scheduler_status_start_stop(monkeypatch, tmp_path):
    from douyin_nwm_tool import main
    from douyin_nwm_tool.autocrawl import AutoCrawlDatabase, AutoCrawlManager

    main.autocrawl_manager = AutoCrawlManager(db=AutoCrawlDatabase(tmp_path / "scheduler.db"))
    main.autocrawl_scheduler.stop()
    client = TestClient(main.app)

    status = client.get("/api/crawl/scheduler").json()
    assert status["enabled"] is False
    assert status["interval_minutes"] >= 1

    started = client.post("/api/crawl/scheduler", json={"enabled": True, "interval_minutes": 15}).json()
    assert started["enabled"] is True
    assert started["interval_minutes"] == 15
    assert started["next_run_at"] is not None
    assert 0 < started["countdown_seconds"] <= 15 * 60

    stopped = client.post("/api/crawl/scheduler", json={"enabled": False}).json()
    assert stopped["enabled"] is False
