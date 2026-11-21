# Flow Playground

Front-end generation độc lập (tách khỏi flow2api): UI mô phỏng Google Flow, gọi
flow2api qua HTTP (`/v1/chat/completions`) làm engine, tự hậu xử lý kết quả —
bao gồm **xoá watermark (LaMa/OpenCV + mask vẽ tay)** và crop viền WM video.

## Kiến trúc

- `app/` — FastAPI backend: session/CSRF, projects + items (SQLite
  `data/playground.db`), job generation nền, HTTP client sang flow2api,
  watermark engine + mask API.
- `static/` — frontend (vanilla JS, không build step).
- `masks/` — mask WM per `{base_model}:{ratio}` (vd
  `gemini-3.0-pro-image_9_16.png`) + mask dùng chung `watermark.png`.
- `data/`, `tmp/` — DB, torch cache, refs upload, kết quả đã hậu xử lý.

## Chạy (docker)

```bash
docker compose up -d --build   # sudo nếu user chưa thuộc docker group
# → http://<host>:38001/playground
```

Biến môi trường chính:

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `FLOW2API_BASE_URL` | `http://192.168.10.132:38000` | Địa chỉ flow2api (engine) |
| `WATERMARK_ENABLED` | `1` | Bật xoá WM ảnh + crop WM video lite |
| `WATERMARK_ENGINE` | `auto` | `auto`/`lama`/`opencv` |
| `WATERMARK_VIDEO_CROP_PERCENT` | `2` | % crop mỗi cạnh cho video lite |

Login bằng API key của flow2api (key được validate qua `GET /v1/models` của
flow2api rồi lưu vào session).

## Cách tạo mask WM

1. Tạo 1 ảnh trong playground (bất kỳ model/ratio nào muốn bảo vệ).
2. Menu ⋯ trên card → **Tạo mask WM từ ảnh này** → tô vùng watermark → Lưu.
3. Mask tự áp cho mọi lần tạo sau của cùng model + tỉ lệ (tự scale theo kích
   thước ảnh thật). Kiểm tra danh sách mask ở nút **Mask WM** trên topbar.

Chạy lần đầu cần mạng để tải `big-lama.pt` (~200MB) vào volume torch cache;
không tải được thì tự rơi về OpenCV TELEA (chất lượng thấp hơn).

## Chạy dev (không docker)

```bash
pip install -r requirements.txt            # + requirements-lama.txt nếu muốn LaMa
FLOW2API_BASE_URL=http://127.0.0.1:8000 python main.py
```

## Test

```bash
python -m pytest tests/ -v
```
