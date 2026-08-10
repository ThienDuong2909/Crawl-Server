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
            return {"status": "completed", "channel_id": channel_id}

    manager = FakeManager()
    scheduler = AutoCrawlScheduler(manager, interval_minutes=60)
    result = asyncio.run(scheduler.run_due_channels())

    assert manager.calls == [due["id"]]
    assert result["processed"] == 1
    assert db.get_channel(due["id"])["last_run_at"] is not None
    assert db.get_channel(due["id"])["next_run_at"] > time.time()
    assert db.get_channel(disabled["id"])["last_run_at"] is None


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
