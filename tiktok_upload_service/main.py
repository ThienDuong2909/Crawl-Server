from __future__ import annotations

import json
import os
import random
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field
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
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def load_tiktok_oauth_config() -> dict[str, str]:
    file_payload = _read_json_file(settings.tiktok_oauth_config_file)
    client_key = os.getenv("TIKTOK_CLIENT_KEY") or str(file_payload.get("client_key") or "")
    client_secret = os.getenv("TIKTOK_CLIENT_SECRET") or str(file_payload.get("client_secret") or "")
    return {"client_key": client_key.strip(), "client_secret": client_secret.strip()}


def tiktok_oauth_token_path() -> Path:
    return settings.data_dir / "tiktok_oauth_token.json"


def redact_token(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "[REDACTED]"
    return f"{value[:4]}...{value[-4:]}"


def save_tiktok_oauth_token(payload: dict[str, Any]) -> dict[str, Any]:
    now = time.time()
    token_payload = dict(payload)
    token_payload["updated_at"] = now
    if "expires_in" in token_payload and "access_token_expires_at" not in token_payload:
        token_payload["access_token_expires_at"] = now + int(token_payload.get("expires_in") or 0)
    if "refresh_expires_in" in token_payload and "refresh_token_expires_at" not in token_payload:
        token_payload["refresh_token_expires_at"] = now + int(token_payload.get("refresh_expires_in") or 0)
    _write_secret_json(tiktok_oauth_token_path(), token_payload)
    return load_tiktok_oauth_status()


def load_tiktok_oauth_token() -> dict[str, Any]:
    return _read_json_file(tiktok_oauth_token_path())


def load_tiktok_oauth_status() -> dict[str, Any]:
    config = load_tiktok_oauth_config()
    token = load_tiktok_oauth_token()
    return {
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
    options: UploadOptions = Field(default_factory=UploadOptions)


class PhotoUploadJobRequest(BaseModel):
    photo_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    account: str = Field(min_length=1, max_length=128)
    caption: str = Field(default="", max_length=4000)


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
            token = refresh_tiktok_access_token_if_needed()
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

    def reset(self):
        self.jobs.clear()
        self.upload_gate.last_finished_at = None
        self.upload_gate.next_ticket = 0
        self.upload_gate.serving_ticket = 0

    @staticmethod
    def _cooldown_enabled() -> bool:
        mode = os.getenv("TIKTOK_UPLOAD_MODE", settings.upload_mode).strip() or "dry_run"
        return mode != "dry_run"

    def create_and_run(self, payload: UploadJobRequest) -> UploadJob:
        video_path = resolve_video_path(payload.filename)
        job = UploadJob(
            id=uuid.uuid4().hex,
            filename=payload.filename,
            account=payload.account,
            caption=payload.caption,
            options=payload.options.model_dump(),
        )
        self.jobs[job.id] = job
        self._run(job, video_path)
        return job

    def _run(self, job: UploadJob, video_path: Path):
        try:
            self._update(job, status="queued", progress=0, message="Queued for serial TikTok upload")
            def execute_upload():
                self._update(job, status="running", progress=20, message="Preparing video for TikTok upload")
                return self.adapter.upload(video_path=video_path, account=job.account, caption=job.caption, options=job.options)
            result = self.upload_gate.run(execute_upload, enforce_cooldown=self._cooldown_enabled())
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


def build_tiktok_authorization_url() -> str:
    config = load_tiktok_oauth_config()
    if not config["client_key"] or not config["client_secret"]:
        raise HTTPException(status_code=400, detail="TikTok OAuth client config is missing")
    params = {
        "client_key": config["client_key"],
        "scope": "user.info.basic,video.upload,video.publish,video.list",
        "response_type": "code",
        "redirect_uri": settings.tiktok_redirect_uri,
        "state": secrets.token_urlsafe(16),
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


def refresh_tiktok_access_token_if_needed() -> dict[str, Any]:
    token = load_tiktok_oauth_token()
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
    save_tiktok_oauth_token(refreshed)
    return load_tiktok_oauth_token()


def fetch_tiktok_publish_status(publish_id: str) -> dict[str, Any]:
    token = refresh_tiktok_access_token_if_needed()
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
    _, manifest, image_paths = resolve_photo_album(payload.photo_id)
    job = UploadJob(
        id=uuid.uuid4().hex,
        filename=f"photo:{payload.photo_id}",
        account=payload.account,
        caption=payload.caption,
        options={"media_type": "photo", "asset_count": len(image_paths)},
        status="running",
        progress=30,
        message="Đang chuẩn bị bài ảnh cho TikTok Inbox",
    )
    job_manager.jobs[job.id] = job
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
            token = refresh_tiktok_access_token_if_needed()
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
    token = refresh_tiktok_access_token_if_needed()
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
async def tiktok_publish_status(publish_id: str):
    return fetch_tiktok_publish_status(publish_id)


@app.get("/api/tiktok/oauth/connect")
async def tiktok_oauth_connect():
    return {"auth_url": build_tiktok_authorization_url(), "redirect_uri": settings.tiktok_redirect_uri}


@app.get("/api/tiktok/oauth/callback")
async def tiktok_oauth_callback(code: str, state: str = ""):
    token = exchange_tiktok_code(code)
    if not token.get("access_token"):
        detail = token.get("error_description") or token.get("error") or "TikTok OAuth did not return access_token"
        raise HTTPException(status_code=400, detail=detail)
    status = save_tiktok_oauth_token(token)
    return {"ok": True, "message": "TikTok OAuth connected", "status": status}


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
