from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import re
import secrets
import threading
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode

import httpx
from Cryptodome.Cipher import AES
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.responses import FileResponse


class Settings:
    def __init__(self):
        self.download_dir = Path(os.getenv("DOWNLOAD_DIR", "/shared/download")).resolve()
        self.data_dir = Path(os.getenv("TIKTOK_UPLOAD_DATA_DIR", "/app/data")).resolve()
        self.secrets_dir = Path(os.getenv("SECRETS_DIR", "/app/secrets")).resolve()
        self.upload_mode = os.getenv("TIKTOK_UPLOAD_MODE", "dry_run").strip() or "dry_run"

    @property
    def public_base_url(self) -> str:
        return os.getenv("PUBLIC_BASE_URL", "https://nwm.thienne.io.vn").rstrip("/")

    @property
    def n8n_webhook_url(self) -> str:
        return os.getenv("TIKTOK_N8N_WEBHOOK_URL", "https://n8n.thienne.io.vn/webhook/tiktok-upload")

    @property
    def tiktok_redirect_uri(self) -> str:
        return os.getenv("TIKTOK_REDIRECT_URI", f"{self.public_base_url}/api/tiktok/callback")

    @property
    def tiktok_oauth_config_file(self) -> Path:
        return Path(os.getenv("TIKTOK_OAUTH_CONFIG_FILE", str(self.secrets_dir / "tiktok_dev_oauth.json"))).resolve()


settings = Settings()
app = FastAPI(title="TikTok Upload Service", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {}


def _write_secret_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def load_tiktok_oauth_config() -> dict[str, str]:
    file_payload = _read_json_file(settings.tiktok_oauth_config_file)
    client_key = os.getenv("TIKTOK_CLIENT_KEY") or str(file_payload.get("client_key") or "")
    client_secret = os.getenv("TIKTOK_CLIENT_SECRET") or str(file_payload.get("client_secret") or "")
    return {"client_key": client_key.strip(), "client_secret": client_secret.strip()}


DEFAULT_TIKTOK_ACCOUNT_ID = "main_tiktok"
_oauth_state_lock = threading.Lock()


def tiktok_oauth_token_path(account_id: str = DEFAULT_TIKTOK_ACCOUNT_ID) -> Path:
    safe_id = validate_account_id(account_id)
    if safe_id == DEFAULT_TIKTOK_ACCOUNT_ID:
        # Preserve the production token path for backwards compatibility.
        return settings.data_dir / "tiktok_oauth_token.json"
    return settings.data_dir / "tiktok_accounts" / f"{safe_id}.token.json"


def tiktok_accounts_registry_path() -> Path:
    return settings.data_dir / "tiktok_accounts.json"


def tiktok_oauth_states_path() -> Path:
    return settings.data_dir / "tiktok_oauth_states.json"


def validate_account_id(account_id: str) -> str:
    value = str(account_id or DEFAULT_TIKTOK_ACCOUNT_ID).strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value):
        raise HTTPException(status_code=400, detail="Invalid TikTok account ID")
    return value


def _decode_token_key(value: str, name: str) -> bytes:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:
        raise RuntimeError(f"{name} must be URL-safe base64") from exc
    if len(raw) != 32:
        raise RuntimeError(f"{name} must decode to 32 bytes")
    return raw


def _token_encryption_key() -> bytes:
    env_value = os.getenv("TIKTOK_TOKEN_ENCRYPTION_KEY", "").strip()
    if env_value:
        return _decode_token_key(env_value, "TIKTOK_TOKEN_ENCRYPTION_KEY")
    key_path = settings.data_dir / ".tiktok_token_encryption.key"
    try:
        if key_path.exists():
            raw = base64.urlsafe_b64decode(key_path.read_text(encoding="utf-8").strip())
            if len(raw) != 32:
                raise RuntimeError("TikTok token encryption key has invalid length")
            return raw
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        raw = secrets.token_bytes(32)
        key_path.write_text(base64.urlsafe_b64encode(raw).decode("ascii"), encoding="utf-8")
        key_path.chmod(0o600)
        return raw
    except OSError as exc:
        raise RuntimeError("Cannot access TikTok token encryption key; refusing plaintext token storage") from exc


def _token_decryption_keys() -> list[bytes]:
    keys = [_token_encryption_key()]
    previous = os.getenv("TIKTOK_TOKEN_ENCRYPTION_PREVIOUS_KEYS", "").strip()
    for index, value in enumerate(part.strip() for part in previous.split(",") if part.strip()):
        candidate = _decode_token_key(value, f"TIKTOK_TOKEN_ENCRYPTION_PREVIOUS_KEYS[{index}]")
        if candidate not in keys:
            keys.append(candidate)
    return keys


