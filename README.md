# Crawl Server — Douyin → TikTok Backend

Backend FastAPI chạy bằng Docker Compose, gồm hai service tách biệt:

- `douyin-downloader` (`127.0.0.1:8000`): parse/crawl Douyin, tải video hoặc album ảnh, tải nhạc tham chiếu, dịch tiêu đề và quản lý queue.
- `tiktok-uploader` (`127.0.0.1:8001`): TikTok OAuth, Video Upload-to-Inbox và native Photo Inbox qua Content Posting API.

Repo này chỉ chứa mã backend và file cấu hình mẫu. Không chứa cookie, token OAuth, API key, database, video, ảnh, nhạc hoặc lịch sử upload thật.

## Tính năng chính

- Video Douyin: chọn H.264 chất lượng cao, tải atomically và lưu media metadata.
- Bài ảnh Douyin (`aweme_type=68`): giữ thứ tự ảnh, lưu nhạc riêng và manifest.
- Watermark fail-closed: ưu tiên biến thể ảnh sạch; giữ source cũ; chỉ push TikTok khi manifest có `watermark_processing.status=clean`.
- Ảnh TikTok dẫn xuất tối đa 1080p, giữ nguyên tỉ lệ và không ghi đè source.
- Auto-crawl nhiều kênh, scheduler, queue, retry và SQLite state.
- TikTok Video Upload-to-Inbox bằng `FILE_UPLOAD`.
- TikTok Photo Inbox bằng `PHOTO + MEDIA_UPLOAD + PULL_FROM_URL`.
- Credential chỉ đọc từ biến môi trường hoặc file secret server-side.

## Cấu trúc

```text
douyin_nwm_tool/       Downloader API, parser, crawler state, scheduler, worker
tiktok_upload_service/ TikTok OAuth và Upload-to-Inbox service
crawlers/              Douyin crawler/signature adapter
tests/                 Backend unit/integration tests
secrets/               Chỉ có file mẫu; secret thật bị gitignore
Dockerfile              Downloader image
Dockerfile.upload       Uploader image
docker-compose.yml      Backend-only Compose stack
.env.example            Danh sách cấu hình không chứa giá trị thật
```

## Khởi động nhanh

Yêu cầu: Docker Engine và Docker Compose plugin.

```bash
git clone https://github.com/ThienDuong2909/Crawl-Server.git
cd Crawl-Server
cp .env.example .env
cp secrets/douyin_cookie.example.txt secrets/douyin_cookie.txt
cp secrets/agentrouter_api_key.example.txt secrets/agentrouter_api_key.txt
cp secrets/tiktok_dev_oauth.example.json secrets/tiktok_dev_oauth.json
chmod 600 .env secrets/douyin_cookie.txt secrets/agentrouter_api_key.txt secrets/tiktok_dev_oauth.json
```

Điền credential thật trực tiếp trên server, sau đó:

```bash
docker compose up -d --build
docker compose ps
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8001/health
```

Không commit `.env` hoặc các file secret thật.

## Cấu hình bảo mật

Các giá trị nhạy cảm phải để trống trong `.env.example` và chỉ cấu hình trong `.env`/`secrets/` ở máy chạy:

- `API_AUTH_TOKEN`
- `COOKIE_UPDATE_TOKEN`
- Douyin cookie
- AgentRouter API key
- TikTok `client_key`/`client_secret`
- TikTok OAuth access/refresh token

Khuyến nghị:

1. Tạo token dài, ngẫu nhiên cho API/webhook.
2. `chmod 600` cho `.env` và secret files.
3. Bind các service vào `127.0.0.1` như Compose mặc định.
4. Public hệ thống qua reverse proxy HTTPS.
5. Không log raw cookie, token hoặc signed media URL.
6. Backup database/media ngoài Git.

## Chạy test

Python 3.11+:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest -q
```

## Runtime data không được commit

Các thư mục sau được tạo tại runtime và đã bị `.gitignore`:

```text
download/
douyin_crawl_data/
tiktok_upload_data/
secrets/* (ngoại trừ file mẫu)
```

## Lưu ý TikTok Photo

TikTok `MEDIA_UPLOAD` cho bài ảnh không cho đính kèm file nhạc ngoài. Hệ thống lưu nhạc Douyin trên VPS để người vận hành nghe đối chiếu; người dùng chọn sound tương ứng trong TikTok khi review Inbox.

Ảnh chỉ được đưa vào payload TikTok khi watermark stage đã xác nhận sạch. Nếu không tìm được URL ảnh sạch, upload bị chặn thay vì gửi ảnh có watermark.
