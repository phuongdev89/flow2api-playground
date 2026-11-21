# 📌 THƯ MỤC MASK WATERMARK (theo từng model)

Thư mục này chứa mask xoá watermark **theo từng model ảnh**, tên file là
`{model_id}.png` — vd `gemini-3.0-pro-image-square.png`.

## Cách chuẩn nhất: vẽ ngay trong trang quản trị

1. Mở **Trang quản trị → tab "水印 Mask" (Watermark Mask)**.
2. Chọn model ảnh ở cột trái → bấm **生成测试图片** (gen thử 1 ảnh nội dung bất kỳ)
   hoặc **上传图片** (upload ảnh có watermark).
3. Dùng **cọ** tô vùng watermark (gạt **橡皮擦** để tẩy, có **撤销/清空**).
4. Bấm **保存 Mask** — mask lưu thành `masks/{model_id}.png`, server dùng ngay
   (hot reload, không cần restart).

## Định dạng mask (nếu thả file thủ công)

- Ảnh grayscale, **trắng (255) = vùng xoá, đen (0) = giữ**.
- Kích thước bất kỳ — hệ thống tự resize (INTER_NEAREST) theo khung ảnh đầu ra.
- `watermark.png` (không theo model) là **mask dùng chung**: model nào chưa có
  mask riêng sẽ dùng nó (nếu tồn tại).

## Ai dùng mask này?

Chỉ các **token được đánh dấu "có watermark"** (cột actions nút **水印** trong
tab Token 管理, hoặc `POST /api/tokens/{id}/watermark`):

- **Ảnh đầu ra** → tự xoá WM theo mask của model đang dùng.
- **Video veo-3.1-lite** → tự crop đều mỗi cạnh 2%.
- Token không đánh dấu → trả nguyên bản như cũ.

Cấu hình liên quan trong `config/setting.toml` (mặc định đã bật):

```toml
[watermark]
enabled = true      # false = tắt hẳn
engine = "auto"     # auto = LaMa nếu cài được, không thì OpenCV
mask_dir = "masks"
```