def _write_encrypted_json(path: Path, payload: dict[str, Any]) -> None:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    nonce = secrets.token_bytes(12)
    cipher = AES.new(_token_encryption_key(), AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(raw)
    envelope = {
        "version": 1,
        "algorithm": "AES-256-GCM",
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        "tag": base64.b64encode(tag).decode("ascii"),
    }
    _write_secret_json(path, envelope)


def _read_encrypted_json(path: Path) -> dict[str, Any]:
    envelope = _read_json_file(path)
    if not envelope:
        return {}
    if envelope.get("version") != 1:
        # One-time migration of the existing single-account plaintext token.
        _write_encrypted_json(path, envelope)
        return envelope
    failures: list[Exception] = []
    for index, key in enumerate(_token_decryption_keys()):
        try:
            cipher = AES.new(key, AES.MODE_GCM, nonce=base64.b64decode(envelope["nonce"]))
            raw = cipher.decrypt_and_verify(base64.b64decode(envelope["ciphertext"]), base64.b64decode(envelope["tag"]))
            decoded = json.loads(raw.decode("utf-8"))
            if not isinstance(decoded, dict):
                return {}
            if index > 0:
                _write_encrypted_json(path, decoded)
            return decoded
        except Exception as exc:
            failures.append(exc)
    raise RuntimeError("TikTok OAuth token could not be decrypted with current or previous keys; refusing to continue") from failures[-1]


def _default_account_record() -> dict[str, Any]:
    return {
        "id": DEFAULT_TIKTOK_ACCOUNT_ID,
        "display_name": "TikTok hiện tại",
        "notification_email": "",
        "is_default": True,
        "created_at": None,
        "updated_at": None,
    }


def load_tiktok_accounts_registry() -> dict[str, Any]:
    payload = _read_json_file(tiktok_accounts_registry_path())
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    by_id = {str(item.get("id")): dict(item) for item in items if isinstance(item, dict) and item.get("id")}
    current = by_id.get(DEFAULT_TIKTOK_ACCOUNT_ID, {})
    by_id[DEFAULT_TIKTOK_ACCOUNT_ID] = {**_default_account_record(), **current, "id": DEFAULT_TIKTOK_ACCOUNT_ID, "is_default": True}
    return {"default_account_id": DEFAULT_TIKTOK_ACCOUNT_ID, "items": list(by_id.values())}


def save_tiktok_accounts_registry(payload: dict[str, Any]) -> None:
    _write_secret_json(tiktok_accounts_registry_path(), payload)


def create_tiktok_account(display_name: str, notification_email: str = "") -> dict[str, Any]:
    label = str(display_name or "").strip()
    email = str(notification_email or "").strip().lower()
    if not label:
        raise HTTPException(status_code=400, detail="TikTok account display name is required")
    registry = load_tiktok_accounts_registry()
    base = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "tiktok"
    account_id = f"{base[:48]}-{secrets.token_hex(3)}"
    now = time.time()
    item = {
        "id": account_id,
        "display_name": label[:128],
        "notification_email": email,
        "is_default": False,
        "created_at": now,
        "updated_at": now,
    }
    registry["items"].append(item)
    save_tiktok_accounts_registry(registry)
    return account_public_status(item)


def account_registry_item(account_id: str) -> dict[str, Any]:
    safe_id = validate_account_id(account_id)
    for item in load_tiktok_accounts_registry()["items"]:
        if item.get("id") == safe_id:
            return item
    raise HTTPException(status_code=404, detail="TikTok account not found")


def account_public_status(item: dict[str, Any]) -> dict[str, Any]:
    account_id = str(item["id"])
    token = load_tiktok_oauth_token(account_id)
    return {
        "id": account_id,
        "display_name": str(item.get("display_name") or account_id),
        "notification_email": str(item.get("notification_email") or ""),
        "is_default": account_id == DEFAULT_TIKTOK_ACCOUNT_ID,
        "has_access_token": bool(token.get("access_token")),
        "open_id": str(token.get("open_id") or ""),
        "scope": str(token.get("scope") or ""),
        "expires_at": token.get("access_token_expires_at"),
        "refresh_expires_at": token.get("refresh_token_expires_at"),
        "updated_at": token.get("updated_at") or item.get("updated_at"),
    }


def list_tiktok_accounts() -> dict[str, Any]:
    registry = load_tiktok_accounts_registry()
    return {"default_account_id": DEFAULT_TIKTOK_ACCOUNT_ID, "items": [account_public_status(item) for item in registry["items"]]}


def update_tiktok_account_notification(account_id: str, notification_email: str) -> dict[str, Any]:
    safe_id = validate_account_id(account_id)
    registry = load_tiktok_accounts_registry()
    for item in registry["items"]:
        if item.get("id") == safe_id:
            item["notification_email"] = str(notification_email or "").strip().lower()
            item["updated_at"] = time.time()
            save_tiktok_accounts_registry(registry)
            return account_public_status(item)
    raise HTTPException(status_code=404, detail="TikTok account not found")


def delete_tiktok_account(account_id: str) -> None:
    safe_id = validate_account_id(account_id)
    if safe_id == DEFAULT_TIKTOK_ACCOUNT_ID:
        raise HTTPException(status_code=400, detail="Default TikTok account cannot be deleted")
    registry = load_tiktok_accounts_registry()
    original = len(registry["items"])
    registry["items"] = [item for item in registry["items"] if item.get("id") != safe_id]
    if len(registry["items"]) == original:
        raise HTTPException(status_code=404, detail="TikTok account not found")
    save_tiktok_accounts_registry(registry)
    try:
        tiktok_oauth_token_path(safe_id).unlink(missing_ok=True)
        with _oauth_state_lock:
            states = _read_encrypted_json(tiktok_oauth_states_path())
            states = {key: value for key, value in states.items() if str((value or {}).get("account_id")) != safe_id}
            _write_encrypted_json(tiktok_oauth_states_path(), states)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Could not remove TikTok account token") from exc


def redact_token(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "[REDACTED]"
    return f"{value[:4]}...{value[-4:]}"


def save_tiktok_oauth_token(payload: dict[str, Any], account_id: str = DEFAULT_TIKTOK_ACCOUNT_ID) -> dict[str, Any]:
    safe_id = validate_account_id(account_id)
    if safe_id != DEFAULT_TIKTOK_ACCOUNT_ID:
        account_registry_item(safe_id)
    now = time.time()
    token_payload = dict(payload)
    token_payload["updated_at"] = now
    if "expires_in" in token_payload and "access_token_expires_at" not in token_payload:
        token_payload["access_token_expires_at"] = now + int(token_payload.get("expires_in") or 0)
    if "refresh_expires_in" in token_payload and "refresh_token_expires_at" not in token_payload:
        token_payload["refresh_token_expires_at"] = now + int(token_payload.get("refresh_expires_in") or 0)
    _write_encrypted_json(tiktok_oauth_token_path(safe_id), token_payload)
    return load_tiktok_oauth_status(safe_id)


def load_tiktok_oauth_token(account_id: str = DEFAULT_TIKTOK_ACCOUNT_ID) -> dict[str, Any]:
    return _read_encrypted_json(tiktok_oauth_token_path(account_id))


def load_tiktok_oauth_status(account_id: str = DEFAULT_TIKTOK_ACCOUNT_ID) -> dict[str, Any]:
    safe_id = validate_account_id(account_id)
    config = load_tiktok_oauth_config()
    token = load_tiktok_oauth_token(safe_id)
    return {
        "account_id": safe_id,
        "has_client_config": bool(config["client_key"] and config["client_secret"]),
        "has_access_token": bool(token.get("access_token")),
        "open_id": token.get("open_id", ""),
        "scope": token.get("scope", ""),
        "redacted_access_token": redact_token(str(token.get("access_token", ""))),
        "expires_at": token.get("access_token_expires_at"),
        "updated_at": token.get("updated_at"),
        "redirect_uri": settings.tiktok_redirect_uri,
        "n8n_webhook_configured": bool(settings.n8n_webhook_url),
    }


class TikTokOAuthAccountCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=128)
    notification_email: str = Field(default="", max_length=254)

    @field_validator("notification_email")
    @classmethod
    def validate_notification_email(cls, value: str) -> str:
        email = str(value or "").strip().lower()
        if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise ValueError("notification_email must be a valid email address")
        return email


class TikTokAccountNotificationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notification_email: str = Field(max_length=254)

    @field_validator("notification_email")
    @classmethod
    def validate_notification_email(cls, value: str) -> str:
        email = str(value or "").strip().lower()
        if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise ValueError("notification_email must be a valid email address")
        return email


class TikTokAccountUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_label: str = Field(min_length=1, max_length=128)
    session_cookie: str = Field(min_length=10, max_length=20000)


def tiktok_session_path() -> Path:
    return settings.data_dir / "tiktok_session.json"


def redact_session_cookie(value: str) -> str:
    parts = [part.strip() for part in value.split(";") if part.strip()]
    redacted = []
    for part in parts[:8]:
        key = part.split("=", 1)[0].strip() or "cookie"
        redacted.append(f"{key}=[REDACTED]")
    if len(parts) > 8:
        redacted.append(f"...+{len(parts)-8} more")
    return "; ".join(redacted) if redacted else "[REDACTED]"


def load_tiktok_account_status() -> dict[str, Any]:
    path = tiktok_session_path()
    if not path.exists():
        return {"has_session": False, "account_label": "", "redacted_session": "", "updated_at": None}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"has_session": False, "account_label": "", "redacted_session": "", "updated_at": None}
    session_cookie = str(payload.get("session_cookie", ""))
    return {
        "has_session": bool(session_cookie),
        "account_label": str(payload.get("account_label", "")),
        "redacted_session": redact_session_cookie(session_cookie) if session_cookie else "",
        "updated_at": payload.get("updated_at"),
    }


