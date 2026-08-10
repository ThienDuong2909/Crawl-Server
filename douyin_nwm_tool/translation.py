import os
import re
from pathlib import Path
from typing import Any

import httpx


class AgentRouterTitleTranslator:
    """Translate Douyin titles to natural Vietnamese without exposing credentials."""

    def __init__(
        self,
        api_key_file: str | Path | None = None,
        base_url: str | None = None,
        model: str | None = None,
        client: Any | None = None,
    ):
        self.api_key_file = Path(api_key_file or os.getenv("AGENTROUTER_API_KEY_FILE", "/app/secrets/agentrouter_api_key.txt"))
        self.base_url = (base_url or os.getenv("AGENTROUTER_BASE_URL", "https://agentrouter.org/v1")).rstrip("/")
        self.model = model or os.getenv("AGENTROUTER_MODEL", "claude-opus-5")
        self.client = client
        self.client_user_agent = os.getenv("AGENTROUTER_CLIENT_USER_AGENT", "Kilo-Code/4.110.0")

    def _read_api_key(self) -> str:
        try:
            key = self.api_key_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError("Translation API credential is unavailable") from exc
        if not key:
            raise RuntimeError("Translation API credential is empty")
        return key

    async def translate_title(self, source_title: str) -> str:
        full_title = str(source_title or "").strip()
        title_without_hashtags = re.split(r"\s*#", full_title, maxsplit=1)[0].strip()
        if not title_without_hashtags:
            return "Video mới"
        prompt = (
            "Hãy dịch tiêu đề sau sang tiếng Việt tự nhiên và đúng ngữ cảnh. "
            "Hashtag đã được loại bỏ; không dịch và không đưa hashtag vào kết quả. "
            "Chỉ trả về bản dịch, không giải thích:\n"
            f"{title_without_hashtags}"
        )
        payload = {
            "model": self.model,
            "max_tokens": 512,
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {
            "x-api-key": self._read_api_key(),
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "User-Agent": self.client_user_agent,
        }
        endpoint = f"{self.base_url}/messages"

        async def request_with(client: Any) -> str:
            for attempt in range(2):
                response = await client.post(endpoint, headers=headers, json=payload, timeout=120)
                try:
                    response.raise_for_status()
                except Exception as exc:
                    status = getattr(response, "status_code", "unknown")
                    raise RuntimeError(f"Translation API request failed (HTTP {status})") from exc
                try:
                    blocks = response.json()["content"]
                    content = "".join(
                        str(block.get("text") or "")
                        for block in blocks
                        if isinstance(block, dict) and block.get("type") == "text"
                    )
                except (KeyError, IndexError, TypeError, ValueError) as exc:
                    if attempt == 0:
                        continue
                    raise RuntimeError("Translation API returned an invalid response") from exc
                translated = str(content or "").strip()
                translated = re.sub(r"^```(?:text|markdown)?\s*|\s*```$", "", translated, flags=re.IGNORECASE).strip()
                if len(translated) >= 2 and translated[0] == translated[-1] and translated[0] in {'"', "'", "“", "”"}:
                    translated = translated[1:-1].strip()
                translated = re.split(r"\s*#", translated, maxsplit=1)[0].strip()
                if translated:
                    return translated[:1800]
            raise RuntimeError("Translation API returned an empty title after retry")

        if self.client is not None:
            return await request_with(self.client)
        async with httpx.AsyncClient(timeout=120) as client:
            return await request_with(client)
