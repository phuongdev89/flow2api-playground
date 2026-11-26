# Flow2API Playground

Front-end generation độc lập đứng trước [flow2api](https://github.com/phuongdev89/flow2api):
UI mô phỏng Google Flow, gọi flow2api qua HTTP (`/v1/chat/completions`) làm
engine, tự hậu xử lý kết quả — bao gồm **xoá watermark ảnh (LaMa/OpenCV + mask
vẽ tay)** và **crop viền WM video**.

## Tính năng

- **Image generation** — Nano Banana Pro (`gemini-3.0-pro-image`): text hoặc
  i2i (tối đa 10 ảnh tham chiếu), ratio 16:9 / 4:3 / 1:1 / 3:4 / 9:16, res
  1K / 2K, tạo 1–4 ảnh mỗi lần.
- **Video generation** — Veo 3.1 Lite / Fast / Fast Ultra / Quality:
  - `Text` — text-to-video thuần.
  - `Frames` — i2v với frame đầu + frame cuối.
  - `Ingredients` — r2v với 1–3 ảnh tham chiếu.
  - `Extend` — nối tiếp video đã tạo trong playground (qua video editor,
    khoá model gốc theo media id của video nguồn).
- **Job nền + hàng đợi** — job persist qua restart tab/server, stream kết quả
  từng item về ngay khi xong, cap số job chạy đồng thời
  (`PLAYGROUND_MAX_RUNNING_JOBS`).
- **Video Studio Flow** (`/studio`) — port từ tool CINEMAFLOW trên labs.google
  Flow: lên kịch bản N cảnh (4–25) từ một chủ đề, mỗi cảnh gen START/END frame
  (i2i tham chiếu tối đa 2 ảnh nhân vật) rồi render video I2V First/Last
  (Omni 1.2 Flash 3–15s hoặc Veo 3.1 Lite), tự động tái dùng END cảnh trước
  làm START cảnh sau, Import/Export JSON tương thích hai chiều với tool gốc.
  Dựng trên pipeline generate + job queue có sẵn; session dùng chung
  playground. Planner kịch bản dùng LLM nếu điền `STUDIO_LLM_*`, không thì
  engine mẫu nội bộ (4 template: kitchen/workshop/journey/cozy + universal).
- **Projects** — mỗi project là một bảng kết quả lưu SQLite; đổi tên item,
  xóa hàng loạt (marquee selection), trim video (ffmpeg) tạo item mới.
- **Mask xoá watermark** — xem chi tiết ở
  [masks/README.md](masks/README.md): mask vẽ tay theo
  `model + ratio + res`, inpaint LaMa (fallback OpenCV TELEA), crop % viền
  cho video lite.

## Kiến trúc

| Thư mục | Nội dung |
|---|---|
| `app/` | FastAPI backend: session/CSRF, projects + jobs (SQLite `data/playground.db`), pipeline generation, HTTP client sang flow2api, watermark engine + mask API |
| `static/` | Frontend vanilla JS, không build step |
| `masks/` | Mask WM + ảnh ví dụ làm nền vẽ mask (runtime data) |
| `data/`, `tmp/` | DB, torch cache, ảnh upload, kết quả đã hậu xử lý |
| `tests/` | pytest — harness mock flow2api, không cần engine thật |

## Chạy (docker)

```bash
docker compose up -d --build   # sudo nếu user chưa thuộc docker group
# → http://<host>:38001/playground
```

Biến môi trường (đặt trong `.env` hoặc truyền thẳng):

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `FLOW2API_BASE_URL` | `http://192.168.10.132:38000` | Địa chỉ flow2api (engine) — fallback khi login kiểu cũ |
| `FLOW2API_TIMEOUT` | `1200` | Timeout một request generation (giây, video chạy lâu) |
| `WATERMARK_ENABLED` | `1` | Bật xoá WM ảnh + crop WM video lite |
| `WATERMARK_ENGINE` | `auto` | `auto` / `lama` / `opencv` |
| `WATERMARK_MASK_DIR` | `masks` | Thư mục chứa mask + ảnh ví dụ |
| `WATERMARK_VIDEO_CROP_PERCENT` | `2` | % crop mỗi cạnh cho video có WM viền |
| `PLAYGROUND_MAX_RUNNING_JOBS` | `3` | Cap job generation chạy đồng thời mỗi owner |
| `PLAYGROUND_RELOAD` | `1` | Uvicorn tự restart worker khi `app/*.py` đổi — sửa code không cần restart container. Đặt `0` cho prod. `static/` luôn đọc trực tiếp từ disk. |
| `STUDIO_LLM_BASE_URL` | rỗng (= `FLOW2API_BASE_URL`) | Video Studio Flow: endpoint OpenAI-compatible cho planner LLM (`/v1/chat/completions`) |
| `STUDIO_LLM_API_KEY` | rỗng | Bearer key của endpoint trên — **điền cặp này + model thì planner mới dùng LLM** |
| `STUDIO_LLM_MODEL` | rỗng | Model id text cho planner (vd `gemini-3-pro`). Rỗng → engine mẫu nội bộ, không gọi LLM |
| `STUDIO_LLM_TIMEOUT` | `120` | Timeout một request planner (giây) |

**Đăng nhập** yêu cầu cả **API URL** (endpoint flow2api) và **API key**: cặp
này được validate qua `GET /v1/models` trên chính endpoint đã nhập, đúng cả
hai mới cấp session. Job nền dùng đúng cặp URL/key của session đó.

## Mask xoá watermark (tóm tắt)

1. Nút **Mask WM** trên topbar → panel liệt kê từng combo
   **model · ratio · res** (1K và 2K có WM ở chỗ khác nhau nên mỗi res một
   dòng riêng).
2. Bấm **Vẽ mask** → editor cần nền có watermark mới tô được: bấm
   **"Gen 1 ảnh ví dụ"** (gen đúng combo, giữ nguyên WM, lưu lại dùng tiếp)
   hoặc dùng ảnh cùng combo trong project (◀ ▶ duyệt).
3. Tô vùng WM → **Lưu mask** → các lần tạo sau với đúng combo đó tự xoá WM
   (mask tự scale theo kích thước ảnh thật). Mở editor lần sau thấy lại mask
   đã lưu — vẽ thêm là nối vào, **Đặt lại** mới xoá sạch.

Chi tiết định dạng, fallback key legacy và cách thả file thủ công:
[masks/README.md](masks/README.md).

Lần đầu chạy với `WATERMARK_ENGINE=auto` cần mạng để tải `big-lama.pt`
(~200MB) vào torch cache; không tải được thì tự rơi về OpenCV TELEA (chất
lượng thấp hơn).

## Chạy dev (không docker)

```bash
pip install -r requirements.txt            # + requirements-lama.txt nếu muốn LaMa
FLOW2API_BASE_URL=http://127.0.0.1:8000 python main.py
# PLAYGROUND_HOST / PLAYGROUND_PORT mặc định 0.0.0.0:8000
```

## Test

```bash
python -m pytest tests/ -v
```

Toàn bộ test mock flow2api ở tầng HTTP client — chạy không cần engine thật
(cần `cv2` + `numpy` cho phần watermark; thiếu thì các test đó tự skip).
