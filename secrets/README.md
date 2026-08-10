# Secret files

Thư mục này chỉ chứa các file mẫu. Không commit thông tin thật.

Tạo file runtime từ mẫu:

```bash
cp secrets/douyin_cookie.example.txt secrets/douyin_cookie.txt
cp secrets/agentrouter_api_key.example.txt secrets/agentrouter_api_key.txt
cp secrets/tiktok_dev_oauth.example.json secrets/tiktok_dev_oauth.json
chmod 600 secrets/douyin_cookie.txt secrets/agentrouter_api_key.txt secrets/tiktok_dev_oauth.json
```

Sau đó thay placeholder bằng giá trị thật trực tiếp trên máy chủ. `.gitignore` chỉ cho phép các file `*.example.*` và README được theo dõi.
