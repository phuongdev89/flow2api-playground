# Thư mục mask xoá watermark

Chứa mask WM **vẽ tay từ playground** và **ảnh ví dụ** làm nền vẽ mask. Đây là
dữ liệu runtime của từng deployment (đã gitignore, trừ seed mask legacy được
track sẵn) — volume mount `./masks:/app/masks` trong docker-compose giữ chúng
qua các lần rebuild.

## Quy ước tên file

### Mask — `{key}.png` với key = `{base}:{ratio}:{res}`

Sanitize: ký tự ngoài `[A-Za-z0-9._-]` đổi thành `_` — vd
`gemini-3.0-pro-image:9:16:2k` → `gemini-3.0-pro-image_9_16_2k.png`.

**Mỗi res một mask riêng** (1K ≠ 2K có WM ở chỗ khác nhau): mask vẽ cho 9:16
res 1K không bao giờ áp dụng cho generation 2K, và ngược lại.

### Ảnh ví dụ — `sample_{key}.{ext}`

Do nút **"Gen 1 ảnh ví dụ"** trong mask editor tạo: gen đúng combo
model + ratio + res, **giữ nguyên watermark** (mục đích là thấy đúng chỗ WM
cần tô), lưu lại để các lần mở editor sau có nền sẵn.

### Fallback khi key chưa có mask

Thứ tự tra của `load_mask_for_key` (`app/watermark.py`):

1. Key chính `{base}:{ratio}:{res}` (exact).
2. Key legacy của model id đã resolve: `{base}-{orient}-{res}` rồi
   `{base}-{orient}` — orient đầy đủ theo `ASPECT_RATIO_MAP`
   (landscape / square / three-four / four-three / portrait). Các seed mask
   `-portrait-2k`, `-four-three.png`… trong repo là dạng này.
3. Prefix sweep: file cùng prefix `{key}-*` — mask legacy lệch suffix res
   trong cùng nhóm orientation (vd mask `-portrait-2k` áp dụng cho generation
   1K cùng nhóm).
4. `watermark.png` — mask dùng chung cho mọi model (fallback cuối).

Không bao giờ lẫn giữa các nhóm orientation (mask 9:16 không áp cho 3:4) và
không lẫn giữa các res ở key chính.

## Định dạng mask

- Ảnh grayscale, **trắng (255) = vùng xoá (inpaint), đen (0) = giữ nguyên**.
- Kích thước bất kỳ — server tự resize INTER_NEAREST về đúng khung ảnh lúc
  xoá WM, nên mask vẽ trên cỡ nào cũng dùng được cho mọi kích thước trong
  cùng combo.
- File do playground lưu luôn là PNG 0/255.

## Cách vẽ trong playground

1. Topbar → **Mask WM** → chọn dòng đúng **model · ratio · res**.
2. **Vẽ mask** → editor cần nền: **"Gen 1 ảnh ví dụ"** (tạo ảnh mẫu có WM,
   đúng combo) hoặc ảnh cùng combo trong project (◀ ▶ duyệt).
3. Tô vùng WM → **Lưu mask**. Mở lại editor thấy mask cũ — vẽ thêm là nối
   vào, **Đặt lại** xoá sạch để vẽ mới. Nút **Xóa** trên dòng panel xoá mọi
   mask của combo đó.

## Ai dùng mask này?

Mọi generation image trong playground (sau khi đăng nhập): nếu
`WATERMARK_ENABLED=1` và có mask khớp combo đang tạo, ảnh kết quả được
inpaint theo mask trước khi lưu vào project. Video không dùng mask — crop
theo `%` viền (`WATERMARK_VIDEO_CROP_PERCENT`).

Cấu hình liên quan: `WATERMARK_ENABLED`, `WATERMARK_ENGINE`, `WATERMARK_MASK_DIR`
(xem README gốc).
