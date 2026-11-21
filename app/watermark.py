"""Watermark removal — LaMa local inpainting với fallback OpenCV.

Port từ flow2api src/services/watermark_remover.py (commit d8728dd), tách thành
module độc lập của playground: thay core.config bằng app/config (env) và
core.logger bằng logging chuẩn (in ra stdout, nhìn được qua docker logs).

Model LaMa (big-lama.pt) qua gói simple-lama-inpainting, fallback cv2.inpaint.
Pixel ngoài mask được ép giữ nguyên 100%.

Mask là **per-key**: đặt tại ``masks/{key}.png`` với key dạng
``{base_model}:{ratio}`` (vd ``gemini-3.0-pro-image:9:16`` — sanitize thành
``gemini-3.0-pro-image_9_16.png``), vẽ từ modal Mask WM trong playground.
Thứ tự fallback khi key không có mask:
  1. mask legacy của model id đã resolve ({base}-{orient}, {base}-{orient}-{res})
  2. mask dùng chung ``masks/watermark.png``

Mask được tự resize INTER_NEAREST về đúng kích thước ảnh lúc remove — một mask
vẽ trên ảnh bất kỳ dùng được cho mọi resolution/aspect cùng nhóm.

Ngoài ra crop video (ffmpeg) cắt vùng WM viền (veo-3.1-lite): crop đều mỗi
cạnh một tỷ lệ phần trăm.
"""

import asyncio
import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import WATERMARK_ENABLED, WATERMARK_ENGINE, WATERMARK_MASK_DIR

logger = logging.getLogger("playground.watermark")

# Ưu tiên opencv-python-headless trong môi trường server; cả hai cùng dùng tên import cv2.
try:
    import cv2
    import numpy as np
except Exception as _cv_err:  # pragma: no cover - chỉ khi thiếu dependency
    cv2 = None
    np = None
    _CV_IMPORT_ERROR = _cv_err
else:
    _CV_IMPORT_ERROR = None

_MASK_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
# Định dạng OpenCV encode lại được; định dạng khác (gif/avif...) sẽ giữ nguyên ảnh gốc.
_REENCODE_BY_EXTENSION = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
GLOBAL_MASK_STEM = "watermark"  # mask dùng chung cho mọi model (fallback)

_lama = None
_lama_failed = False
_lama_lock = threading.Lock()

# Cache mask theo đường dẫn file: {path_str: (mtime, mask)}
_mask_cache: Dict[str, Tuple[float, "np.ndarray"]] = {}


def _mask_dir() -> Path:
    path = Path(WATERMARK_MASK_DIR)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / path
    return path


def sanitize_key(key: str) -> str:
    """mask key → tên file an toàn (chữ cái/số/dấu chấm/gạch dưới/gạch ngang)."""
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in str(key or ""))
    return safe.strip("._") or "default"


def mask_path_for_key(key: Optional[str]) -> Path:
    """Đường dẫn file mask của key (chưa chắc tồn tại)."""
    if not key:
        return _mask_dir() / f"{GLOBAL_MASK_STEM}.png"
    return _mask_dir() / f"{sanitize_key(key)}.png"


def list_saved_masks() -> List[Dict]:
    """Danh sách mask đang có trong thư mục mask (cho API settings)."""
    mask_dir = _mask_dir()
    result = []
    if not mask_dir.is_dir():
        return result
    for p in sorted(mask_dir.iterdir()):
        if p.is_file() and p.suffix.lower() in _MASK_EXTENSIONS:
            try:
                stat = p.stat()
            except OSError:
                continue
            result.append(
                {
                    "filename": p.name,
                    "key": "__global__" if p.stem.lower() == GLOBAL_MASK_STEM else p.stem,
                    "size_bytes": stat.st_size,
                    "updated_at": stat.st_mtime,
                }
            )
    return result


def is_available(key: Optional[str] = None, fallback_keys: Optional[List[str]] = None) -> bool:
    """Đúng nghĩa "sẵn sàng xoá WM": bật trong config + có cv2 + có mask."""
    if not WATERMARK_ENABLED:
        return False
    if cv2 is None or np is None:
        return False
    return load_mask_for_key(key, fallback_keys) is not None


def get_engine_name() -> str:
    """Tên engine sẽ dùng cho log: lama / opencv / disabled."""
    if not WATERMARK_ENABLED or cv2 is None or np is None:
        return "disabled"
    if WATERMARK_ENGINE in ("auto", "lama") and get_lama() is not None:
        return "lama"
    return "opencv"


