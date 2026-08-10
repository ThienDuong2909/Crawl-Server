import os

import pytest

from douyin_nwm_tool.cookie_manager import CookieManager, redact_cookie


def test_redact_cookie_keeps_names_hides_values():
    raw = "ttwid=fake-cookie-123; sessionid=fake-session-456; flagonly; msToken=fake-token-789"

    redacted = redact_cookie(raw)

    assert "ttwid=fak...123" in redacted
    assert "sessionid=fak...456" in redacted
    assert "msToken=fak...789" in redacted
    assert "fake-session-456" not in redacted
    assert "flagonly" not in redacted


def test_cookie_manager_loads_env_then_persists_and_updates_process_env(tmp_path, monkeypatch):
    cookie_file = tmp_path / "douyin_cookie.txt"
    monkeypatch.setenv("DOUYIN_COOKIE", "ttwid=from_env; sessionid=env_session")

    manager = CookieManager(cookie_file=cookie_file)

    assert manager.load_cookie() == "ttwid=from_env; sessionid=env_session"

    manager.update_cookie("ttwid=from_update; sessionid=updated_session", source="test")

    assert os.environ["DOUYIN_COOKIE"] == "ttwid=from_update; sessionid=updated_session"
    assert cookie_file.read_text() == "ttwid=from_update; sessionid=updated_session"
    assert manager.status()["source"] == "test"
    assert manager.status()["has_cookie"] is True
    assert "updated_session" not in manager.status()["redacted_cookie"]


def test_cookie_manager_rejects_invalid_cookie(tmp_path):
    manager = CookieManager(cookie_file=tmp_path / "douyin_cookie.txt")

    with pytest.raises(ValueError):
        manager.update_cookie("not-a-cookie-without-equals")


def test_cookie_manager_refreshes_when_file_changes(tmp_path, monkeypatch):
    cookie_file = tmp_path / "douyin_cookie.txt"
    cookie_file.write_text("ttwid=from_file; sessionid=file_session")
    monkeypatch.delenv("DOUYIN_COOKIE", raising=False)
    manager = CookieManager(cookie_file=cookie_file)

    assert manager.refresh_from_file() == "ttwid=from_file; sessionid=file_session"
    assert os.environ["DOUYIN_COOKIE"] == "ttwid=from_file; sessionid=file_session"