def save_tiktok_account(payload: TikTokAccountUpdate) -> dict[str, Any]:
    if "=" not in payload.session_cookie or ";" not in payload.session_cookie:
        raise HTTPException(status_code=400, detail="TikTok session must be a raw Cookie header like key=value; key2=value2")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    path = tiktok_session_path()
    path.write_text(json.dumps({
        "account_label": payload.account_label,
        "session_cookie": payload.session_cookie,
        "updated_at": time.time(),
    }, ensure_ascii=False), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return load_tiktok_account_status()


class UploadOptions(BaseModel):
    allow_comment: Literal[0, 1] = 1
    allow_duet: Literal[0, 1] = 0
    allow_stitch: Literal[0, 1] = 0
    visibility_type: Literal[0, 1] = 0
    brand_organic_type: Literal[0, 1] = 0
    branded_content_type: Literal[0, 1] = 0
    ai_label: Literal[0, 1] = 0
    proxy: str = ""


class UploadJobRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    account: str = Field(min_length=1, max_length=128)
    caption: str = Field(min_length=1, max_length=2200)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=256, pattern=r"^[A-Za-z0-9:_-]+$")
    options: UploadOptions = Field(default_factory=UploadOptions)


class PhotoUploadJobRequest(BaseModel):
    photo_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    account: str = Field(min_length=1, max_length=128)
    caption: str = Field(default="", max_length=4000)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=256, pattern=r"^[A-Za-z0-9:_-]+$")


class N8nStatusCallback(BaseModel):
    local_video_id: str = Field(min_length=1, max_length=128)
    status: Literal["success", "failed", "submitted", "uploading"] = "success"
    tiktok_publish_id: str = ""
    tiktok_video_id: str = ""
    error: str = ""
    raw: dict[str, Any] | None = None


@dataclass
class UploadJob:
    id: str
    filename: str
    account: str
    caption: str
    options: dict[str, Any]
    idempotency_key: str | None = None
    status: Literal["queued", "running", "success", "failed"] = "queued"
    progress: int = 0
    message: str = "Queued"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    result: dict[str, Any] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "filename": self.filename,
            "account": self.account,
            "caption": self.caption,
            "options": self.options,
            "idempotency_key": self.idempotency_key,
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "result": self.result,
            "error": self.error,
        }


