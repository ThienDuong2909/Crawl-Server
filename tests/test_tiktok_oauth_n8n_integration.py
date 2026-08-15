from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient


def test_tiktok_oauth_status_and_connect_url_are_secret_safe(monkeypatch, tmp_path):
    from tiktok_upload_service import main

    main.settings.data_dir = tmp_path
    monkeypatch.setenv('TIKTOK_CLIENT_KEY', 'client_key_test')
    monkeypatch.setenv('TIKTOK_CLIENT_SECRET', 'secret_should_not_leak')
    monkeypatch.setenv('TIKTOK_REDIRECT_URI', 'https://nwm.thienne.io.vn/upload-api/api/tiktok/oauth/callback')

    client = TestClient(main.app)
    status = client.get('/api/tiktok/oauth/status').json()
    assert status['has_client_config'] is True
    assert status['has_access_token'] is False
    assert 'secret_should_not_leak' not in str(status)

    connect = client.get('/api/tiktok/oauth/connect').json()
    assert connect['auth_url'].startswith('https://www.tiktok.com/v2/auth/authorize/')
    assert 'client_key_test' in connect['auth_url']
    assert 'secret_should_not_leak' not in connect['auth_url']
    assert 'video.publish' in connect['auth_url']
    assert 'video.upload' in connect['auth_url']


def test_tiktok_oauth_callback_stores_redacted_token(monkeypatch, tmp_path):
    from tiktok_upload_service import main

    main.settings.data_dir = tmp_path
    monkeypatch.setenv('TIKTOK_CLIENT_KEY', 'client_key_test')
    monkeypatch.setenv('TIKTOK_CLIENT_SECRET', 'secret_test')
    monkeypatch.setenv('TIKTOK_REDIRECT_URI', 'https://nwm.thienne.io.vn/upload-api/api/tiktok/oauth/callback')

    class FakeResponse:
        def raise_for_status(self):
            return None
        def json(self):
            return {
                'access_token': 'act.secret.access',
                'refresh_token': 'rft.secret.refresh',
                'expires_in': 86400,
                'refresh_expires_in': 31536000,
                'open_id': 'sandbox_open_id',
                'scope': 'user.info.basic,video.upload,video.publish,video.list',
            }

    def fake_post(url, data=None, headers=None, timeout=None):
        assert url == 'https://open.tiktokapis.com/v2/oauth/token/'
        assert data['client_secret'] == 'secret_test'
        assert data['code'] == 'code123'
        return FakeResponse()

    monkeypatch.setattr(main.httpx, 'post', fake_post)
    client = TestClient(main.app)
    auth_url = client.get('/api/tiktok/oauth/connect').json()['auth_url']
    state = parse_qs(urlparse(auth_url).query)['state'][0]
    resp = client.get(f'/api/tiktok/oauth/callback?code=code123&state={state}')
    assert resp.status_code == 200
    body = resp.json()
    assert body['ok'] is True
    assert 'act.secret.access' not in str(body)

    status = client.get('/api/tiktok/oauth/status').json()
    assert status['has_access_token'] is True
    assert status['open_id'] == 'sandbox_open_id'
    assert status['redacted_access_token'].startswith('act.')
    assert 'secret.access' not in str(status)


def test_n8n_publish_job_sends_video_url_and_callback_without_leaking_tokens(monkeypatch, tmp_path):
    from tiktok_upload_service import main

    video_dir = tmp_path / 'douyin_video'
    video_dir.mkdir()
    (video_dir / 'douyin_123.mp4').write_bytes(b'video-data')
    main.settings.download_dir = tmp_path
    main.settings.data_dir = tmp_path / 'data'
    main.job_manager.reset()
    monkeypatch.setenv('TIKTOK_N8N_WEBHOOK_URL', 'https://n8n.thienne.io.vn/webhook/tiktok-upload')
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://nwm.thienne.io.vn')
    main.save_tiktok_oauth_token({
        'access_token': 'act.secret.access',
        'refresh_token': 'rft.secret.refresh',
        'expires_in': 86400,
        'refresh_expires_in': 31536000,
        'open_id': 'sandbox_open_id',
        'scope': 'video.publish,video.upload,user.info.basic',
    })

    sent = {}
    class FakeResponse:
        def raise_for_status(self):
            return None
        def json(self):
            return {'ok': True, 'publish_id': 'publish_sandbox_1'}

    def fake_post(url, json=None, timeout=None):
        sent['url'] = url
        sent['json'] = json
        return FakeResponse()

    monkeypatch.setattr(main.httpx, 'post', fake_post)
    client = TestClient(main.app)
    resp = client.post('/api/upload/n8n/jobs', json={'filename': 'douyin_123.mp4', 'account': 'main_tiktok', 'caption': 'hello #test'})
    assert resp.status_code == 200
    body = resp.json()
    assert body['status'] in {'running', 'success'}
    assert body['result']['mode'] == 'n8n'
    assert sent['url'].endswith('/webhook/tiktok-upload')
    assert sent['json']['video_url'] == 'https://nwm.thienne.io.vn/upload-api/api/videos/douyin_123.mp4'
    assert sent['json']['callback_url'] == 'https://nwm.thienne.io.vn/api/tiktok/update-status'
    assert sent['json']['access_token'] == 'act.secret.access'
    assert 'act.secret.access' not in str(body)

    cb = client.post('/api/tiktok/update-status', json={'local_video_id': body['id'], 'status': 'success', 'tiktok_publish_id': 'publish_sandbox_1'})
    assert cb.status_code == 200
    assert client.get(f"/api/upload/jobs/{body['id']}").json()['status'] == 'success'
    deleted = client.delete(f"/api/upload/jobs/{body['id']}")
    assert deleted.status_code == 200
    assert deleted.json()['deleted'] is True
    assert client.get(f"/api/upload/jobs/{body['id']}").status_code == 404


