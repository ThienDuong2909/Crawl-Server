from fastapi.testclient import TestClient

from douyin_nwm_tool.main import app


def test_cookie_status_and_webhook_update(monkeypatch, tmp_path):
    # Rewire app-level cookie manager to an isolated temp file for this test.
    from douyin_nwm_tool import main
    from douyin_nwm_tool.cookie_manager import CookieManager

    monkeypatch.delenv("DOUYIN_COOKIE", raising=False)
    monkeypatch.delenv("COOKIE_UPDATE_TOKEN", raising=False)
    manager = CookieManager(cookie_file=tmp_path / "douyin_cookie.txt")
    main.cookie_manager = manager
    main.parser.cookie_manager = manager

    client = TestClient(app)

    before = client.get("/api/cookie/douyin/status")
    assert before.status_code == 200
    assert before.json()["has_cookie"] is False

    resp = client.post("/api/cookie/douyin/webhook", json={
        "service": "douyin",
        "cookie": "ttwid=webhook_cookie; sessionid=webhook_session",
        "timestamp": "2026-07-28T00:00:00Z",
    })

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["status"]["has_cookie"] is True
    assert "webhook_session" not in resp.json()["status"]["redacted_cookie"]
    assert (tmp_path / "douyin_cookie.txt").read_text() == "ttwid=webhook_cookie; sessionid=webhook_session"


def test_cookie_webhook_rejects_bad_token(monkeypatch):
    monkeypatch.setenv("COOKIE_UPDATE_TOKEN", "expected-token")
    client = TestClient(app)

    resp = client.post("/api/cookie/douyin/webhook", json={
        "service": "douyin",
        "cookie": "ttwid=x; sessionid=y",
    }, headers={"X-Cookie-Token": "wrong-token"})

    assert resp.status_code == 401
