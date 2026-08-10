# VẬN HÀNH TIKTOK UPLOAD-TO-INBOX

## Luồng hiện tại

```text
Video đã xử lý
→ TikTok Upload API (`video.upload`)
→ TikTok Inbox
→ người dùng mở thông báo
→ chỉnh sửa/dán caption
→ bấm Post
```

Upload-to-Inbox không yêu cầu đặt toàn bộ TikTok account thành private. Đây không phải Direct Post: TikTok yêu cầu chủ tài khoản mở thông báo Inbox để hoàn tất bài đăng.

### Bài Douyin dạng ảnh ghép nhạc

```text
Bài Douyin aweme_type=68
→ tải từng ảnh theo đúng thứ tự
→ lưu riêng nhạc gốc làm bản tham chiếu
→ TikTok Photo MEDIA_UPLOAD (`media_type=PHOTO`)
→ TikTok Inbox
→ người dùng kiểm tra thứ tự ảnh, chọn nhạc trong TikTok và bấm Post
```

Hệ thống không dựng ảnh thành MP4 và không crop/kéo méo ảnh. Mỗi ảnh, file nhạc và `manifest.json` được giữ riêng trong thư mục album nguồn.

TikTok Photo Posting API hiện nhận danh sách URL ảnh nhưng không có trường để đính kèm file âm thanh ngoài. Vì vậy nhạc Douyin được tải và phát thử trên dashboard để đối chiếu; người dùng cần chọn bài nhạc tương ứng trong trình chỉnh sửa TikTok. Không được báo rằng nhạc đã chuyển sang TikTok nếu chưa chọn trong ứng dụng.

## Trạng thái hệ thống

Provider được chọn trong `/home/douyin_nwm_tool/.env`:

```text
TIKTOK_UPLOAD_MODE=inbox
```

Credential OAuth tiếp tục được giữ server-side. Frontend và log không nhận access token, refresh token, client secret hoặc upload URL tạm thời.

## Thao tác trên dashboard

1. Kiểm tra TikTok OAuth hiển thị “TikTok đã kết nối”.
2. Chọn video.
3. Bấm “Gửi vào TikTok Inbox”.
4. Chờ trạng thái “Chờ bạn hoàn tất trên TikTok”.
5. Mở ứng dụng TikTok trên điện thoại.
6. Mở Inbox/Thông báo do TikTok gửi.
7. Chọn video, chỉnh sửa nếu cần.
8. Dán caption tiếng Việt đã chuẩn bị trên dashboard.
9. Chọn quyền riêng tư của bài đăng và bấm Post.

Caption mặc định chỉ gồm tiêu đề tiếng Việt, không tự thêm hashtag:

```text
<tiêu đề tiếng Việt>
```

TikTok có thể tự chèn hashtag mang tên ứng dụng Sandbox (ví dụ `#AutoUploaderTZ`) vào bản nháp. Hashtag này không được tạo bởi mã nguồn của dashboard và Upload-to-Inbox API không có trường caption để xóa nó. Nếu TikTok vẫn hiển thị, hãy xóa hashtag này trong màn hình chỉnh sửa TikTok trước khi bấm Post.

Lưu ý: API Upload-to-Inbox chỉ chuyển file video, không tự điền caption vào trình chỉnh sửa TikTok.

## Trạng thái TikTok API

- `PROCESSING_UPLOAD`: TikTok đang nhận/xử lý file.
- `SEND_TO_USER_INBOX`: thông báo đã được gửi tới Inbox; cần thao tác trên TikTok.
- `PUBLISH_COMPLETE`: người dùng đã mở thông báo và đăng thành công.
- `FAILED`: quy trình thất bại; xem `fail_reason`.

Tra cứu thủ công:

```text
GET /upload-api/api/tiktok/publish/status/{publish_id}
```

Endpoint chỉ trả metadata đã lọc; không trả token hoặc upload URL.

## Auto-crawl

Khi bật “Tự động gửi video vào TikTok Inbox sau khi tải xong”, hệ thống tự chuyển từng video mới vào Inbox. Mỗi video vẫn cần một lần xác nhận cuối trên ứng dụng TikTok.

Không bật hàng loạt cho nhiều channel trước khi xác minh video thử nghiệm đầu tiên xuất hiện đúng trong Inbox.
