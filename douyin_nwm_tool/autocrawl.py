import asyncio
import hashlib
import json
import math
import os
import re
import sqlite3
import time
import httpx
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .cookie_manager import CookieManager
from .downloader import DownloadService
from .translation import AgentRouterTitleTranslator


@dataclass
class AutoCrawlConfig:
    channels_per_batch: int = 20
    max_videos_per_channel: int = 20
    max_video_age_hours: int | None = 72
    min_view_count: int = 0
    min_like_count: int = 0
    min_duration_sec: int | None = None
    max_duration_sec: int | None = 600
    download_new_videos: bool = True
    max_channel_retries: int = 3

    @classmethod
    def from_env(cls) -> "AutoCrawlConfig":
        def int_or_none(name: str, default: int | None) -> int | None:
            raw = os.getenv(name)
            if raw is None or raw == "":
                return default
            if raw.lower() in {"none", "0", "false"} and name == "AUTO_CRAWL_MAX_VIDEO_AGE_HOURS":
                return None
            return int(raw)

        return cls(
            channels_per_batch=int(os.getenv("AUTO_CRAWL_CHANNELS_PER_BATCH", "20")),
            max_videos_per_channel=int(os.getenv("AUTO_CRAWL_MAX_VIDEOS_PER_CHANNEL", "20")),
            max_video_age_hours=int_or_none("AUTO_CRAWL_MAX_VIDEO_AGE_HOURS", 72),
            min_view_count=int(os.getenv("AUTO_CRAWL_MIN_VIEW_COUNT", "0")),
            min_like_count=int(os.getenv("AUTO_CRAWL_MIN_LIKE_COUNT", "0")),
            min_duration_sec=int_or_none("AUTO_CRAWL_MIN_DURATION_SEC", None),
            max_duration_sec=int_or_none("AUTO_CRAWL_MAX_DURATION_SEC", 600),
            download_new_videos=os.getenv("AUTO_CRAWL_DOWNLOAD_NEW", "true").lower() != "false",
            max_channel_retries=int(os.getenv("AUTO_CRAWL_MAX_CHANNEL_RETRIES", "3")),
        )


class ChannelDeletedDuringCrawl(Exception):
    """Stop an in-flight channel crawl after the operator deletes it."""


@dataclass
class VideoCandidate:
    video_id: str
    douyin_url: str
    title: str = ""
    description: str = ""
    author_nickname: str = ""
    author_uid: str = ""
    published_at: float | None = None
    view_count: int = 0
    like_count: int = 0
    comment_count: int = 0
    share_count: int = 0
    duration_sec: int = 0
    raw_data: dict[str, Any] | None = None

    @property
    def metadata_fingerprint(self) -> str:
        basis = f"{self.author_uid}|{int(self.published_at or 0)}|{self.duration_sec}|{self.title[:80]}"
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()


