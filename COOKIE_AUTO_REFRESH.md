# Cookie tự động cho Douyin

Mục tiêu: Cookie hết hạn thì không phải sửa code/YAML. Tool có endpoint nhận Cookie mới, ghi vào file secret và cập nhật runtime.

## Cách hoạt động

Tool đọc Cookie theo thứ tự:

1. Biến môi trường `DOUYIN_COOKIE`
2. File `DOUYIN_COOKIE_FILE`, default: `/home/douyin_nwm_tool/secrets/douyin_cookie.txt`

Khi gọi endpoint update Cookie, tool sẽ:

- validate format raw Cookie header
- ghi vào `/home/douyin_nwm_tool/secrets/douyin_cookie.txt`
- cập nhật `os.environ["DOUYIN_COOKIE"]` trong process đang chạy
- apply Cookie mới vào crawler vendor
- không trả Cookie raw ra response, chỉ trả redacted

Khi parse gặp lỗi, parser sẽ thử reload Cookie từ file và retry 1 lần. Vì vậy nếu Chrome Cookie Sniffer/webhook vừa cập nhật file, request tiếp theo có thể tự hồi phục.

## Endpoint

### Xem trạng thái Cookie

```bash
curl http://127.0.0.1:8000/api/cookie/douyin/status | python3 -m json.tool
```

### Update Cookie thủ công

```bash
curl -X POST http://127.0.0.1:8000/api/cookie/douyin \
  -H 'Content-Type: application/json' \
  -d '{"service":"douyin","cookie":"ttwid=xxx; sessionid=yyy; sid_guard=zzz"}' | python3 -m json.tool
```

### Webhook cho Chrome Cookie Sniffer

Repo gốc extension gửi JSON dạng:

```json
{
  "service": "douyin",
  "cookie": "raw Cookie string",
  "timestamp": "2026-07-28T00:00:00Z"
}
```

Set webhook URL trong extension là:

```text
http://127.0.0.1:8000/api/cookie/douyin/webhook
```

Nếu extension chạy trên máy cá nhân còn API chạy trên server, dùng URL server public/private tương ứng, ví dụ:

```text
http://SERVER_IP:8000/api/cookie/douyin/webhook
```

Khuyến nghị không public endpoint này nếu chưa có token/TLS.

## Bảo vệ endpoint bằng token

Chạy API với:

```bash
export COOKIE_UPDATE_TOKEN="$(openssl rand -hex 32)"
```

Khi đó update Cookie cần header:

```bash
curl -X POST http://127.0.0.1:8000/api/cookie/douyin \
  -H 'Content-Type: application/json' \
  -H "X-Cookie-Token: $COOKIE_UPDATE_TOKEN" \
  -d '{"service":"douyin","cookie":"ttwid=xxx; sessionid=yyy"}'
```

Lưu ý: Chrome Cookie Sniffer gốc hiện không thêm custom header token. Nếu muốn dùng token với extension, cần sửa popup/background để gửi header `X-Cookie-Token`, hoặc đặt endpoint sau reverse proxy nội bộ có auth.

## Tự động login lại?

Không nên tự động login hoàn toàn bằng username/password vì Douyin thường có QR/OTP/captcha/2FA và rủi ro bảo mật cao. Hướng an toàn hơn:

1. Trình duyệt người dùng vẫn đăng nhập Douyin bình thường.
2. Chrome Cookie Sniffer tự bắt Cookie mới khi phiên thay đổi.
3. Extension POST Cookie mới về webhook.
4. API cập nhật Cookie runtime + file secret.

Nếu session hết hạn hẳn, người dùng chỉ cần login lại trên trình duyệt; extension sẽ đẩy Cookie mới. Đây là semi-auto an toàn hơn auto-login bằng mật khẩu.