class TikTokUploadAdapter:
    @staticmethod
    def _read_secret(name: str) -> str:
        env_name = name.removesuffix(".txt").upper().replace(".", "_")
        value = os.getenv(env_name, "").strip()
        if value:
            return value
        path = settings.secrets_dir / name.lower()
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def upload(self, *, video_path: Path, account: str, caption: str, options: dict[str, Any]) -> dict[str, Any]:
        mode = os.getenv("TIKTOK_UPLOAD_MODE", settings.upload_mode).strip() or "dry_run"
        if mode == "dry_run":
            return {
                "mode": "dry_run",
                "ok": True,
                "account": account,
                "caption": caption,
                "video_path": str(video_path),
                "size_bytes": video_path.stat().st_size,
                "options": options,
                "note": "Dry-run only: no request was sent to TikTok. Switch TIKTOK_UPLOAD_MODE after configuring a real uploader adapter.",
            }
        if mode == "inbox":
            token = refresh_tiktok_access_token_if_needed(account)
            access_token = str(token.get("access_token") or "")
            if not access_token:
                raise RuntimeError("TikTok OAuth token unavailable; connect TikTok first")
            video_size = video_path.stat().st_size
            if video_size <= 0:
                raise RuntimeError("Video file is empty")
            if video_size <= 64_000_000:
                chunk_size = video_size
                total_chunk_count = 1
            else:
                chunk_size = 10_000_000
                total_chunk_count = video_size // chunk_size
            init_response = httpx.post(
                "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json; charset=UTF-8",
                },
                json={
                    "source_info": {
                        "source": "FILE_UPLOAD",
                        "video_size": video_size,
                        "chunk_size": chunk_size,
                        "total_chunk_count": total_chunk_count,
                    }
                },
                timeout=60,
            )
            try:
                init_payload = init_response.json()
            except ValueError:
                init_payload = {}
            init_error = init_payload.get("error") or {}
            init_code = str(init_error.get("code") or "")
            init_message = str(init_error.get("message") or "")
            init_log_id = str(init_error.get("log_id") or "")
            if init_response.status_code >= 400 or init_code not in {"", "ok"}:
                if init_code == "spam_risk_too_many_pending_share":
                    explanation = "TikTok đã đạt giới hạn upload API trong ngày; cần xử lý/đăng các bản nháp đang chờ và đợi TikTok mở lại hạn mức"
                else:
                    explanation = init_message or f"HTTP {init_response.status_code}"
                details = f"TikTok Inbox init failed: {init_code or 'http_error'}: {explanation}"
                if init_log_id:
                    details += f" (log_id={init_log_id})"
                raise RuntimeError(details)
            init_data = init_payload.get("data") or {}
            publish_id = str(init_data.get("publish_id") or "")
            upload_url = str(init_data.get("upload_url") or "")
            if not publish_id or not upload_url:
                raise RuntimeError("TikTok Inbox init did not return publish_id and upload_url")
            with video_path.open("rb") as media:
                offset = 0
                for chunk_index in range(total_chunk_count):
                    if chunk_index == total_chunk_count - 1:
                        current_size = video_size - offset
                    else:
                        current_size = chunk_size
                    chunk = media.read(current_size)
                    if len(chunk) != current_size:
                        raise RuntimeError("Could not read the complete video chunk")
                    last_byte = offset + current_size - 1
                    upload_response = httpx.put(
                        upload_url,
                        headers={
                            "Content-Type": "video/mp4",
                            "Content-Length": str(current_size),
                            "Content-Range": f"bytes {offset}-{last_byte}/{video_size}",
                        },
                        content=chunk,
                        timeout=180,
                    )
                    upload_response.raise_for_status()
                    offset = last_byte + 1
            return {
                "mode": "inbox",
                "ok": True,
                "workflow_status": "awaiting_user_review",
                "publish_id": publish_id,
                "log_id": str(init_error.get("log_id") or ""),
                "uploaded_bytes": video_size,
                "chunk_count": total_chunk_count,
                "message": "Video đã được gửi vào TikTok Inbox; người dùng phải mở thông báo trong TikTok để chỉnh sửa và hoàn tất đăng.",
            }
        if mode == "ayrshare":
            api_key = self._read_secret("ayrshare_api_key.txt")
            if not api_key:
                raise RuntimeError("AYRSHARE API key is missing; create secrets/ayrshare_api_key.txt")
            profile_key = self._read_secret("ayrshare_profile_key.txt")
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            if profile_key:
                headers["Profile-Key"] = profile_key
            video_url = f"{settings.public_base_url}/upload-api/api/videos/{video_path.name}"
            response = httpx.post(
                "https://app.ayrshare.com/api/post",
                headers=headers,
                json={
                    "post": caption,
                    "platforms": ["tiktok"],
                    "mediaUrls": [video_url],
                },
                timeout=120,
            )
            response.raise_for_status()
            provider = response.json()
            provider_status = str(provider.get("status") or "")
            if provider_status.lower() in {"error", "failed"}:
                raise RuntimeError(str(provider.get("message") or provider.get("error") or "Ayrshare publish failed"))
            return {
                "mode": "ayrshare",
                "ok": True,
                "provider_status": provider_status or "submitted",
                "provider_post_id": str(provider.get("id") or ""),
                "platform_post_ids": provider.get("postIds") or [],
                "video_url": video_url,
                "size_bytes": video_path.stat().st_size,
            }
        raise RuntimeError(f"Unsupported TIKTOK_UPLOAD_MODE={mode!r}; use dry_run or ayrshare")


class TikTokUploadGate:
    def __init__(
        self,
        min_interval_seconds: int | None = None,
        max_interval_seconds: int | None = None,
        *,
        clock=time.monotonic,
        sleeper=time.sleep,
        choose_interval=random.uniform,
    ):
        configured_min = int(os.getenv("TIKTOK_UPLOAD_MIN_INTERVAL_SECONDS", "60")) if min_interval_seconds is None else int(min_interval_seconds)
        configured_max = int(os.getenv("TIKTOK_UPLOAD_MAX_INTERVAL_SECONDS", "120")) if max_interval_seconds is None else int(max_interval_seconds)
        self.min_interval_seconds = max(0, configured_min)
        self.max_interval_seconds = max(self.min_interval_seconds, configured_max)
        self.clock = clock
        self.sleeper = sleeper
        self.choose_interval = choose_interval
        self.condition = threading.Condition()
        self.lock = self.condition
        self.last_finished_at: float | None = None
        self.next_ticket = 0
        self.serving_ticket = 0

    def run(self, operation, *, enforce_cooldown: bool = True):
        with self.condition:
            ticket = self.next_ticket
            self.next_ticket += 1
            while ticket != self.serving_ticket:
                self.condition.wait()
            if enforce_cooldown and self.last_finished_at is not None:
                interval = float(self.choose_interval(self.min_interval_seconds, self.max_interval_seconds))
                remaining = interval - (self.clock() - self.last_finished_at)
                if remaining > 0:
                    self.sleeper(remaining)
            try:
                return operation()
            finally:
                if enforce_cooldown:
                    self.last_finished_at = self.clock()
                self.serving_ticket += 1
                self.condition.notify_all()