def get_lama():
    """Nạp lười engine LaMa. Trả về None nếu không khả dụng (rơi về OpenCV).

    Gói simple-lama-inpainting tự dùng env LAMA_MODEL nếu có, không thì tự tải
    big-lama.pt về ~/.cache/torch/hub/checkpoints/ (chỉ lần đầu, cần mạng).
    """
    global _lama, _lama_failed
    if _lama_failed:
        return None
    if _lama is None:
        with _lama_lock:
            if _lama is None and not _lama_failed:
                try:
                    import torch

                    if not hasattr(torch, "_playground_wm_patched"):
                        # checkpoint big-lama.pt được trace trên CUDA — máy torch
                        # CPU-only phải ép map_location="cpu" lúc load, nếu không nạp sẽ vỡ.
                        _orig_jit_load = torch.jit.load

                        def _cpu_jit_load(path, *args, **kwargs):
                            kwargs.setdefault("map_location", "cpu")
                            return _orig_jit_load(path, *args, **kwargs)

                        torch.jit.load = _cpu_jit_load
                        torch._playground_wm_patched = True
                    from simple_lama_inpainting import SimpleLama

                    started_at = time.time()
                    _lama = SimpleLama()  # có CUDA thì tự chạy GPU, không thì CPU
                    logger.info(
                        "[WATERMARK] Engine LaMa sẵn sàng (nạp %.1fs)", time.time() - started_at
                    )
                except Exception as e:
                    _lama_failed = True
                    logger.warning("[WATERMARK] Không nạp được LaMa (%s) — dùng OpenCV inpaint.", e)
    return _lama


def _read_mask_file(mask_path: Path) -> Optional["np.ndarray"]:
    """Đọc + chuẩn hoá một file mask (0/255), cache theo mtime."""
    try:
        mtime = mask_path.stat().st_mtime
    except OSError:
        return None

    cached = _mask_cache.get(str(mask_path))
    if cached and cached[0] == mtime:
        return cached[1]

    try:
        # Đọc qua bộ đệm để không vỡ đường dẫn tiếng Việt trên Windows.
        buf = np.fromfile(str(mask_path), dtype=np.uint8)
        mask = cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE)
        if mask is None:
            raise ValueError("cv2.imdecode trả về None (file không phải ảnh hợp lệ)")
        mask = (mask > 127).astype(np.uint8) * 255  # chuẩn hoá 0/255
    except Exception as e:
        logger.warning("[WATERMARK] Không đọc được mask %s: %s", mask_path.name, e)
        return None

    _mask_cache[str(mask_path)] = (mtime, mask)
    logger.info("[WATERMARK] Đã nạp mask: %s %dx%d", mask_path.name, mask.shape[1], mask.shape[0])
    return mask


def load_mask_for_key(
    key: Optional[str] = None, fallback_keys: Optional[List[str]] = None
) -> Optional["np.ndarray"]:
    """Mask theo thứ tự: key chính → fallback keys → watermark.png dùng chung."""
    if cv2 is None or np is None:
        return None
    mask_dir = _mask_dir()
    if not mask_dir.is_dir():
        return None

    candidates: List[Path] = []
    for candidate_key in ([key, *(fallback_keys or [])] if key is not None else []):
        if candidate_key:
            candidates.append(mask_path_for_key(candidate_key))
    candidates.append(mask_dir / f"{GLOBAL_MASK_STEM}.png")

    for path in candidates:
        if path.is_file():
            mask = _read_mask_file(path)
            if mask is not None:
                return mask
    return None


def save_mask_for_key(key: str, png_bytes: bytes) -> Path:
    """Lưu mask (bytes PNG) cho key. Trả về đường dẫn đã ghi."""
    if cv2 is None or np is None:
        raise RuntimeError("Thiếu opencv/numpy — không thể lưu mask")
    import numpy as _np

    buf = _np.frombuffer(png_bytes, dtype=_np.uint8)
    decoded = cv2.imdecode(buf, cv2.IMREAD_UNCHANGED)
    if decoded is None:
        raise ValueError("File mask không phải ảnh hợp lệ")
    if decoded.ndim == 3:
        # RGBA/BGR → grayscale theo độ sáng (vùng tô trắng sẽ giữ 255)
        decoded = cv2.cvtColor(
            decoded, cv2.COLOR_BGRA2GRAY if decoded.shape[2] == 4 else cv2.COLOR_BGR2GRAY
        )
    mask = (decoded > 127).astype("uint8") * 255

    mask_dir = _mask_dir()
    mask_dir.mkdir(parents=True, exist_ok=True)
    target = mask_path_for_key(key)
    ok, encoded = cv2.imencode(".png", mask)
    if not ok:
        raise RuntimeError("Mã hoá mask lỗi")
    encoded.tofile(str(target))
    # Invalidate cache để request sau đọc ngay mask mới (mtime có thể trông giống cũ)
    _mask_cache.pop(str(target), None)
    logger.info(
        "[WATERMARK] Đã lưu mask cho key %s: %s %dx%d",
        key, target.name, mask.shape[1], mask.shape[0],
    )
    return target


