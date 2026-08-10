from pathlib import Path

import httpx
import pytest

from douyin_nwm_tool.worker import WorkerClient


class FakeResponse:
    status_code = 200
    headers = {"content-disposition": 'attachment; filename="douyin_123.mp4"'}

    def raise_for_status(self):
        return None

    async def aiter_bytes(self):
        yield b"video"


class FakeStreamContext:
    def __init__(self, outer):
        self.outer = outer

    async def __aenter__(self):
        return FakeResponse()

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeHttpClient:
    def __init__(self):
        self.request = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, method, url, params=None, timeout=None):
        self.request = (method, url, params, timeout)
        return FakeStreamContext(self)


@pytest.mark.asyncio
async def test_worker_calls_api_download_and_saves_response(monkeypatch, tmp_path: Path):
    created = []

    class ClientFactory:
        def __call__(self, *args, **kwargs):
            client = FakeHttpClient()
            created.append(client)
            return client

    monkeypatch.setattr(httpx, "AsyncClient", ClientFactory())
    worker = WorkerClient(api_base="http://api.local", output_dir=tmp_path)

    saved = await worker.download_once("https://www.douyin.com/video/123")

    assert created[0].request == (
        "GET",
        "http://api.local/api/download",
        {"url": "https://www.douyin.com/video/123"},
        300,
    )
    assert saved == tmp_path / "douyin_123.mp4"
    assert saved.read_bytes() == b"video"
