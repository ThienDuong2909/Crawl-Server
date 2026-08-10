from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


class VideoData(BaseModel):
    wm_video_url: str | None = None
    wm_video_url_HQ: str | None = None
    nwm_video_url: str
    nwm_video_url_HQ: str | None = None

    @property
    def best_no_watermark_url(self) -> str:
        return self.nwm_video_url_HQ or self.nwm_video_url


class PhotoImage(BaseModel):
    url: str
    width: int = 0
    height: int = 0
    watermark_free: bool = True


class PhotoData(BaseModel):
    images: list[PhotoImage]
    music_url: str | None = None
    music_title: str | None = None
    music_author: str | None = None
    music_duration_sec: int | None = None


class DouyinParseResult(BaseModel):
    type: Literal["video", "photo"] = "video"
    platform: Literal["douyin"] = "douyin"
    video_id: str
    desc: str | None = None
    create_time: int | None = None
    author: dict[str, Any] | None = None
    music: dict[str, Any] | None = None
    statistics: dict[str, Any] | None = None
    cover_data: dict[str, Any] = Field(default_factory=dict)
    hashtags: list[Any] | None = None
    video_data: VideoData | None = None
    photo_data: PhotoData | None = None


class DownloadResult(BaseModel):
    platform: Literal["douyin"] = "douyin"
    type: Literal["video", "photo"] = "video"
    video_id: str
    file_path: Path
    source_url: str
    bytes_written: int
    image_paths: list[Path] = Field(default_factory=list)
    music_path: Path | None = None
    manifest_path: Path | None = None
