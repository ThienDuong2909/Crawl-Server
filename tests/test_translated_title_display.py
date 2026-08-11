from pathlib import Path

import pytest

from douyin_nwm_tool.autocrawl import AutoCrawlDatabase, AutoCrawlManager, VideoCandidate


def completed_video(db: AutoCrawlDatabase, tmp_path: Path, *, auto_upload: bool = True):
    channel = db.add_channel(
        "https://www.douyin.com/user/translated-title",
        "Translated title",
        auto_upload_enabled=auto_upload,
        translate_enabled=True,
    )
    candidate = VideoCandidate(
        video_id="translated-title-1",
        douyin_url="https://www.douyin.com/video/translated-title-1",
        title="卷发教程来啦",
        raw_data={"aweme_type": 0, "translate_enabled": True},
    )
    db.record_video(channel["id"], candidate, status="pending")
    video_file = tmp_path / "translated-title-1.mp4"
    video_file.write_bytes(b"video")
    db.update_video_downloaded(candidate.video_id, str(video_file), 0.001)
    return candidate


def test_database_migrates_and_persists_vietnamese_translated_title(tmp_path):
    db = AutoCrawlDatabase(tmp_path / "translated-title.db")
    candidate = completed_video(db, tmp_path)

    updated = db.update_video_translated_title(
        candidate.video_id,
        "Hướng dẫn uốn tóc xoăn đây!",
    )

    assert updated["translated_title"] == "Hướng dẫn uốn tóc xoăn đây!"
    assert updated["translated_at"] is not None
    assert db.list_videos()[0]["translated_title"] == "Hướng dẫn uốn tóc xoăn đây!"


@pytest.mark.asyncio
async def test_translation_is_saved_before_tiktok_upload_can_fail_with_403(tmp_path):
    db = AutoCrawlDatabase(tmp_path / "translation-before-upload.db")
    candidate = completed_video(db, tmp_path)

    class FailingTikTokUploader:
        async def translate_title(self, video):
            assert video["title"] == "卷发教程来啦"
            return "Hướng dẫn uốn tóc xoăn đây!"

        async def upload_translated(self, video, translated_title):
            assert translated_title == "Hướng dẫn uốn tóc xoăn đây!"
            raise RuntimeError("Request failed with status code 403")

    manager = AutoCrawlManager(db=db, auto_uploader=FailingTikTokUploader())
    await manager._auto_upload_if_enabled(candidate.video_id)

    video = db.get_video(candidate.video_id)
    assert video["translated_title"] == "Hướng dẫn uốn tóc xoăn đây!"
    assert video["auto_upload_status"] == "failed"
    assert "403" in video["auto_upload_error"]


@pytest.mark.asyncio
async def test_retry_403_reuses_saved_translation_instead_of_translating_again(tmp_path):
    db = AutoCrawlDatabase(tmp_path / "reuse-translation.db")
    candidate = completed_video(db, tmp_path)
    db.update_video_translated_title(candidate.video_id, "Bản dịch đã lưu #tagcu")
    db.update_video_auto_upload(candidate.video_id, status="failed", error="HTTP 403")

    class ReuseUploader:
        async def translate_title(self, video):
            raise AssertionError("Không được dịch lại khi translated_title đã tồn tại")

        async def upload_translated(self, video, translated_title):
            assert translated_title == "Bản dịch đã lưu"
            return {"id": "retry-upload-1", "status": "running"}

    manager = AutoCrawlManager(db=db, auto_uploader=ReuseUploader())
    action, _ = manager.prepare_video_retry(candidate.video_id)
    await manager.retry_video(candidate.video_id, action)

    video = db.get_video(candidate.video_id)
    assert video["auto_upload_job_id"] == "retry-upload-1"
    assert video["auto_upload_status"] == "running"
    assert video["translated_title"] == "Bản dịch đã lưu"


@pytest.mark.asyncio
async def test_channel_can_skip_translation_and_upload_original_caption(tmp_path):
    db = AutoCrawlDatabase(tmp_path / "skip-translation.db")
    channel = db.add_channel(
        "https://www.douyin.com/user/no-translate",
        "Kênh thường",
        auto_upload_enabled=True,
        translate_enabled=False,
    )
    candidate = VideoCandidate(
        video_id="no-translate-1",
        douyin_url="https://www.douyin.com/video/no-translate-1",
        title="Nội dung thường #hashtagnguon",
    )
    db.record_video(channel["id"], candidate)
    source = tmp_path / "no-translate-1.mp4"
    source.write_bytes(b"video")
    db.update_video_downloaded(candidate.video_id, str(source), 0.001)

    class RecordingUploader:
        async def translate_title(self, video):
            raise AssertionError("Không được gọi dịch khi kênh tắt option translate")

        async def upload_translated(self, video, caption):
            assert caption == "Nội dung thường"
            return {"id": "upload-original-1", "status": "running"}

    manager = AutoCrawlManager(db=db, auto_uploader=RecordingUploader())
    await manager._auto_upload_if_enabled(candidate.video_id)

    video = db.get_video(candidate.video_id)
    assert video["translated_title"] is None
    assert video["auto_upload_job_id"] == "upload-original-1"


@pytest.mark.asyncio
async def test_retry_legacy_video_without_translation_policy_skips_translation(tmp_path):
    db = AutoCrawlDatabase(tmp_path / "legacy-retry.db")
    channel = db.add_channel(
        "https://www.douyin.com/user/legacy-retry",
        "Kênh cũ",
        auto_upload_enabled=True,
        translate_enabled=True,
    )
    candidate = VideoCandidate(
        video_id="legacy-retry-1",
        douyin_url="https://www.douyin.com/video/legacy-retry-1",
        title="Video cũ #hashtagnguon",
        raw_data={"aweme_type": 0},
    )
    db.record_video(channel["id"], candidate)
    source = tmp_path / "legacy-retry-1.mp4"
    source.write_bytes(b"video")
    db.update_video_downloaded(candidate.video_id, str(source), 0.001)
    db.update_video_auto_upload(candidate.video_id, status="failed", error="Translation API request failed (HTTP 400)")

    class LegacyUploader:
        async def translate_title(self, video):
            raise AssertionError("Video legacy không có translate_enabled phải bỏ qua dịch")

        async def upload_translated(self, video, caption):
            assert caption == "Video cũ"
            return {"id": "legacy-retry-upload", "status": "running"}

    manager = AutoCrawlManager(db=db, auto_uploader=LegacyUploader())
    action, _ = manager.prepare_video_retry(candidate.video_id)
    await manager.retry_video(candidate.video_id, action)

    video = db.get_video(candidate.video_id)
    assert action == "upload"
    assert video["auto_upload_job_id"] == "legacy-retry-upload"
    assert video["auto_upload_status"] == "running"
    assert video["translated_title"] is None
