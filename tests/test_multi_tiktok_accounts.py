from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient


def test_tiktok_account_stores_valid_notification_email_without_exposing_secrets(monkeypatch, tmp_path):
    from tiktok_upload_service import main

    monkeypatch.setattr(main.settings, "data_dir", tmp_path)
    client = TestClient(main.app)
    created = client.post("/api/tiktok/accounts", json={
        "display_name": "Báo cáo",
        "notification_email": "owner@example.com",
    })

    assert created.status_code == 200
    assert created.json()["notification_email"] == "owner@example.com"
    listed = client.get("/api/tiktok/accounts").json()["items"]
    assert next(item for item in listed if item["id"] == created.json()["id"])["notification_email"] == "owner@example.com"
    assert client.post("/api/tiktok/accounts", json={
        "display_name": "Sai email",
        "notification_email": "not-an-email",
    }).status_code == 422


def test_token_key_rotation_reencrypts_with_new_primary_key(monkeypatch, tmp_path):
    import base64
    from tiktok_upload_service import main

    monkeypatch.setattr(main.settings, "data_dir", tmp_path)
    old_key = base64.urlsafe_b64encode(b"o" * 32).decode("ascii")
    new_key = base64.urlsafe_b64encode(b"n" * 32).decode("ascii")
    monkeypatch.setenv("TIKTOK_TOKEN_ENCRYPTION_KEY", old_key)
    monkeypatch.delenv("TIKTOK_TOKEN_ENCRYPTION_PREVIOUS_KEYS", raising=False)
    main.save_tiktok_oauth_token({"access_token": "secret-old", "refresh_token": "refresh-old"})

    monkeypatch.setenv("TIKTOK_TOKEN_ENCRYPTION_KEY", new_key)
    monkeypatch.setenv("TIKTOK_TOKEN_ENCRYPTION_PREVIOUS_KEYS", old_key)
    assert main.load_tiktok_oauth_token()["access_token"] == "secret-old"

    monkeypatch.delenv("TIKTOK_TOKEN_ENCRYPTION_PREVIOUS_KEYS")
    assert main.load_tiktok_oauth_token()["refresh_token"] == "refresh-old"


def test_cannot_complete_oauth_for_an_account_deleted_after_connect(monkeypatch, tmp_path):
    from tiktok_upload_service import main

    monkeypatch.setattr(main.settings, "data_dir", tmp_path)
    monkeypatch.setattr(main.settings, "secrets_dir", tmp_path)
    monkeypatch.setenv("TIKTOK_REDIRECT_URI", "https://example.test/api/tiktok/callback")
    (tmp_path / "tiktok_dev_oauth.json").write_text(
        '{"client_key":"client-key","client_secret":"client-secret"}', encoding="utf-8"
    )
    account = main.create_tiktok_account("Temporary")
    client = TestClient(main.app)
    auth_url = client.get(f"/api/tiktok/accounts/{account['id']}/oauth/connect").json()["auth_url"]
    state = parse_qs(urlparse(auth_url).query)["state"][0]
    assert client.delete(f"/api/tiktok/accounts/{account['id']}").status_code == 200
    assert client.get(f"/api/tiktok/oauth/callback?code=unused&state={state}").status_code == 400


