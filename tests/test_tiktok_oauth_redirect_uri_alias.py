from fastapi.testclient import TestClient


def test_tiktok_oauth_uses_doc_public_redirect_uri(monkeypatch, tmp_path):
    from tiktok_upload_service import main

    main.settings.data_dir = tmp_path
    monkeypatch.setenv('TIKTOK_CLIENT_KEY', 'client_key_test')
    monkeypatch.setenv('TIKTOK_CLIENT_SECRET', 'secret_test')
    monkeypatch.delenv('TIKTOK_REDIRECT_URI', raising=False)
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://nwm.thienne.io.vn')

    client = TestClient(main.app)
    status = client.get('/api/tiktok/oauth/status').json()
    assert status['redirect_uri'] == 'https://nwm.thienne.io.vn/api/tiktok/callback'

    connect = client.get('/api/tiktok/oauth/connect').json()
    assert 'redirect_uri=https%3A%2F%2Fnwm.thienne.io.vn%2Fapi%2Ftiktok%2Fcallback' in connect['auth_url']


def test_doc_callback_alias_exchanges_code(monkeypatch, tmp_path):
    from tiktok_upload_service import main

    main.settings.data_dir = tmp_path
    monkeypatch.setenv('TIKTOK_CLIENT_KEY', 'client_key_test')
    monkeypatch.setenv('TIKTOK_CLIENT_SECRET', 'secret_test')
    monkeypatch.delenv('TIKTOK_REDIRECT_URI', raising=False)
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://nwm.thienne.io.vn')

    class FakeResponse:
        def raise_for_status(self):
            return None
        def json(self):
            return {'access_token': 'act.redirect.test', 'refresh_token': 'rft.redirect.test', 'expires_in': 86400, 'open_id': 'sandbox_open_id'}

    def fake_post(url, data=None, headers=None, timeout=None):
        assert data['redirect_uri'] == 'https://nwm.thienne.io.vn/api/tiktok/callback'
        return FakeResponse()

    monkeypatch.setattr(main.httpx, 'post', fake_post)
    client = TestClient(main.app)
    resp = client.get('/api/tiktok/callback?code=code123')
    assert resp.status_code == 200
    assert resp.json()['ok'] is True
    assert client.get('/api/tiktok/oauth/status').json()['has_access_token'] is True


def test_doc_callback_alias_does_not_store_tiktok_error_response(monkeypatch, tmp_path):
    from tiktok_upload_service import main

    main.settings.data_dir = tmp_path
    monkeypatch.setenv('TIKTOK_CLIENT_KEY', 'client_key_test')
    monkeypatch.setenv('TIKTOK_CLIENT_SECRET', 'secret_test')

    class FakeResponse:
        def raise_for_status(self):
            return None
        def json(self):
            return {'error': 'invalid_grant', 'error_description': 'bad code'}

    monkeypatch.setattr(main.httpx, 'post', lambda *args, **kwargs: FakeResponse())
    client = TestClient(main.app)
    resp = client.get('/api/tiktok/callback?code=badcode')
    assert resp.status_code == 400
    assert client.get('/api/tiktok/oauth/status').json()['has_access_token'] is False
