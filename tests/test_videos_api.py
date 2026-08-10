from fastapi.testclient import TestClient

from douyin_nwm_tool.main import app


def test_videos_api_lists_downloaded_mp4_files(monkeypatch, tmp_path):
    from douyin_nwm_tool import main

    video_dir = tmp_path / "douyin_video"
    video_dir.mkdir(parents=True)
    f = video_dir / "douyin_123.mp4"
    f.write_bytes(b"abc123")
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)
    main.downloader.download_dir = tmp_path

    client = TestClient(app)
    resp = client.get("/api/videos")

    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["video_id"] == "123"
    assert data[0]["filename"] == "douyin_123.mp4"
    assert data[0]["size_bytes"] == 6
    assert data[0]["download_url"] == "/api/videos/douyin_123.mp4"


def test_photo_library_api_lists_saved_album_and_music(monkeypatch, tmp_path):
    from douyin_nwm_tool import main

    album = tmp_path / "douyin_photo" / "douyin_789"
    album.mkdir(parents=True)
    (album / "01.webp").write_bytes(b"one")
    (album / "02.webp").write_bytes(b"two")
    (album / "music.mp3").write_bytes(b"music")
    (album / "manifest.json").write_text(
        '{"type":"photo","video_id":"789","description":"Bài ảnh thử","image_files":["01.webp","02.webp"],"music_file":"music.mp3","music_title":"Nhạc nguồn","music_author":"Tác giả","music_duration_sec":23}',
        encoding="utf-8",
    )
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)
    main.downloader.download_dir = tmp_path

    response = TestClient(app).get("/api/photos")

    assert response.status_code == 200
    assert response.json() == [{
        "video_id": "789",
        "media_type": "photo",
        "title": "Bài ảnh thử",
        "asset_count": 2,
        "photo_preview_urls": [
            "/upload-api/api/photos/789/01.webp",
            "/upload-api/api/photos/789/02.webp",
        ],
        "music_preview_url": "/upload-api/api/photos/789/music",
        "music_title": "Nhạc nguồn",
        "music_author": "Tác giả",
        "music_duration_sec": 23,
        "modified_at": (album / "manifest.json").stat().st_mtime,
    }]


def test_video_file_endpoint_serves_file(monkeypatch, tmp_path):
    from douyin_nwm_tool import main

    video_dir = tmp_path / "douyin_video"
    video_dir.mkdir(parents=True)
    (video_dir / "douyin_456.mp4").write_bytes(b"mp4data")
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)
    main.downloader.download_dir = tmp_path

    client = TestClient(app)
    resp = client.get("/api/videos/douyin_456.mp4")

    assert resp.status_code == 200
    assert resp.content == b"mp4data"
