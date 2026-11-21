"""Cấu hình playground qua biến môi trường (không cần file config).

Project này là một front-end độc lập đứng trước flow2api:
- FLOW2API_BASE_URL: địa chỉ container/service flow2api (engine generation).
- Các biến WATERMARK_*: bật/tắt + tham số xoá watermark phía playground.
"""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


# Địa chỉ flow2api đích (engine generation). Có thể là container nội mạng
# (http://flow2api:8000) hoặc LAN (http://192.168.10.132:38000).
FLOW2API_BASE_URL = os.environ.get("FLOW2API_BASE_URL", "http://192.168.10.132:38000").rstrip("/")

# Timeout một request generation (video chạy lâu) — khớp JOB_MAX_WAIT_MS phía client.
FLOW2API_TIMEOUT = _env_float("FLOW2API_TIMEOUT", 20 * 60)

# ── Watermark (nằm ở playground, không còn ở flow2api) ──────────────────
WATERMARK_ENABLED = _env_bool("WATERMARK_ENABLED", True)
# auto = LaMa nếu cài được, fallback OpenCV TELEA.
WATERMARK_ENGINE = os.environ.get("WATERMARK_ENGINE", "auto").strip().lower()
WATERMARK_MASK_DIR = os.environ.get("WATERMARK_MASK_DIR", "masks")
# Crop % mỗi cạnh cho video có WM viền (veo-3.1-lite). 0 = tắt crop.
WATERMARK_VIDEO_CROP_PERCENT = _env_float("WATERMARK_VIDEO_CROP_PERCENT", 2.0)

# ── Đường dẫn runtime ───────────────────────────────────────────────────
DATA_DIR = PROJECT_ROOT / "data"
TMP_DIR = PROJECT_ROOT / "tmp"
STATIC_DIR = PROJECT_ROOT / "static"
RESULTS_DIR = TMP_DIR / "playground_results"
REFS_DIR = TMP_DIR / "playground_refs"

# ── Generation ──────────────────────────────────────────────────────────
PLAYGROUND_MAX_RUNNING_JOBS = _env_int("PLAYGROUND_MAX_RUNNING_JOBS", 3)

# Cap ảnh tham chiếu: image i2i cho phép 10, video ingredients upstream chỉ
# xác nhận tới 3 nên giữ 3 để không đốt quota tổ hợp thất bại.
MAX_REFERENCE_IMAGES = {"image": 10, "video": 3}
