import json
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.responses import JSONResponse
from starlette.responses import FileResponse, HTMLResponse, StreamingResponse

from .autocrawl import AutoCrawlManager, AutoCrawlScheduler, public_video_row
from .cookie_manager import CookieManager
from .downloader import DownloadService
from .jobs import JobManager
from .parser import DouyinParser

@asynccontextmanager
async def lifespan(app: FastAPI):
    autocrawl_scheduler.manager = autocrawl_manager
    has_scheduled_jobs = any(row.get("schedule_enabled") and row.get("status") == "active" for row in autocrawl_manager.db.list_channels())
    if autocrawl_scheduler.enabled or has_scheduled_jobs:
        autocrawl_scheduler.start()
    yield
    autocrawl_scheduler.stop()


app = FastAPI(title="Douyin No-Watermark Tool", version="0.1.0", lifespan=lifespan)
cookie_manager = CookieManager()
parser = DouyinParser(cookie_manager=cookie_manager)
downloader = DownloadService(parser=parser)
job_manager = JobManager(downloader=downloader)
autocrawl_manager = AutoCrawlManager(downloader=downloader)
autocrawl_scheduler = AutoCrawlScheduler(autocrawl_manager)
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
_rate_limit_hits: dict[str, list[float]] = {}


def _video_response(path: Path, request: Request):
    size = path.stat().st_size
    range_header = request.headers.get("range", "").strip()
    common_headers = {"Accept-Ranges": "bytes"}
    if not range_header:
        return FileResponse(path=path, filename=path.name, media_type="video/mp4", headers=common_headers)

    match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
    if not match or (not match.group(1) and not match.group(2)):
        raise HTTPException(status_code=416, detail="Invalid range", headers={"Content-Range": f"bytes */{size}"})
    start_text, end_text = match.groups()
    if start_text:
        start = int(start_text)
        end = int(end_text) if end_text else size - 1
    else:
        suffix_length = int(end_text)
        start = max(0, size - suffix_length)
        end = size - 1
    if start >= size or start < 0 or end < start:
        raise HTTPException(status_code=416, detail="Range not satisfiable", headers={"Content-Range": f"bytes */{size}"})
    end = min(end, size - 1)
    content_length = end - start + 1

    def iterator():
        remaining = content_length
        with path.open("rb") as source:
            source.seek(start)
            while remaining > 0:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        iterator(),
        status_code=206,
        media_type="video/mp4",
        headers={
            **common_headers,
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Content-Length": str(content_length),
        },
    )


@app.middleware("http")
async def auth_and_rate_limit_middleware(request: Request, call_next):
    public_path = request.url.path == "/health" or request.url.path == "/" or request.url.path.startswith("/static/")
    if not public_path:
        auth_token = os.getenv("API_AUTH_TOKEN", "").strip()
        if auth_token and request.headers.get("X-API-Token") != auth_token:
            return JSONResponse({"detail": "Invalid or missing API token"}, status_code=401)

        limit = int(os.getenv("RATE_LIMIT_PER_MINUTE", "0") or "0")
        if limit > 0:
            now = time.time()
            client = request.client.host if request.client else "unknown"
            key = f"{client}:{request.url.path}"
            window_start = now - 60
            hits = [ts for ts in _rate_limit_hits.get(key, []) if ts >= window_start]
            if len(hits) >= limit:
                _rate_limit_hits[key] = hits
                return JSONResponse({"detail": "Rate limit exceeded"}, status_code=429)
            hits.append(now)
            _rate_limit_hits[key] = hits

    return await call_next(request)


class CookieUpdateRequest(BaseModel):
    cookie: str
    service: str = "douyin"
    timestamp: str | None = None


class DownloadJobRequest(BaseModel):
    url: str


class CrawlChannelRequest(BaseModel):
    profile_url: str | None = None
    user_id: str | None = None
    display_name: str | None = None
    douyin_uid: str | None = None
    interval_minutes: int = 60
    schedule_enabled: bool = True
    auto_upload_enabled: bool = False


class CrawlRunRequest(BaseModel):
    channel_id: int | None = None
    download: bool = True


class CrawlChannelStatusRequest(BaseModel):
    status: str | None = None
    interval_minutes: int | None = None
    schedule_enabled: bool | None = None
    auto_upload_enabled: bool | None = None


class CrawlVideoActionRequest(BaseModel):
    is_starred: bool


class CrawlSchedulerRequest(BaseModel):
    enabled: bool
    interval_minutes: int | None = None