def test_n8n_business_failure_marks_upload_job_failed_with_debug_error(monkeypatch, tmp_path):
    from tiktok_upload_service import main

    video_dir = tmp_path / 'douyin_video'
    video_dir.mkdir()
    (video_dir / 'douyin_403.mp4').write_bytes(b'video-data')
    main.settings.download_dir = tmp_path
    main.settings.data_dir = tmp_path / 'data'
    main.job_manager.reset()
    main.save_tiktok_oauth_token({'access_token': 'act.secret.access', 'refresh_token': 'rft.secret.refresh', 'expires_in': 86400, 'open_id': 'sandbox_open_id'})

    class FakeResponse:
        def raise_for_status(self):
            return None
        def json(self):
            return {'ok': False, 'status': 'failed', 'callback_ok': False, 'error': 'Request failed with status code 403'}

    monkeypatch.setattr(main.httpx, 'post', lambda *args, **kwargs: FakeResponse())
    body = TestClient(main.app).post('/api/upload/n8n/jobs', json={'filename': 'douyin_403.mp4', 'account': 'main_tiktok', 'caption': 'hello'}).json()

    assert body['status'] == 'failed'
    assert body['progress'] == 100
    assert body['error'] == 'Request failed with status code 403'
    assert body['message'] == 'n8n/TikTok publish failed'


def test_refreshes_expired_tiktok_access_token_before_n8n_publish(monkeypatch, tmp_path):
    from tiktok_upload_service import main

    video_dir = tmp_path / 'douyin_video'
    video_dir.mkdir()
    (video_dir / 'douyin_456.mp4').write_bytes(b'video-data')
    main.settings.download_dir = tmp_path
    main.settings.data_dir = tmp_path / 'data'
    main.job_manager.reset()
    monkeypatch.setenv('TIKTOK_CLIENT_KEY', 'client_key_test')
    monkeypatch.setenv('TIKTOK_CLIENT_SECRET', 'secret_test')
    monkeypatch.setenv('TIKTOK_N8N_WEBHOOK_URL', 'https://n8n.thienne.io.vn/webhook/tiktok-upload')
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://nwm.thienne.io.vn')
    main.save_tiktok_oauth_token({
        'access_token': 'expired.access',
        'refresh_token': 'rft.refresh',
        'access_token_expires_at': 1,
        'open_id': 'sandbox_open_id',
        'scope': 'video.publish,video.upload,user.info.basic',
    })

    calls = []
    class FakeResponse:
        def __init__(self, body):
            self._body = body
            self.text = '{}'
        def raise_for_status(self):
            return None
        def json(self):
            return self._body

    def fake_post(url, data=None, headers=None, json=None, timeout=None):
        calls.append((url, data, json))
        if 'oauth/token' in url:
            assert data['grant_type'] == 'refresh_token'
            return FakeResponse({'access_token': 'fresh.access', 'refresh_token': 'fresh.refresh', 'expires_in': 86400, 'open_id': 'sandbox_open_id'})
        assert json['access_token'] == 'fresh.access'
        return FakeResponse({'ok': True})

    monkeypatch.setattr(main.httpx, 'post', fake_post)
    client = TestClient(main.app)
    resp = client.post('/api/upload/n8n/jobs', json={'filename': 'douyin_456.mp4', 'account': 'main_tiktok', 'caption': 'hello #test'})
    assert resp.status_code == 200
    assert len(calls) == 2
    assert main.load_tiktok_oauth_token()['access_token'] == 'fresh.access'
