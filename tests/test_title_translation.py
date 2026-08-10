import pytest


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class RecordingClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


@pytest.mark.asyncio
async def test_agentrouter_translates_chinese_title_contextually_with_exact_model(tmp_path):
    from douyin_nwm_tool.translation import AgentRouterTitleTranslator

    key_file = tmp_path / "agentrouter.key"
    key_file.write_text("secret-test-key\n", encoding="utf-8")
    client = RecordingClient(FakeResponse({
        "content": [
            {"type": "thinking", "thinking": "internal"},
            {"type": "text", "text": "  Rời xa anh, bầu trời bỗng trong xanh #nhạctâmtrạng  "},
        ]
    }))
    translator = AgentRouterTitleTranslator(
        api_key_file=key_file,
        base_url="https://agentrouter.org/v1",
        model="claude-opus-5",
        client=client,
    )

    translated = await translator.translate_title("离开你天空都放晴了 #对口型 #最佳男主角")

    assert translated == "Rời xa anh, bầu trời bỗng trong xanh"
    url, request = client.calls[0]
    assert url == "https://agentrouter.org/v1/messages"
    assert request["headers"]["x-api-key"] == "secret-test-key"
    assert request["headers"]["anthropic-version"] == "2023-06-01"
    assert request["headers"]["User-Agent"].startswith("Kilo-Code/")
    assert request["json"]["model"] == "claude-opus-5"
    user_prompt = request["json"]["messages"][0]["content"]
    assert "đúng ngữ cảnh" in user_prompt
    assert "tự nhiên" in user_prompt
    assert "chỉ trả về" in user_prompt.lower()
    assert "#对口型" not in user_prompt
    assert "#最佳男主角" not in user_prompt
    assert "离开你天空都放晴了" in user_prompt
    assert "không dịch" in user_prompt.lower()
    assert "không đưa" in user_prompt.lower()


@pytest.mark.asyncio
async def test_agentrouter_retries_once_when_first_success_response_has_no_text(tmp_path):
    from douyin_nwm_tool.translation import AgentRouterTitleTranslator

    class SequenceClient:
        def __init__(self):
            self.calls = []
            self.responses = [
                FakeResponse({"content": [{"type": "thinking", "thinking": ""}], "stop_reason": "end_turn"}),
                FakeResponse({"content": [{"type": "text", "text": "Em gái ơi, rốt cuộc có yêu anh không!!!"}]}),
            ]

        async def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return self.responses.pop(0)

    key_file = tmp_path / "agentrouter.key"
    key_file.write_text("secret-test-key\n", encoding="utf-8")
    client = SequenceClient()
    translator = AgentRouterTitleTranslator(api_key_file=key_file, client=client)

    translated = await translator.translate_title("老妹儿 到底处不处！！！")

    assert translated == "Em gái ơi, rốt cuộc có yêu anh không!!!"
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_tiktok_auto_uploader_uses_vietnamese_translation_as_caption():
    from douyin_nwm_tool.autocrawl import TikTokAutoUploader

    class Translator:
        async def translate_title(self, title):
            assert title == "往事流转～ 在你眼眸"
            return "Chuyện cũ trôi qua trong ánh mắt em"

    client = RecordingClient(FakeResponse({"id": "upload-1", "status": "running"}))
    uploader = TikTokAutoUploader(
        endpoint="http://tiktok-uploader:8001/api/upload/n8n/jobs",
        translator=Translator(),
        client=client,
    )

    result = await uploader.upload({"title": "往事流转～ 在你眼眸", "local_path": "/shared/download/douyin_video/douyin_1.mp4"})

    assert result["id"] == "upload-1"
    payload = client.calls[0][1]["json"]
    assert payload["caption"] == "Chuyện cũ trôi qua trong ánh mắt em"
    assert "#xh" not in payload["caption"]
    assert "往事" not in payload["caption"]


@pytest.mark.asyncio
async def test_tiktok_auto_uploader_routes_photo_album_to_photo_inbox_endpoint():
    from douyin_nwm_tool.autocrawl import TikTokAutoUploader

    client = RecordingClient(FakeResponse({"id": "photo-job-1", "status": "awaiting_user_review"}))
    uploader = TikTokAutoUploader(
        endpoint="http://tiktok-uploader:8001/api/upload/jobs",
        photo_endpoint="http://tiktok-uploader:8001/api/upload/photo-jobs",
        client=client,
    )

    result = await uploader.upload_translated(
        {"video_id": "photo-68", "media_type": "photo", "local_path": "/shared/download/douyin_photo/douyin_photo-68/manifest.json"},
        "Bộ ảnh mới",
    )

    assert result["id"] == "photo-job-1"
    url, request = client.calls[0]
    assert url == "http://tiktok-uploader:8001/api/upload/photo-jobs"
    assert request["json"] == {"photo_id": "photo-68", "account": "main_tiktok", "caption": "Bộ ảnh mới"}


@pytest.mark.asyncio
async def test_translation_failure_stops_auto_publish_instead_of_using_chinese_title():
    from douyin_nwm_tool.autocrawl import TikTokAutoUploader

    class BrokenTranslator:
        async def translate_title(self, title):
            raise RuntimeError("Translation service unavailable")

    client = RecordingClient(FakeResponse({"id": "should-not-run"}))
    uploader = TikTokAutoUploader(
        endpoint="http://tiktok-uploader:8001/api/upload/n8n/jobs",
        translator=BrokenTranslator(),
        client=client,
    )

    with pytest.raises(RuntimeError, match="Translation service unavailable"):
        await uploader.upload({"title": "中文标题", "local_path": "/shared/download/douyin_video/douyin_1.mp4"})
    assert client.calls == []