def _check_cookie_token(token: str | None):
    expected = os.getenv("COOKIE_UPDATE_TOKEN", "").strip()
    if expected and token != expected:
        raise HTTPException(status_code=401, detail="Invalid cookie update token")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/api/cookie/douyin/status")
async def douyin_cookie_status():
    return cookie_manager.status()


@app.get("/api/metrics")
async def metrics():
    download_dir = downloader.download_dir
    try:
        usage = os.statvfs(download_dir if download_dir.exists() else download_dir.parent)
        disk_free = usage.f_bavail * usage.f_frsize
        disk_total = usage.f_blocks * usage.f_frsize
    except Exception:
        disk_free = None
        disk_total = None
    jobs = job_manager.list_jobs()
    return {
        "status": "ok",
        "jobs": {
            "total": len(jobs),
            "queued": sum(1 for j in jobs if j.status == "queued"),
            "running": sum(1 for j in jobs if j.status == "running"),
            "success": sum(1 for j in jobs if j.status == "success"),
            "failed": sum(1 for j in jobs if j.status == "failed"),
        },
        "cookie": {"has_cookie": cookie_manager.status().get("has_cookie")},
        "disk": {
            "download_dir": str(download_dir),
            "free_bytes": disk_free,
            "total_bytes": disk_total,
        },
    }


