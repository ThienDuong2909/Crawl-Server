import json
from typing import Any

from .cookie_manager import CookieManager
from .schemas import DouyinParseResult, PhotoData, PhotoImage, VideoData


class DouyinParser:
    """Parse một Douyin URL thành schema tối giản phục vụ tải video no-watermark.

    Crawler/signature được vendor từ repo gốc Evil0ctal/Douyin_TikTok_Download_API
    để giữ tương thích với a_bogus và logic resolve aweme_id.
    """

    def __init__(self, crawler: Any | None = None, cookie_manager: CookieManager | None = None):
        self._crawler = crawler
        self.cookie_manager = cookie_manager or CookieManager()

    @property
    def crawler(self):
        if self._crawler is None:
            self._crawler = self._build_default_crawler()
        return self._crawler

    def _build_default_crawler(self):
        from crawlers.douyin.web.web_crawler import DouyinWebCrawler

        self.cookie_manager.apply_to_vendor_crawler()
        return DouyinWebCrawler()

    async def parse(self, url: str) -> DouyinParseResult:
        if "douyin" not in url.lower():
            raise ValueError("Chỉ hỗ trợ Douyin URL ở bước 1")

        aweme_id = await self.crawler.get_aweme_id(url)
        try:
            raw = await self.crawler.fetch_one_video(aweme_id)
        except Exception:
            refreshed = self.cookie_manager.refresh_from_file()
            if not refreshed:
                raise
            self.cookie_manager.apply_to_vendor_crawler()
            raw = await self.crawler.fetch_one_video(aweme_id)

        return self._normalize_detail(aweme_id, raw)

    async def parse_detail(self, url: str, detail: dict[str, Any]) -> DouyinParseResult:
        if "douyin" not in url.lower():
            raise ValueError("Chỉ hỗ trợ Douyin URL ở bước 1")
        aweme_id = str(detail.get("aweme_id") or detail.get("video_id") or await self.crawler.get_aweme_id(url))
        return self._normalize_detail(aweme_id, {"aweme_detail": detail})

    @staticmethod
    def _highest_quality_video_url(video: dict[str, Any]) -> str | None:
        variants: list[tuple[tuple[int, int, int], str]] = []
        for variant in video.get("bit_rate") or []:
            play_addr = variant.get("play_addr") or {}
            urls = play_addr.get("url_list") or []
            if not urls:
                continue
            width = int(play_addr.get("width") or video.get("width") or 0)
            height = int(play_addr.get("height") or video.get("height") or 0)
            bitrate = int(variant.get("bit_rate") or 0)
            data_size = int(play_addr.get("data_size") or 0)
            variants.append(((width * height, bitrate, data_size), str(urls[0]).replace("playwm", "play")))
        return max(variants, key=lambda item: item[0])[1] if variants else None

    @staticmethod
    def _photo_url_has_watermark(url: str) -> bool:
        lowered = url.lower()
        return any(marker in lowered for marker in ("tplv-dy-water", "watermark", "playwm"))

    @classmethod
    def _select_photo_url(cls, item: dict[str, Any]) -> tuple[str | None, bool]:
        explicit_clean = [str(url) for url in item.get("watermark_free_download_url_list") or [] if url]
        display_urls = [str(url) for url in item.get("url_list") or [] if url]
        download_urls = [str(url) for url in item.get("download_url_list") or [] if url]
        for url in [*explicit_clean, *display_urls, *download_urls]:
            if not cls._photo_url_has_watermark(url):
                return url, True
        fallback = [*explicit_clean, *download_urls, *display_urls]
        return (fallback[0], False) if fallback else (None, False)

    def _normalize_detail(self, aweme_id: str, raw: dict[str, Any] | None) -> DouyinParseResult:
        detail = (raw or {}).get("aweme_detail")
        if not detail:
            raise ValueError("Không tìm thấy aweme_detail trong response Douyin")

        aweme_type = detail.get("aweme_type")
        source_images = detail.get("images") or detail.get("image_infos") or detail.get("image_list") or []
        if aweme_type == 68 or source_images:
            images: list[PhotoImage] = []
            for item in source_images:
                selected_url, watermark_free = self._select_photo_url(item)
                if not selected_url:
                    continue
                images.append(PhotoImage(
                    url=selected_url,
                    width=int(item.get("width") or 0),
                    height=int(item.get("height") or 0),
                    watermark_free=watermark_free,
                ))
            if not images:
                raise ValueError("Bài ảnh Douyin không có URL ảnh hợp lệ")
            music = detail.get("music") or {}
            play_url = music.get("play_url") or {}
            music_urls = play_url.get("url_list") or []
            if not music_urls:
                try:
                    music_extra = json.loads(music.get("extra") or "{}")
                except (TypeError, ValueError):
                    music_extra = {}
                original_song_url = str(music_extra.get("original_song_url") or "").strip()
                if original_song_url.startswith(("http://", "https://")):
                    music_urls = [original_song_url]
            return DouyinParseResult(
                type="photo",
                video_id=str(aweme_id),
                desc=detail.get("desc"),
                create_time=detail.get("create_time"),
                author=detail.get("author"),
                music=music,
                statistics=detail.get("statistics"),
                cover_data={"cover": images[0].url},
                hashtags=detail.get("text_extra"),
                photo_data=PhotoData(
                    images=images,
                    music_url=str(music_urls[0]) if music_urls else None,
                    music_title=music.get("title"),
                    music_author=music.get("author"),
                    music_duration_sec=int(music.get("duration") or 0) or None,
                ),
            )
        if aweme_type not in (0, 4):
            raise ValueError(f"Loại nội dung Douyin chưa được hỗ trợ, aweme_type={aweme_type!r}")

        video = detail.get("video") or {}
        play_addr = video.get("play_addr") or {}
        uri = play_addr.get("uri")
        url_list = play_addr.get("url_list") or []
        if not uri and not url_list:
            raise ValueError("Không tìm thấy play_addr/uri để tạo URL tải")

        wm_hq = url_list[0] if url_list else None
        nwm_hq = self._highest_quality_video_url(video)
        if not nwm_hq:
            nwm_hq = wm_hq.replace("playwm", "play") if wm_hq else None
        nwm = f"https://aweme.snssdk.com/aweme/v1/play/?video_id={uri}&ratio=1080p&line=0" if uri else nwm_hq
        wm = f"https://aweme.snssdk.com/aweme/v1/playwm/?video_id={uri}&radio=1080p&line=0" if uri else wm_hq

        return DouyinParseResult(
            video_id=str(aweme_id),
            desc=detail.get("desc"),
            create_time=detail.get("create_time"),
            author=detail.get("author"),
            music=detail.get("music"),
            statistics=detail.get("statistics"),
            cover_data={
                "cover": video.get("cover"),
                "origin_cover": video.get("origin_cover"),
                "dynamic_cover": video.get("dynamic_cover"),
            },
            hashtags=detail.get("text_extra"),
            video_data=VideoData(
                wm_video_url=wm,
                wm_video_url_HQ=wm_hq,
                nwm_video_url=nwm,
                nwm_video_url_HQ=nwm_hq,
            ),
        )
