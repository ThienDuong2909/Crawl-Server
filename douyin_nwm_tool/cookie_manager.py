import os
import time
from pathlib import Path
from typing import Any


DEFAULT_COOKIE_FILE = Path(os.getenv("DOUYIN_COOKIE_FILE", "/home/douyin_nwm_tool/secrets/douyin_cookie.txt"))


def redact_cookie(cookie: str | None) -> str:
    if not cookie:
        return ""
    parts: list[str] = []
    for raw_part in cookie.split(";"):
        part = raw_part.strip()
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        if not name:
            continue
        if len(value) <= 8:
            safe = "***"
        else:
            safe = f"{value[:3]}...{value[-3:]}"
        parts.append(f"{name}={safe}")
    return "; ".join(parts)


def validate_cookie(cookie: str) -> str:
    cleaned = (cookie or "").strip()
    if not cleaned:
        raise ValueError("Cookie rỗng")
    pairs = [p.strip() for p in cleaned.split(";") if p.strip()]
    valid_pairs = [p for p in pairs if "=" in p and p.split("=", 1)[0].strip()]
    if len(valid_pairs) < 2:
        raise ValueError("Cookie phải là raw Cookie header dạng 'key=value; key2=value2', không phải một chuỗi đơn lẻ")
    return "; ".join(valid_pairs)


class CookieManager:
    """Quản lý Douyin Cookie runtime + persisted file.

    Thứ tự load:
    1. DOUYIN_COOKIE trong environment hiện tại
    2. DOUYIN_COOKIE_FILE hoặc /home/douyin_nwm_tool/secrets/douyin_cookie.txt

    update_cookie() cập nhật os.environ để process hiện tại dùng ngay và ghi file
    để lần restart sau tự load lại.
    """

    def __init__(self, cookie_file: str | Path | None = None):
        self.cookie_file = Path(cookie_file or DEFAULT_COOKIE_FILE)
        self.source = "none"
        self.last_updated_at: float | None = None

    def load_cookie(self) -> str:
        env_cookie = os.getenv("DOUYIN_COOKIE", "").strip()
        if env_cookie:
            self.source = "env"
            return validate_cookie(env_cookie)
        if self.cookie_file.exists():
            cookie = self.cookie_file.read_text(encoding="utf-8").strip()
            if cookie:
                self.source = "file"
                os.environ["DOUYIN_COOKIE"] = validate_cookie(cookie)
                return os.environ["DOUYIN_COOKIE"]
        return ""

    def refresh_from_file(self) -> str:
        if not self.cookie_file.exists():
            return ""
        cookie = self.cookie_file.read_text(encoding="utf-8").strip()
        if not cookie:
            return ""
        cookie = validate_cookie(cookie)
        os.environ["DOUYIN_COOKIE"] = cookie
        self.source = "file"
        self.last_updated_at = time.time()
        return cookie

    def update_cookie(self, cookie: str, source: str = "api") -> str:
        cookie = validate_cookie(cookie)
        self.cookie_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cookie_file.with_suffix(self.cookie_file.suffix + ".tmp")
        tmp.write_text(cookie, encoding="utf-8")
        tmp.replace(self.cookie_file)
        os.environ["DOUYIN_COOKIE"] = cookie
        self.source = source
        self.last_updated_at = time.time()
        return cookie

    def apply_to_vendor_crawler(self) -> bool:
        cookie = self.load_cookie()
        if not cookie:
            return False
        from crawlers.douyin.web import web_crawler as web_crawler_module
        web_crawler_module.config["TokenManager"]["douyin"]["headers"]["Cookie"] = cookie
        return True

    def status(self) -> dict[str, Any]:
        cookie = os.getenv("DOUYIN_COOKIE", "").strip()
        if not cookie and self.cookie_file.exists():
            cookie = self.cookie_file.read_text(encoding="utf-8").strip()
        return {
            "has_cookie": bool(cookie),
            "source": self.source,
            "cookie_file": str(self.cookie_file),
            "last_updated_at": self.last_updated_at,
            "redacted_cookie": redact_cookie(cookie),
        }
