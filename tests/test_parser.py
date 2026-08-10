import pytest

from douyin_nwm_tool.parser import DouyinParser


class FakeCrawler:
    async def get_aweme_id(self, url: str) -> str:
        assert url == "https://www.douyin.com/video/123"
        return "123"

    async def fetch_one_video(self, aweme_id: str):
        assert aweme_id == "123"
        return {
            "aweme_detail": {
                "aweme_type": 0,
                "desc": "demo video",
                "create_time": 1710000000,
                "author": {"nickname": "tester"},
                "music": {"title": "song"},
                "statistics": {"digg_count": 1},
                "text_extra": [],
                "video": {
                    "play_addr": {
                        "uri": "v0200abc",
                        "url_list": ["https://example.com/aweme/v1/playwm/?video_id=v0200abc"]
                    },
                    "cover": {"url_list": ["cover"]},
                    "origin_cover": {"url_list": ["origin"]},
                    "dynamic_cover": {"url_list": ["dynamic"]},
                    "bit_rate": [
                        {
                            "gear_name": "low_540_0",
                            "bit_rate": 900000,
                            "is_h265": 0,
                            "play_addr": {
                                "width": 576,
                                "height": 1024,
                                "data_size": 30000000,
                                "url_list": ["https://media.example/540p.mp4"],
                            },
                        },
                        {
                            "gear_name": "normal_720_0",
                            "bit_rate": 1500000,
                            "is_h265": 0,
                            "play_addr": {
                                "width": 720,
                                "height": 1280,
                                "data_size": 50000000,
                                "url_list": ["https://media.example/720p.mp4"],
                            },
                        },
                    ],
                },
            }
        }


@pytest.mark.asyncio
async def test_parse_douyin_video_returns_minimal_no_watermark_urls():
    parser = DouyinParser(crawler=FakeCrawler())

    result = await parser.parse("https://www.douyin.com/video/123")

    assert result.platform == "douyin"
    assert result.type == "video"
    assert result.video_id == "123"
    assert result.desc == "demo video"
    assert result.video_data.nwm_video_url == "https://aweme.snssdk.com/aweme/v1/play/?video_id=v0200abc&ratio=1080p&line=0"
    assert result.video_data.nwm_video_url_HQ == "https://media.example/720p.mp4"
    assert "playwm" not in result.video_data.nwm_video_url_HQ


class ExpiringCookieCrawler(FakeCrawler):
    def __init__(self):
        self.calls = 0

    async def fetch_one_video(self, aweme_id: str):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("cookie expired")
        return await super().fetch_one_video(aweme_id)


class FakeCookieManager:
    def __init__(self):
        self.refreshed = False

    def refresh_from_file(self):
        self.refreshed = True
        return "ttwid=new; sessionid=new"

    def apply_to_vendor_crawler(self):
        return True


@pytest.mark.asyncio
async def test_parse_douyin_photo_post_preserves_image_order_and_music():
    class PhotoCrawler:
        async def get_aweme_id(self, url: str) -> str:
            return "photo-68"

        async def fetch_one_video(self, aweme_id: str):
            return {
                "aweme_detail": {
                    "aweme_type": 68,
                    "desc": "四张照片",
                    "create_time": 1710000001,
                    "author": {"nickname": "album author"},
                    "statistics": {},
                    "text_extra": [],
                    "images": [
                        {"width": 1080, "height": 1440, "watermark_free_download_url_list": ["https://img.example/01.webp"], "url_list": ["https://img.example/01-low.webp"]},
                        {"width": 1440, "height": 1080, "download_url_list": ["https://img.example/02.webp"]},
                        {"width": 1080, "height": 1920, "url_list": ["https://img.example/03.webp"]},
                    ],
                    "music": {"title": "凌风(宿命版)", "author": "歌手", "duration": 20, "play_url": {"url_list": []}, "extra": "{\"original_song_url\":\"https://audio.example/song.mp3\"}"},
                }
            }

    result = await DouyinParser(crawler=PhotoCrawler()).parse("https://www.douyin.com/note/photo-68")

    assert result.type == "photo"
    assert result.video_id == "photo-68"
    assert [image.url for image in result.photo_data.images] == [
        "https://img.example/01.webp",
        "https://img.example/02.webp",
        "https://img.example/03.webp",
    ]
    assert [(image.width, image.height) for image in result.photo_data.images] == [(1080, 1440), (1440, 1080), (1080, 1920)]
    assert result.photo_data.music_url == "https://audio.example/song.mp3"
    assert result.photo_data.music_title == "凌风(宿命版)"


@pytest.mark.asyncio
async def test_parse_photo_prefers_clean_display_url_over_watermarked_download_url():
    class PhotoCrawler:
        async def get_aweme_id(self, url: str) -> str:
            return "photo-clean"

        async def fetch_one_video(self, aweme_id: str):
            return {
                "aweme_detail": {
                    "aweme_type": 68,
                    "images": [{
                        "width": 2160,
                        "height": 2880,
                        "download_url_list": ["https://img.example/source~tplv-dy-water-v2:account.webp"],
                        "url_list": ["https://img.example/source~tplv-dy-aweme-images:q75.webp"],
                    }],
                }
            }

    result = await DouyinParser(crawler=PhotoCrawler()).parse("https://www.douyin.com/note/photo-clean")

    assert result.photo_data.images[0].url == "https://img.example/source~tplv-dy-aweme-images:q75.webp"
    assert result.photo_data.images[0].watermark_free is True


@pytest.mark.asyncio
async def test_parse_retries_once_after_cookie_refresh():
    crawler = ExpiringCookieCrawler()
    cookie_manager = FakeCookieManager()
    parser = DouyinParser(crawler=crawler, cookie_manager=cookie_manager)

    result = await parser.parse("https://www.douyin.com/video/123")

    assert result.video_id == "123"
    assert crawler.calls == 2
    assert cookie_manager.refreshed is True