def delete_mask_for_key(key: str) -> bool:
    """Xoá mask của key. Trả True nếu có file bị xoá."""
    target = mask_path_for_key(key)
    existed = target.exists()
    try:
        if existed:
            target.unlink()
    finally:
        _mask_cache.pop(str(target), None)
    return existed


# --- Inpaint core (đồng bộ, chạy trong thread riêng) ---


def resize_mask(mask: "np.ndarray", shape) -> "np.ndarray":
    """Resize mask 0/255 về đúng kích thước ảnh đích (nearest, giữ 0/255)."""
    h, w = shape[:2]
    if mask.shape[0] == h and mask.shape[1] == w:
        return mask
    return cv2.resize(mask, (w, h), interpolation=cv2.INTER_NEAREST)


def mask_bbox(mask: "np.ndarray", pad_ratio: float = 0.5, min_pad: int = 16):
    """Hộp chữ nhật quanh mask + đệm bối cảnh, giới hạn trong khung — (x0, y0, x1, y1)."""
    ys, xs = np.where(mask > 0)
    h, w = mask.shape[:2]
    y0, y1 = int(ys.min()), int(ys.max())
    x0, x1 = int(xs.min()), int(xs.max())
    pad = int(max(x1 - x0, y1 - y0) * pad_ratio) + min_pad
    return (
        max(0, x0 - pad),
        max(0, y0 - pad),
        min(w, x1 + pad + 1),
        min(h, y1 + pad + 1),
    )


def inpaint_image(img: "np.ndarray", mask: "np.ndarray", engine: str):
    """Trả về (ảnh đã xử lý, engine đã dùng). Pixel ngoài mask giữ nguyên 100%."""
    if engine in ("auto", "lama"):
        lama = get_lama()
        if lama is not None:
            from PIL import Image as PILImage

            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            res = lama(PILImage.fromarray(rgb), PILImage.fromarray(mask))
            if res.size != (rgb.shape[1], rgb.shape[0]):
                # SimpleLama đệm khung lên bội số 8 rồi trả nguyên khung đệm — cắt về kích thước gốc
                res = res.crop((0, 0, rgb.shape[1], rgb.shape[0]))
            out = cv2.cvtColor(np.asarray(res), cv2.COLOR_RGB2BGR)
            outside = mask == 0
            out[outside] = img[outside]
            return out, "lama"
    result = cv2.inpaint(img, mask, 5, cv2.INPAINT_TELEA)
    return result, "opencv"


def inpaint_image_crop(img: "np.ndarray", mask: "np.ndarray", engine: str):
    """Inpaint chỉ vùng crop quanh mask rồi paste lại khung — nhanh hơn nhiều so
    với LaMa full-frame, phần ngoài mask vẫn giữ nguyên 100%."""
    x0, y0, x1, y1 = mask_bbox(mask)
    crop, mcrop = img[y0:y1, x0:x1], mask[y0:y1, x0:x1]
    res_crop, used = inpaint_image(crop, mcrop, engine)
    if res_crop.shape[:2] != crop.shape[:2]:
        # LaMa đệm khung lên bội số 8 — cắt bỏ phần đệm để paste khớp crop
        res_crop = res_crop[: crop.shape[0], : crop.shape[1]]
    out = img.copy()
    out[y0:y1, x0:x1] = res_crop
    return out, used


def _encode_image(img: "np.ndarray", ext: str) -> Optional[bytes]:
    """Encode ảnh về đúng định dạng gốc (jpg/png/webp/bmp); định dạng khác trả None."""
    ext = ext.lower()
    if ext not in _REENCODE_BY_EXTENSION:
        return None
    params = [cv2.IMWRITE_JPEG_QUALITY, 95] if ext in (".jpg", ".jpeg") else []
    ok, encoded = cv2.imencode(ext, img, params)
    if not ok:
        return None
    return encoded.tobytes()


def _inpaint_bytes_sync(
    image_bytes: bytes, ext_hint: str, mask_template: "np.ndarray"
) -> Tuple[bytes, str]:
    """Đồng bộ: bytes ảnh + mask → (bytes ảnh đã xoá WM, engine)."""
    buf = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Không decode được ảnh (định dạng không hỗ trợ?)")

    mask = resize_mask(mask_template, img.shape)

    engine = WATERMARK_ENGINE if WATERMARK_ENGINE in ("auto", "lama", "opencv") else "auto"
    # LaMa full-frame với ảnh lớn rất chậm — crop quanh mask là đủ và giữ pixel ngoài mask nguyên vẹn.
    out, used = inpaint_image_crop(img, mask, engine)

    encoded = _encode_image(out, ext_hint)
    if encoded is None:
        raise ValueError(f"Không encode lại được ảnh định dạng {ext_hint}")
    return encoded, used


