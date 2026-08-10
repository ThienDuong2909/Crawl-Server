import asyncio
from pathlib import Path

import pytest

from douyin_nwm_tool.jobs import JobManager
from douyin_nwm_tool.schemas import DownloadResult


class SlowFakeDownloader:
    async def download(self, url: str, progress_callback=None):
        await asyncio.sleep(0.01)
        return DownloadResult(video_id="123", file_path=Path("/tmp/douyin_123.mp4"), source_url=url, bytes_written=42)


@pytest.mark.asyncio
async def test_job_manager_tracks_download_job_lifecycle():
    manager = JobManager(downloader=SlowFakeDownloader())

    job = await manager.create_download_job("https://www.douyin.com/video/123")
    assert job.status in {"queued", "running"}
    assert job.url == "https://www.douyin.com/video/123"

    final = await manager.wait(job.id, timeout=2)

    assert final.status == "success"
    assert final.progress == 100
    assert final.result["video_id"] == "123"
    assert final.result["bytes_written"] == 42
    assert manager.get(job.id).status == "success"
    assert manager.list_jobs()[0].id == job.id


@pytest.mark.asyncio
async def test_job_manager_reports_failure():
    class FailingDownloader:
        async def download(self, url: str, progress_callback=None):
            raise RuntimeError("boom")

    manager = JobManager(downloader=FailingDownloader())
    job = await manager.create_download_job("https://www.douyin.com/video/bad")

    final = await manager.wait(job.id, timeout=2)

    assert final.status == "failed"
    assert final.progress == 100
    assert "boom" in final.error