@app.post("/api/cookie/douyin/webhook")
async def douyin_cookie_webhook(payload: CookieUpdateRequest, x_cookie_token: str | None = Header(default=None)):
    _check_cookie_token(x_cookie_token)
    if payload.service.lower() != "douyin":
        raise HTTPException(status_code=400, detail="Only service=douyin is supported")
    try:
        cookie_manager.update_cookie(payload.cookie, source="webhook")
        cookie_manager.apply_to_vendor_crawler()
        return {"ok": True, "status": cookie_manager.status()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/cookie/douyin")
async def douyin_cookie_update(payload: CookieUpdateRequest, x_cookie_token: str | None = Header(default=None)):
    return await douyin_cookie_webhook(payload, x_cookie_token)


@app.post("/api/jobs/download")
async def create_download_job(payload: DownloadJobRequest, background_tasks: BackgroundTasks):
    job = job_manager.create_download_job_record(payload.url)
    background_tasks.add_task(job_manager.run_download_job, job.id)
    return job.to_dict()


@app.get("/api/jobs")
async def list_jobs():
    return [job.to_dict() for job in job_manager.list_jobs()]


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    try:
        return job_manager.get(job_id).to_dict()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc


@app.get("/api/crawl/status")
async def crawl_status():
    stats = autocrawl_manager.db.stats()
    sessions = autocrawl_manager.db.list_sessions(limit=1)
    return {"status": "ok", **stats, "last_session": sessions[0] if sessions else None}


@app.get("/api/crawl/channels")
async def crawl_channels(status: str | None = Query(default=None)):
    return autocrawl_manager.db.list_channels(status=status)


async def _run_initial_channel_crawl(
    manager: AutoCrawlManager,
    scheduler: AutoCrawlScheduler,
    channel_id: int,
    schedule_enabled: bool,
) -> None:
    try:
        await manager.run_once(channel_id=channel_id, download=True)
    finally:
        channel = manager.db.get_channel(channel_id)
        now = time.time()
        if not channel or channel.get("is_deleted"):
            manager.db.update_channel(
                channel_id,
                status="paused",
                schedule_enabled=0,
                next_run_at=None,
                last_run_at=now,
            )
        else:
            manager.db.update_channel(channel_id, schedule_enabled=int(schedule_enabled))
            channel = manager.db.get_channel(channel_id)
            if channel and channel.get("status") == "active" and schedule_enabled:
                manager.db.mark_channel_scheduled_run(channel_id)
                scheduler.manager = manager
                scheduler.start()
            else:
                manager.db.update_channel(channel_id, last_run_at=now, next_run_at=None)


@app.post("/api/crawl/channels")
async def crawl_add_channel(payload: CrawlChannelRequest, background_tasks: BackgroundTasks):
    try:
        user_id = (payload.user_id or payload.douyin_uid or "").strip()
        profile_url = (payload.profile_url or (f"https://www.douyin.com/user/{user_id}" if user_id else "")).strip()
        if not profile_url:
            raise ValueError("Cần nhập Douyin user ID")
        if not user_id and "/user/" in profile_url:
            user_id = profile_url.split("/user/", 1)[1].split("?", 1)[0].strip("/")
        display_name = (payload.display_name or (f"Douyin {user_id[:12]}" if user_id else "Kênh Douyin")).strip()
        channel = autocrawl_manager.db.add_channel(
            profile_url,
            display_name,
            user_id or None,
            interval_minutes=payload.interval_minutes,
            schedule_enabled=payload.schedule_enabled,
            auto_upload_enabled=payload.auto_upload_enabled,
        )
        autocrawl_manager.db.update_channel(channel["id"], schedule_enabled=0, next_run_at=None)
        background_tasks.add_task(
            _run_initial_channel_crawl,
            autocrawl_manager,
            autocrawl_scheduler,
            channel["id"],
            payload.schedule_enabled,
        )
        return {**channel, "initial_crawl_started": True}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            raise HTTPException(status_code=409, detail="Channel already exists") from exc
        raise


@app.patch("/api/crawl/channels/{channel_id}")
async def crawl_update_channel(channel_id: int, payload: CrawlChannelStatusRequest):
    channel = autocrawl_manager.db.get_channel(channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    if payload.status is not None:
        if payload.status not in {"active", "paused"}:
            raise HTTPException(status_code=400, detail="status must be active or paused")
        if payload.status == "paused":
            autocrawl_manager.db.pause_channel(channel_id)
        else:
            autocrawl_manager.db.resume_channel(channel_id)
    if payload.interval_minutes is not None or payload.schedule_enabled is not None:
        autocrawl_manager.db.update_channel_schedule(
            channel_id,
            interval_minutes=payload.interval_minutes or int(channel.get("interval_minutes") or 60),
            schedule_enabled=payload.schedule_enabled if payload.schedule_enabled is not None else bool(channel.get("schedule_enabled")),
        )
        if payload.schedule_enabled:
            autocrawl_scheduler.manager = autocrawl_manager
            autocrawl_scheduler.start()
    if payload.auto_upload_enabled is not None:
        autocrawl_manager.db.update_channel(channel_id, auto_upload_enabled=int(payload.auto_upload_enabled))
    return autocrawl_manager.db.get_channel(channel_id)


@app.delete("/api/crawl/channels/{channel_id}")
async def crawl_delete_channel(channel_id: int):
    channel = autocrawl_manager.db.delete_channel(channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    return {"deleted": True, "channel_id": channel_id}


@app.post("/api/crawl/run")
async def crawl_run(payload: CrawlRunRequest, background_tasks: BackgroundTasks):
    # Run synchronously for explicit operator feedback; per-channel errors are captured in the session.
    return await autocrawl_manager.run_once(channel_id=payload.channel_id, download=payload.download)


@app.get("/api/crawl/scheduler")
async def crawl_scheduler_status():
    return autocrawl_scheduler.status()


@app.post("/api/crawl/scheduler")
async def crawl_scheduler_update(payload: CrawlSchedulerRequest):
    if payload.enabled:
        return autocrawl_scheduler.start(payload.interval_minutes)
    return autocrawl_scheduler.stop()


@app.get("/api/crawl/sessions")
async def crawl_sessions(limit: int = Query(default=50, le=200)):
    return autocrawl_manager.db.list_sessions(limit=limit)


@app.get("/api/crawl/videos")
async def crawl_videos(limit: int = Query(default=100, le=500)):
    return [public_video_row(row) for row in autocrawl_manager.db.list_videos(limit=limit)]


@app.get("/api/crawl/videos/{video_id}/subtitle-cues")
async def crawl_video_subtitle_cues(video_id: str):
    if not autocrawl_manager.db.get_video(video_id):
        raise HTTPException(status_code=404, detail="Video not found")
    items = autocrawl_manager.db.list_subtitle_cues(video_id)
    return {"video_id": video_id, "count": len(items), "items": items}


@app.patch("/api/crawl/videos/{video_id}")
async def crawl_update_video(video_id: str, payload: CrawlVideoActionRequest):
    if not autocrawl_manager.db.get_video(video_id):
        raise HTTPException(status_code=404, detail="Video not found")
    return public_video_row(autocrawl_manager.db.set_video_starred(video_id, payload.is_starred))


@app.post("/api/crawl/videos/{video_id}/retry")
async def crawl_retry_video(video_id: str, background_tasks: BackgroundTasks):
    try:
        action, video = autocrawl_manager.prepare_video_retry(video_id)
    except ValueError as exc:
        status_code = 404 if str(exc) == "Video not found" else 409
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    background_tasks.add_task(autocrawl_manager.retry_video, video_id, action)
    return {"retry_started": True, "action": action, "video": public_video_row(video)}


@app.delete("/api/crawl/videos/{video_id}")
async def crawl_delete_video(video_id: str):
    row = autocrawl_manager.db.delete_video(video_id)
    if not row:
        raise HTTPException(status_code=404, detail="Video not found")
    local_path = row.get("local_path")
    if local_path:
        try:
            path = Path(local_path).resolve()
            root = downloader.download_dir.resolve()
            if path.is_relative_to(root) and path.is_file():
                path.unlink()
        except OSError:
            pass
    return {"deleted": True, "video_id": video_id}


@app.get("/api/crawl/queue")
async def crawl_queue(status: str | None = Query(default=None)):
    return autocrawl_manager.db.list_queue(status=status)


@app.post("/api/crawl/queue/process")
async def crawl_process_queue(limit: int = Query(default=20, le=100)):
    return await autocrawl_manager.process_queue(limit=limit)


@app.get("/api/photos")
async def list_photo_albums():
    target_dir = downloader.download_dir / "douyin_photo"
    if not target_dir.exists():
        return []
    albums = []
    manifests = sorted(target_dir.glob("douyin_*/manifest.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for manifest_path in manifests:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            photo_id = str(manifest.get("video_id") or manifest_path.parent.name.removeprefix("douyin_"))
            filenames = manifest.get("image_files") or []
            if not photo_id or not isinstance(filenames, list) or not filenames:
                continue
            safe_files = []
            for filename in filenames:
                name = str(filename)
                if "/" in name or "\\" in name or ".." in name:
                    raise ValueError("unsafe photo filename")
                path = manifest_path.parent / name
                if not path.exists() or path.stat().st_size <= 0:
                    raise ValueError("missing photo asset")
                safe_files.append(name)
            music_filename = str(manifest.get("music_file") or "")
            has_music = bool(music_filename and "/" not in music_filename and "\\" not in music_filename and ".." not in music_filename and (manifest_path.parent / music_filename).exists())
            albums.append({
                "video_id": photo_id,
                "media_type": "photo",
                "title": str(manifest.get("description") or ""),
                "asset_count": len(safe_files),
                "photo_preview_urls": [f"/upload-api/api/photos/{photo_id}/{name}" for name in safe_files],
                "music_preview_url": f"/upload-api/api/photos/{photo_id}/music" if has_music else None,
                "music_title": manifest.get("music_title"),
                "music_author": manifest.get("music_author"),
                "music_duration_sec": manifest.get("music_duration_sec"),
                "modified_at": manifest_path.stat().st_mtime,
            })
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return albums


@app.get("/api/videos")
async def list_videos():
    target_dir = downloader.download_dir / "douyin_video"
    if not target_dir.exists():
        return []
    videos = []
    for path in sorted(target_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
        video_id = path.stem.removeprefix("douyin_")
        stat = path.stat()
        videos.append(
            {
                "video_id": video_id,
                "filename": path.name,
                "file_path": str(path),
                "size_bytes": stat.st_size,
                "modified_at": stat.st_mtime,
                "download_url": f"/api/videos/{path.name}",
                "ready_for_tiktok_upload": stat.st_size > 0,
            }
        )
    return videos


@app.get("/api/videos/subtitled/{filename}")
async def get_subtitled_video_file(filename: str, request: Request):
    if "/" in filename or ".." in filename or not filename.endswith(".mp4"):
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = downloader.download_dir / "douyin_video" / "subtitled" / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Subtitled video not found")
    return _video_response(path, request)


@app.get("/api/videos/{filename}")
async def get_video_file(filename: str, request: Request):
    if "/" in filename or ".." in filename or not filename.endswith(".mp4"):
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = downloader.download_dir / "douyin_video" / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Video not found")
    return _video_response(path, request)


@app.get("/api/parse")
async def parse_douyin(url: str = Query(..., description="Douyin share/video URL")):
    try:
        result = await parser.parse(url)
        return result.model_dump()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/download")
async def download_douyin(url: str = Query(..., description="Douyin share/video URL")):
    try:
        result = await downloader.download(url)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return FileResponse(
        path=result.file_path,
        filename=result.file_path.name,
        media_type="video/mp4",
        headers={
            "X-Saved-Path": str(result.file_path),
            "X-Video-Id": result.video_id,
            "X-Bytes-Written": str(result.bytes_written),
        },
    )


def run():
    import uvicorn

    uvicorn.run(
        "douyin_nwm_tool.main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=os.getenv("RELOAD", "false").lower() == "true",
    )


if __name__ == "__main__":
    run()