async def remove_watermark(
    image_bytes: bytes,
    ext_hint: str = ".jpg",
    key: Optional[str] = None,
    fallback_keys: Optional[List[str]] = None,
) -> Tuple[bytes, str]:
    """Điểm vào chính (async): xoá watermark trong ảnh bytes theo mask của key.

    Trả về ``(image_bytes, status)`` với status một trong:
      - ``"processed:<engine>"`` — đã xoá WM (engine = lama / opencv)
      - ``"skipped:<lý do>"``    — không xử lý (tắt trong config, thiếu mask/deps,
        decode lỗi...), trả nguyên ảnh gốc, không làm vỡ request.

    Chạy inpaint trong thread riêng để không block event loop của FastAPI.
    """
    if not image_bytes:
        return image_bytes, "skipped:empty"

    if not WATERMARK_ENABLED:
        return image_bytes, "skipped:disabled"

    if cv2 is None or np is None:
        logger.warning("[WATERMARK] Thiếu opencv/numpy (%s) — bỏ qua xoá WM.", _CV_IMPORT_ERROR)
        return image_bytes, "skipped:no_cv2"

    mask_template = load_mask_for_key(key, fallback_keys)
    if mask_template is None:
        logger.warning(
            "[WATERMARK] Chưa có mask cho key %s — tô mask trong modal Mask WM của playground "
            "(menu ⋯ trên ảnh → 'Tạo mask WM') hoặc thả file vào %s.",
            key or "(mặc định)", _mask_dir(),
        )
        return image_bytes, "skipped:no_mask"

    try:
        result, engine_used = await asyncio.to_thread(
            _inpaint_bytes_sync, image_bytes, ext_hint, mask_template
        )
        return result, f"processed:{engine_used}"
    except Exception as e:
        logger.warning("[WATERMARK] Xoá WM thất bại, trả ảnh gốc: %s", e)
        return image_bytes, f"skipped:error:{e}"


# --- Crop video (ffmpeg) — cắt vùng watermark viền video (veo-3.1-lite) ---


def _find_ffmpeg() -> Optional[str]:
    return shutil.which("ffmpeg")


def crop_video_percent_sync(file_path, percent_per_edge: float = 2.0) -> str:
    """Đồng bộ: crop video đều mỗi cạnh ``percent_per_edge``% (nền giữa giữ nguyên),
    re-encode H.264 CRF 18, copy âm thanh. Ghi đè an toàn qua file .crop.mp4.

    Trả về status: "cropped" / "skipped:no_ffmpeg" / "skipped:error:<lý do>".
    """
    path = Path(file_path)
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        logger.warning("[WATERMARK] Không tìm thấy ffmpeg trên PATH — bỏ qua crop video.")
        return "skipped:no_ffmpeg"

    percent_per_edge = max(0.0, min(25.0, float(percent_per_edge or 0.0)))
    if percent_per_edge <= 0:
        return "skipped:disabled"
    # percent_per_edge là tỷ lệ cắt MỖI CẠNH (trái+phải, trên+dưới) → giữ lại 1 - 2p
    keep = 1.0 - (percent_per_edge * 2.0) / 100.0
    # Ép chiều ra số chẵn (yuv420p yêu cầu) và giữ crop căn giữa.
    vf = f"crop=trunc(iw*{keep:.6f}/2)*2:trunc(ih*{keep:.6f}/2)*2"

    out_path = path.with_suffix(f"{path.suffix}.crop{path.suffix}")
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(path),
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-c:a",
        "copy",
        str(out_path),
    ]
    import subprocess

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=600)
        if result.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
            err = result.stderr.decode("utf-8", errors="ignore")[-300:] if result.stderr else "unknown"
            raise RuntimeError(f"ffmpeg returncode={result.returncode}: {err}")
        out_path.replace(path)
        return "cropped"
    except Exception as e:
        logger.warning("[WATERMARK] Crop video thất bại: %s", e)
        if out_path.exists():
            try:
                out_path.unlink()
            except Exception:
                pass
        return f"skipped:error:{e}"


async def crop_video_percent(file_path, percent_per_edge: float = 2.0) -> str:
    """Async crop video (chạy trong thread riêng, không block event loop)."""
    return await asyncio.to_thread(crop_video_percent_sync, file_path, percent_per_edge)
