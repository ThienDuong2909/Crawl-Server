from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from douyin_nwm_tool.autocrawl import AutoCrawlDatabase, AutoCrawlManager, VideoCandidate
from douyin_nwm_tool.schemas import DownloadResult


class SuccessfulRetryUploader:
    def __init__(self):
        self.calls = []

    async def upload(self, video):
        self.calls.append(video)
        return {"id": f"retry-job-{len(self.calls)}", "status": "running"}


@pytest.mark.parametrize(
    "failure",
    ["Translation API request failed (HTTP 401)", "Request failed with status code 403"],
)
def test_retry_endpoint_replays_failed_auto_upload_for_each_video(tmp_path, monkeypatch, failure):
    import douyin_nwm_tool.main as main

    db = AutoCrawlDatabase(tmp_path / "retry-video.db")
    channel = db.add_channel(
        "https://www.douyin.com/user/retry-user",
        "Retry user",
        auto_upload_enabled=True,
    )
    candidate = VideoCandidate(
        video_id="retry-video-1",
        douyin_url="https://www.douyin.com/video/retry-video-1",
        title="中文标题",
        raw_data={"aweme_type": 0},
    )
    db.record_video(channel["id"], candidate, status="pending")
    local_path = tmp_path / "douyin_retry-video-1.mp4"
    local_path.write_bytes(b"video")
    db.update_video_downloaded(candidate.video_id, str(local_path), 0.01)
    db.update_video_auto_upload(candidate.video_id, status="failed", error=failure)

    uploader = SuccessfulRetryUploader()
    manager = AutoCrawlManager(db=db, auto_uploader=uploader)
    monkeypatch.setattr(main, "autocrawl_manager", manager)
    monkeypatch.setattr(main.autocrawl_scheduler, "manager", manager)
    client = TestClient(main.app)

    response = client.post(f"/api/crawl/videos/{candidate.video_id}/retry")

    assert response.status_code == 200
    assert response.json()["retry_started"] is True
    updated = db.get_video(candidate.video_id)
    assert updated["auto_upload_status"] == "running"
    assert updated["auto_upload_error"] is None
    assert updated["auto_upload_job_id"] == "retry-job-1"
    assert len(uploader.calls) == 1

    duplicate = client.post(f"/api/crawl/videos/{candidate.video_id}/retry")
    assert duplicate.status_code == 409


def test_retry_endpoint_redownloads_video_when_download_step_failed(tmp_path, monkeypatch):
    import douyin_nwm_tool.main as main

    db = AutoCrawlDatabase(tmp_path / "retry-download.db")
    channel = db.add_channel("https://www.douyin.com/user/retry-download", "Retry download", auto_upload_enabled=False)
    candidate = VideoCandidate(
        video_id="retry-download-1",
        douyin_url="https://www.douyin.com/video/retry-download-1",
        title="Retry download",
        raw_data={"aweme_type": 0},
    )
    db.record_video(channel["id"], candidate, status="pending")
    db.mark_video_failed(candidate.video_id, "Download HTTP 500")

    class SuccessfulDownloader:
        async def download(self, url, progress_callback=None):
            if progress_callback:
                progress_callback({"phase": "downloading", "progress": 50})
            path = tmp_path / "retry-download-1.mp4"
            path.write_bytes(b"video")
            return DownloadResult(video_id=candidate.video_id, file_path=path, source_url=url, bytes_written=5)

    manager = AutoCrawlManager(db=db, downloader=SuccessfulDownloader())
    monkeypatch.setattr(main, "autocrawl_manager", manager)
    monkeypatch.setattr(main.autocrawl_scheduler, "manager", manager)
    client = TestClient(main.app)

    response = client.post(f"/api/crawl/videos/{candidate.video_id}/retry")

    assert response.status_code == 200
    assert response.json()["action"] == "download"
    updated = db.get_video(candidate.video_id)
    assert updated["download_status"] == "completed"
    assert updated["download_error"] is None
    assert updated["auto_upload_status"] == "disabled"
