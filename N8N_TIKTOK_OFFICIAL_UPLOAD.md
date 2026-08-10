# N8N + TikTok Official Content Posting API

## Mục tiêu

Chuyển bước upload TikTok thật khỏi cơ chế cookie/browser sang luồng chính thức:

React UI → tiktok-uploader Python → n8n webhook → TikTok Content Posting API → callback về Python → UI cập nhật job.

## Domain

- Web NWM: https://nwm.thienne.io.vn
- n8n: https://n8n.thienne.io.vn
- Webhook upload: https://n8n.thienne.io.vn/webhook/tiktok-upload

## Secret

TikTok Developer sandbox credential được lưu trong file local có quyền 600:

```text
/home/douyin_nwm_tool/secrets/tiktok_dev_oauth.json
```

Không commit, không in ra UI/log/chat.

## Backend endpoints mới

TikTok OAuth:

```text
GET /upload-api/api/tiktok/oauth/status
GET /upload-api/api/tiktok/oauth/connect
GET /upload-api/api/tiktok/oauth/callback?code=...
GET /api/tiktok/callback?code=...              # public redirect URI nên khai báo trong TikTok Developer
```

Upload thật qua n8n:

```text
POST /upload-api/api/upload/n8n/jobs
POST /upload-api/api/tiktok/update-status
POST /api/tiktok/update-status                  # public callback alias cho n8n
```

Redirect URI cần khai báo đúng trong TikTok Developer Portal:

```text
https://nwm.thienne.io.vn/api/tiktok/callback
```

Video public URL cho TikTok pull:

```text
GET /upload-api/api/videos/{filename}
```

## Luồng vận hành

1. Mở https://nwm.thienne.io.vn.
2. Vào phần Tài khoản.
3. Bấm Connect TikTok OAuth.
4. TikTok redirect về callback để backend đổi code lấy access_token/refresh_token.
5. Chọn video đã tải trong Video Library.
6. Nhập caption.
7. Bấm Upload thật qua n8n.
8. Backend gửi payload sang n8n gồm local_video_id, access_token, video_url, caption, callback_url.
9. n8n gọi TikTok:

```text
POST https://open.tiktokapis.com/v2/post/publish/video/init/
```

10. n8n callback về:

```text
POST https://nwm.thienne.io.vn/api/tiktok/update-status
```

## Payload n8n nhận

```json
{
  "local_video_id": "job_id",
  "access_token": "[REDACTED]",
  "video_url": "https://nwm.thienne.io.vn/upload-api/api/videos/douyin_....mp4",
  "caption": "Video caption",
  "callback_url": "https://nwm.thienne.io.vn/api/tiktok/update-status",
  "post_info": {
    "title": "Video caption",
    "privacy_level": "SELF_ONLY",
    "disable_duet": true,
    "disable_comment": false,
    "disable_stitch": true,
    "video_cover_timestamp_ms": 1000
  },
  "source_info": {
    "source": "PULL_FROM_URL",
    "video_url": "https://nwm.thienne.io.vn/upload-api/api/videos/douyin_....mp4"
  }
}
```

## n8n workflow

Workflow đã import:

```text
NWM TikTok Upload via Official API
```

File workflow lưu trong project:

```text
/home/douyin_nwm_tool/n8n_tiktok_upload_workflow.json
```

Workflow gồm:

- Webhook node: POST /webhook/tiktok-upload
- Code node: gọi TikTok Direct Post API và callback status về Web NWM

## Kiểm thử đã thực hiện

- Unit/API focused tests: OAuth, n8n job, token refresh, React UI.
- n8n webhook probe với fake token: workflow chạy và trả status failed an toàn, không tạo post thật.
- Frontend build pass.

## Lưu ý sandbox

Nếu TikTok sandbox/app chưa duyệt đủ scope hoặc access token chưa được connect qua OAuth callback, nút upload thật sẽ chưa gửi được post hợp lệ. Hãy bấm Connect TikTok OAuth trên UI trước khi upload thật.
