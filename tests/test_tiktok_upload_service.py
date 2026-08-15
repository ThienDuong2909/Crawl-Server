from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import threading
import time

from fastapi.testclient import TestClient


def test_upload_service_lists_shared_videos(monkeypatch, tmp_path):
    from tiktok_upload_service import main

    video_dir = tmp_path / "douyin_video"
    video_dir.mkdir(parents=True)
    (video_dir / "douyin_abc123.mp4").write_bytes(b"video-bytes")
    monkeypatch.setenv("TIKTOK_UPLOAD_MODE", "dry_run")
    main.settings.download_dir = tmp_path

    client = TestClient(main.app)
    resp = client.get("/api/videos")

    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["video_id"] == "abc123"
    assert data[0]["filename"] == "douyin_abc123.mp4"
    assert data[0]["ready_for_tiktok_upload"] is True


def test_upload_service_creates_dry_run_upload_job(monkeypatch, tmp_path):
    from tiktok_upload_service import main

    video_dir = tmp_path / "douyin_video"
    video_dir.mkdir(parents=True)
    (video_dir / "douyin_abc123.mp4").write_bytes(b"video-bytes")
    monkeypatch.setenv("TIKTOK_UPLOAD_MODE", "dry_run")
    main.settings.download_dir = tmp_path
    main.job_manager.reset()

    client = TestClient(main.app)
    created = client.post(
        "/api/upload/jobs",
        json={
            "filename": "douyin_abc123.mp4",
            "account": "account_test",
            "caption": "caption #test",
            "options": {"visibility_type": 1, "allow_comment": 0},
        },
    )

    assert created.status_code == 200
    job_id = created.json()["id"]

    detail = client.get(f"/api/upload/jobs/{job_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["status"] == "success"
    assert body["progress"] == 100
    assert body["result"]["mode"] == "dry_run"
    assert body["result"]["account"] == "account_test"
    assert body["result"]["caption"] == "caption #test"


def test_upload_job_succeeds_even_if_history_write_fails(monkeypatch, tmp_path):
    from tiktok_upload_service import main

    video_dir = tmp_path / "douyin_video"
    video_dir.mkdir(parents=True)
    (video_dir / "douyin_abc123.mp4").write_bytes(b"video-bytes")
    monkeypatch.setenv("TIKTOK_UPLOAD_MODE", "dry_run")
    main.settings.download_dir = tmp_path
    main.job_manager.reset()

    def fail_history(_job):
        raise PermissionError("history not writable")

    monkeypatch.setattr(main.job_manager, "_append_history", fail_history)
    client = TestClient(main.app)
    resp = client.post(
        "/api/upload/jobs",
        json={"filename": "douyin_abc123.mp4", "account": "a", "caption": "c"},
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "success"


def test_upload_service_rejects_path_traversal(monkeypatch, tmp_path):
    from tiktok_upload_service import main

    monkeypatch.setenv("TIKTOK_UPLOAD_MODE", "dry_run")
    main.settings.download_dir = tmp_path
    client = TestClient(main.app)

    resp = client.post(
        "/api/upload/jobs",
        json={"filename": "../secret.mp4", "account": "a", "caption": "c"},
    )

    assert resp.status_code == 400


def test_upload_queue_waits_60_to_120_seconds_between_tiktok_jobs(tmp_path, monkeypatch):
    from tiktok_upload_service import main

    video_dir = tmp_path / "douyin_video"
    video_dir.mkdir(parents=True)
    for name in ("douyin_one.mp4", "douyin_two.mp4"):
        (video_dir / name).write_bytes(b"video")
    monkeypatch.setattr(main.settings, "download_dir", tmp_path)
    monkeypatch.setattr(main.settings, "data_dir", tmp_path / "data")

    now = [1000.0]
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    gate = main.TikTokUploadGate(
        min_interval_seconds=60,
        max_interval_seconds=120,
        clock=lambda account_id="main_tiktok": now[0],
        sleeper=fake_sleep,
        choose_interval=lambda low, high: 75,
    )

    class Adapter:
        def upload(self, **kwargs):
            return {"mode": "inbox", "ok": True, "workflow_status": "awaiting_user_review"}

    manager = main.UploadJobManager(adapter=Adapter(), upload_gate=gate)
    monkeypatch.setenv("TIKTOK_UPLOAD_MODE", "inbox")
    first = manager.create_and_run(main.UploadJobRequest(filename="douyin_one.mp4", account="a", caption="c"))
    second = manager.create_and_run(main.UploadJobRequest(filename="douyin_two.mp4", account="a", caption="c"))

    assert first.status == "awaiting_user_review"
    assert second.status == "awaiting_user_review"
    assert sleeps == [75]
    assert gate.min_interval_seconds == 60
    assert gate.max_interval_seconds == 120


def test_upload_jobs_are_processed_one_at_a_time(tmp_path, monkeypatch):
    from tiktok_upload_service import main

    video_dir = tmp_path / "douyin_video"
    video_dir.mkdir(parents=True)
    for name in ("douyin_one.mp4", "douyin_two.mp4"):
        (video_dir / name).write_bytes(b"video")
    monkeypatch.setattr(main.settings, "download_dir", tmp_path)
    monkeypatch.setattr(main.settings, "data_dir", tmp_path / "data")
    monkeypatch.setenv("TIKTOK_UPLOAD_MODE", "dry_run")

    class SerialAdapter:
        active = 0
        max_active = 0
        lock = threading.Lock()

        def upload(self, *, video_path, account, caption, options):
            with self.lock:
                type(self).active += 1
                type(self).max_active = max(type(self).max_active, type(self).active)
            time.sleep(0.03)
            with self.lock:
                type(self).active -= 1
            return {"mode": "test", "ok": True}

    adapter = SerialAdapter()
    manager = main.UploadJobManager(adapter=adapter)
    def create(filename):
        return manager.create_and_run(main.UploadJobRequest(filename=filename, account="a", caption="c"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = list(pool.map(create, ["douyin_one.mp4", "douyin_two.mp4"]))
    assert all(job.status == "success" for job in jobs)
    assert adapter.max_active == 1


def test_ayrshare_mode_auto_publishes_public_video_without_exposing_api_key(tmp_path, monkeypatch):
    from tiktok_upload_service import main

    video_dir = tmp_path / "douyin_video"
    video_dir.mkdir()
    (video_dir / "douyin_public.mp4").write_bytes(b"video-data")
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "ayrshare_api_key.txt").write_text("ayr-secret-key")
    (secrets_dir / "ayrshare_profile_key.txt").write_text("profile-key-123")
    monkeypatch.setattr(main.settings, "download_dir", tmp_path)
    monkeypatch.setattr(main.settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(main.settings, "secrets_dir", secrets_dir)
    main.job_manager.reset()
    main.job_manager.adapter = main.TikTokUploadAdapter()
    monkeypatch.setenv("TIKTOK_UPLOAD_MODE", "ayrshare")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://nwm.thienne.io.vn")

    sent = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"status": "success", "id": "ayr-post-123", "postIds": [{"platform": "tiktok", "id": "tt-123"}]}

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        sent.update({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr(main.httpx, "post", fake_post)
    body = TestClient(main.app).post("/api/upload/jobs", json={
        "filename": "douyin_public.mp4",
        "account": "main_tiktok",
        "caption": "Caption Việt #xh #girl #chinesegirl",
    }).json()

    assert body["status"] == "success"
    assert body["result"]["mode"] == "ayrshare"
    assert body["result"]["provider_post_id"] == "ayr-post-123"
    assert body["result"]["provider_status"] == "success"
    assert "ayr-secret-key" not in str(body)
    assert "profile-key-123" not in str(body)
    assert sent["url"] == "https://app.ayrshare.com/api/post"
    assert sent["headers"]["Authorization"] == "Bearer ayr-secret-key"
    assert sent["headers"]["Profile-Key"] == "profile-key-123"
    assert sent["json"]["platforms"] == ["tiktok"]
    assert sent["json"]["mediaUrls"] == ["https://nwm.thienne.io.vn/upload-api/api/videos/douyin_public.mp4"]
    assert sent["json"]["post"] == "Caption Việt #xh #girl #chinesegirl"
    health = TestClient(main.app).get("/health").json()
    assert health["mode"] == "ayrshare"
    assert health["provider_ready"] is True


def test_ayrshare_mode_fails_closed_when_api_key_is_missing(tmp_path, monkeypatch):
    from tiktok_upload_service import main

    video_dir = tmp_path / "douyin_video"
    video_dir.mkdir()
    (video_dir / "douyin_public.mp4").write_bytes(b"video-data")
    monkeypatch.setattr(main.settings, "download_dir", tmp_path)
    monkeypatch.setattr(main.settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(main.settings, "secrets_dir", tmp_path / "empty-secrets")
    main.job_manager.reset()
    main.job_manager.adapter = main.TikTokUploadAdapter()
    monkeypatch.setenv("TIKTOK_UPLOAD_MODE", "ayrshare")
    body = TestClient(main.app).post("/api/upload/jobs", json={
        "filename": "douyin_public.mp4", "account": "main_tiktok", "caption": "caption",
    }).json()
    assert body["status"] == "failed"
    assert "AYRSHARE API key" in body["error"]


def test_inbox_mode_initializes_and_uploads_video_for_user_review(tmp_path, monkeypatch):
    from tiktok_upload_service import main

    video_dir = tmp_path / "douyin_video"
    video_dir.mkdir()
    video_bytes = b"inbox-video-bytes"
    (video_dir / "douyin_inbox.mp4").write_bytes(video_bytes)
    monkeypatch.setattr(main.settings, "download_dir", tmp_path)
    monkeypatch.setattr(main.settings, "data_dir", tmp_path / "data")
    main.job_manager.reset()
    main.job_manager.adapter = main.TikTokUploadAdapter()
    monkeypatch.setenv("TIKTOK_UPLOAD_MODE", "inbox")
    monkeypatch.setattr(main, "refresh_tiktok_access_token_if_needed", lambda account_id="main_tiktok": {"access_token": "sandbox-access-token"})
    sent = {"posts": [], "puts": []}

    class InitResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": {"publish_id": "v_inbox_file~v2.123", "upload_url": "https://open-upload.tiktokapis.com/video/?secret=hidden"},
                "error": {"code": "ok", "message": "", "log_id": "log-init-123"},
            }

    class UploadResponse:
        status_code = 201

        def raise_for_status(self):
            return None

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        sent["posts"].append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return InitResponse()

    def fake_put(url, headers=None, content=None, timeout=None, **kwargs):
        sent["puts"].append({"url": url, "headers": headers, "content": content, "timeout": timeout})
        return UploadResponse()

    monkeypatch.setattr(main.httpx, "post", fake_post)
    monkeypatch.setattr(main.httpx, "put", fake_put)
    body = TestClient(main.app).post("/api/upload/jobs", json={
        "filename": "douyin_inbox.mp4", "account": "main_tiktok", "caption": "ignored in inbox",
    }).json()

    assert body["status"] == "awaiting_user_review"
    assert body["result"]["mode"] == "inbox"
    assert body["result"]["workflow_status"] == "awaiting_user_review"
    assert body["result"]["publish_id"] == "v_inbox_file~v2.123"
    assert body["result"]["log_id"] == "log-init-123"
    assert "sandbox-access-token" not in str(body)
    assert "open-upload.tiktokapis.com" not in str(body)
    init = sent["posts"][0]
    assert init["url"] == "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
    assert init["headers"]["Authorization"] == "Bearer sandbox-access-token"
    assert init["json"] == {"source_info": {
        "source": "FILE_UPLOAD",
        "video_size": len(video_bytes),
        "chunk_size": len(video_bytes),
        "total_chunk_count": 1,
    }}
    upload = sent["puts"][0]
    assert upload["content"] == video_bytes
    assert upload["headers"]["Content-Type"] == "video/mp4"
    assert upload["headers"]["Content-Length"] == str(len(video_bytes))
    assert upload["headers"]["Content-Range"] == f"bytes 0-{len(video_bytes)-1}/{len(video_bytes)}"


def test_inbox_mode_preserves_tiktok_daily_cap_error_without_uploading(tmp_path, monkeypatch):
    from tiktok_upload_service import main

    video_dir = tmp_path / "douyin_video"
    video_dir.mkdir()
    (video_dir / "douyin_capped.mp4").write_bytes(b"video-bytes")
    monkeypatch.setattr(main.settings, "download_dir", tmp_path)
    monkeypatch.setattr(main.settings, "data_dir", tmp_path / "data")
    main.job_manager.reset()
    main.job_manager.adapter = main.TikTokUploadAdapter()
    monkeypatch.setenv("TIKTOK_UPLOAD_MODE", "inbox")
    monkeypatch.setattr(main, "refresh_tiktok_access_token_if_needed", lambda account_id="main_tiktok": {"access_token": "secret-token"})

    class CappedResponse:
        status_code = 400
        text = '{"error":{"code":"spam_risk_too_many_pending_share","message":"spam_risk_too_many_pending_share","log_id":"log-cap-123"}}'

        def json(self):
            return {
                "error": {
                    "code": "spam_risk_too_many_pending_share",
                    "message": "spam_risk_too_many_pending_share",
                    "log_id": "log-cap-123",
                }
            }

        def raise_for_status(self):
            raise AssertionError("structured TikTok errors must be handled before raise_for_status")

    monkeypatch.setattr(main.httpx, "post", lambda *args, **kwargs: CappedResponse())
    monkeypatch.setattr(main.httpx, "put", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not upload after rejected init")))

    body = TestClient(main.app).post("/api/upload/jobs", json={
        "filename": "douyin_capped.mp4", "account": "main_tiktok", "caption": "caption",
    }).json()

    assert body["status"] == "failed"
    assert "đã đạt giới hạn upload API trong ngày" in body["error"]
    assert "spam_risk_too_many_pending_share" in body["error"]
    assert "log-cap-123" in body["error"]
    assert "secret-token" not in str(body)


def test_inbox_mode_fails_closed_when_oauth_token_is_unavailable(tmp_path, monkeypatch):
    from tiktok_upload_service import main

    video_dir = tmp_path / "douyin_video"
    video_dir.mkdir()
    (video_dir / "douyin_inbox.mp4").write_bytes(b"video")
    monkeypatch.setattr(main.settings, "download_dir", tmp_path)
    monkeypatch.setattr(main.settings, "data_dir", tmp_path / "data")
    main.job_manager.reset()
    main.job_manager.adapter = main.TikTokUploadAdapter()
    monkeypatch.setenv("TIKTOK_UPLOAD_MODE", "inbox")
    monkeypatch.setattr(main, "refresh_tiktok_access_token_if_needed", lambda account_id="main_tiktok": (_ for _ in ()).throw(RuntimeError("TikTok OAuth token unavailable")))
    body = TestClient(main.app).post("/api/upload/jobs", json={
        "filename": "douyin_inbox.mp4", "account": "main_tiktok", "caption": "caption",
    }).json()
    assert body["status"] == "failed"
    assert body["result"] is None
    assert "OAuth token unavailable" in body["error"]


def test_photo_media_upload_sends_ordered_images_to_tiktok_inbox(tmp_path, monkeypatch):
    from tiktok_upload_service import main

    album = tmp_path / "douyin_photo" / "douyin_photo-68"
    album.mkdir(parents=True)
    (album / "01.webp").write_bytes(b"one")
    (album / "02.jpg").write_bytes(b"two")
    (album / "tiktok_01.webp").write_bytes(b"optimized-one")
    (album / "tiktok_02.webp").write_bytes(b"optimized-two")
    (album / "music.mp3").write_bytes(b"music-reference")
    (album / "manifest.json").write_text(
        '{"type":"photo","video_id":"photo-68","image_files":["01.webp","02.jpg"],"tiktok_image_files":["tiktok_01.webp","tiktok_02.webp"],"watermark_processing":{"status":"clean","method":"clean_source_url"},"music_file":"music.mp3","music_title":"凌风"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(main.settings, "download_dir", tmp_path)
    monkeypatch.setattr(main.settings, "data_dir", tmp_path / "data")
    monkeypatch.setenv("TIKTOK_UPLOAD_MODE", "inbox")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://nwm.thienne.io.vn")
    monkeypatch.setattr(main, "refresh_tiktok_access_token_if_needed", lambda account_id="main_tiktok": {"access_token": "photo-access-token"})
    main.job_manager.reset()
    sent = {}

    class Response:
        def raise_for_status(self):
            return None
        def json(self):
            return {"data": {"publish_id": "p_pub_url~v2.photo123"}, "error": {"code": "ok", "message": "", "log_id": "photo-log"}}

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        sent.update({"url": url, "headers": headers, "json": json})
        return Response()

    monkeypatch.setattr(main.httpx, "post", fake_post)
    client = TestClient(main.app)
    response = client.post("/api/upload/photo-jobs", json={"photo_id": "photo-68", "account": "main_tiktok", "caption": "Tiêu đề Việt"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "awaiting_user_review"
    assert body["result"]["publish_id"] == "p_pub_url~v2.photo123"
    assert body["result"]["media_type"] == "photo"
    assert "photo-access-token" not in str(body)
    assert sent["url"] == "https://open.tiktokapis.com/v2/post/publish/content/init/"
    assert sent["json"] == {
        "post_info": {"title": "Tiêu đề Việt", "description": ""},
        "source_info": {
            "source": "PULL_FROM_URL",
            "photo_cover_index": 0,
            "photo_images": [
                "https://nwm.thienne.io.vn/upload-api/api/photos/photo-68/tiktok_01.webp",
                "https://nwm.thienne.io.vn/upload-api/api/photos/photo-68/tiktok_02.webp",
            ],
        },
        "post_mode": "MEDIA_UPLOAD",
        "media_type": "PHOTO",
    }
    assert "music" not in str(sent["json"]).lower()
    served = client.get("/api/photos/photo-68/01.webp")
    assert served.status_code == 200
    assert served.content == b"one"
    assert served.headers["content-type"].startswith("image/webp")
    optimized = client.get("/api/photos/photo-68/tiktok_01.webp")
    assert optimized.status_code == 200
    assert optimized.content == b"optimized-one"
    music = client.get("/api/photos/photo-68/music")
    assert music.status_code == 200
    assert music.content == b"music-reference"
    assert client.get("/api/photos/../01.webp").status_code in {400, 404}


def test_photo_upload_fails_closed_when_watermark_stage_is_not_clean(tmp_path, monkeypatch):
    from tiktok_upload_service import main

    album = tmp_path / "douyin_photo" / "douyin_blocked"
    album.mkdir(parents=True)
    (album / "01.webp").write_bytes(b"source")
    (album / "tiktok_01.webp").write_bytes(b"possibly-watermarked")
    (album / "manifest.json").write_text(
        '{"type":"photo","video_id":"blocked","image_files":["01.webp"],"tiktok_image_files":["tiktok_01.webp"]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(main.settings, "download_dir", tmp_path)
    monkeypatch.setattr(main.settings, "data_dir", tmp_path / "data")
    monkeypatch.setenv("TIKTOK_UPLOAD_MODE", "inbox")
    monkeypatch.setattr(main, "refresh_tiktok_access_token_if_needed", lambda account_id="main_tiktok": {"access_token": "test-token"})
    main.job_manager.reset()
    called = {"tiktok": False}

    def forbidden_post(*args, **kwargs):
        called["tiktok"] = True
        raise AssertionError("TikTok must not be called")

    monkeypatch.setattr(main.httpx, "post", forbidden_post)
    body = TestClient(main.app).post("/api/upload/photo-jobs", json={
        "photo_id": "blocked", "account": "main_tiktok", "caption": "Tiêu đề Việt",
    }).json()

    assert body["status"] == "failed"
    assert "watermark" in body["error"].lower()
    assert called["tiktok"] is False


def test_publish_status_endpoint_returns_scrubbed_inbox_state(monkeypatch):
    from tiktok_upload_service import main

    monkeypatch.setattr(main, "refresh_tiktok_access_token_if_needed", lambda account_id="main_tiktok": {"access_token": "sandbox-access-token"})
    sent = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": {"status": "SEND_TO_USER_INBOX", "uploaded_bytes": 12345, "publicaly_available_post_id": []},
                "error": {"code": "ok", "message": "", "log_id": "status-log-1"},
            }

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        sent.update({"url": url, "headers": headers, "json": json})
        return FakeResponse()

    monkeypatch.setattr(main.httpx, "post", fake_post)
    response = TestClient(main.app).get("/api/tiktok/publish/status/v_inbox_file~v2.123")
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "publish_id": "v_inbox_file~v2.123",
        "status": "SEND_TO_USER_INBOX",
        "uploaded_bytes": 12345,
        "fail_reason": "",
        "post_ids": [],
        "error_code": "ok",
        "error_message": "",
        "log_id": "status-log-1",
    }
    assert sent["url"] == "https://open.tiktokapis.com/v2/post/publish/status/fetch/"
    assert sent["json"] == {"publish_id": "v_inbox_file~v2.123"}
    assert "sandbox-access-token" not in str(body)
