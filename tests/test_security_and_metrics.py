from fastapi.testclient import TestClient

from douyin_nwm_tool.main import app


def test_api_token_protects_dashboard_and_apis(monkeypatch):
    monkeypatch.setenv("API_AUTH_TOKEN", "secret-token")
    client = TestClient(app)

    assert client.get("/health").status_code == 200
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/api/jobs").status_code == 401

    ok = client.get("/api/jobs", headers={"X-API-Token": "secret-token"})
    assert ok.status_code == 200


def test_metrics_endpoint_reports_process_and_jobs(monkeypatch):
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)
    client = TestClient(app)

    resp = client.get("/api/metrics")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "jobs" in data
    assert "disk" in data
    assert "download_dir" in data["disk"]


def test_rate_limit_can_block_after_threshold(monkeypatch):
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "2")
    client = TestClient(app)

    assert client.get("/api/jobs").status_code == 200
    assert client.get("/api/jobs").status_code == 200
    assert client.get("/api/jobs").status_code == 429
