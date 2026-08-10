from pathlib import Path
import importlib.util

from fastapi.testclient import TestClient

from douyin_nwm_tool.autocrawl import AutoCrawlDatabase, VideoCandidate


VIDEO_ID = "7669005459542857414"


def database_with_video(tmp_path: Path) -> AutoCrawlDatabase:
    db = AutoCrawlDatabase(tmp_path / "subtitle-cues.db")
    channel = db.add_channel("https://www.douyin.com/user/cues", "Subtitle cues")
    db.record_video(
        channel["id"],
        VideoCandidate(
            video_id=VIDEO_ID,
            douyin_url=f"https://www.douyin.com/video/{VIDEO_ID}",
            title="卷发教程",
            raw_data={"aweme_type": 0},
        ),
        status="completed",
    )
    return db


def sample_cues():
    return [
        {"id": 1, "start": 0.0, "end": 2.4, "zh": "大家好", "vi": "Xin chào mọi người!"},
        {"id": 2, "start": 2.4, "end": 5.8, "zh": "开始卷头发", "vi": "Bắt đầu uốn tóc nhé."},
    ]


def test_database_replaces_and_lists_timestamped_subtitle_cues(tmp_path):
    db = database_with_video(tmp_path)

    stored = db.replace_subtitle_cues(VIDEO_ID, sample_cues(), model="gpt-5.6-sol")

    assert [cue["cue_index"] for cue in stored] == [1, 2]
    assert stored[0]["start_time"] == 0.0
    assert stored[0]["end_time"] == 2.4
    assert stored[0]["source_text"] == "大家好"
    assert stored[0]["translated_text"] == "Xin chào mọi người!"
    assert stored[0]["translation_model"] == "gpt-5.6-sol"

    replaced = db.replace_subtitle_cues(VIDEO_ID, [sample_cues()[1]], model="claude-opus-4-8")
    assert len(replaced) == 1
    assert replaced[0]["cue_index"] == 2
    assert replaced[0]["translation_model"] == "claude-opus-4-8"


def test_subtitle_cues_endpoint_returns_persisted_cues(tmp_path, monkeypatch):
    from douyin_nwm_tool import main

    db = database_with_video(tmp_path)
    db.replace_subtitle_cues(VIDEO_ID, sample_cues(), model="gpt-5.6-sol")
    monkeypatch.setattr(main.autocrawl_manager, "db", db)

    with TestClient(main.app) as client:
        response = client.get(f"/api/crawl/videos/{VIDEO_ID}/subtitle-cues")

    assert response.status_code == 200
    payload = response.json()
    assert payload["video_id"] == VIDEO_ID
    assert payload["count"] == 2
    assert payload["items"][1]["translated_text"] == "Bắt đầu uốn tóc nhé."


def test_subtitle_cues_endpoint_returns_404_for_unknown_video(tmp_path, monkeypatch):
    from douyin_nwm_tool import main

    monkeypatch.setattr(main.autocrawl_manager, "db", AutoCrawlDatabase(tmp_path / "empty.db"))
    with TestClient(main.app) as client:
        response = client.get("/api/crawl/videos/missing/subtitle-cues")

    assert response.status_code == 404