class UploadJobManager:
    def __init__(self, adapter: TikTokUploadAdapter | None = None, upload_gate: TikTokUploadGate | None = None):
        self.adapter = adapter or TikTokUploadAdapter()
        self.jobs: dict[str, UploadJob] = {}
        # One FIFO gate is shared by video and photo jobs. It serializes the
        # complete external upload and enforces a cooldown before the next init.
        self.upload_gate = upload_gate or TikTokUploadGate()
        self._upload_lock = self.upload_gate.lock
        self._account_gate_registry_lock = threading.Lock()
        self._account_gates: dict[str, TikTokUploadGate] = {}

    def reset(self):
        self.jobs.clear()
        self.upload_gate.last_finished_at = None
        self.upload_gate.next_ticket = 0
        self.upload_gate.serving_ticket = 0
        with self._account_gate_registry_lock:
            self._account_gates.clear()

    def _gate_for_account(self, account: str) -> TikTokUploadGate:
        account_id = str(account or "main_tiktok").strip() or "main_tiktok"
        with self._account_gate_registry_lock:
            gate = self._account_gates.get(account_id)
            if gate is not None:
                return gate
            # Preserve the injectable legacy gate for the first account, then
            # allocate independent FIFO/cooldown gates for other accounts.
            gate = self.upload_gate if not self._account_gates else TikTokUploadGate()
            self._account_gates[account_id] = gate
            return gate

    @staticmethod
    def _cooldown_enabled() -> bool:
        mode = os.getenv("TIKTOK_UPLOAD_MODE", settings.upload_mode).strip() or "dry_run"
        return mode != "dry_run"

    def _find_idempotent_job(self, key: str) -> UploadJob | None:
        path = settings.data_dir / "upload_history.jsonl"
        if not path.exists():
            return None
        found = None
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    item = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if item.get("idempotency_key") == key:
                    found = item
        except OSError:
            return None
        if not found:
            return None
        allowed = {"id", "filename", "account", "caption", "options", "idempotency_key", "status", "progress", "message", "created_at", "updated_at", "result", "error"}
        return UploadJob(**{name: found.get(name) for name in allowed if name in found})

    def create_and_run(self, payload: UploadJobRequest) -> UploadJob:
        if payload.idempotency_key:
            existing = self.jobs.get(payload.idempotency_key) or self._find_idempotent_job(payload.idempotency_key)
            if existing:
                self.jobs[existing.id] = existing
                return existing
        video_path = resolve_video_path(payload.filename)
        job = UploadJob(
            id=uuid.uuid4().hex,
            filename=payload.filename,
            account=payload.account,
            caption=payload.caption,
            options=payload.options.model_dump(),
            idempotency_key=payload.idempotency_key,
        )
        self.jobs[job.id] = job
        self._safe_append_history(job)
        self._run(job, video_path)
        return job

    def _run(self, job: UploadJob, video_path: Path):
        try:
            self._update(job, status="queued", progress=0, message="Queued for serial TikTok upload")
            def execute_upload():
                self._update(job, status="running", progress=20, message="Preparing video for TikTok upload")
                return self.adapter.upload(video_path=video_path, account=job.account, caption=job.caption, options=job.options)
            result = self._gate_for_account(job.account).run(execute_upload, enforce_cooldown=self._cooldown_enabled())
            workflow_status = str(result.get("workflow_status") or "success")
            message = str(result.get("message") or "Completed")
            self._update(job, status=workflow_status, progress=100, message=message, result=result)
            self._safe_append_history(job)
        except Exception as exc:
            self._update(job, status="failed", progress=100, message="Failed", error=str(exc))
            self._safe_append_history(job)

    def _safe_append_history(self, job: UploadJob):
        try:
            self._append_history(job)
        except OSError as exc:
            job.message = f"{job.message} (history not persisted: {exc})"
            job.updated_at = time.time()

    def _append_history(self, job: UploadJob):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        path = settings.data_dir / "upload_history.jsonl"
        import json
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(job.to_dict(), ensure_ascii=False) + "\n")

    def _update(self, job: UploadJob, **kwargs):
        for key, value in kwargs.items():
            setattr(job, key, value)
        job.updated_at = time.time()

    def get(self, job_id: str) -> UploadJob:
        if job_id not in self.jobs:
            raise KeyError(job_id)
        return self.jobs[job_id]

    def delete(self, job_id: str) -> UploadJob:
        if job_id not in self.jobs:
            raise KeyError(job_id)
        return self.jobs.pop(job_id)

    def list_jobs(self) -> list[UploadJob]:
        return sorted(self.jobs.values(), key=lambda j: j.created_at, reverse=True)


job_manager = UploadJobManager()


def photos_dir() -> Path:
    return settings.download_dir / "douyin_photo"


def resolve_photo_album(photo_id: str) -> tuple[Path, dict[str, Any], list[Path]]:
    if not photo_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in photo_id):
        raise HTTPException(status_code=400, detail="Invalid photo ID")
    album_dir = photos_dir() / f"douyin_{photo_id}"
    manifest_path = album_dir / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(status_code=404, detail="Photo album not found")
    manifest = _read_json_file(manifest_path)
    source_filenames = manifest.get("image_files") or []
    filenames = manifest.get("tiktok_image_files") or source_filenames
    if not isinstance(source_filenames, list) or not isinstance(filenames, list) or not 1 <= len(filenames) <= 35 or len(filenames) != len(source_filenames):
        raise HTTPException(status_code=400, detail="Photo album must contain between 1 and 35 ordered images")
    paths: list[Path] = []
    for filename in filenames:
        name = str(filename)
        if "/" in name or "\\" in name or ".." in name or Path(name).suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            raise HTTPException(status_code=400, detail="Invalid photo filename")
        path = album_dir / name
        if not path.exists() or not path.is_file() or path.stat().st_size <= 0:
            raise HTTPException(status_code=404, detail=f"Photo asset not found: {name}")
        paths.append(path)
    return album_dir, manifest, paths


def resolve_photo_path(photo_id: str, filename: str) -> Path:
    album_dir, manifest, _ = resolve_photo_album(photo_id)
    allowed_names = [*(manifest.get("image_files") or []), *(manifest.get("tiktok_image_files") or [])]
    if filename not in {str(name) for name in allowed_names} or "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=404, detail="Photo asset not found")
    path = album_dir / filename
    if not path.exists() or not path.is_file() or path.stat().st_size <= 0:
        raise HTTPException(status_code=404, detail="Photo asset not found")
    return path


def resolve_music_path(photo_id: str) -> Path:
    album_dir, manifest, _ = resolve_photo_album(photo_id)
    filename = str(manifest.get("music_file") or "")
    if not filename or "/" in filename or "\\" in filename or ".." in filename or Path(filename).suffix.lower() not in {".mp3", ".m4a", ".aac"}:
        raise HTTPException(status_code=404, detail="Music reference not found")
    path = album_dir / filename
    if not path.exists() or not path.is_file() or path.stat().st_size <= 0:
        raise HTTPException(status_code=404, detail="Music reference not found")
    return path


def public_photo_url(photo_id: str, filename: str) -> str:
    path = resolve_photo_path(photo_id, filename)
    return f"{settings.public_base_url}/upload-api/api/photos/{photo_id}/{path.name}"