def test_multi_account_oauth_state_routes_token_without_leaking_secrets(monkeypatch, tmp_path):
    from tiktok_upload_service import main

    main.settings.data_dir = tmp_path
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "client_key_test")
    monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "client_secret_must_not_leak")
    monkeypatch.setenv("TIKTOK_REDIRECT_URI", "https://example.test/api/tiktok/callback")

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "access_token": "access_second_secret",
                "refresh_token": "refresh_second_secret",
                "open_id": "open-second",
                "scope": "user.info.basic,video.upload",
                "expires_in": 86400,
                "refresh_expires_in": 31536000,
            }

    monkeypatch.setattr(main.httpx, "post", lambda *args, **kwargs: FakeResponse())
    client = TestClient(main.app)

    created = client.post("/api/tiktok/accounts", json={"display_name": "Kênh TikTok phụ"})
    assert created.status_code == 200
    account_id = created.json()["id"]
    assert account_id != "main_tiktok"

    connect = client.get(f"/api/tiktok/accounts/{account_id}/oauth/connect")
    assert connect.status_code == 200
    auth_url = connect.json()["auth_url"]
    state = parse_qs(urlparse(auth_url).query)["state"][0]

    callback = client.get(f"/api/tiktok/callback?code=code-second&state={state}")
    assert callback.status_code == 200
    body = callback.json()
    assert body["account"]["id"] == account_id
    assert "access_second_secret" not in str(body)
    assert "refresh_second_secret" not in str(body)
    assert "client_secret_must_not_leak" not in str(body)

    accounts = client.get("/api/tiktok/accounts").json()
    selected = next(item for item in accounts["items"] if item["id"] == account_id)
    assert selected["has_access_token"] is True
    assert selected["open_id"] == "open-second"
    assert "access_second_secret" not in str(accounts)
    assert "refresh_second_secret" not in str(accounts)


def test_oauth_state_is_one_time_and_cannot_be_rebound(monkeypatch, tmp_path):
    from tiktok_upload_service import main

    main.settings.data_dir = tmp_path
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "client_key_test")
    monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "client_secret_test")

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"access_token": "a", "refresh_token": "r", "open_id": "o", "expires_in": 3600}

    monkeypatch.setattr(main.httpx, "post", lambda *args, **kwargs: FakeResponse())
    client = TestClient(main.app)
    account_id = client.post("/api/tiktok/accounts", json={"display_name": "Phụ"}).json()["id"]
    auth_url = client.get(f"/api/tiktok/accounts/{account_id}/oauth/connect").json()["auth_url"]
    state = parse_qs(urlparse(auth_url).query)["state"][0]

    assert client.get(f"/api/tiktok/callback?code=first&state={state}").status_code == 200
    assert client.get(f"/api/tiktok/callback?code=replay&state={state}").status_code == 400
    assert client.get("/api/tiktok/callback?code=unknown&state=attacker-state").status_code == 400


def test_legacy_token_remains_the_default_main_account(monkeypatch, tmp_path):
    from tiktok_upload_service import main

    main.settings.data_dir = tmp_path
    main.save_tiktok_oauth_token({
        "access_token": "legacy_access_secret",
        "refresh_token": "legacy_refresh_secret",
        "open_id": "legacy-open",
        "expires_in": 86400,
    })
    client = TestClient(main.app)
    accounts = client.get("/api/tiktok/accounts").json()

    assert accounts["default_account_id"] == "main_tiktok"
    default = next(item for item in accounts["items"] if item["id"] == "main_tiktok")
    assert default["is_default"] is True
    assert default["has_access_token"] is True
    assert default["open_id"] == "legacy-open"
    assert "legacy_access_secret" not in str(accounts)


def test_upload_adapter_uses_token_for_requested_account(monkeypatch, tmp_path):
    from tiktok_upload_service import main

    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    seen = {}

    def fake_refresh(account_id="main_tiktok"):
        seen["account_id"] = account_id
        return {"access_token": "selected-token"}

    class FakeResponse:
        status_code = 400

        def json(self):
            return {"error": {"code": "rate_limit_exceeded", "message": "limited"}}

    monkeypatch.setenv("TIKTOK_UPLOAD_MODE", "inbox")
    monkeypatch.setattr(main, "refresh_tiktok_access_token_if_needed", fake_refresh)
    monkeypatch.setattr(main.httpx, "post", lambda *args, **kwargs: FakeResponse())

    try:
        main.TikTokUploadAdapter().upload(video_path=video, account="second-account", caption="x", options={})
    except RuntimeError:
        pass

    assert seen["account_id"] == "second-account"
