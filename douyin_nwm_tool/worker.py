import argparse
import re
from pathlib import Path
from urllib.parse import unquote

import httpx


def _filename_from_content_disposition(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', value)
    if not match:
        return None
    return Path(unquote(match.group(1))).name


class WorkerClient:
    """Worker HTTP độc lập: tự gọi /api/download rồi lưu response về output_dir."""

    def __init__(self, api_base: str = "http://127.0.0.1:8000", output_dir: str | Path = "./worker_downloads"):
        self.api_base = api_base.rstrip("/")
        self.output_dir = Path(output_dir).resolve()

    async def download_once(self, url: str) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        endpoint = f"{self.api_base}/api/download"
        async with httpx.AsyncClient(follow_redirects=True) as client:
            async with client.stream("GET", endpoint, params={"url": url}, timeout=300) as response:
                response.raise_for_status()
                filename = _filename_from_content_disposition(response.headers.get("content-disposition"))
                if not filename:
                    filename = f"douyin_download_{len(list(self.output_dir.glob('*.mp4'))) + 1}.mp4"
                target = self.output_dir / filename
                tmp = target.with_suffix(target.suffix + ".tmp")
                with tmp.open("wb") as fh:
                    async for chunk in response.aiter_bytes():
                        if chunk:
                            fh.write(chunk)
                tmp.replace(target)
                return target


async def _amain():
    parser = argparse.ArgumentParser(description="Worker gọi /api/download và lưu video Douyin no-watermark")
    parser.add_argument("url", help="Douyin URL/share text")
    parser.add_argument("--api-base", default="http://127.0.0.1:8000")
    parser.add_argument("--output-dir", default="./worker_downloads")
    args = parser.parse_args()
    saved = await WorkerClient(args.api_base, args.output_dir).download_once(args.url)
    print(saved)


def main():
    import asyncio

    asyncio.run(_amain())


if __name__ == "__main__":
    main()