def videos_dir() -> Path:
    return settings.download_dir / "douyin_video"


def validate_filename(filename: str) -> str:
    if "/" in filename or "\\" in filename or ".." in filename or not filename.endswith(".mp4"):
        raise HTTPException(status_code=400, detail="Invalid video filename")
    return filename


def resolve_video_path(filename: str) -> Path:
    filename = validate_filename(filename)
    path = videos_dir() / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Video file not found")
    if path.stat().st_size <= 0:
        raise HTTPException(status_code=400, detail="Video file is empty")
    return path


def public_video_url(filename: str) -> str:
    return f"{settings.public_base_url}/upload-api/api/videos/{validate_filename(filename)}"


def callback_url() -> str:
    return f"{settings.public_base_url}/api/tiktok/update-status"


def _create_oauth_state(account_id: str) -> str:
    safe_id = validate_account_id(account_id)
    account_registry_item(safe_id)
    state = secrets.token_urlsafe(32)
    now = time.time()
    with _oauth_state_lock:
        states = _read_encrypted_json(tiktok_oauth_states_path())
        states = {key: value for key, value in states.items() if float((value or {}).get("expires_at") or 0) > now}
        states[state] = {"account_id": safe_id, "expires_at": now + 600}
        _write_encrypted_json(tiktok_oauth_states_path(), states)
    return state


def _consume_oauth_state(state: str) -> str:
    if not state:
        raise HTTPException(status_code=400, detail="TikTok OAuth state is missing or expired")
    now = time.time()
    with _oauth_state_lock:
        states = _read_encrypted_json(tiktok_oauth_states_path())
        item = states.pop(state, None)
        states = {key: value for key, value in states.items() if float((value or {}).get("expires_at") or 0) > now}
        _write_encrypted_json(tiktok_oauth_states_path(), states)
    if not item or float(item.get("expires_at") or 0) <= now:
        raise HTTPException(status_code=400, detail="TikTok OAuth state is invalid, expired, or already used")
    return validate_account_id(str(item.get("account_id") or ""))


def build_tiktok_authorization_url(account_id: str = DEFAULT_TIKTOK_ACCOUNT_ID) -> str:
    config = load_tiktok_oauth_config()
    if not config["client_key"] or not config["client_secret"]:
        raise HTTPException(status_code=400, detail="TikTok OAuth client config is missing")
    params = {
        "client_key": config["client_key"],
        "scope": "user.info.basic,video.upload,video.publish,video.list",
        "response_type": "code",
        "redirect_uri": settings.tiktok_redirect_uri,
        "state": _create_oauth_state(account_id),
    }
    return "https://www.tiktok.com/v2/auth/authorize/?" + urlencode(params)


