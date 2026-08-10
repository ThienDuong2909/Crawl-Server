from pathlib import Path


def test_dashboard_contains_upload_service_controls():
    html = Path("douyin_nwm_tool/static/index.html").read_text()
    js = Path("douyin_nwm_tool/static/app.js").read_text()
    css = Path("douyin_nwm_tool/static/app.css").read_text()
    assert "TikTok Upload Service" in html
    assert "uploadServiceBase" in js
    assert "/api/upload/jobs" in js
    assert "refreshUploadJobs" in js
    assert "videoPreviewGrid" in html
    assert "selectedVideoPreview" in html
    assert "<video" in js
    assert "selectUploadVideo" in js
    assert "video-card" in css
    assert "upload-workbench" in css
