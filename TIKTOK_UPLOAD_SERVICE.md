# TikTok Upload Service — phase tách container

Tài liệu này mô tả trạng thái hiện tại sau khi tách hệ thống thành 2 service/container riêng.

## 1. Kiến trúc hiện tại

Docker Compose hiện có 2 container chính:

| Service | Container | Port | Vai trò |
|---|---|---:|---|
| `douyin-downloader` | `douyin-downloader` | `8000` | Dashboard chính, quản lý Cookie Douyin, parse/download video Douyin no-watermark, liệt kê video đã tải |
| `tiktok-uploader` | `tiktok-uploader` | `8001` | Upload service riêng, đọc video từ volume chung, tạo upload job TikTok |

Volume chung:

```text
./download           -> /shared/download
./tiktok_upload_data -> /app/data trong upload service
./secrets            -> /app/secrets trong download service
```

Video Douyin đã tải nằm ở:

```text
Host:      /home/douyin_nwm_tool/download/douyin_video/*.mp4
Container: /shared/download/douyin_video/*.mp4
```

## 2. Trạng thái upload TikTok hiện tại

Upload service hiện đang chạy mặc định ở chế độ:

```text
TIKTOK_UPLOAD_MODE=dry_run
```

Nghĩa là:

- Có API, job manager, status, history.
- Có thể chọn video đã tải để tạo upload job.
- Job trả `success` nếu file tồn tại/hợp lệ.
- Không gửi request thật lên TikTok.
- Không cần TikTok cookie/session thật ở phase này.

Lý do giữ dry-run trước:

- Repo mẫu dùng TikTok web endpoint không chính thức, có rủi ro hỏng signer.
- Báo cáo kỹ thuật đã chỉ ra lỗi ở signer và payload options.
- Cần tách service/container + luồng quản lý job ổn định trước khi bật upload thật.

## 3. API upload service

Health:

```bash
curl http://127.0.0.1:8001/health
```

Liệt kê video sẵn sàng upload:

```bash
curl http://127.0.0.1:8001/api/videos | python3 -m json.tool
```

Tạo upload job dry-run:

```bash
curl -X POST http://127.0.0.1:8001/api/upload/jobs \
  -H 'Content-Type: application/json' \
  -d '{
    "filename":"douyin_7661129618388274483.mp4",
    "account":"account_test",
    "caption":"Caption #test",
    "options":{"visibility_type":0,"allow_comment":1,"allow_duet":0,"allow_stitch":0}
  }' | python3 -m json.tool
```

Xem danh sách upload jobs:

```bash
curl http://127.0.0.1:8001/api/upload/jobs | python3 -m json.tool
```

Xem job cụ thể:

```bash
curl http://127.0.0.1:8001/api/upload/jobs/<job_id> | python3 -m json.tool
```

## 4. Dashboard

Dashboard chính vẫn ở:

```text
http://VPS_IP:8000/
```

Có thêm khu vực `TikTok Upload Service`:

1. Nhập `Upload service base URL`, ví dụ:

```text
http://VPS_IP:8001
```

2. Chọn video ở mục `Video đã tải / chuẩn bị upload TikTok`.
3. Nhập account label và caption.
4. Bấm `Start TikTok Upload Job`.
5. Theo dõi job trong danh sách upload jobs.

## 5. Docker Compose commands

```bash
cd /home/douyin_nwm_tool

docker compose ps
docker compose logs -f douyin-downloader
docker compose logs -f tiktok-uploader

docker compose restart douyin-downloader
docker compose restart tiktok-uploader

docker compose up -d --build
```

## 6. Bước tiếp theo để upload thật

Để chuyển từ dry-run sang upload TikTok thật, cần làm tiếp theo thứ tự an toàn:

1. Tạo TikTok session/cookie manager riêng cho upload service.
2. Không lưu cookie dạng pickle không kiểm soát nếu có thể tránh; nếu phải import từ repo mẫu thì bọc bằng secret directory permission hạn chế.
3. Thêm API/UI kiểm tra account/session TikTok.
4. Tích hợp repo `makiisthenes/TiktokAutoUploader` qua adapter riêng, không trộn vào downloader.
5. Viết test mock adapter trước, sau đó smoke test có giám sát với tài khoản TikTok test.
6. Sửa/kiểm chứng các lỗi báo cáo kỹ thuật đã nêu: signer, payload privacy/comment/duet/stitch, timeout, memory, retry.

Không nên bật upload thật hàng loạt trước khi kiểm chứng bằng tài khoản test và video nhỏ.
