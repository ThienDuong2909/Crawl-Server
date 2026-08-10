from fastapi.testclient import TestClient


def test_tiktok_account_secret_status_and_update_are_redacted(monkeypatch, tmp_path):
    from tiktok_upload_service import main

    main.settings.data_dir = tmp_path
    client = TestClient(main.app)

    empty = client.get('/api/account/tiktok/status')
    assert empty.status_code == 200
    assert empty.json()['has_session'] is False

    raw_session = 'sessionid=abc123; tt_chain_token=secret456; odin_tt=secret789'
    updated = client.post('/api/account/tiktok', json={'account_label': 'main_shop', 'session_cookie': raw_session})
    assert updated.status_code == 200
    body = updated.json()
    assert body['has_session'] is True
    assert body['account_label'] == 'main_shop'
    assert '[REDACTED]' in body['redacted_session']
    assert 'abc123' not in str(body)
    assert 'secret456' not in str(body)

    status = client.get('/api/account/tiktok/status')
    assert status.status_code == 200
    status_body = status.json()
    assert status_body['has_session'] is True
    assert status_body['account_label'] == 'main_shop'
    assert 'abc123' not in str(status_body)
    assert (tmp_path / 'tiktok_session.json').exists()


def test_tiktok_account_secret_rejects_password_payload(monkeypatch, tmp_path):
    from tiktok_upload_service import main

    main.settings.data_dir = tmp_path
    client = TestClient(main.app)
    resp = client.post('/api/account/tiktok', json={'account_label': 'main_shop', 'username': 'user', 'password': 'pass'})
    assert resp.status_code == 422
