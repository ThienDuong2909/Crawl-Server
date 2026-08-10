import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from douyin_nwm_tool.main import app
from douyin_nwm_tool.schemas import DownloadResult


class FakeJobDownloader:
    async def download(self, url: str, progress_callback=None):
        if progress_callback:
            progress_callback({"phase": "downloading", "progress": 50, "bytes_written": 50, "total_bytes": 100})
        await asyncio.sleep(0.01)
        return DownloadResult(video_id="999", file_path=Path("/tmp/douyin_999.mp4"), source_url=url, bytes_written=100)


def test_dashboard_page_loads():
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Douyin NWM Dashboard" in resp.text
    assert "Cookie" in resp.text
    assert "Download Jobs" in resp.text


def test_download_job_api(monkeypatch):
    from douyin_nwm_tool import main
    from douyin_nwm_tool.jobs import JobManager

    main.job_manager = JobManager(downloader=FakeJobDownloader())
    client = TestClient(app)

    created = client.post("/api/jobs/download", json={"url": "https://www.douyin.com/video/999"})
    assert created.status_code == 200
    job_id = created.json()["id"]

    # Poll until background task finishes.
    for _ in range(20):
        detail = client.get(f"/api/jobs/{job_id}")
        assert detail.status_code == 200
        if detail.json()["status"] == "success":
            break
        import time
        time.sleep(0.02)

    assert detail.json()["status"] == "success"
    assert detail.json()["result"]["video_id"] == "999"
    listed = client.get("/api/jobs").json()
    assert any(j["id"] == job_id for j in listed)