def exchange_tiktok_code(code: str) -> dict[str, Any]:
    config = load_tiktok_oauth_config()
    if not config["client_key"] or not config["client_secret"]:
        raise HTTPException(status_code=400, detail="TikTok OAuth client config is missing")
    response = httpx.post(
        "https://open.tiktokapis.com/v2/oauth/token/",
        data={
            "client_key": config["client_key"],
            "client_secret": config["client_secret"],
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": settings.tiktok_redirect_uri,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def refresh_tiktok_access_token_if_needed(account_id: str = DEFAULT_TIKTOK_ACCOUNT_ID) -> dict[str, Any]:
    safe_id = validate_account_id(account_id)
    token = load_tiktok_oauth_token(safe_id)
    access_token = str(token.get("access_token") or "")
    expires_at = float(token.get("access_token_expires_at") or 0)
    if access_token and expires_at and expires_at > time.time() + 300:
        return token
    refresh_token = str(token.get("refresh_token") or "")
    if not refresh_token:
        return token
    config = load_tiktok_oauth_config()
    if not config["client_key"] or not config["client_secret"]:
        return token
    response = httpx.post(
        "https://open.tiktokapis.com/v2/oauth/token/",
        data={
            "client_key": config["client_key"],
            "client_secret": config["client_secret"],
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    response.raise_for_status()
    refreshed = {**token, **response.json()}
    save_tiktok_oauth_token(refreshed, safe_id)
    return load_tiktok_oauth_token(safe_id)


def fetch_tiktok_publish_status(publish_id: str, account_id: str = DEFAULT_TIKTOK_ACCOUNT_ID) -> dict[str, Any]:
    token = refresh_tiktok_access_token_if_needed(account_id)
    access_token = str(token.get("access_token") or "")
    if not access_token:
        raise HTTPException(status_code=400, detail="TikTok OAuth access token is missing; connect TikTok first")
    response = httpx.post(
        "https://open.tiktokapis.com/v2/post/publish/status/fetch/",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
        json={"publish_id": publish_id},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data") or {}
    error = payload.get("error") or {}
    return {
        "publish_id": publish_id,
        "status": str(data.get("status") or ""),
        "uploaded_bytes": int(data.get("uploaded_bytes") or 0),
        "fail_reason": str(data.get("fail_reason") or ""),
        "post_ids": data.get("publicaly_available_post_id") or [],
        "error_code": str(error.get("code") or ""),
        "error_message": str(error.get("message") or ""),
        "log_id": str(error.get("log_id") or ""),
    }


def create_photo_upload_job(payload: PhotoUploadJobRequest) -> UploadJob:
    if payload.idempotency_key:
        existing = job_manager.jobs.get(payload.idempotency_key) or job_manager._find_idempotent_job(payload.idempotency_key)
        if existing:
            job_manager.jobs[existing.id] = existing
            return existing
    _, manifest, image_paths = resolve_photo_album(payload.photo_id)
    job = UploadJob(
        id=uuid.uuid4().hex,
        filename=f"photo:{payload.photo_id}",
        account=payload.account,
        caption=payload.caption,
        options={"media_type": "photo", "asset_count": len(image_paths)},
        idempotency_key=payload.idempotency_key,
        status="running",
        progress=30,
        message="Đang chuẩn bị bài ảnh cho TikTok Inbox",
    )
    job_manager.jobs[job.id] = job
    job_manager._safe_append_history(job)
    try:
        watermark_processing = manifest.get("watermark_processing") or {}
        if watermark_processing.get("status") != "clean":
            raise RuntimeError("Watermark stage is not clean; TikTok upload blocked")
        mode = os.getenv("TIKTOK_UPLOAD_MODE", settings.upload_mode).strip() or "dry_run"
        if mode == "dry_run":
            result = {
                "mode": "dry_run",
                "media_type": "photo",
                "workflow_status": "success",
                "asset_count": len(image_paths),
                "photo_id": payload.photo_id,
            }
        elif mode == "inbox":
            token = refresh_tiktok_access_token_if_needed(payload.account)
            access_token = str(token.get("access_token") or "")
            if not access_token:
                raise RuntimeError("TikTok OAuth token unavailable; connect TikTok first")
            image_urls = [public_photo_url(payload.photo_id, path.name) for path in image_paths]
            response = httpx.post(
                "https://open.tiktokapis.com/v2/post/publish/content/init/",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json; charset=UTF-8",
                },
                json={
                    "post_info": {"title": payload.caption, "description": ""},
                    "source_info": {
                        "source": "PULL_FROM_URL",
                        "photo_cover_index": 0,
                        "photo_images": image_urls,
                    },
                    "post_mode": "MEDIA_UPLOAD",
                    "media_type": "PHOTO",
                },
                timeout=60,
            )
            response.raise_for_status()
            provider = response.json()
            error = provider.get("error") or {}
            if error.get("code") not in {None, "", "ok"}:
                raise RuntimeError(f"TikTok Photo Inbox init failed: {error.get('code')}: {error.get('message') or ''}")
            publish_id = str((provider.get("data") or {}).get("publish_id") or "")
            if not publish_id:
                raise RuntimeError("TikTok Photo Inbox init did not return publish_id")
            result = {
                "mode": "inbox",
                "media_type": "photo",
                "workflow_status": "awaiting_user_review",
                "publish_id": publish_id,
                "log_id": str(error.get("log_id") or ""),
                "asset_count": len(image_paths),
                "music_preserved_locally": bool(manifest.get("music_file")),
                "message": "Ảnh đã được gửi vào TikTok Inbox; mở thông báo để chọn nhạc và hoàn tất đăng.",
            }
        else:
            raise RuntimeError(f"Photo upload is not supported in TIKTOK_UPLOAD_MODE={mode!r}")
        job.status = str(result.get("workflow_status") or "success")
        job.progress = 100
        job.message = str(result.get("message") or "Completed")
        job.result = result
    except Exception as exc:
        job.status = "failed"
        job.progress = 100
        job.message = "Gửi bài ảnh TikTok thất bại"
        job.error = str(exc)
    job.updated_at = time.time()
    job_manager._safe_append_history(job)
    return job


def create_n8n_publish_job(payload: UploadJobRequest) -> UploadJob:
    video_path = resolve_video_path(payload.filename)
    token = refresh_tiktok_access_token_if_needed(payload.account)
    access_token = str(token.get("access_token") or "")
    if not access_token:
        raise HTTPException(status_code=400, detail="TikTok OAuth access token is missing; connect TikTok first")
    job = UploadJob(
        id=uuid.uuid4().hex,
        filename=payload.filename,
        account=payload.account,
        caption=payload.caption,
        options=payload.options.model_dump(),
        status="running",
        progress=30,
        message="Sent to n8n TikTok workflow",
    )
    job_manager.jobs[job.id] = job
    n8n_payload = {
        "local_video_id": job.id,
        "filename": job.filename,
        "account": job.account,
        "access_token": access_token,
        "video_url": public_video_url(job.filename),
        "caption": job.caption,
        "callback_url": callback_url(),
        "post_info": {
            "title": job.caption,
            "privacy_level": "SELF_ONLY",
            "disable_duet": bool(1 - int(job.options.get("allow_duet", 0))),
            "disable_comment": bool(1 - int(job.options.get("allow_comment", 1))),
            "disable_stitch": bool(1 - int(job.options.get("allow_stitch", 0))),
            "video_cover_timestamp_ms": 1000,
        },
        "source_info": {"source": "PULL_FROM_URL", "video_url": public_video_url(job.filename)},
    }
    try:
        response = httpx.post(settings.n8n_webhook_url, json=n8n_payload, timeout=60)
        response.raise_for_status()
        try:
            n8n_response = response.json()
        except ValueError:
            n8n_response = {"text": response.text[:500]}
        job.result = {
            "mode": "n8n",
            "ok": True,
            "webhook": "submitted",
            "video_url": public_video_url(job.filename),
            "n8n_response": n8n_response,
            "size_bytes": video_path.stat().st_size,
        }
        job.progress = 60
        n8n_failed = isinstance(n8n_response, dict) and (
            n8n_response.get("ok") is False
            or str(n8n_response.get("status", "")).lower() in {"failed", "error"}
        )
        if n8n_failed:
            job.status = "failed"
            job.progress = 100
            job.message = "n8n/TikTok publish failed"
            job.error = str(n8n_response.get("error") or n8n_response.get("message") or "n8n workflow returned failed status")
        elif isinstance(n8n_response, dict) and (n8n_response.get("publish_id") or n8n_response.get("tiktok_publish_id")):
            job.status = "success"
            job.progress = 100
            job.message = "TikTok publish accepted"
        job.updated_at = time.time()
        job_manager._safe_append_history(job)
    except Exception as exc:
        job.status = "failed"
        job.progress = 100
        job.message = "n8n workflow failed"
        job.error = str(exc)
        job.updated_at = time.time()
        job_manager._safe_append_history(job)
    return job


@app.get("/health")
async def health():
    mode = os.getenv("TIKTOK_UPLOAD_MODE", settings.upload_mode).strip() or "dry_run"
    provider_ready = bool(TikTokUploadAdapter._read_secret("ayrshare_api_key.txt")) if mode == "ayrshare" else True
    return {
        "status": "ok",
        "service": "tiktok-uploader",
        "mode": mode,
        "provider_ready": provider_ready,
        "queue": {
            "serial": True,
            "min_interval_seconds": job_manager.upload_gate.min_interval_seconds,
            "max_interval_seconds": job_manager.upload_gate.max_interval_seconds,
            "waiting": max(0, job_manager.upload_gate.next_ticket - job_manager.upload_gate.serving_ticket),
        },
    }


@app.get("/api/account/tiktok/status")
async def tiktok_account_status():
    status = load_tiktok_account_status()
    status["oauth"] = load_tiktok_oauth_status()
    return status


@app.get("/api/tiktok/oauth/status")
async def tiktok_oauth_status():
    return load_tiktok_oauth_status()


@app.get("/api/tiktok/publish/status/{publish_id}")
async def tiktok_publish_status(publish_id: str, account_id: str = DEFAULT_TIKTOK_ACCOUNT_ID):
    return fetch_tiktok_publish_status(publish_id, account_id)


@app.get("/api/tiktok/accounts")
async def tiktok_accounts():
    return list_tiktok_accounts()


@app.post("/api/tiktok/accounts")
async def tiktok_create_account(payload: TikTokOAuthAccountCreate):
    return create_tiktok_account(payload.display_name, payload.notification_email)


@app.patch("/api/tiktok/accounts/{account_id}/notification")
async def tiktok_update_account_notification(account_id: str, payload: TikTokAccountNotificationUpdate):
    return update_tiktok_account_notification(account_id, payload.notification_email)


@app.delete("/api/tiktok/accounts/{account_id}")
async def tiktok_delete_account(account_id: str):
    delete_tiktok_account(account_id)
    return {"deleted": True, "account_id": account_id}


@app.get("/api/tiktok/accounts/{account_id}/oauth/connect")
async def tiktok_account_oauth_connect(account_id: str):
    return {"auth_url": build_tiktok_authorization_url(account_id), "redirect_uri": settings.tiktok_redirect_uri}


@app.get("/api/tiktok/oauth/connect")
async def tiktok_oauth_connect():
    return {"auth_url": build_tiktok_authorization_url(DEFAULT_TIKTOK_ACCOUNT_ID), "redirect_uri": settings.tiktok_redirect_uri}


@app.get("/api/tiktok/oauth/callback")
async def tiktok_oauth_callback(code: str, state: str = ""):
    account_id = _consume_oauth_state(state)
    account_registry_item(account_id)
    token = exchange_tiktok_code(code)
    if not token.get("access_token"):
        detail = token.get("error_description") or token.get("error") or "TikTok OAuth did not return access_token"
        raise HTTPException(status_code=400, detail=detail)
    save_tiktok_oauth_token(token, account_id)
    account = account_public_status(account_registry_item(account_id))
    return {"ok": True, "message": "TikTok OAuth connected", "account": account, "status": load_tiktok_oauth_status(account_id)}


@app.get("/api/tiktok/callback")
async def tiktok_oauth_doc_callback(code: str, state: str = ""):
    return await tiktok_oauth_callback(code=code, state=state)


@app.post("/api/account/tiktok")
async def update_tiktok_account(payload: TikTokAccountUpdate):
    return save_tiktok_account(payload)


@app.get("/api/videos")
async def list_videos():
    target = videos_dir()
    if not target.exists():
        return []
    result = []
    for path in sorted(target.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
        stat = path.stat()
        video_id = path.stem.removeprefix("douyin_")
        result.append({
            "video_id": video_id,
            "filename": path.name,
            "file_path": str(path),
            "size_bytes": stat.st_size,
            "modified_at": stat.st_mtime,
            "ready_for_tiktok_upload": stat.st_size > 0,
            "download_url": f"/api/videos/{path.name}",
        })
    return result


@app.get("/api/videos/{filename}")
async def get_video(filename: str):
    return FileResponse(resolve_video_path(filename), media_type="video/mp4", filename=filename)


@app.get("/api/photos/{photo_id}/music")
async def get_photo_music(photo_id: str):
    path = resolve_music_path(photo_id)
    return FileResponse(path, filename=path.name)


@app.get("/api/photos/{photo_id}/{filename}")
async def get_photo(photo_id: str, filename: str):
    path = resolve_photo_path(photo_id, filename)
    media_type = {".webp": "image/webp", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media_type, filename=filename)


@app.post("/api/upload/photo-jobs")
def create_photo_job(payload: PhotoUploadJobRequest):
    return job_manager.upload_gate.run(
        lambda: create_photo_upload_job(payload),
        enforce_cooldown=job_manager._cooldown_enabled(),
    ).to_dict()


@app.post("/api/upload/jobs")
def create_upload_job(payload: UploadJobRequest):
    mode = os.getenv("TIKTOK_UPLOAD_MODE", settings.upload_mode).strip() or "dry_run"
    if mode == "n8n":
        return create_n8n_publish_job(payload).to_dict()
    job = job_manager.create_and_run(payload)
    return job.to_dict()


@app.post("/api/upload/n8n/jobs")
async def create_n8n_upload_job(payload: UploadJobRequest):
    job = create_n8n_publish_job(payload)
    return job.to_dict()


@app.post("/api/tiktok/update-status")
async def update_tiktok_status(payload: N8nStatusCallback):
    try:
        job = job_manager.get(payload.local_video_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Upload job not found") from exc
    if payload.status in {"success", "submitted"}:
        job.status = "success"
        job.progress = 100
        job.message = "TikTok publish accepted"
        job.error = None
    elif payload.status == "uploading":
        job.status = "running"
        job.progress = max(job.progress, 70)
        job.message = "n8n is uploading to TikTok"
    else:
        job.status = "failed"
        job.progress = 100
        job.message = "TikTok publish failed"
        job.error = payload.error or "TikTok/n8n returned failed status"
    job.result = {**(job.result or {}), "mode": "n8n", "tiktok_publish_id": payload.tiktok_publish_id, "tiktok_video_id": payload.tiktok_video_id, "callback_status": payload.status}
    job.updated_at = time.time()
    job_manager._safe_append_history(job)
    return {"ok": True, "job": job.to_dict()}


@app.get("/api/upload/jobs")
async def list_upload_jobs():
    return [job.to_dict() for job in job_manager.list_jobs()]


@app.delete("/api/upload/jobs/{job_id}")
async def delete_upload_job(job_id: str):
    try:
        job_manager.delete(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Upload job not found") from exc
    return {"deleted": True, "job_id": job_id}


@app.get("/api/upload/jobs/{job_id}")
async def get_upload_job(job_id: str):
    try:
        return job_manager.get(job_id).to_dict()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Upload job not found") from exc