class AutoCrawlDatabase:
    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path or os.getenv("AUTO_CRAWL_DB_PATH", "/app/data/autocrawl.db"))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self.connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tracked_channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile_url TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    douyin_uid TEXT,
                    status TEXT DEFAULT 'active' CHECK (status IN ('active','paused','error','banned')),
                    last_scraped_at REAL,
                    last_video_id TEXT,
                    error_count INTEGER DEFAULT 0,
                    error_message TEXT,
                    created_at REAL DEFAULT (strftime('%s','now')),
                    updated_at REAL DEFAULT (strftime('%s','now')),
                    metadata TEXT DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_channels_status ON tracked_channels(status);
                CREATE INDEX IF NOT EXISTS idx_channels_last_scraped ON tracked_channels(last_scraped_at);

                CREATE TABLE IF NOT EXISTS videos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id TEXT NOT NULL UNIQUE,
                    channel_id INTEGER NOT NULL,
                    title TEXT,
                    description TEXT,
                    douyin_url TEXT,
                    author_nickname TEXT,
                    author_uid TEXT,
                    view_count INTEGER DEFAULT 0,
                    like_count INTEGER DEFAULT 0,
                    comment_count INTEGER DEFAULT 0,
                    share_count INTEGER DEFAULT 0,
                    duration_sec INTEGER DEFAULT 0,
                    local_path TEXT,
                    file_size_mb REAL,
                    file_hash TEXT,
                    metadata_fingerprint TEXT,
                    download_status TEXT DEFAULT 'pending' CHECK (download_status IN ('pending','downloading','completed','failed','skipped')),
                    retry_count INTEGER DEFAULT 0,
                    published_at REAL,
                    scraped_at REAL DEFAULT (strftime('%s','now')),
                    downloaded_at REAL,
                    skip_reason TEXT,
                    raw_data TEXT DEFAULT '{}',
                    FOREIGN KEY(channel_id) REFERENCES tracked_channels(id)
                );
                CREATE INDEX IF NOT EXISTS idx_videos_channel_id ON videos(channel_id);
                CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(download_status);
                CREATE INDEX IF NOT EXISTS idx_videos_published_at ON videos(published_at);
                CREATE INDEX IF NOT EXISTS idx_videos_file_hash ON videos(file_hash);
                CREATE INDEX IF NOT EXISTS idx_videos_fingerprint ON videos(metadata_fingerprint);

                CREATE TABLE IF NOT EXISTS subtitle_cues (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id TEXT NOT NULL,
                    cue_index INTEGER NOT NULL,
                    start_time REAL NOT NULL CHECK (start_time >= 0),
                    end_time REAL NOT NULL CHECK (end_time > start_time),
                    source_text TEXT NOT NULL DEFAULT '',
                    translated_text TEXT NOT NULL,
                    translation_model TEXT,
                    created_at REAL DEFAULT (strftime('%s','now')),
                    updated_at REAL DEFAULT (strftime('%s','now')),
                    FOREIGN KEY(video_id) REFERENCES videos(video_id) ON DELETE CASCADE,
                    UNIQUE(video_id, cue_index)
                );
                CREATE INDEX IF NOT EXISTS idx_subtitle_cues_video_time
                    ON subtitle_cues(video_id, start_time, cue_index);

                CREATE TABLE IF NOT EXISTS scraper_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at REAL DEFAULT (strftime('%s','now')),
                    ended_at REAL,
                    total_channels INTEGER DEFAULT 0,
                    success_count INTEGER DEFAULT 0,
                    fail_count INTEGER DEFAULT 0,
                    new_videos INTEGER DEFAULT 0,
                    skipped_videos INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'running' CHECK (status IN ('running','completed','failed','interrupted')),
                    error_message TEXT
                );

                CREATE TABLE IF NOT EXISTS download_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id TEXT NOT NULL UNIQUE,
                    video_url TEXT NOT NULL,
                    priority INTEGER DEFAULT 5,
                    status TEXT DEFAULT 'queued' CHECK (status IN ('queued','processing','done','failed')),
                    attempts INTEGER DEFAULT 0,
                    max_attempts INTEGER DEFAULT 3,
                    last_error TEXT,
                    created_at REAL DEFAULT (strftime('%s','now')),
                    processed_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_queue_status ON download_queue(status);

                CREATE TABLE IF NOT EXISTS system_config (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at REAL DEFAULT (strftime('%s','now'))
                );
                """
            )
            self._ensure_columns(conn, "tracked_channels", {
                "interval_minutes": "INTEGER NOT NULL DEFAULT 60",
                "schedule_enabled": "INTEGER NOT NULL DEFAULT 1",
                "next_run_at": "REAL",
                "last_run_at": "REAL",
                "auto_upload_enabled": "INTEGER NOT NULL DEFAULT 0",
                # Preserve mandatory translation for existing rows during migration.
                # add_channel writes an explicit opt-in value for every new row.
                "translate_enabled": "INTEGER NOT NULL DEFAULT 1",
                "is_deleted": "INTEGER NOT NULL DEFAULT 0",
            })
            self._ensure_columns(conn, "videos", {
                "is_starred": "INTEGER NOT NULL DEFAULT 0",
                "auto_upload_status": "TEXT NOT NULL DEFAULT 'disabled'",
                "auto_upload_job_id": "TEXT",
                "auto_upload_error": "TEXT",
                "auto_upload_updated_at": "REAL",
                "download_error": "TEXT",
                "translated_title": "TEXT",
                "translated_at": "REAL",
                "subtitle_status": "TEXT NOT NULL DEFAULT 'not_started'",
                "subtitled_video_path": "TEXT",
                "subtitle_model": "TEXT",
                "subtitle_cue_count": "INTEGER",
                "subtitle_completed_at": "REAL",
                "subtitle_error": "TEXT",
                "media_type": "TEXT NOT NULL DEFAULT 'video'",
                "asset_count": "INTEGER NOT NULL DEFAULT 1",
                "music_path": "TEXT",
            })
            conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_channels_visible_status_id
                    ON tracked_channels(is_deleted, status, id DESC);
                CREATE INDEX IF NOT EXISTS idx_channels_schedule_due
                    ON tracked_channels(is_deleted, status, schedule_enabled, next_run_at, id);
                CREATE INDEX IF NOT EXISTS idx_videos_dashboard_order
                    ON videos(is_starred DESC, scraped_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_videos_status_dashboard_order
                    ON videos(download_status, is_starred DESC, scraped_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_queue_status_priority_id
                    ON download_queue(status, priority, id);
                """
            )
            conn.execute(
                """UPDATE videos
                   SET download_error=(SELECT q.last_error FROM download_queue q WHERE q.video_id=videos.video_id)
                   WHERE download_status='failed' AND (download_error IS NULL OR download_error='')
                     AND EXISTS (SELECT 1 FROM download_queue q WHERE q.video_id=videos.video_id AND q.last_error IS NOT NULL)"""
            )
            for key, value in {
                "min_view_count": "0",
                "min_like_count": "0",
                "max_video_age_hours": "72",
                "download_concurrency": "1",
            }.items():
                conn.execute("INSERT OR IGNORE INTO system_config(key, value) VALUES (?, ?)", (key, value))

    @staticmethod
    def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def _row(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row else None

    def add_channel(
        self,
        profile_url: str,
        display_name: str,
        douyin_uid: str | None = None,
        interval_minutes: int = 60,
        schedule_enabled: bool = True,
        auto_upload_enabled: bool = False,
        translate_enabled: bool = False,
    ) -> dict[str, Any]:
        if "douyin.com" not in profile_url:
            raise ValueError("URL kênh phải là douyin.com")
        interval = max(1, int(interval_minutes))
        next_run_at = time.time() + interval * 60 if schedule_enabled else None
        with self.connection() as conn:
            archived = conn.execute("SELECT id FROM tracked_channels WHERE profile_url=? AND is_deleted=1", (profile_url.strip(),)).fetchone()
            if archived:
                next_run_at = time.time() + interval * 60 if schedule_enabled else None
                conn.execute("""UPDATE tracked_channels SET display_name=?, douyin_uid=?, status='active', interval_minutes=?, schedule_enabled=?, next_run_at=?, auto_upload_enabled=?, translate_enabled=?, is_deleted=0, updated_at=? WHERE id=?""", (display_name.strip(), douyin_uid, interval, int(schedule_enabled), next_run_at, int(auto_upload_enabled), int(translate_enabled), time.time(), archived["id"]))
                row = conn.execute("SELECT * FROM tracked_channels WHERE id=?", (archived["id"],)).fetchone()
                return dict(row) if row else {}
            cur = conn.execute(
                """INSERT INTO tracked_channels(
                    profile_url, display_name, douyin_uid, interval_minutes, schedule_enabled, next_run_at, auto_upload_enabled, translate_enabled
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (profile_url.strip(), display_name.strip(), douyin_uid, interval, int(schedule_enabled), next_run_at, int(auto_upload_enabled), int(translate_enabled)),
            )
            row = conn.execute("SELECT * FROM tracked_channels WHERE id=?", (cur.lastrowid,)).fetchone()
            return dict(row) if row else {}

    def list_channels(self, status: str | None = None) -> list[dict[str, Any]]:
        with self.connection() as conn:
            if status and status != "all":
                rows = conn.execute("SELECT * FROM tracked_channels WHERE status=? AND is_deleted=0 ORDER BY id DESC", (status,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM tracked_channels WHERE is_deleted=0 ORDER BY id DESC").fetchall()
            return [dict(r) for r in rows]

    @staticmethod
    def _pagination(page: int, page_size: int, total: int) -> tuple[int, int, int, dict[str, Any]]:
        safe_page = max(1, int(page or 1))
        safe_size = min(8, max(1, int(page_size or 8)))
        total_pages = max(1, math.ceil(total / safe_size))
        safe_page = min(safe_page, total_pages)
        offset = (safe_page - 1) * safe_size
        return safe_page, safe_size, offset, {
            "page": safe_page,
            "page_size": safe_size,
            "total": int(total),
            "total_pages": total_pages,
            "has_previous": safe_page > 1,
            "has_next": safe_page < total_pages,
        }

    def paginate_channels(self, page: int = 1, page_size: int = 8, status: str | None = None) -> dict[str, Any]:
        with self.connection() as conn:
            clauses = ["is_deleted=0"]
            params: list[Any] = []
            if status and status != "all":
                clauses.append("status=?")
                params.append(status)
            where = " AND ".join(clauses)
            total = int(conn.execute(f"SELECT COUNT(*) FROM tracked_channels WHERE {where}", params).fetchone()[0])
            _, safe_size, offset, pagination = self._pagination(page, page_size, total)
            rows = conn.execute(
                f"SELECT * FROM tracked_channels WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                (*params, safe_size, offset),
            ).fetchall()
            return {"items": [dict(row) for row in rows], "pagination": pagination}

    def get_channel(self, channel_id: int) -> dict[str, Any] | None:
        with self.connection() as conn:
            return self._row(conn.execute("SELECT * FROM tracked_channels WHERE id=?", (channel_id,)).fetchone())

    def pause_channel(self, channel_id: int) -> None:
        self.update_channel(channel_id, status="paused")

    def resume_channel(self, channel_id: int) -> None:
        self.update_channel(channel_id, status="active", error_count=0, error_message=None)

    def update_channel(self, channel_id: int, **fields: Any) -> None:
        allowed = {"status", "last_scraped_at", "last_video_id", "error_count", "error_message", "douyin_uid", "metadata", "interval_minutes", "schedule_enabled", "next_run_at", "last_run_at", "auto_upload_enabled", "translate_enabled", "is_deleted"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        updates["updated_at"] = time.time()
        sql = ", ".join(f"{k}=?" for k in updates)
        with self.connection() as conn:
            conn.execute(f"UPDATE tracked_channels SET {sql} WHERE id=?", (*updates.values(), channel_id))

    def delete_channel(self, channel_id: int) -> dict[str, Any] | None:
        channel = self.get_channel(channel_id)
        if not channel or channel.get("is_deleted"):
            return None
        self.update_channel(channel_id, is_deleted=1, status="paused", schedule_enabled=0, next_run_at=None)
        return self.get_channel(channel_id)

    def update_channel_schedule(self, channel_id: int, interval_minutes: int, schedule_enabled: bool) -> None:
        interval = max(1, int(interval_minutes))
        self.update_channel(
            channel_id,
            interval_minutes=interval,
            schedule_enabled=int(schedule_enabled),
            next_run_at=time.time() + interval * 60 if schedule_enabled else None,
        )

    def list_due_channels(self, now: float | None = None) -> list[dict[str, Any]]:
        current = time.time() if now is None else now
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT * FROM tracked_channels
                   WHERE status='active' AND schedule_enabled=1 AND is_deleted=0
                     AND (next_run_at IS NULL OR next_run_at<=?)
                   ORDER BY COALESCE(next_run_at, 0), id""",
                (current,),
            ).fetchall()
            return [dict(row) for row in rows]

    def mark_channel_scheduled_run(self, channel_id: int) -> None:
        channel = self.get_channel(channel_id)
        if not channel:
            return
        interval = max(1, int(channel.get("interval_minutes") or 60))
        now = time.time()
        self.update_channel(channel_id, last_run_at=now, next_run_at=now + interval * 60)

    def create_session(self) -> int:
        with self.connection() as conn:
            cur = conn.execute("INSERT INTO scraper_sessions(status) VALUES ('running')")
            return int(cur.lastrowid)

    def update_session_progress(self, session_id: int, **fields: Any) -> None:
        allowed = {"total_channels", "success_count", "fail_count", "new_videos", "skipped_videos", "error_message"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        sql = ", ".join(f"{key}=?" for key in updates)
        with self.connection() as conn:
            conn.execute(f"UPDATE scraper_sessions SET {sql} WHERE id=?", (*updates.values(), session_id))

    def close_session(self, session_id: int, **fields: Any) -> dict[str, Any]:
        fields.setdefault("ended_at", time.time())
        if "status" not in fields:
            fields["status"] = "completed"
        sql = ", ".join(f"{k}=?" for k in fields)
        with self.connection() as conn:
            conn.execute(f"UPDATE scraper_sessions SET {sql} WHERE id=?", (*fields.values(), session_id))
        return self.get_session(session_id) or {}

    def get_session(self, session_id: int) -> dict[str, Any] | None:
        with self.connection() as conn:
            return self._row(conn.execute("SELECT * FROM scraper_sessions WHERE id=?", (session_id,)).fetchone())

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM scraper_sessions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]

    def paginate_sessions(self, page: int = 1, page_size: int = 8) -> dict[str, Any]:
        with self.connection() as conn:
            total = int(conn.execute("SELECT COUNT(*) FROM scraper_sessions").fetchone()[0])
            _, safe_size, offset, pagination = self._pagination(page, page_size, total)
            rows = conn.execute("SELECT * FROM scraper_sessions ORDER BY id DESC LIMIT ? OFFSET ?", (safe_size, offset)).fetchall()
            return {"items": [dict(row) for row in rows], "pagination": pagination}

    def video_exists(self, video_id: str, fingerprint: str | None = None) -> bool:
        with self.connection() as conn:
            if fingerprint:
                row = conn.execute("SELECT 1 FROM videos WHERE video_id=? OR metadata_fingerprint=?", (video_id, fingerprint)).fetchone()
            else:
                row = conn.execute("SELECT 1 FROM videos WHERE video_id=?", (video_id,)).fetchone()
            return row is not None

    def record_video(self, channel_id: int, video: VideoCandidate, status: str = "pending", skip_reason: str | None = None) -> dict[str, Any]:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO videos(
                    video_id, channel_id, title, description, douyin_url, author_nickname, author_uid,
                    view_count, like_count, comment_count, share_count, duration_sec, metadata_fingerprint,
                    download_status, published_at, skip_reason, raw_data
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    video.video_id, channel_id, video.title, video.description, video.douyin_url,
                    video.author_nickname, video.author_uid, video.view_count, video.like_count,
                    video.comment_count, video.share_count, video.duration_sec, video.metadata_fingerprint,
                    status, video.published_at, skip_reason, json.dumps(video.raw_data or {}, ensure_ascii=False),
                ),
            )
            if status == "pending":
                conn.execute(
                    "INSERT OR IGNORE INTO download_queue(video_id, video_url) VALUES (?, ?)",
                    (video.video_id, video.douyin_url),
                )
        return self.get_video(video.video_id) or {}

    def get_video(self, video_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            return self._row(conn.execute("SELECT * FROM videos WHERE video_id=?", (video_id,)).fetchone())

    def list_videos(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM videos ORDER BY is_starred DESC, scraped_at DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]

    def paginate_videos(
        self,
        page: int = 1,
        page_size: int = 8,
        *,
        status: str | None = None,
        query: str | None = None,
        sort: str = "newest",
    ) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if status and status != "all":
            clauses.append("v.download_status=?")
            params.append(status)
        cleaned_query = str(query or "").strip().lower()
        if cleaned_query:
            clauses.append("(LOWER(COALESCE(v.title,'')) LIKE ? OR LOWER(v.video_id) LIKE ? OR LOWER(COALESCE(c.display_name,'')) LIKE ?)")
            needle = f"%{cleaned_query}%"
            params.extend([needle, needle, needle])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order_by = {
            "oldest": "v.is_starred DESC, v.scraped_at ASC, v.id ASC",
            "views": "v.is_starred DESC, v.view_count DESC, v.scraped_at DESC, v.id DESC",
        }.get(sort, "v.is_starred DESC, v.scraped_at DESC, v.id DESC")
        with self.connection() as conn:
            total = int(conn.execute(
                f"SELECT COUNT(*) FROM videos v JOIN tracked_channels c ON c.id=v.channel_id {where}",
                params,
            ).fetchone()[0])
            _, safe_size, offset, pagination = self._pagination(page, page_size, total)
            rows = conn.execute(
                f"SELECT v.*, c.display_name AS channel_display_name, c.auto_upload_enabled AS channel_auto_upload_enabled, c.translate_enabled AS channel_translate_enabled FROM videos v JOIN tracked_channels c ON c.id=v.channel_id {where} ORDER BY {order_by} LIMIT ? OFFSET ?",
                (*params, safe_size, offset),
            ).fetchall()
            return {"items": [dict(row) for row in rows], "pagination": pagination}

    def set_video_starred(self, video_id: str, is_starred: bool) -> dict[str, Any]:
        with self.connection() as conn:
            conn.execute("UPDATE videos SET is_starred=? WHERE video_id=?", (int(is_starred), video_id))
        return self.get_video(video_id) or {}

    def update_video_translated_title(self, video_id: str, translated_title: str) -> dict[str, Any]:
        cleaned_title = str(translated_title or "").strip()
        if not cleaned_title:
            raise ValueError("Translated title must not be empty")
        with self.connection() as conn:
            conn.execute(
                "UPDATE videos SET translated_title=?, translated_at=? WHERE video_id=?",
                (cleaned_title, time.time(), video_id),
            )
        return self.get_video(video_id) or {}

    def update_video_subtitle(
        self,
        video_id: str,
        *,
        status: str,
        video_path: str | None = None,
        model: str | None = None,
        cue_count: int | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"not_started", "processing", "completed", "failed"}:
            raise ValueError("Invalid subtitle status")
        cleaned_path = str(video_path or "").strip() or None
        if status == "completed":
            if not cleaned_path or not cleaned_path.endswith(".mp4") or not Path(cleaned_path).is_file():
                raise ValueError("Completed subtitle video must be an existing MP4 file")
            error = None
        completed_at = time.time() if status == "completed" else None
        with self.connection() as conn:
            conn.execute(
                """UPDATE videos
                   SET subtitle_status=?, subtitled_video_path=?, subtitle_model=?,
                       subtitle_cue_count=?, subtitle_completed_at=?, subtitle_error=?
                   WHERE video_id=?""",
                (status, cleaned_path, model, cue_count, completed_at, error, video_id),
            )
        return self.get_video(video_id) or {}

    def list_subtitle_cues(self, video_id: str) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT cue_index, start_time, end_time, source_text,
                          translated_text, translation_model, created_at, updated_at
                   FROM subtitle_cues
                   WHERE video_id=?
                   ORDER BY cue_index""",
                (video_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def replace_subtitle_cues(
        self,
        video_id: str,
        cues: list[dict[str, Any]],
        *,
        model: str | None = None,
    ) -> list[dict[str, Any]]:
        prepared: list[tuple[Any, ...]] = []
        indexes: set[int] = set()
        now = time.time()
        for cue in cues:
            cue_index = int(cue.get("cue_index", cue.get("id", 0)))
            start_time = float(cue.get("start_time", cue.get("start", -1)))
            end_time = float(cue.get("end_time", cue.get("end", -1)))
            translated_text = str(cue.get("translated_text", cue.get("vi", ""))).strip()
            source_text = str(cue.get("source_text", cue.get("zh", ""))).strip()
            if cue_index <= 0 or cue_index in indexes:
                raise ValueError("Subtitle cue indexes must be unique positive integers")
            if not math.isfinite(start_time) or not math.isfinite(end_time) or start_time < 0 or end_time <= start_time:
                raise ValueError("Subtitle cue timestamps are invalid")
            if not translated_text:
                raise ValueError("Translated subtitle text must not be empty")
            indexes.add(cue_index)
            prepared.append(
                (
                    video_id,
                    cue_index,
                    start_time,
                    end_time,
                    source_text,
                    translated_text,
                    str(cue.get("translation_model") or model or "").strip() or None,
                    now,
                    now,
                )
            )

        with self.connection() as conn:
            if not conn.execute("SELECT 1 FROM videos WHERE video_id=?", (video_id,)).fetchone():
                raise ValueError("Video not found")
            conn.execute("DELETE FROM subtitle_cues WHERE video_id=?", (video_id,))
            conn.executemany(
                """INSERT INTO subtitle_cues(
                       video_id, cue_index, start_time, end_time, source_text,
                       translated_text, translation_model, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                prepared,
            )
        return self.list_subtitle_cues(video_id)

    def update_video_auto_upload(self, video_id: str, status: str, job_id: str | None = None, error: str | None = None) -> dict[str, Any]:
        with self.connection() as conn:
            conn.execute(
                "UPDATE videos SET auto_upload_status=?, auto_upload_job_id=?, auto_upload_error=?, auto_upload_updated_at=? WHERE video_id=?",
                (status, job_id, error, time.time(), video_id),
            )
        return self.get_video(video_id) or {}

    def delete_video(self, video_id: str) -> dict[str, Any] | None:
        video = self.get_video(video_id)
        if not video:
            return None
        with self.connection() as conn:
            conn.execute("DELETE FROM download_queue WHERE video_id=?", (video_id,))
            conn.execute("DELETE FROM videos WHERE video_id=?", (video_id,))
        return video

    def update_video_downloaded(
        self,
        video_id: str,
        local_path: str,
        file_size_mb: float,
        file_hash: str | None = None,
        media_type: str = "video",
        asset_count: int = 1,
        music_path: str | None = None,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE videos SET download_status='completed', local_path=?, file_size_mb=?, file_hash=?, media_type=?, asset_count=?, music_path=?, downloaded_at=?, download_error=NULL WHERE video_id=?",
                (local_path, file_size_mb, file_hash, media_type, asset_count, music_path, time.time(), video_id),
            )
            conn.execute("UPDATE download_queue SET status='done', processed_at=? WHERE video_id=?", (time.time(), video_id))

    def mark_video_failed(self, video_id: str, error: str) -> None:
        with self.connection() as conn:
            conn.execute("UPDATE videos SET download_status='failed', download_error=?, retry_count=retry_count+1 WHERE video_id=?", (error, video_id))
            conn.execute("UPDATE download_queue SET status='failed', attempts=attempts+1, last_error=?, processed_at=? WHERE video_id=?", (error, time.time(), video_id))

    def reset_video_download_for_retry(self, video_id: str) -> dict[str, Any]:
        with self.connection() as conn:
            conn.execute(
                "UPDATE videos SET download_status='pending', download_error=NULL WHERE video_id=?",
                (video_id,),
            )
            video = conn.execute("SELECT douyin_url FROM videos WHERE video_id=?", (video_id,)).fetchone()
            if video:
                conn.execute(
                    """INSERT INTO download_queue(video_id, video_url, status, attempts, last_error, processed_at)
                       VALUES (?, ?, 'queued', 0, NULL, NULL)
                       ON CONFLICT(video_id) DO UPDATE SET
                           video_url=excluded.video_url,
                           status='queued',
                           last_error=NULL,
                           processed_at=NULL""",
                    (video_id, video["douyin_url"]),
                )
        return self.get_video(video_id) or {}

    def list_queue(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self.connection() as conn:
            if status:
                rows = conn.execute("SELECT * FROM download_queue WHERE status=? ORDER BY priority, id LIMIT ?", (status, limit)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM download_queue ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]

    def paginate_queue(self, page: int = 1, page_size: int = 8, status: str | None = None) -> dict[str, Any]:
        with self.connection() as conn:
            where = "WHERE status=?" if status else ""
            params: tuple[Any, ...] = (status,) if status else ()
            total = int(conn.execute(f"SELECT COUNT(*) FROM download_queue {where}", params).fetchone()[0])
            _, safe_size, offset, pagination = self._pagination(page, page_size, total)
            order_by = "priority, id" if status else "id DESC"
            rows = conn.execute(
                f"SELECT * FROM download_queue {where} ORDER BY {order_by} LIMIT ? OFFSET ?",
                (*params, safe_size, offset),
            ).fetchall()
            return {"items": [dict(row) for row in rows], "pagination": pagination}

    def set_config(self, key: str, value: str) -> None:
        with self.connection() as conn:
            conn.execute("INSERT INTO system_config(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at", (key, value, time.time()))

    def get_config(self, key: str) -> str | None:
        with self.connection() as conn:
            row = conn.execute("SELECT value FROM system_config WHERE key=?", (key,)).fetchone()
            return row["value"] if row else None

    def stats(self) -> dict[str, Any]:
        with self.connection() as conn:
            def one(sql: str, params: tuple = ()) -> int:
                return int(conn.execute(sql, params).fetchone()[0] or 0)
            return {
                "channels": {
                    "total": one("SELECT COUNT(*) FROM tracked_channels WHERE is_deleted=0"),
                    "active": one("SELECT COUNT(*) FROM tracked_channels WHERE status='active' AND is_deleted=0"),
                    "paused": one("SELECT COUNT(*) FROM tracked_channels WHERE status='paused' AND is_deleted=0"),
                    "error": one("SELECT COUNT(*) FROM tracked_channels WHERE status='error' AND is_deleted=0"),
                },
                "videos": {
                    "total": one("SELECT COUNT(*) FROM videos"),
                    "completed": one("SELECT COUNT(*) FROM videos WHERE download_status='completed'"),
                    "pending": one("SELECT COUNT(*) FROM videos WHERE download_status='pending'"),
                    "failed": one("SELECT COUNT(*) FROM videos WHERE download_status='failed'"),
                },
                "queue": {
                    "queued": one("SELECT COUNT(*) FROM download_queue WHERE status='queued'"),
                    "done": one("SELECT COUNT(*) FROM download_queue WHERE status='done'"),
                    "failed": one("SELECT COUNT(*) FROM download_queue WHERE status='failed'"),
                },
            }


class DouyinChannelProvider:
    def __init__(self, cookie_manager: CookieManager | None = None, crawler: Any | None = None):
        self.cookie_manager = cookie_manager or CookieManager()
        self.crawler = crawler

    async def _crawler(self):
        if self.crawler is not None:
            return self.crawler
        from crawlers.douyin.web.web_crawler import DouyinWebCrawler
        self.cookie_manager.apply_to_vendor_crawler()
        self.crawler = DouyinWebCrawler()
        return self.crawler

    async def fetch_channel_videos(self, channel: dict[str, Any], config: AutoCrawlConfig) -> list[VideoCandidate]:
        crawler = await self._crawler()
        sec_user_id = channel.get("douyin_uid")
        if not sec_user_id:
            sec_user_id = await crawler.get_sec_user_id(channel["profile_url"])
        data = await crawler.fetch_user_post_videos(sec_user_id, 0, config.max_videos_per_channel)
        aweme_list = data.get("aweme_list") or data.get("data", {}).get("aweme_list") or []
        return [candidate for item in aweme_list if (candidate := self._parse_aweme(item))]

    def _parse_aweme(self, item: dict[str, Any]) -> VideoCandidate | None:
        video_id = str(item.get("aweme_id") or item.get("group_id") or "").strip()
        if not video_id:
            return None
        stats = item.get("statistics") or {}
        author = item.get("author") or {}
        video = item.get("video") or {}
        duration_ms = int(video.get("duration") or item.get("duration") or 0)
        return VideoCandidate(
            video_id=video_id,
            douyin_url=f"https://www.douyin.com/video/{video_id}",
            title=(item.get("desc") or item.get("title") or "")[:220],
            description=item.get("desc") or "",
            author_nickname=author.get("nickname") or "",
            author_uid=str(author.get("uid") or author.get("sec_uid") or ""),
            published_at=float(item.get("create_time") or 0) or None,
            view_count=int(stats.get("play_count") or 0),
            like_count=int(stats.get("digg_count") or 0),
            comment_count=int(stats.get("comment_count") or 0),
            share_count=int(stats.get("share_count") or 0),
            duration_sec=duration_ms // 1000 if duration_ms > 1000 else duration_ms,
            raw_data=item,
        )


class TikTokAutoUploader:
    def __init__(self, endpoint: str | None = None, photo_endpoint: str | None = None, translator: Any | None = None, client: Any | None = None):
        service_url = os.getenv("TIKTOK_UPLOAD_SERVICE_URL", "http://tiktok-uploader:8001").rstrip("/")
        self.endpoint = endpoint or os.getenv("TIKTOK_UPLOAD_INTERNAL_URL", f"{service_url}/api/upload/jobs")
        self.photo_endpoint = photo_endpoint or os.getenv("TIKTOK_PHOTO_UPLOAD_INTERNAL_URL", f"{service_url}/api/upload/photo-jobs")
        self.translator = translator or AgentRouterTitleTranslator()
        self.client = client

    async def translate_title(self, video: dict[str, Any]) -> str:
        return await self.translator.translate_title(str(video.get("title") or ""))

    async def upload_translated(self, video: dict[str, Any], translated_title: str) -> dict[str, Any]:
        translated_title = str(translated_title or "").split("#", 1)[0].strip()
        if str(video.get("media_type") or "video") == "photo":
            photo_id = str(video.get("video_id") or "").strip()
            if not photo_id:
                raise RuntimeError("Downloaded photo ID is missing")
            payload = {
                "photo_id": photo_id,
                "account": "main_tiktok",
                "caption": translated_title or "Bài ảnh mới",
            }
            target_endpoint = self.photo_endpoint
        else:
            filename = Path(str(video.get("local_path") or "")).name
            if not filename:
                raise RuntimeError("Downloaded video filename is missing")
            payload = {
                "filename": filename,
                "account": "main_tiktok",
                "caption": translated_title or "Video mới",
                "options": {"visibility_type": 0, "allow_comment": 1, "allow_duet": 0, "allow_stitch": 0},
            }
            target_endpoint = self.endpoint
        if self.client is not None:
            response = await self.client.post(target_endpoint, json=payload, timeout=90)
        else:
            async with httpx.AsyncClient(timeout=90) as client:
                response = await client.post(target_endpoint, json=payload)
        response.raise_for_status()
        result = response.json()
        result["translated_title"] = translated_title
        return result

    async def upload(self, video: dict[str, Any]) -> dict[str, Any]:
        translated_title = str(video.get("translated_title") or "").strip()
        if not translated_title:
            translated_title = await self.translate_title(video)
        return await self.upload_translated(video, translated_title)


class AutoCrawlManager:
    def __init__(self, db: AutoCrawlDatabase | None = None, provider: Any | None = None, downloader: Any | None = None, auto_uploader: Any | None = None, config: AutoCrawlConfig | None = None):
        self.db = db or AutoCrawlDatabase()
        self.config = config or AutoCrawlConfig.from_env()
        self.provider = provider or DouyinChannelProvider()
        self.downloader = downloader or DownloadService()
        self.auto_uploader = auto_uploader or TikTokAutoUploader()

    def run_once_sync(self, channel_id: int | None = None, download: bool | None = None) -> dict[str, Any]:
        return asyncio.run(self.run_once(channel_id=channel_id, download=download))

    def process_queue_sync(self, limit: int = 20) -> dict[str, int]:
        return asyncio.run(self.process_queue(limit=limit))

    def prepare_video_retry(self, video_id: str) -> tuple[str, dict[str, Any]]:
        video = self.db.get_video(video_id)
        if not video:
            raise ValueError("Video not found")
        channel = self.db.get_channel(int(video["channel_id"]))
        if not channel or channel.get("is_deleted"):
            raise ValueError("Channel was deleted")
        if video.get("download_status") in {"pending", "downloading"} or video.get("auto_upload_status") in {"queued", "running"}:
            raise RuntimeError("Video action is already running")
        if video.get("download_status") != "completed" and video.get("download_error"):
            return "download", self.db.reset_video_download_for_retry(video_id)
        if video.get("download_status") == "completed" and (video.get("auto_upload_error") or video.get("auto_upload_status") == "failed"):
            return "upload", self.db.update_video_auto_upload(video_id, status="queued", job_id=None, error=None)
        raise RuntimeError("Video has no retryable error")

    async def retry_video(self, video_id: str, action: str) -> None:
        video = self.db.get_video(video_id)
        if not video:
            return
        if action == "download":
            raw_data = video.get("raw_data") or {}
            if isinstance(raw_data, str):
                try:
                    raw_data = json.loads(raw_data)
                except ValueError:
                    raw_data = {}
            candidate = VideoCandidate(
                video_id=video_id,
                douyin_url=str(video.get("douyin_url") or ""),
                title=str(video.get("title") or ""),
                author_uid=str(video.get("author_uid") or ""),
                published_at=video.get("published_at"),
                view_count=int(video.get("view_count") or 0),
                like_count=int(video.get("like_count") or 0),
                duration_sec=int(video.get("duration_sec") or 0),
                raw_data=raw_data if isinstance(raw_data, dict) else {},
            )
            try:
                await self._download_candidate(candidate, channel_id=int(video["channel_id"]))
            except ChannelDeletedDuringCrawl:
                return
            video = self.db.get_video(video_id)
            if not video or video.get("download_status") != "completed":
                return
            await self._auto_upload_if_enabled(video_id)
            return
        await self._auto_upload_if_enabled(video_id, force=True)

    async def _auto_upload_if_enabled(self, video_id: str, force: bool = False) -> None:
        video = self.db.get_video(video_id)
        if not video or video.get("download_status") != "completed":
            return
        channel = self.db.get_channel(int(video["channel_id"]))
        if not channel or channel.get("is_deleted") or (not force and not channel.get("auto_upload_enabled")):
            return
        if video.get("auto_upload_status") in {"running", "success"}:
            return
        self.db.update_video_auto_upload(video_id, status="queued", error=None)
        try:
            current_video = self.db.get_video(video_id) or video
            translate_title = getattr(self.auto_uploader, "translate_title", None)
            upload_translated = getattr(self.auto_uploader, "upload_translated", None)
            if callable(translate_title) and callable(upload_translated):
                if channel.get("translate_enabled"):
                    stored_title = str(current_video.get("translated_title") or "").strip()
                    translated_title = stored_title.split("#", 1)[0].strip()
                    if translated_title and translated_title != stored_title:
                        current_video = self.db.update_video_translated_title(video_id, translated_title)
                    if not translated_title:
                        translated_title = str(await translate_title(current_video)).split("#", 1)[0].strip()
                        current_video = self.db.update_video_translated_title(video_id, translated_title)
                    job = await upload_translated(current_video, translated_title)
                else:
                    # Skipping translation is an intentional channel policy, not
                    # a translation failure. Source hashtags are still removed.
                    original_caption = str(current_video.get("title") or "").split("#", 1)[0].strip()
                    if not original_caption:
                        original_caption = "Bài ảnh mới" if current_video.get("media_type") == "photo" else "Video mới"
                    job = await upload_translated(current_video, original_caption)
            else:
                job = await self.auto_uploader.upload(current_video)
                translated_title = str(job.get("translated_title") or "").strip()
                if translated_title:
                    self.db.update_video_translated_title(video_id, translated_title)
            status = str(job.get("status") or "running")
            error = str(job.get("error") or "") or None
            self.db.update_video_auto_upload(video_id, status=status, job_id=str(job.get("id") or "") or None, error=error)
        except Exception as exc:
            self.db.update_video_auto_upload(video_id, status="failed", error=str(exc))

    async def process_queue(self, limit: int = 20) -> dict[str, int]:
        rows = self.db.list_queue(status="queued", limit=limit)
        completed = failed = 0
        for row in rows:
            video = self.db.get_video(row["video_id"])
            candidate = VideoCandidate(
                video_id=row["video_id"],
                douyin_url=row["video_url"],
                title=(video or {}).get("title") or "",
                author_uid=(video or {}).get("author_uid") or "",
                published_at=(video or {}).get("published_at"),
                view_count=(video or {}).get("view_count") or 0,
                like_count=(video or {}).get("like_count") or 0,
                duration_sec=(video or {}).get("duration_sec") or 0,
            )
            before = self.db.get_video(candidate.video_id) or {}
            await self._download_candidate(candidate)
            after = self.db.get_video(candidate.video_id) or {}
            if after.get("download_status") == "completed" and before.get("download_status") != "completed":
                completed += 1
                await self._auto_upload_if_enabled(candidate.video_id)
            elif after.get("download_status") == "failed":
                failed += 1
        return {"processed": len(rows), "completed": completed, "failed": failed}

    async def run_once(self, channel_id: int | None = None, download: bool | None = None) -> dict[str, Any]:
        do_download = self.config.download_new_videos if download is None else bool(download)
        session_id = self.db.create_session()
        channels = [self.db.get_channel(channel_id)] if channel_id else self.db.list_channels(status="active")[: self.config.channels_per_batch]
        channels = [c for c in channels if c]
        success = fail = new_videos = skipped = 0
        error_message = None
        self.db.update_session_progress(session_id, total_channels=len(channels))
        for channel in channels:
            try:
                candidates = await self.provider.fetch_channel_videos(channel, self.config)
                first_crawl = not channel.get("last_scraped_at")
                has_recent_supported_video = any(
                    self._is_supported_media(candidate) and self._is_within_age_window(candidate)
                    for candidate in candidates
                )
                fallback_to_latest = bool(
                    first_crawl
                    and self.config.max_video_age_hours
                    and not has_recent_supported_video
                )
                ordered_candidates = sorted(
                    candidates,
                    key=lambda candidate: float(candidate.published_at or 0),
                    reverse=True,
                ) if fallback_to_latest else candidates
                fallback_collected = 0
                last_video_id = None
                for candidate_index, candidate in enumerate(ordered_candidates):
                    if self._channel_was_deleted(channel["id"]):
                        skipped += len(ordered_candidates) - candidate_index
                        error_message = "Đã dừng crawl vì job đã bị xóa"
                        break
                    if fallback_to_latest and fallback_collected >= 3:
                        skipped += 1
                        continue
                    should, reason = self._should_collect(candidate, ignore_age=fallback_to_latest)
                    if not should:
                        skipped += 1
                        continue
                    self.db.record_video(channel["id"], candidate, status="pending")
                    new_videos += 1
                    if fallback_to_latest:
                        fallback_collected += 1
                    last_video_id = candidate.video_id
                    if do_download:
                        try:
                            await self._download_candidate(candidate, channel_id=channel["id"])
                        except ChannelDeletedDuringCrawl:
                            skipped += len(ordered_candidates) - candidate_index - 1
                            error_message = "Đã dừng crawl vì job đã bị xóa"
                            break
                        if self._channel_was_deleted(channel["id"]):
                            skipped += len(ordered_candidates) - candidate_index - 1
                            error_message = "Đã dừng crawl vì job đã bị xóa"
                            break
                        await self._auto_upload_if_enabled(candidate.video_id)
                current_channel = self.db.get_channel(channel["id"])
                if current_channel and not current_channel.get("is_deleted"):
                    self.db.update_channel(channel["id"], status="active", last_scraped_at=time.time(), last_video_id=last_video_id or channel.get("last_video_id"), error_count=0, error_message=None)
                success += 1
            except Exception as exc:
                fail += 1
                error_message = str(exc)
                count = int(channel.get("error_count") or 0) + 1
                status = "paused" if count >= self.config.max_channel_retries else "error"
                self.db.update_channel(channel["id"], status=status, error_count=count, error_message=error_message, last_scraped_at=time.time())
            self.db.update_session_progress(
                session_id,
                total_channels=len(channels),
                success_count=success,
                fail_count=fail,
                new_videos=new_videos,
                skipped_videos=skipped,
                error_message=error_message,
            )
        status = "completed" if fail == 0 else "failed"
        return self.db.close_session(session_id, status=status, total_channels=len(channels), success_count=success, fail_count=fail, new_videos=new_videos, skipped_videos=skipped, error_message=error_message)

    @staticmethod
    def _candidate_aweme_type(video: VideoCandidate) -> int | Any:
        aweme_type = (video.raw_data or {}).get("aweme_type")
        if aweme_type is None:
            return None
        try:
            return int(aweme_type)
        except (TypeError, ValueError):
            return aweme_type

    def _is_supported_media(self, video: VideoCandidate) -> bool:
        aweme_type = self._candidate_aweme_type(video)
        if aweme_type is None:
            return True
        if aweme_type in {0, 4}:
            return True
        if aweme_type == 68:
            raw = video.raw_data or {}
            return bool(raw.get("images") or raw.get("image_infos") or raw.get("image_list"))
        return False

    def _is_within_age_window(self, video: VideoCandidate) -> bool:
        if not self.config.max_video_age_hours or not video.published_at:
            return True
        return video.published_at >= time.time() - self.config.max_video_age_hours * 3600

    def _should_collect(self, video: VideoCandidate, ignore_age: bool = False) -> tuple[bool, str | None]:
        if not self._is_supported_media(video):
            return False, "unsupported_media_type"
        if self.db.video_exists(video.video_id, video.metadata_fingerprint):
            return False, "duplicate"
        if not ignore_age and not self._is_within_age_window(video):
            return False, "filter_date"
        if video.view_count < self.config.min_view_count:
            return False, "filter_view"
        if video.like_count < self.config.min_like_count:
            return False, "filter_like"
        if self.config.min_duration_sec is not None and video.duration_sec < self.config.min_duration_sec:
            return False, "filter_duration_min"
        if self.config.max_duration_sec is not None and video.duration_sec > self.config.max_duration_sec:
            return False, "filter_duration_max"
        return True, None

    def _channel_was_deleted(self, channel_id: int) -> bool:
        channel = self.db.get_channel(channel_id)
        return not channel or bool(channel.get("is_deleted"))

    async def _download_candidate(self, candidate: VideoCandidate, channel_id: int | None = None) -> None:
        def stop_if_deleted(event: dict[str, Any]) -> None:
            if channel_id is not None and self._channel_was_deleted(channel_id):
                raise ChannelDeletedDuringCrawl("Crawl job was deleted")

        try:
            download_kwargs: dict[str, Any] = {
                "progress_callback": stop_if_deleted if channel_id is not None else None,
            }
            if self._candidate_aweme_type(candidate) == 68:
                download_kwargs["raw_detail"] = candidate.raw_data or {}
            result = await self.downloader.download(candidate.douyin_url, **download_kwargs)
            size_mb = result.bytes_written / (1024 * 1024)
            file_hash = None
            try:
                path = Path(result.file_path)
                if path.exists():
                    file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                    size_mb = path.stat().st_size / (1024 * 1024)
            except Exception:
                pass
            self.db.update_video_downloaded(
                candidate.video_id,
                str(result.file_path),
                size_mb,
                file_hash,
                media_type=str(getattr(result, "type", "video")),
                asset_count=len(getattr(result, "image_paths", []) or []) or 1,
                music_path=str(result.music_path) if getattr(result, "music_path", None) else None,
            )
        except ChannelDeletedDuringCrawl:
            self.db.mark_video_failed(candidate.video_id, "Đã dừng tải vì crawl job đã bị xóa")
            raise
        except Exception as exc:
            self.db.mark_video_failed(candidate.video_id, str(exc))


class AutoCrawlScheduler:
    def __init__(self, manager: AutoCrawlManager, interval_minutes: int | None = None):
        self.manager = manager
        self.interval_minutes = interval_minutes or int(os.getenv("AUTO_CRAWL_INTERVAL_MINUTES", "60"))
        self.enabled = os.getenv("AUTO_CRAWL_SCHEDULER_ENABLED", "false").lower() == "true"
        self.last_run_at: float | None = None
        self.last_result: dict[str, Any] | None = None
        self.last_error: str | None = None
        self.next_run_at: float | None = None
        self._task: asyncio.Task | None = None

    def status(self) -> dict[str, Any]:
        scheduled = [row for row in self.manager.db.list_channels(status="active") if row.get("schedule_enabled")]
        due_times = [row.get("next_run_at") for row in scheduled if row.get("next_run_at")]
        effective_next_run = min(due_times) if due_times else self.next_run_at
        countdown = None
        if self.enabled and effective_next_run:
            countdown = max(0, int(effective_next_run - time.time()))
        return {
            "enabled": self.enabled,
            "running": bool(self._task and not self._task.done()),
            "interval_minutes": self.interval_minutes,
            "scheduled_jobs": len(scheduled),
            "last_run_at": self.last_run_at,
            "next_run_at": effective_next_run,
            "countdown_seconds": countdown,
            "last_result": self.last_result,
            "last_error": self.last_error,
        }

    def start(self, interval_minutes: int | None = None) -> dict[str, Any]:
        if interval_minutes is not None:
            self.interval_minutes = max(1, int(interval_minutes))
        self.enabled = True
        self.next_run_at = time.time() + max(1, self.interval_minutes) * 60
        try:
            loop = asyncio.get_running_loop()
            if not self._task or self._task.done():
                self._task = loop.create_task(self._loop())
        except RuntimeError:
            # In sync tests/CLI contexts there may be no active event loop; status still records enabled config.
            pass
        return self.status()

    def stop(self) -> dict[str, Any]:
        self.enabled = False
        self.next_run_at = None
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        return self.status()

    async def run_due_channels(self) -> dict[str, Any]:
        due = self.manager.db.list_due_channels()
        completed = failed = 0
        results = []
        for channel in due:
            try:
                result = await self.manager.run_once(channel_id=channel["id"], download=True)
                results.append(result)
                if result.get("status") == "completed":
                    completed += 1
                else:
                    failed += 1
            except Exception as exc:
                failed += 1
                self.last_error = str(exc)
            finally:
                self.manager.db.mark_channel_scheduled_run(channel["id"])
        if due:
            self.last_run_at = time.time()
            self.last_result = {"processed": len(due), "completed": completed, "failed": failed, "results": results}
        return {"processed": len(due), "completed": completed, "failed": failed}

    async def _loop(self) -> None:
        while self.enabled:
            due_times = [row.get("next_run_at") for row in self.manager.db.list_channels(status="active") if row.get("schedule_enabled") and row.get("next_run_at")]
            self.next_run_at = min(due_times) if due_times else time.time() + 30
            await asyncio.sleep(30)
            if not self.enabled:
                break
            try:
                await self.run_due_channels()
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)


def public_video_row(row: dict[str, Any]) -> dict[str, Any]:
    public = {k: v for k, v in row.items() if k not in {"local_path", "music_path", "subtitled_video_path", "raw_data"}}
    subtitle_path = str(row.get("subtitled_video_path") or "").strip()
    public["subtitled_video_filename"] = Path(subtitle_path).name if subtitle_path else None
    public["photo_files"] = []
    public["photo_preview_urls"] = []
    public["music_title"] = None
    public["music_preview_url"] = None
    if str(row.get("media_type") or "video") == "photo":
        manifest_path = Path(str(row.get("local_path") or ""))
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = {}
        filenames = [
            str(name) for name in (manifest.get("image_files") or [])
            if "/" not in str(name) and "\\" not in str(name) and ".." not in str(name)
        ]
        public["photo_files"] = filenames
        public["photo_preview_urls"] = [f"/upload-api/api/photos/{row.get('video_id')}/{name}" for name in filenames]
        public["music_title"] = manifest.get("music_title")
        if manifest.get("music_file"):
            public["music_preview_url"] = f"/upload-api/api/photos/{row.get('video_id')}/music"
    return public
