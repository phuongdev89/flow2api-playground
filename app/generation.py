"""Playground generation engine — HTTP client của flow2api.

Trước đây playground chạy pipeline in-process (dùng chung generation_handler
của flow2api). Sau khi tách project, playground compose request OpenAI-compatible
(model alias + generationConfig + content parts) rồi POST sang flow2api
/v1/chat/completions; kết quả URL được tải về, xoá watermark (nếu bật) và lưu
vào tmp của playground trước khi trả về cho frontend.

Các hàm compose được copy nguyên logic từ flow2api src/main.py:632-957 — chỉ
đổi nguồn tra bảng capability sang app/catalog.py (snapshot tĩnh).
"""

import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Optional

import httpx

from . import watermark
from .catalog import ASPECT_RATIO_MAP, MODEL_CONFIG, MODEL_SUPPORTED_SIZES, VIDEO_BASE_MODELS
from .config import (
    FLOW2API_BASE_URL,
    FLOW2API_TIMEOUT,
    MAX_REFERENCE_IMAGES,
    RESULTS_DIR,
    TMP_DIR,
    WATERMARK_ENABLED,
    WATERMARK_VIDEO_CROP_PERCENT,
)

logger = logging.getLogger("playground.generation")

MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)")
HTML_VIDEO_RE = re.compile(r"<video[^>]*src=[\"']([^\"']+)[\"']")


# ──────────────────────────────────────────────────────────────────
# Capability / compose (copy từ flow2api src/main.py)
# ──────────────────────────────────────────────────────────────────

PLAYGROUND_IMAGE_RATIOS = ("16:9", "4:3", "1:1", "3:4", "9:16")
PLAYGROUND_VIDEO_RATIOS = ("16:9", "9:16")
PLAYGROUND_LENGTHS = (4, 6, 8)
PLAYGROUND_IMAGE_BASE_RES = "1K"
# Bỏ 4K cho ảnh: upstream ảnh chỉ dùng 1K/2K cho playground (giống Flow).
PLAYGROUND_IMAGE_UPSAMPLE_RES = ("2K",)
PLAYGROUND_VIDEO_BASE_RES = "720p"
PLAYGROUND_VIDEO_UPSAMPLE_RES = ("1080p", "4K")

# Bảng model hiển thị cho UI (base alias dùng để compose request).
PLAYGROUND_IMAGE_MODELS = (
    {"id": "nano-banana-pro", "label": "Nano Banana Pro", "base": "gemini-3.0-pro-image"},
)
PLAYGROUND_VIDEO_MODELS = (
    {"id": "veo-3.1-lite", "label": "Veo 3.1 - Lite", "base": "veo_3_1_t2v_lite"},
    {"id": "veo-3.1-fast", "label": "Veo 3.1 - Fast", "base": "veo_3_1_t2v_fast"},
    {"id": "veo-3.1-fast-ultra", "label": "Veo 3.1 - Fast Ultra", "base": "veo_3_1_t2v_fast_ultra"},
    {"id": "veo-3.1-quality", "label": "Veo 3.1 - Quality", "base": "veo_3_1_t2v"},
)

_IMAGE_ORIENT_TO_RATIO = {
    "landscape": "16:9",
    "portrait": "9:16",
    "square": "1:1",
    "four-three": "4:3",
    "four_three": "4:3",
    "three-four": "3:4",
    "three_four": "3:4",
}

_KNOWN_IMAGE_FAMILIES = {
    "gemini-3.0-pro-image": {
        "id": "gemini-3.0-pro-image",
        "base": "gemini-3.0-pro-image",
        "label": "Nano Banana Pro",
        "default_res": ["1K", "2K"],
    },
    "nano-banana-pro": {
        "id": "nano-banana-pro",
        "base": "gemini-3.0-pro-image",
        "label": "Nano Banana Pro",
        "default_res": ["1K", "2K"],
    },
    "gemini-3.1-flash-image": {
        "id": "gemini-3.1-flash-image",
        "base": "gemini-3.1-flash-image",
        "label": "Nano Banana 2",
        "default_res": ["1K", "2K"],
    },
    "nano-banana-2": {
        "id": "nano-banana-2",
        "base": "gemini-3.1-flash-image",
        "label": "Nano Banana 2",
        "default_res": ["1K", "2K"],
    },
    "nano-banana-2.1": {
        "id": "nano-banana-2.1",
        "base": "gemini-3.1-flash-image",
        "label": "Nano Banana 2.1",
        "default_res": ["1K", "2K"],
    },
    "gemini-3.1-flash-lite-image": {
        "id": "gemini-3.1-flash-lite-image",
        "base": "gemini-3.1-flash-lite-image",
        "label": "Nano Banana 2 Lite",
        "default_res": ["1K", "2K"],
    },
    "nano-banana-2-lite": {
        "id": "nano-banana-2-lite",
        "base": "gemini-3.1-flash-lite-image",
        "label": "Nano Banana 2 Lite",
        "default_res": ["1K", "2K"],
    },
    "gemini-2.5-flash-image": {
        "id": "gemini-2.5-flash-image",
        "base": "gemini-2.5-flash-image",
        "label": "Nano Banana",
        "default_res": ["1K"],
    },
    "gemini_2_5_flash_image": {
        "id": "gemini_2_5_flash_image",
        "base": "gemini-2.5-flash-image",
        "label": "Nano Banana",
        "default_res": ["1K"],
    },
    "nano-banana": {
        "id": "nano-banana",
        "base": "gemini-2.5-flash-image",
        "label": "Nano Banana",
        "default_res": ["1K"],
    },
}

_IMAGE_VARIANT_RE = re.compile(
    r"^(?P<stem>.+?)(?:[-_](?P<orient>landscape|portrait|square|four[-_]three|three[-_]four))?(?:[-_](?P<res>1k|2k|4k))?$",
    re.IGNORECASE,
)

_IMAGE_HINT_RE = re.compile(
    r"(?:nano[-_]banana|imagen|[-_]image(?:$|[-_])|gpt[-_]image|dall[-_]e|flux|sdxl|stable[-_]diffusion|midjourney|ideogram|recraft|seedream)",
    re.IGNORECASE,
)
_VIDEO_HINT_RE = re.compile(
    r"(?:^veo[-_]|[-_](?:t2v|i2v|r2v|video)(?:$|[-_])|interpolation[-_]lite|^omni[-_]|sora|kling|hailuo|minimax|runway|luma|seedance|^wan[-_0-9]|pika|hunyuan[-_]video)",
    re.IGNORECASE,
)
_VIDEO_VARIANT_RE = re.compile(
    r"^(?P<stem>.+?)(?:[-_](?P<orient>landscape|portrait))?(?:[-_](?P<dur>\d+s))?(?:[-_](?P<res>720p|1080p|4k))?$",
    re.IGNORECASE,
)

# Các họ video model sắp/đã discontinue — bỏ qua khỏi dropdown video động
# (vẫn giữ extend variant ở tầng resolve nếu user nối tiếp video cũ).
_DISCONTINUED_VIDEO_FAMILIES = frozenset({
    "veo-3.1-fast",
    "veo-3.1-fast-ultra",
    "veo-3.1-fast-relaxed",
    "veo-3.1-fast-ultra-relaxed",
    "veo-3.1-quality",
    "omni-1.1-flash",
})


def _playground_orientation(ratio: str) -> str:
    return "portrait" if ASPECT_RATIO_MAP.get(ratio, ratio) == "portrait" else "landscape"


def _playground_model_spec(mode: str, model_id: str) -> dict | None:
    catalog = PLAYGROUND_IMAGE_MODELS if mode == "image" else PLAYGROUND_VIDEO_MODELS
    return next((spec for spec in catalog if spec["id"] == model_id), None)


def _direct_video_res_key(
    alias: str, plain_key: str, upsampling: str, orientation: str
) -> str | None:
    """Tìm key MODEL_CONFIG đã gắn sẵn resolution + orientation (fast-ultra...)."""
    for candidate in (f"{plain_key}_{upsampling}", f"{alias}_{upsampling}"):
        config_entry = MODEL_CONFIG.get(candidate)
        if not config_entry or config_entry.get("type") != "video":
            continue
        aspect = str(config_entry.get("aspect_ratio") or "")
        if orientation == "portrait":
            if "PORTRAIT" in aspect:
                return candidate
        elif "PORTRAIT" not in aspect:
            return candidate
    return None


def _compose_video_request(
    base: str, length: int | None, res: str | None, ratio: str
) -> tuple[str, dict] | None:
    """Compose alias video gửi cho flow2api; None nếu combo không tồn tại thật."""
    orientation = _playground_orientation(ratio)
    upsampling = res.lower() if res and res.lower() in ("1080p", "4k") else None
    alias = f"{base}_{length}s" if length else base
    if alias not in VIDEO_BASE_MODELS:
        return None
    plain_key = VIDEO_BASE_MODELS[alias].get(orientation)
    if not plain_key or plain_key not in MODEL_CONFIG:
        return None
    gen_config = {"aspectRatio": ratio}
    if not upsampling:
        return alias, gen_config
    if f"{alias}_{upsampling}" in VIDEO_BASE_MODELS:
        # Resolver tự nối suffix resolution rồi map orientation.
        gen_config["imageSize"] = upsampling
        return alias, gen_config
    direct_key = _direct_video_res_key(alias, plain_key, upsampling, orientation)
    if direct_key:
        # Key MODEL_CONFIG có sẵn orientation — resolver trả về nguyên xi.
        return direct_key, gen_config
    return None


def _video_combo_supported(base: str, length: int | None, res: str | None) -> bool:
    return all(
        _compose_video_request(base, length, res, ratio) is not None
        for ratio in PLAYGROUND_VIDEO_RATIOS
    )


def _compose_video_reference_request(
    base: str, length: int | None, res: str | None, ratio: str
) -> tuple[str, dict] | None:
    """Compose key video r2v (reference-to-video).

    T2V base (veo_3_1_t2v*) không nhận ảnh — map sang họ r2v tương ứng bằng
    cách probe các biến thể orientation/res/duration trong MODEL_CONFIG, ưu
    tiên key đúng orientation trước key plain. None nghĩa là model không
    hỗ trợ tham chiếu.
    """
    family = base.replace("_t2v", "_r2v")
    if family == base:
        return None
    orientation = _playground_orientation(ratio)
    upsampling = res.lower() if res and res.lower() in ("1080p", "4k") else None
    if family.endswith("_ultra"):
        # fast_ultra: orientation đứng trước ultra (veo_3_1_r2v_fast_portrait_ultra).
        stem = family[: -len("_ultra")]
        forms = [f"{stem}_{orientation}_ultra", f"{stem}_ultra_{orientation}", family]
    else:
        forms = [f"{family}_{orientation}", family]
    if base.endswith("_lite"):
        # Lite chỉ có r2v qua alias cố định 8s (veo-3.1-lite-r2v-8s-*).
        forms.append(f"veo-3.1-lite-r2v-8s-{orientation}")
    for form in forms:
        candidates = []
        if upsampling and length:
            candidates += [f"{form}_{upsampling}_{length}s", f"{form}_{length}s_{upsampling}"]
        if length:
            candidates += [f"{form}_{length}s"]
        if upsampling:
            candidates += [f"{form}_{upsampling}"]
        candidates.append(form)
        for key in candidates:
            entry = MODEL_CONFIG.get(key)
            if entry and entry.get("type") == "video":
                return key, {"aspectRatio": ratio}
    return None


def _model_reference_supported(mode: str, base: str) -> bool:
    if mode == "image":
        # Image pipeline upload reference images như IMAGE_INPUT_TYPE_REFERENCE.
        return True
    return any(
        _compose_video_reference_request(base, None, None, ratio) is not None
        for ratio in PLAYGROUND_VIDEO_RATIOS
    )


def _compose_video_frames_request(
    base: str, length: int | None, res: str | None, ratio: str, images: int | None = None
) -> tuple[str, dict] | None:
    """Compose key i2v (Frames to Video — 1-2 ảnh làm frame đầu/cuối).

    Chỉ lite (veo_3_1_i2v_lite*) và quality (veo_3_1_i2v_s*) có i2v trong
    catalog; fast/fast-ultra không có → None. Key lite có suffix orientation,
    key quality không (resolver tự map từ generationConfig.aspectRatio).

    Lite tách theo số ảnh: 1 ảnh → veo_3_1_i2v_lite* (upstream chỉ nhận
    min=max=1); 2 ảnh → veo_3_1_interpolation_lite* (interpolation frame
    đầu+cuối, min=max=2) — gửi 2 ảnh vào key i2v thường sẽ bị upstream từ
    chối ("first/last-frame model requires 1-1 images; 2 provided").
    Quality dùng chung key i2v_s* (nhận 1-2 ảnh). ``images`` None là probe
    capability — chỉ tìm key, không lọc theo số ảnh.
    """
    if base.endswith("_lite"):
        family = "veo_3_1_interpolation_lite" if images == 2 else "veo_3_1_i2v_lite"
        forms = [f"{family}_{_playground_orientation(ratio)}", family]
    elif base == "veo_3_1_t2v":
        family = "veo_3_1_i2v_s"
        forms = [family]
    else:
        return None
    upsampling = res.lower() if res and res.lower() in ("1080p", "4k") else None
    for form in forms:
        candidates = []
        if upsampling and length:
            candidates += [f"{form}_{upsampling}_{length}s", f"{form}_{length}s_{upsampling}"]
        if length:
            candidates += [f"{form}_{length}s"]
        if upsampling:
            candidates += [f"{form}_{upsampling}"]
        candidates.append(form)
        for key in candidates:
            entry = MODEL_CONFIG.get(key)
            if entry and entry.get("type") == "video" and entry.get("video_type") == "i2v":
                if images is not None:
                    min_images = entry.get("min_images", 1)
                    max_images = entry.get("max_images", 2)
                    if not min_images <= images <= max_images:
                        continue
                return key, {"aspectRatio": ratio}
    return None


def _video_frames_supported(base: str) -> bool:
    # Probe không có length lẫn với các length chuẩn — quality (i2v_s) chỉ
    # tồn tại key khi có suffix duration.
    for length in (None, *PLAYGROUND_LENGTHS):
        if any(
            _compose_video_frames_request(base, length, None, ratio) is not None
            for ratio in PLAYGROUND_VIDEO_RATIOS
        ):
            return True
    return False


def _compose_video_extend_request(ratio: str) -> tuple[str, dict] | None:
    """Compose key extend (nối tiếp video qua extend://MEDIA_ID).

    Upstream chỉ có 1 model extend: key landscape là ``veo_3_1_extend``
    (model_key nội bộ mới mang suffix), portrait là ``veo_3_1_extend_portrait``.
    """
    orientation = _playground_orientation(ratio)
    for key in (f"veo_3_1_extend_{orientation}", "veo_3_1_extend"):
        entry = MODEL_CONFIG.get(key)
        if entry and entry.get("type") == "video" and entry.get("video_type") == "extend":
            return key, {"aspectRatio": ratio}
    return None


def _playground_image_model_options() -> list[dict]:
    models = []
    for spec in PLAYGROUND_IMAGE_MODELS:
        base = spec["base"]
        ratios = [
            ratio
            for ratio in PLAYGROUND_IMAGE_RATIOS
            if f"{base}-{ASPECT_RATIO_MAP[ratio]}" in MODEL_CONFIG
        ]
        if not ratios:
            continue
        upsampling = []
        for res in PLAYGROUND_IMAGE_UPSAMPLE_RES:
            size = res.lower()
            if size in MODEL_SUPPORTED_SIZES.get(base, []) and all(
                f"{base}-{ASPECT_RATIO_MAP[ratio]}-{size}" in MODEL_CONFIG
                for ratio in ratios
            ):
                upsampling.append(res)
        models.append(
            {
                "id": spec["id"],
                "label": spec["label"],
                # base alias — frontend dùng để compose mask key (WM) từ meta item.
                "base": spec["base"],
                "ratios": ratios,
                "res": [PLAYGROUND_IMAGE_BASE_RES, *upsampling],
                "refs": _model_reference_supported("image", base),
            }
        )
    return models


def _playground_video_model_options() -> list[dict]:
    models = []
    for spec in PLAYGROUND_VIDEO_MODELS:
        base = spec["base"]
        if not _video_combo_supported(base, None, None):
            continue
        lengths = [
            length for length in PLAYGROUND_LENGTHS if _video_combo_supported(base, length, None)
        ]
        probe_lengths = lengths or [None]
        upsampling = [
            res
            for res in PLAYGROUND_VIDEO_UPSAMPLE_RES
            if all(_video_combo_supported(base, length, res) for length in probe_lengths)
        ]
        models.append(
            {
                "id": spec["id"],
                "label": spec["label"],
                "base": spec["base"],
                "ratios": list(PLAYGROUND_VIDEO_RATIOS),
                # res=None nghĩa là model không có lựa chọn upsampling nào.
                "res": [PLAYGROUND_VIDEO_BASE_RES, *upsampling] if upsampling else None,
                "length": lengths or None,
                # refs = ingredients (r2v, tối đa 3 ảnh); frames = i2v frame
                # đầu/cuối (1-2 ảnh) — catalog chỉ có i2v cho lite + quality.
                "refs": _model_reference_supported("video", base),
                "frames": _video_frames_supported(base),
            }
        )
    return models


def _humanize_model_label(stem: str) -> str:
    """Chuyển model id (vd 'imagen-4.0-generate') thành label dễ đọc."""
    tokens = {
        "veo": "Veo",
        "gemini": "Gemini",
        "imagen": "Imagen",
        "omni": "Omni",
        "sora": "Sora",
        "kling": "Kling",
        "flux": "FLUX",
        "sdxl": "SDXL",
        "gpt": "GPT",
        "pro": "Pro",
        "flash": "Flash",
        "lite": "Lite",
        "fast": "Fast",
        "ultra": "Ultra",
        "relaxed": "Relaxed",
        "quality": "Quality",
        "image": "Image",
        "video": "Video",
        "r2v": "R2V",
        "i2v": "I2V",
        "t2v": "T2V",
        "2k": "2K",
        "4k": "4K",
        "1080p": "1080p",
        "720p": "720p",
    }
    parts = [part for part in re.split(r"[-_]+", stem.strip()) if part]
    if not parts:
        return stem
    rendered = []
    for part in parts:
        low = part.lower()
        rendered.append(tokens.get(low, part.capitalize() if part.isalpha() else part))
    return " ".join(rendered)


def _extract_model_res_from_entry(entry: dict) -> list[str] | None:
    raw = entry.get("res") or entry.get("resolutions") or entry.get("supported_sizes")
    if isinstance(raw, (list, tuple)):
        cleaned = [str(item).strip() for item in raw if str(item).strip()]
        if cleaned:
            return cleaned
    return None


def _extract_model_ratios_from_entry(entry: dict, default_ratios: list[str]) -> list[str]:
    raw = entry.get("ratios") or entry.get("aspect_ratios")
    if isinstance(raw, (list, tuple)):
        cleaned = [str(item).strip() for item in raw if str(item).strip()]
        if cleaned:
            return cleaned
    aspect = str(entry.get("aspect_ratio") or "").upper()
    if "PORTRAIT_THREE_FOUR" in aspect:
        return ["3:4"]
    if "FOUR_THREE" in aspect:
        return ["4:3"]
    if "SQUARE" in aspect:
        return ["1:1"]
    if "PORTRAIT" in aspect:
        return ["9:16"]
    if "LANDSCAPE" in aspect:
        return ["16:9"]
    return list(default_ratios)


def _extract_model_lengths_from_entry(entry: dict) -> list[int] | None:
    raw = entry.get("length") or entry.get("lengths") or entry.get("durations")
    if isinstance(raw, (list, tuple)):
        cleaned = []
        for item in raw:
            try:
                val = int(str(item).rstrip("sS"))
                if val > 0 and val not in cleaned:
                    cleaned.append(val)
            except ValueError:
                continue
        if cleaned:
            return sorted(cleaned)
    return None


def _label_from_model_description(entry: dict) -> str | None:
    """Lấy tên model sạch từ trường description của /v1/models (bỏ suffix · 4s · landscape / I2V)."""
    raw_desc = entry.get("description")
    if not isinstance(raw_desc, str):
        return None
    head = raw_desc.split(" · ")[0].strip()
    if not head:
        return None
    cleaned = re.sub(r"\s+(?:I2V|R2V|T2V|Extend)$", "", head, flags=re.IGNORECASE).strip()
    return cleaned or None


def _is_discontinued_video_family(family_id: str) -> bool:
    low = family_id.lower()
    return (
        low in _DISCONTINUED_VIDEO_FAMILIES
        or re.match(r"^omni[-_]1[._][01](?:$|[-_])", low) is not None
    )


def _strip_video_qualifiers(model_id: str) -> tuple[str, str, dict]:
    """Tách stem + sub_type + attributes khỏi video id bất kể thứ tự token.

    Ví dụ:
      ``omni-1.2-flash-3s-landscape`` -> (``omni-1.2-flash``, ``t2v``, {dur=3, ratio='16:9'})
      ``custom-video-portrait-6s-1080p`` -> (``custom-video``, ``t2v``, {dur=6, ratio='9:16', res='1080p'})
      ``omni-1.2-flash-i2v-5s-portrait`` -> (``omni-1.2-flash``, ``i2v``, {dur=5, ratio='9:16'})
    """
    stem = model_id.strip()
    ratio = None
    duration = None
    res = None
    sub_type = None
    had_qualifier = False

    token_re = re.compile(
        r"^(?P<head>.+?)[-_](?P<tok>landscape|portrait|4k|1080p|720p|\d+s|t2v|i2v|r2v|extend)$",
        re.IGNORECASE,
    )
    while True:
        match = token_re.match(stem)
        if not match:
            break
        tok = match.group("tok").lower()
        next_head = match.group("head")
        if tok == "landscape" and ratio is None:
            ratio = "16:9"
        elif tok == "portrait" and ratio is None:
            ratio = "9:16"
        elif tok == "4k" and res is None:
            res = "4K"
        elif tok == "1080p" and res is None:
            res = "1080p"
        elif tok == "720p" and res is None:
            res = "720p"
        elif tok.endswith("s") and tok[:-1].isdigit() and duration is None:
            duration = int(tok[:-1])
        elif tok in ("t2v", "i2v", "r2v", "extend") and sub_type is None:
            sub_type = tok
        else:
            break
        had_qualifier = True
        stem = next_head

    return stem, (sub_type or "t2v"), {
        "ratio": ratio,
        "duration": duration,
        "res": res,
        "had_qualifier": had_qualifier,
    }


def _classify_raw_model_type(entry: dict) -> str | None:
    """Xác định model từ /v1/models là 'image', 'video' hay bỏ qua (None)."""
    explicit = str(entry.get("type") or "").strip().lower()
    if explicit in ("image", "video"):
        return explicit
    model_id = str(entry.get("id") or "").strip()
    low = model_id.lower()
    if not low or "upsampler" in low or low in ("veo_3_1_extend", "veo_3_1_extend_portrait"):
        return None
    if str(entry.get("video_type") or "").strip().lower() == "extend" or re.search(r"(?:^|[-_])extend(?:$|[-_])", low):
        return None
    cfg = MODEL_CONFIG.get(model_id)
    if cfg and cfg.get("type") in ("image", "video"):
        return cfg["type"]
    if model_id in VIDEO_BASE_MODELS:
        return "video"
    modalities = entry.get("output_modalities") or entry.get("modalities")
    if isinstance(modalities, (list, tuple)):
        mods = {str(m).strip().lower() for m in modalities}
        if "video" in mods:
            return "video"
        if "image" in mods and "text" not in mods:
            return "image"
    if _VIDEO_HINT_RE.search(low):
        return "video"
    if _IMAGE_HINT_RE.search(low):
        return "image"
    return None


def _is_extend_video_entry(entry: dict) -> bool:
    model_id = str(entry.get("id") or "").strip().lower()
    if not model_id or "upsampler" in model_id:
        return False
    if str(entry.get("video_type") or "").strip().lower() == "extend":
        return True
    return bool(re.search(r"(?:^|[-_])extend(?:$|[-_])", model_id))


def _video_family_from_id(model_id: str) -> tuple[str, str, str, str] | None:
    """Map video model id -> (family_id, catalog_base, label, sub_type).

    Trả None nếu không thuộc họ Veo 3.1 chuẩn của flow2api.
    sub_type: 't2v' | 'i2v' | 'r2v' | 'extend'.
    """
    low = model_id.lower()
    if not (low.startswith("veo_3_1") or low.startswith("veo-3.1")):
        return None
    # Model tên riêng của gateway/upstream khác (vd veo-3.1-fast-generate-001) giữ nguyên làm model riêng.
    known_aliases = {
        "veo-3.1-lite", "veo_3_1_lite",
        "veo-3.1-fast", "veo_3_1_fast",
        "veo-3.1-fast-ultra", "veo_3_1_fast_ultra",
        "veo-3.1-fast-relaxed", "veo_3_1_fast_relaxed",
        "veo-3.1-fast-ultra-relaxed", "veo_3_1_fast_ultra_relaxed",
        "veo-3.1-quality", "veo_3_1_quality", "veo_3_1_t2v",
        "veo_3_1_extend", "veo_3_1_extend_portrait",
    }
    is_catalog_shape = (
        low in known_aliases
        or low.startswith(("veo_3_1_t2v", "veo_3_1_i2v", "veo_3_1_r2v", "veo_3_1_interpolation", "veo_3_1_extend"))
        or re.match(
            r"^veo[-_]3[._]1[-_](?:lite|fast|quality)[-_](?:t2v|i2v|r2v|extend|landscape|portrait|\d+s)",
            low,
        )
        is not None
    )
    if not is_catalog_shape:
        return None
    if re.search(r"(?:^|[-_])extend(?:$|[-_])", low):
        sub_type = "extend"
    elif re.search(r"(?:^|[-_])i2v(?:$|[-_])|interpolation", low):
        sub_type = "i2v"
    elif re.search(r"(?:^|[-_])r2v(?:$|[-_])", low):
        sub_type = "r2v"
    else:
        sub_type = "t2v"

    if re.search(r"(?:^|[-_])lite(?:$|[-_])", low):
        return ("veo-3.1-lite", "veo_3_1_t2v_lite", "Veo 3.1 - Lite", sub_type)
    if re.search(r"(?:^|[-_])ultra(?:$|[-_])", low) and "relaxed" in low:
        return (
            "veo-3.1-fast-ultra-relaxed",
            "veo_3_1_t2v_fast_ultra_relaxed",
            "Veo 3.1 - Fast Ultra Relaxed",
            sub_type,
        )
    if re.search(r"(?:^|[-_])fast(?:$|[-_])", low) and "relaxed" in low:
        return (
            "veo-3.1-fast-relaxed",
            "veo_3_1_t2v_fast_relaxed",
            "Veo 3.1 - Fast Relaxed",
            sub_type,
        )
    if re.search(r"(?:^|[-_])ultra(?:$|[-_])", low):
        return ("veo-3.1-fast-ultra", "veo_3_1_t2v_fast_ultra", "Veo 3.1 - Fast Ultra", sub_type)
    if re.search(r"(?:^|[-_])fast(?:$|[-_])", low):
        return ("veo-3.1-fast", "veo_3_1_t2v_fast", "Veo 3.1 - Fast", sub_type)
    return ("veo-3.1-quality", "veo_3_1_t2v", "Veo 3.1 - Quality", sub_type)


def _parse_video_variant_attrs(model_id: str, entry: dict) -> dict:
    """Đọc orientation / duration / res từ video variant id hoặc metadata entry."""
    low = model_id.lower()
    orient = None
    if "portrait" in low:
        orient = "9:16"
    elif "landscape" in low:
        orient = "16:9"
    else:
        aspect = str(entry.get("aspect_ratio") or "").upper()
        if "PORTRAIT" in aspect:
            orient = "9:16"
        elif "LANDSCAPE" in aspect:
            orient = "16:9"

    dur_match = re.search(r"(?:^|[-_])(\d+)s(?:$|[-_])", low)
    duration = int(dur_match.group(1)) if dur_match else None

    res = None
    if re.search(r"(?:^|[-_])4k(?:$|[-_])", low):
        res = "4K"
    elif re.search(r"(?:^|[-_])1080p(?:$|[-_])", low):
        res = "1080p"
    elif re.search(r"(?:^|[-_])720p(?:$|[-_])", low):
        res = "720p"

    images_minmax = None
    if "interpolation" in low:
        images_minmax = (2, 2)
    elif entry.get("min_images") is not None or entry.get("max_images") is not None:
        images_minmax = (
            int(entry.get("min_images", 1)),
            int(entry.get("max_images", 3)),
        )
    elif model_id in MODEL_CONFIG and MODEL_CONFIG[model_id].get("supports_images"):
        cfg = MODEL_CONFIG[model_id]
        images_minmax = (int(cfg.get("min_images", 1)), int(cfg.get("max_images", 2)))

    return {
        "id": model_id,
        "ratio": orient,
        "duration": duration,
        "res": res,
        "images_minmax": images_minmax,
    }


def _normalize_video_family_id(model_id: str | None) -> str | None:
    if not model_id:
        return None
    veo = _video_family_from_id(model_id)
    if veo is not None:
        return veo[0]
    stem, _sub, _attrs = _strip_video_qualifiers(model_id)
    return stem or model_id


def _build_dynamic_gen_options(raw_models: list[dict]) -> tuple[dict, dict]:
    """Dựng (public_options, resolver_meta) từ danh sách model thực của /v1/models.

    ``public_options`` giữ đúng schema hợp đồng của /playground/api/gen-options.
    ``resolver_meta`` lưu map ẩn theo (mode, model_id) để validate/compose dùng
    đúng model ID thực mà endpoint + API key đã cấp.
    """
    raw_id_set = {
        str(entry.get("id") or "").strip()
        for entry in raw_models
        if isinstance(entry, dict) and str(entry.get("id") or "").strip()
    }
    image_groups: dict[str, dict] = {}
    video_groups: dict[str, dict] = {}
    extend_variants: list[dict] = []

    for entry in raw_models:
        if not isinstance(entry, dict):
            continue
        model_id = str(entry.get("id") or "").strip()
        if not model_id:
            continue

        if _is_extend_video_entry(entry):
            veo_ext = _video_family_from_id(model_id)
            ext_attrs = _parse_video_variant_attrs(model_id, entry)
            if veo_ext is not None:
                ext_family = veo_ext[0]
            else:
                ext_stem, _sub, qattrs = _strip_video_qualifiers(model_id)
                ext_family = ext_stem
                if ext_attrs["ratio"] is None:
                    ext_attrs["ratio"] = qattrs["ratio"]
                if ext_attrs["duration"] is None:
                    ext_attrs["duration"] = qattrs["duration"]
                if ext_attrs["res"] is None:
                    ext_attrs["res"] = qattrs["res"]
            extend_variants.append(
                {
                    **ext_attrs,
                    "family_id": ext_family,
                    "discontinued": _is_discontinued_video_family(ext_family),
                }
            )
            continue

        kind = _classify_raw_model_type(entry)
        desc_label = _label_from_model_description(entry)

        if kind == "image":
            match = _IMAGE_VARIANT_RE.match(model_id)
            stem = match.group("stem") if match else model_id
            orient = match.group("orient").lower() if match and match.group("orient") else None
            res_token = match.group("res").upper() if match and match.group("res") else None
            # Không tách nhầm nếu vừa không có orient vừa không có res và bản thân stem == model_id.
            known = _KNOWN_IMAGE_FAMILIES.get(stem.lower()) or _KNOWN_IMAGE_FAMILIES.get(model_id.lower())
            if known:
                group_key = known["base"]
                group_id = model_id if (not orient and not res_token and model_id.lower() == known["id"]) else known["id"]
                base = known["base"]
                label = desc_label or known["label"]
                default_res = known["default_res"]
            else:
                group_key = stem.lower()
                group_id = stem
                base = stem
                explicit_label = entry.get("display_name") or entry.get("label") or entry.get("name")
                label = (
                    desc_label
                    or (
                        str(explicit_label).strip()
                        if (isinstance(explicit_label, str) and explicit_label.strip() and not orient and not res_token)
                        else _humanize_model_label(stem)
                    )
                )
                default_res = ["1K", "2K"] if ("gemini" in group_key or stem in MODEL_SUPPORTED_SIZES) else ["1K"]

            group = image_groups.get(group_key)
            if group is None:
                group = {
                    "id": group_id,
                    "label": label,
                    "has_desc_label": bool(desc_label),
                    "base": base,
                    "default_res": default_res,
                    "has_base": False,
                    "base_raw_id": None,
                    "ratios_seen": set(),
                    "res_seen": set(),
                    "variants": {},  # (ratio, res_upper) -> raw model_id
                    "refs": True,
                    "explicit_ratios": None,
                    "explicit_res": None,
                }
                image_groups[group_key] = group
            elif desc_label and not group["has_desc_label"]:
                group["label"] = desc_label
                group["has_desc_label"] = True

            if not orient and not res_token:
                group["has_base"] = True
                group["base_raw_id"] = model_id
                # Khi server trả trực tiếp alias như "nano-banana-pro" hoặc "gemini-3.0-pro-image",
                # ưu tiên giữ id chính là id đó.
                group["id"] = model_id
                if desc_label:
                    group["label"] = desc_label
                    group["has_desc_label"] = True
                exp_ratios = entry.get("ratios") or entry.get("aspect_ratios")
                if isinstance(exp_ratios, (list, tuple)) and exp_ratios:
                    group["explicit_ratios"] = _extract_model_ratios_from_entry(entry, list(PLAYGROUND_IMAGE_RATIOS))
                exp_res = _extract_model_res_from_entry(entry)
                if exp_res:
                    group["explicit_res"] = exp_res
                if entry.get("refs") is False or entry.get("supports_images") is False:
                    group["refs"] = False
            else:
                ratio = _IMAGE_ORIENT_TO_RATIO.get(orient) if orient else None
                res_val = res_token or "1K"
                if ratio:
                    group["ratios_seen"].add(ratio)
                    group["variants"][(ratio, res_val)] = model_id
                group["res_seen"].add(res_val)

        elif kind == "video":
            veo = _video_family_from_id(model_id)
            if veo is not None:
                family_id, catalog_base, fallback_label, sub_type = veo
                group_key = family_id
            else:
                stem, parsed_sub, qattrs = _strip_video_qualifiers(model_id)
                group_key = stem.lower()
                family_id = stem
                catalog_base = stem
                explicit_label = entry.get("display_name") or entry.get("label") or entry.get("name")
                is_plain = not qattrs["had_qualifier"]
                fallback_label = (
                    str(explicit_label).strip()
                    if isinstance(explicit_label, str) and explicit_label.strip() and is_plain
                    else _humanize_model_label(stem)
                )
                vt = str(entry.get("video_type") or "").strip().lower()
                sub_type = vt if vt in ("t2v", "i2v", "r2v") else (parsed_sub if parsed_sub in ("t2v", "i2v", "r2v") else "t2v")

            if _is_discontinued_video_family(family_id):
                continue

            label = desc_label or fallback_label
            group = video_groups.get(group_key)
            if group is None:
                group = {
                    "id": family_id,
                    "label": label,
                    "has_desc_label": bool(desc_label),
                    "base": catalog_base,
                    "is_known_veo": veo is not None,
                    "raw_ids": set(),
                    "by_type": {"t2v": [], "i2v": [], "r2v": []},
                    "has_base_t2v": False,
                    "base_raw_id": None,
                    "explicit_ratios": None,
                    "explicit_res": None,
                    "explicit_lengths": None,
                    "explicit_refs": None,
                    "explicit_frames": None,
                }
                video_groups[group_key] = group
            elif desc_label and (not group["has_desc_label"] or sub_type == "t2v"):
                if not group["has_desc_label"]:
                    group["label"] = desc_label
                    group["has_desc_label"] = True

            group["raw_ids"].add(model_id)
            attrs = _parse_video_variant_attrs(model_id, entry)
            group["by_type"][sub_type].append(attrs)

            is_unqualified = (
                model_id == family_id
                or model_id == catalog_base
                or (
                    attrs["ratio"] is None
                    and attrs["duration"] is None
                    and attrs["res"] is None
                    and sub_type == "t2v"
                )
            )
            if is_unqualified:
                group["has_base_t2v"] = True
                if group["base_raw_id"] is None or model_id == catalog_base:
                    group["base_raw_id"] = model_id
                if desc_label:
                    group["label"] = desc_label
                    group["has_desc_label"] = True
                if entry.get("ratios") or entry.get("aspect_ratios"):
                    group["explicit_ratios"] = _extract_model_ratios_from_entry(entry, list(PLAYGROUND_VIDEO_RATIOS))
                exp_res = _extract_model_res_from_entry(entry)
                if exp_res:
                    group["explicit_res"] = exp_res
                exp_len = _extract_model_lengths_from_entry(entry)
                if exp_len:
                    group["explicit_lengths"] = exp_len
                if isinstance(entry.get("refs"), bool):
                    group["explicit_refs"] = entry["refs"]
                if isinstance(entry.get("frames"), bool):
                    group["explicit_frames"] = entry["frames"]

    image_models: list[dict] = []
    video_models: list[dict] = []
    meta_index: dict[tuple[str, str], dict] = {}

    for group in image_groups.values():
        if group["explicit_ratios"]:
            ratios = group["explicit_ratios"]
        elif group["has_base"] or not group["ratios_seen"]:
            ratios = list(PLAYGROUND_IMAGE_RATIOS)
        else:
            ratios = [r for r in PLAYGROUND_IMAGE_RATIOS if r in group["ratios_seen"]] or list(PLAYGROUND_IMAGE_RATIOS)

        if group["explicit_res"]:
            res = group["explicit_res"]
        elif group["has_base"] and not group["res_seen"]:
            res = list(group["default_res"])
        elif group["res_seen"]:
            order = ("1K", "2K", "4K")
            seen = set(group["res_seen"])
            if group["has_base"]:
                seen.update(group["default_res"])
            res = [r for r in order if r in seen]
        else:
            res = list(group["default_res"])

        entry_out = {
            "id": group["id"],
            "label": group["label"],
            "base": group["base"],
            "ratios": ratios,
            "res": res,
            "refs": group["refs"],
        }
        image_models.append(entry_out)
        meta_index[("image", group["id"])] = {
            "kind": "image",
            "has_base": group["has_base"],
            "base_raw_id": group["base_raw_id"],
            "variants": group["variants"],
            "raw_id_set": raw_id_set,
        }

    for group in video_groups.values():
        base = group["base"]
        t2v_items = group["by_type"]["t2v"]
        i2v_items = group["by_type"]["i2v"]
        r2v_items = group["by_type"]["r2v"]
        all_items = t2v_items + i2v_items + r2v_items

        # Nếu là Veo 3.1 trong catalog tĩnh và endpoint liệt kê base key (không chỉ liệt kê riêng variant)
        in_catalog = base in VIDEO_BASE_MODELS or any(
            k.startswith(base) for k in MODEL_CONFIG if isinstance(k, str)
        )
        has_catalog_t2v = base in VIDEO_BASE_MODELS and _video_combo_supported(base, None, None)

        if group["explicit_ratios"]:
            ratios = group["explicit_ratios"]
        else:
            seen_ratios = {it["ratio"] for it in all_items if it["ratio"]}
            has_neutral = any(it["ratio"] is None for it in all_items)
            if has_neutral or not seen_ratios:
                ratios = list(PLAYGROUND_VIDEO_RATIOS)
            else:
                ratios = [r for r in PLAYGROUND_VIDEO_RATIOS if r in seen_ratios]

        if group["explicit_lengths"]:
            lengths = group["explicit_lengths"]
        else:
            seen_durs = sorted({it["duration"] for it in all_items if it["duration"]})
            if group["has_base_t2v"] and has_catalog_t2v:
                cat_lengths = [l for l in PLAYGROUND_LENGTHS if _video_combo_supported(base, l, None)]
                lengths = sorted(set(cat_lengths) | set(seen_durs)) or None
            elif seen_durs:
                lengths = seen_durs
            else:
                lengths = None

        if group["explicit_res"]:
            res = group["explicit_res"]
        else:
            seen_res = {it["res"] for it in all_items if it["res"]}
            if group["has_base_t2v"] and has_catalog_t2v:
                probe_lengths = lengths or [None]
                ups = [
                    r for r in PLAYGROUND_VIDEO_UPSAMPLE_RES
                    if all(_video_combo_supported(base, l, r) for l in probe_lengths)
                ]
                res_set = set(ups) | ({r for r in seen_res if r != "720p"})
                res = [PLAYGROUND_VIDEO_BASE_RES, *[r for r in ("1080p", "4K") if r in res_set]] if res_set else None
            elif seen_res - {"720p"}:
                res = [PLAYGROUND_VIDEO_BASE_RES, *[r for r in ("1080p", "4K") if r in seen_res]]
            else:
                res = None

        # Khả năng refs (Ingredients) và frames (Start/End frame)
        if group["explicit_refs"] is not None:
            refs = group["explicit_refs"]
        elif r2v_items:
            refs = True
        elif group["has_base_t2v"] and group["is_known_veo"]:
            if base == "veo_3_1_t2v_fast_ultra_relaxed":
                refs = any(k.startswith("veo_3_1_r2v_fast") and "ultra_relaxed" in k for k in MODEL_CONFIG)
            elif in_catalog:
                refs = _model_reference_supported("video", base)
            else:
                refs = False
        elif not group["is_known_veo"]:
            refs = True
        else:
            refs = False

        if group["explicit_frames"] is not None:
            frames = group["explicit_frames"]
        elif i2v_items:
            frames = True
        elif group["has_base_t2v"] and group["is_known_veo"]:
            if base == "veo_3_1_t2v_fast_ultra_relaxed":
                frames = any(k.startswith("veo_3_1_i2v") and "ultra_relaxed" in k for k in MODEL_CONFIG)
            elif in_catalog:
                frames = _video_frames_supported(base)
            else:
                frames = False
        elif not group["is_known_veo"]:
            frames = True
        else:
            frames = False

        entry_out = {
            "id": group["id"],
            "label": group["label"],
            "base": base,
            "ratios": ratios,
            "res": res,
            "length": lengths,
            "refs": refs,
            "frames": frames,
        }
        video_models.append(entry_out)
        meta_index[("video", group["id"])] = {
            "kind": "video",
            "has_base_t2v": group["has_base_t2v"],
            "base_raw_id": group["base_raw_id"],
            "is_known_veo": group["is_known_veo"],
            "raw_ids": group["raw_ids"],
            "by_type": group["by_type"],
            "raw_id_set": raw_id_set,
        }

    if extend_variants:
        meta_index[("video_extend", "default")] = {
            "variants": extend_variants,
            "raw_id_set": raw_id_set,
        }

    return {"image": {"models": image_models}, "video": {"models": video_models}}, meta_index


def playground_gen_options(raw_models: list[dict] | None = None) -> dict:
    if raw_models is not None:
        options, _index = _build_dynamic_gen_options(raw_models)
        return options
    return {
        "image": {"models": _playground_image_model_options()},
        "video": {"models": _playground_video_model_options()},
    }


def _pick_dynamic_video_variant(
    items: list[dict],
    ratio: str,
    length: int | None,
    res: str | None,
    images: int | None = None,
    *,
    allow_nearest_duration: bool = False,
) -> str | None:
    """Chọn variant khớp nhất trong nhóm raw /v1/models cho (ratio, length, res, images)."""
    if not items:
        return None
    upsampling = res if res and res.lower() in ("1080p", "4k") else None

    def score(item: dict, *, relax_duration: bool) -> tuple[int, int, int, int, int] | None:
        if item["ratio"] is not None and item["ratio"] != ratio:
            return None
        if images is not None and item["images_minmax"] is not None:
            lo, hi = item["images_minmax"]
            if not lo <= images <= hi:
                return None
        if upsampling:
            if item["res"] is not None and item["res"].lower() != upsampling.lower():
                return None
        elif item["res"] is not None and item["res"].lower() != "720p":
            return None
        dur = item["duration"]
        if not relax_duration and length is not None and dur is not None and dur != length:
            return None

        if length is not None and dur is not None:
            dur_exact = 1 if dur == length else 0
            # Gần nhất (chỉ dùng khi relax_duration=True); đồng khoảng cách ưu tiên thời lượng lớn hơn.
            dur_closeness = -abs(dur - length) * 100 + dur
        elif dur is None:
            dur_exact = 0
            dur_closeness = -10_000
        else:
            dur_exact = 0
            dur_closeness = dur

        return (
            1 if item["ratio"] == ratio else 0,
            1 if (upsampling and item["res"] and item["res"].lower() == upsampling.lower()) else (1 if not upsampling and item["res"] is None else 0),
            dur_exact,
            dur_closeness,
            1 if (images is not None and item["images_minmax"] is not None) else 0,
        )

    for relax in (False, True) if allow_nearest_duration else (False,):
        ranked = []
        for item in items:
            s = score(item, relax_duration=relax)
            if s is not None:
                ranked.append((s, item["id"]))
        if ranked:
            ranked.sort(key=lambda pair: pair[0], reverse=True)
            return ranked[0][1]
    return None


def _resolve_dynamic_extend_target(
    meta_extend: dict | None,
    source_model: str | None,
    ratio: str,
) -> tuple[str, dict] | None:
    """Resolve model extend thực từ /v1/models cho video nối tiếp."""
    variants = (meta_extend or {}).get("variants") or []
    if not variants:
        return _compose_video_extend_request(ratio)
    desired_family = _normalize_video_family_id(source_model)
    best: tuple[tuple[int, int, int], str] | None = None
    for item in variants:
        if item["ratio"] is not None and item["ratio"] != ratio:
            continue
        score = (
            1 if desired_family and item["family_id"] == desired_family else 0,
            1 if not item.get("discontinued") else 0,
            1 if item["ratio"] == ratio else 0,
        )
        if best is None or score > best[0]:
            best = (score, item["id"])
    if best:
        return best[1], {"aspectRatio": ratio}
    return _compose_video_extend_request(ratio)


def _resolve_dynamic_target(
    meta: dict,
    spec_entry: dict,
    mode: str,
    ratio: str,
    res: str | None,
    length: int | None,
    vmode: str,
    image_count: int,
) -> tuple[str, dict] | None:
    """Resolve (request_model, generationConfig) cho model từ /v1/models."""
    raw_id_set = meta["raw_id_set"]
    if mode == "image":
        res_upper = (res or "1K").upper()
        res_lower = res_upper.lower()
        gen_config = {"aspectRatio": ratio, "imageSize": res_lower}
        variants = meta.get("variants") or {}
        exact = variants.get((ratio, res_upper))
        if exact:
            return exact, gen_config
        if meta.get("has_base") and meta.get("base_raw_id"):
            return meta["base_raw_id"], gen_config
        fallback = variants.get((ratio, "1K")) or next(
            (mid for (r, _sz), mid in variants.items() if r == ratio), None
        )
        if fallback:
            return fallback, gen_config
        return spec_entry["base"], gen_config

    # Video mode
    base = spec_entry["base"]
    orientation = _playground_orientation(ratio)
    gen_config = {"aspectRatio": ratio}
    if res and res.lower() in ("1080p", "4k"):
        gen_config["imageSize"] = res.lower()

    if vmode == "frames":
        # 2 ảnh (frame đầu + cuối): raw id trong /v1/models không mang metadata
        # min/max images nên không phân biệt được key 1-1 hay 2-2 — đi theo
        # catalog compose (lite → veo_3_1_interpolation_lite* 2-2, quality →
        # veo_3_1_i2v_s* 1-2; đã verify upstream), trừ khi endpoint tự quảng bá
        # variant i2v nhận 2 ảnh. Nếu không, 2 ảnh sẽ bị đẩy vào key i2v 1-1
        # của upstream và từ chối ("first/last-frame model requires 1-1
        # images; 2 provided") hoặc lệch duration (i2v chỉ có 4s/6s).
        if image_count == 2:
            admits_two = any(
                item["images_minmax"] is not None and item["images_minmax"][1] >= 2
                for item in meta["by_type"]["i2v"]
            )
            if not admits_two:
                cat_two = _compose_video_frames_request(base, length, res, ratio, images=2)
                if cat_two:
                    return cat_two
        # Ưu tiên variant i2v thực của endpoint (khớp exact duration, thiếu thì chọn i2v gần nhất).
        matched = _pick_dynamic_video_variant(
            meta["by_type"]["i2v"], ratio, length, res, images=image_count, allow_nearest_duration=True
        )
        if matched:
            return matched, gen_config
        cat = _compose_video_frames_request(base, length, res, ratio, images=image_count)
        if cat:
            cat_model, cat_cfg = cat
            if cat_model in raw_id_set or meta.get("base_raw_id") == base:
                return cat_model, cat_cfg
        # Model ngoài Veo (vd omni-1.2-flash) không tách riêng -i2v- -> map đúng t2v theo (ratio, length).
        if spec_entry.get("frames") and not meta.get("is_known_veo"):
            t2v_matched = _pick_dynamic_video_variant(
                meta["by_type"]["t2v"], ratio, length, res, allow_nearest_duration=True
            )
            if t2v_matched:
                return t2v_matched, gen_config
            if meta.get("base_raw_id"):
                return meta["base_raw_id"], gen_config
            return spec_entry["id"], gen_config
        if meta.get("base_raw_id"):
            return meta["base_raw_id"], gen_config
        return None

    if vmode == "ingredients":
        matched = _pick_dynamic_video_variant(
            meta["by_type"]["r2v"], ratio, length, res, images=image_count, allow_nearest_duration=True
        )
        if matched:
            return matched, gen_config
        cat = _compose_video_reference_request(base, length, res, ratio)
        if cat:
            cat_model, cat_cfg = cat
            if cat_model in raw_id_set or meta.get("base_raw_id") == base:
                return cat_model, cat_cfg
        if spec_entry.get("refs") and not meta.get("is_known_veo"):
            t2v_matched = _pick_dynamic_video_variant(
                meta["by_type"]["t2v"], ratio, length, res, allow_nearest_duration=True
            )
            if t2v_matched:
                return t2v_matched, gen_config
            if meta.get("base_raw_id"):
                return meta["base_raw_id"], gen_config
            return spec_entry["id"], gen_config
        if meta.get("base_raw_id"):
            return meta["base_raw_id"], gen_config
        return None

    # Text (t2v): ưu tiên variant t2v thực khớp đúng (ratio, length, res) trong /v1/models
    matched = _pick_dynamic_video_variant(meta["by_type"]["t2v"], ratio, length, res)
    if matched:
        return matched, gen_config
    cat = _compose_video_request(base, length, res, ratio)
    if cat:
        cat_model, cat_cfg = cat
        plain_key = VIDEO_BASE_MODELS.get(cat_model, {}).get(orientation)
        if cat_model in raw_id_set:
            return cat_model, cat_cfg
        if plain_key and plain_key in raw_id_set:
            return plain_key, cat_cfg
        if meta.get("base_raw_id") == base:
            return cat_model, cat_cfg
    nearest_t2v = _pick_dynamic_video_variant(
        meta["by_type"]["t2v"], ratio, length, res, allow_nearest_duration=True
    )
    if nearest_t2v:
        return nearest_t2v, gen_config
    if meta.get("base_raw_id"):
        return meta["base_raw_id"], gen_config
    # Fallback cho model chỉ liệt kê r2v/i2v (vd veo-3.1-lite-r2v-8s-*)
    any_matched = _pick_dynamic_video_variant(
        meta["by_type"]["r2v"] + meta["by_type"]["i2v"], ratio, length, res, allow_nearest_duration=True
    )
    if any_matched:
        return any_matched, gen_config
    return spec_entry["id"], gen_config


def validate_generate_payload(payload: dict, raw_models: list[dict] | None = None) -> dict:
    """Validate payload generate từ frontend; trả params dict cho job nền.

    vmode cho video: 'text' (t2v) / 'frames' (i2v 1-2 ảnh đầu+cuối) /
    'ingredients' (r2v 1-3 ảnh tham chiếu) / 'extend' (nối tiếp video đã tạo).
    ``raw_models`` (khi có) là danh sách từ GET /v1/models của session hiện tại.
    """
    from fastapi import HTTPException

    references = [str(ref).strip() for ref in (payload.get("references") or []) if str(ref).strip()]
    vmode = (payload.get("vmode") or "text").strip().lower() or "text"
    valid_vmodes = ("text", "frames", "ingredients", "extend")
    if vmode not in valid_vmodes:
        raise HTTPException(
            status_code=400,
            detail=f"Chế độ generation không hợp lệ, chỉ hỗ trợ: {', '.join(valid_vmodes)}.",
        )
    mode = (payload.get("mode") or "").strip().lower()
    if mode not in ("image", "video"):
        raise HTTPException(status_code=400, detail="Mode phải là 'image' hoặc 'video'.")
    if mode == "image" and vmode != "text":
        vmode = "text"  # image luôn "text" — refs đi kèm i2i, không dùng vmode

    if mode == "video":
        if vmode == "text" and references:
            raise HTTPException(
                status_code=400,
                detail="Chế độ Text không dùng ảnh — hãy chuyển sang Frames/Ingredients.",
            )
        if vmode == "frames" and not 1 <= len(references) <= 2:
            raise HTTPException(
                status_code=400,
                detail="Chế độ Frames cần chọn đúng 1-2 ảnh (frame đầu, frame cuối).",
            )
        if vmode == "ingredients" and not 1 <= len(references) <= 3:
            raise HTTPException(
                status_code=400,
                detail="Chế độ Ingredients cần chọn 1-3 ảnh tham chiếu.",
            )
        if vmode == "extend":
            if references:
                raise HTTPException(
                    status_code=400, detail="Chế độ Extend không dùng ảnh tham chiếu."
                )
            if not str(payload.get("extend_item_id") or "").strip():
                raise HTTPException(
                    status_code=400, detail="Chế độ Extend cần chọn video gốc để nối tiếp."
                )

    max_refs = MAX_REFERENCE_IMAGES.get(mode, 3)
    if len(references) > max_refs:
        raise HTTPException(
            status_code=400,
            detail=f"Tối đa {max_refs} ảnh tham chiếu được chọn cùng lúc cho {mode}.",
        )

    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt không được để trống.")

    qty = payload.get("qty") if payload.get("qty") is not None else 1
    if not 1 <= qty <= 4:
        raise HTTPException(status_code=400, detail="Số lượng phải từ 1 đến 4.")
    if vmode == "extend" and qty != 1:
        raise HTTPException(status_code=400, detail="Extend chỉ hỗ trợ 1 kết quả mỗi lần.")

    if raw_models is not None:
        options, resolver_meta = _build_dynamic_gen_options(raw_models)
    else:
        options, resolver_meta = playground_gen_options(), None

    spec_entry = next(
        (item for item in options[mode]["models"] if item["id"] == payload.get("model")), None
    )
    if spec_entry is None and vmode != "extend":
        raise HTTPException(status_code=400, detail="Model không được hỗ trợ cho chế độ này.")
    spec = (
        spec_entry
        if resolver_meta is not None
        else (_playground_model_spec(mode, payload.get("model")) if spec_entry else None)
    )

    ratio = (payload.get("ratio") or "").strip()
    if spec_entry is not None and ratio not in spec_entry["ratios"]:
        raise HTTPException(
            status_code=400, detail="Tỉ lệ khung hình không được hỗ trợ cho model này."
        )
    if vmode == "extend" and ratio not in PLAYGROUND_VIDEO_RATIOS:
        raise HTTPException(status_code=400, detail="Extend hỗ trợ tỉ lệ 16:9 hoặc 9:16.")

    # res/length không quảng bá cho model hiện tại → degrade về mặc định
    # thay vì để resolver fail.
    res = (payload.get("res") or "").strip() or None
    if spec_entry is not None and res is not None and res not in (spec_entry["res"] or []):
        res = None
    if mode == "image" and not res:
        res = "1K"
    length = payload.get("length")
    if (
        spec_entry is not None
        and length is not None
        and length not in (spec_entry["length"] or [])
    ):
        length = None

    resolved_model = None
    resolved_config = None
    if resolver_meta is not None and vmode == "extend":
        target = _resolve_dynamic_extend_target(
            resolver_meta.get(("video_extend", "default")),
            payload.get("model"),
            ratio,
        )
        if target is None:
            raise HTTPException(
                status_code=400, detail="Endpoint hiện tại không hỗ trợ Extend cho tỉ lệ này."
            )
        resolved_model, resolved_config = target
    elif resolver_meta is not None and spec_entry is not None and vmode != "extend":
        meta = resolver_meta.get((mode, spec_entry["id"]))
        if mode == "video" and vmode == "frames" and not spec_entry.get("frames"):
            raise HTTPException(
                status_code=400, detail="Model này không hỗ trợ Frames to Video (i2v)."
            )
        if mode == "video" and vmode == "ingredients" and not spec_entry.get("refs"):
            raise HTTPException(
                status_code=400, detail="Model này không hỗ trợ ảnh tham chiếu."
            )
        target = (
            _resolve_dynamic_target(
                meta, spec_entry, mode, ratio, res, length, vmode, len(references)
            )
            if meta
            else None
        )
        if target is None:
            raise HTTPException(
                status_code=400, detail="Model này không hỗ trợ tổ hợp tham số đã chọn."
            )
        resolved_model, resolved_config = target
    elif vmode != "extend":
        # Probe key compose thật với combo đã degrade — không có key phù hợp thì
        # chặn sớm thay vì để job chạy rồi mới lỗi.
        if mode == "video" and vmode == "frames":
            if not spec_entry.get("frames"):
                raise HTTPException(
                    status_code=400, detail="Model này không hỗ trợ Frames to Video (i2v)."
                )
            if _compose_video_frames_request(
                spec["base"], length, res, ratio, images=len(references)
            ) is None:
                raise HTTPException(
                    status_code=400, detail="Model này không hỗ trợ tổ hợp Frames đã chọn."
                )
        if mode == "video" and vmode == "ingredients":
            if not spec_entry.get("refs"):
                raise HTTPException(
                    status_code=400, detail="Model này không hỗ trợ ảnh tham chiếu."
                )
            if _compose_video_reference_request(spec["base"], length, res, ratio) is None:
                raise HTTPException(
                    status_code=400, detail="Model này không hỗ trợ tổ hợp ảnh tham chiếu đã chọn."
                )

    result = {
        "mode": mode,
        "model": payload.get("model") or "",
        "base": spec["base"] if spec else "veo_3_1_extend",
        "ratio": ratio,
        "res": res,
        "length": length,
        "qty": qty,
        "prompt": prompt,
        "vmode": vmode,
        "references": references,
        "extend_item_id": str(payload.get("extend_item_id") or "").strip() or None,
    }
    if resolved_model:
        result["resolved_model"] = resolved_model
        result["resolved_config"] = resolved_config
    return result


# ──────────────────────────────────────────────────────────────────
# Payload parse (copy từ flow2api src/api/routes.py)
# ──────────────────────────────────────────────────────────────────


def _extract_openai_message_content(payload: dict) -> str:
    choices = payload.get("choices", [])
    if not choices:
        return payload.get("result", "")

    message = choices[0].get("message", {})
    content = message.get("content", "")
    return content if isinstance(content, str) else ""


def _extract_url_from_openai_payload(payload: dict) -> Optional[str]:
    direct_url = payload.get("url")
    if isinstance(direct_url, str) and direct_url.strip():
        return direct_url.strip()

    content = _extract_openai_message_content(payload).strip()
    if not content:
        return None

    image_match = MARKDOWN_IMAGE_RE.search(content)
    if image_match:
        return image_match.group(1).strip()

    video_match = HTML_VIDEO_RE.search(content)
    if video_match:
        return video_match.group(1).strip()

    return None


def _payload_error_message(payload: dict) -> str:
    error = payload.get("error")
    if isinstance(error, dict):
        message = str(error.get("message") or "").strip()
        if message:
            return message[:300]
    if isinstance(error, str) and error.strip():
        return error.strip()[:300]
    content = ""
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        content = message.get("content") if isinstance(message, dict) else ""
        content = content if isinstance(content, str) else ""
    content = content or str(payload.get("result") or "")
    content = content.strip()
    if content:
        return f"Không lấy được URL từ kết quả: {content[:200]}"
    return "Không lấy được URL từ kết quả generation."


def _absolute_flow_url(url: str | None) -> str | None:
    """URL tương đối (/tmp/...) → absolute theo FLOW2API_BASE_URL."""
    if not url:
        return None
    url = url.strip()
    if url.startswith("/"):
        return f"{FLOW2API_BASE_URL}{url}"
    return url


def _media_item(kind: str, url: str, meta: dict) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "kind": kind,
        "url": url,
        "created_at": _now_ms(),
        "meta": meta,
    }


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


# ──────────────────────────────────────────────────────────────────
# Tên item tự sinh từ prompt (server-side, dedupe additive)
# ──────────────────────────────────────────────────────────────────

ITEM_NAME_MAX_CHARS = 20


def _item_base_name(prompt: str | None) -> str:
    """Tên gốc cho item generation: prompt đã trim, cắt 20 ký tự đầu; prompt
    dài hơn 20 ký tự thì thêm "…" vào cuối. Python str slice theo codepoint
    nên an toàn với tiếng Nhật/emoji (không cắt giữa surrogate pair).
    """
    name = (prompt or "").strip()
    if not name:
        return ""
    if len(name) <= ITEM_NAME_MAX_CHARS:
        return name
    return name[:ITEM_NAME_MAX_CHARS] + "…"


def _assign_generated_item_names(
    project: dict | None, items: list, prompt: str | None, base_name: str | None = None
) -> list:
    """Gán ``name`` cho item kết quả generation chưa có name (hoặc name rỗng),
    dedupe với tên item đã có trong project VÀ tên các item khác trong cùng
    batch bằng suffix " (2)", " (3)"...

    Item do user tự đặt tên (ảnh tham chiếu/upload) được giữ nguyên — chỉ ghi
    vào item chưa có name. Mutate items in place và trả về ``items``.

    ``base_name`` cho phép caller truyền sẵn tên gốc (vd trim video: "{tên
    item gốc} (trim)") thay vì suy ra từ prompt — truyền thì ``prompt`` bị bỏ
    qua. Generation chỉ truyền prompt nên hành vi cũ giữ nguyên.
    """
    base = (str(base_name).strip() if base_name is not None else "") or _item_base_name(prompt)
    if not base:
        return items
    taken: set[str] = set()
    for item in (project or {}).get("results") or []:
        if isinstance(item, dict):
            existing = str(item.get("name") or "").strip()
            if existing:
                taken.add(existing)
    for item in items:
        if not isinstance(item, dict):
            continue
        existing = str(item.get("name") or "").strip()
        if existing:
            # Item user đã đặt tên: giữ nguyên, nhưng chặn item sau trong batch
            # trùng đúng tên đó.
            taken.add(existing)
            continue
        candidate = base
        counter = 2
        while candidate in taken:
            candidate = f"{base} ({counter})"
            counter += 1
        item["name"] = candidate
        taken.add(candidate)
    return items


# ──────────────────────────────────────────────────────────────────
# Trim video (ffmpeg) — cắt giữ lại đoạn [start, end) thành item mới
# ──────────────────────────────────────────────────────────────────

TRIM_MIN_DURATION_SEC = 0.2
TRIM_MAX_DURATION_SEC = 120.0
# Dung sai so với độ dài video thật (ffprobe làm tròn theo container/keyframe).
TRIM_END_TOLERANCE_SEC = 0.5
TRIM_SUBPROCESS_TIMEOUT_SEC = 60
_RESULTS_URL_PREFIX = "/tmp/playground_results/"


class TrimVideoError(RuntimeError):
    """ffmpeg/subprocess thất bại khi cắt video — message chỉ dùng để log
    server-side, không trả thẳng cho client (có thể chứa path hệ thống)."""


def _find_ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def _resolve_results_file(url) -> Path:
    """URL item (``/tmp/playground_results/<file>``) → Path thật trong RESULTS_DIR.

    Từ chối URL không thuộc RESULTS_DIR, kể cả traversal ``../`` — chặn đọc file
    ngoài thư mục kết quả. ValueError mang message tự viết, không lộ path hệ thống.
    """
    if not isinstance(url, str) or not url.startswith(_RESULTS_URL_PREFIX):
        raise ValueError("File video của item không hợp lệ.")
    results_dir = RESULTS_DIR.resolve()
    candidate = (results_dir / url[len(_RESULTS_URL_PREFIX):]).resolve()
    if not candidate.is_relative_to(results_dir):
        raise ValueError("File video của item không hợp lệ.")
    return candidate


def probe_video_duration_sync(path: Path) -> float | None:
    """Độ dài video (giây) qua ffprobe; None nếu thiếu ffprobe hoặc đọc lỗi."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    cmd = [
        ffprobe,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=TRIM_SUBPROCESS_TIMEOUT_SEC)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        return float(result.stdout.decode("ascii", errors="ignore").strip())
    except ValueError:
        return None


async def probe_video_duration(path: Path) -> float | None:
    """Async wrapper (ffprobe chạy trong thread riêng, không block event loop)."""
    return await asyncio.to_thread(probe_video_duration_sync, path)


def trim_video_file_sync(src: Path, start: float, duration: float, dst: Path) -> Path:
    """Đồng bộ: cắt ``[start, start+duration)`` của src → dst (mp4).

    Lượt 1 dùng ``-c copy`` (không re-encode — nhanh, giữ nguyên chất lượng);
    codec lạ không đóng gói được vào mp4 thì retry re-encode libx264+aac.
    Mọi lượt fail đều xoá file dở để không rác trong RESULTS_DIR.
    """
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        raise TrimVideoError("Máy chủ chưa cài ffmpeg.")
    seek_args = ["-ss", f"{start:.3f}", "-i", str(src), "-t", f"{duration:.3f}"]
    attempts = (
        seek_args + ["-c", "copy", "-movflags", "+faststart"],
        # Fallback codec lạ: re-encode H.264 + AAC (giống style crop WM của watermark.py).
        seek_args + [
            "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-movflags", "+faststart",
        ],
    )
    last_error = "unknown"
    for extra_args in attempts:
        cmd = [ffmpeg, "-y", *extra_args, str(dst)]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=TRIM_SUBPROCESS_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            last_error = f"ffmpeg timeout sau {TRIM_SUBPROCESS_TIMEOUT_SEC}s"
            dst.unlink(missing_ok=True)
            continue
        except OSError as exc:
            last_error = f"subprocess {type(exc).__name__}"
            dst.unlink(missing_ok=True)
            continue
        if result.returncode == 0 and dst.is_file() and dst.stat().st_size > 0:
            return dst
        stderr_tail = (
            result.stderr.decode("utf-8", errors="ignore")[-300:] if result.stderr else ""
        )
        last_error = f"returncode={result.returncode} stderr={stderr_tail!r}"
        dst.unlink(missing_ok=True)
    raise TrimVideoError(last_error)


async def trim_video_file(src: Path, start: float, duration: float, dst: Path) -> Path:
    """Async trim (chạy trong thread riêng, không block event loop)."""
    return await asyncio.to_thread(trim_video_file_sync, src, start, duration, dst)


async def trim_playground_video_item(source_item: dict, start: float, end: float) -> dict:
    """Cắt item video trong project thành item MỚI — file và item gốc giữ nguyên.

    meta copy từ item gốc NHƯNG bỏ ``media_generation_id`` (bản trim không tồn
    tại trên Flow nên không extend tiếp được), thêm ``trimmed_from`` = id item
    gốc, và cập nhật ``length`` theo độ dài thực của file mới (nếu meta gốc có
    length). ValueError = request sai (path lạ / file mất / end vượt độ dài).
    """
    src = _resolve_results_file(source_item.get("url"))
    if not src.is_file():
        raise ValueError("File video của item không còn tồn tại.")
    total = await probe_video_duration(src)
    if total is not None and end > total + TRIM_END_TOLERANCE_SEC:
        raise ValueError(f"Đoạn cắt vượt quá độ dài video ({total:.1f}s).")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    dst = RESULTS_DIR / f"{uuid.uuid4().hex}.mp4"
    await trim_video_file(src, start, end - start, dst)
    new_duration = await probe_video_duration(dst)
    meta = dict(source_item.get("meta") or {})
    meta.pop("media_generation_id", None)
    meta["trimmed_from"] = str(source_item.get("id") or "")
    if "length" in meta:
        meta["length"] = round(new_duration if new_duration is not None else end - start)
    return {
        "id": str(uuid.uuid4()),
        "kind": "video",
        "url": f"{_RESULTS_URL_PREFIX}{dst.name}",
        "created_at": _now_ms(),
        "meta": meta,
    }


# ──────────────────────────────────────────────────────────────────
# HTTP client sang flow2api
# ──────────────────────────────────────────────────────────────────


def _parse_models_payload(payload) -> list[dict] | None:
    """Chuẩn hóa JSON từ GET /v1/models (OpenAI list / models / array) thành list[dict]."""
    if isinstance(payload, dict):
        items = payload.get("data")
        if not isinstance(items, list):
            items = payload.get("models")
    elif isinstance(payload, list):
        items = payload
    else:
        items = None
    if not isinstance(items, list):
        return None
    parsed: list[dict] = []
    for item in items:
        if isinstance(item, str) and item.strip():
            parsed.append({"id": item.strip()})
        elif isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip():
            parsed.append({**item, "id": item["id"].strip()})
    return parsed


# Cache ngắn hạn danh sách model theo (endpoint, api_key_hash) để vừa đăng nhập
# xong gọi /gen-options dùng ngay kết quả /v1/models mà không lặp request thừa.
_MODELS_CACHE_TTL_SEC = 10.0
_models_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}


def _models_cache_key(api_key: str, api_url: str | None) -> tuple[str, str]:
    base = (api_url or FLOW2API_BASE_URL).strip().rstrip("/")
    key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    return (base, key_hash)


def cache_models_for_credentials(api_key: str, api_url: str | None, models: list[dict] | None):
    if not api_key or models is None:
        return
    _models_cache[_models_cache_key(api_key, api_url)] = (time.monotonic(), list(models))


def invalidate_models_cache(api_key: str | None, api_url: str | None):
    if not api_key:
        return
    _models_cache.pop(_models_cache_key(api_key, api_url), None)


async def get_models_for_session(
    playground_db,
    owner_hash: str,
    *,
    refresh: bool = False,
) -> list[dict] | None:
    """Lấy danh sách model thực của session hiện tại (ưu tiên cache/DB → /v1/models).

    Khi ``refresh=True`` (endpoint /gen-options lúc login/F5): gọi /v1/models
    nếu cache TTL đã hết, cập nhật DB + cache; nếu upstream tạm lỗi thì rơi về
    list đã lưu trong session (chỉ fallback static catalog khi chưa từng fetch).
    """
    api_key, api_url, stored_models = await playground_db.session_credentials_and_models(owner_hash)
    if not api_key:
        return None
    ck = _models_cache_key(api_key, api_url)
    cached = _models_cache.get(ck)
    now = time.monotonic()
    if cached and (not refresh or (now - cached[0]) <= _MODELS_CACHE_TTL_SEC):
        return cached[1]
    if refresh:
        client = get_flow_client(api_url)
        fetched = await client.fetch_models(api_key)
        if fetched is not None:
            cache_models_for_credentials(api_key, api_url, fetched)
            await playground_db.update_session_models(owner_hash, fetched)
            return fetched
    if stored_models is not None:
        cache_models_for_credentials(api_key, api_url, stored_models)
    return stored_models


class FlowClient:
    """Client gọi flow2api (engine generation) qua OpenAI-compatible API."""

    def __init__(self, base_url: str = FLOW2API_BASE_URL):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(FLOW2API_TIMEOUT),
            follow_redirects=True,
        )
        # Phân loại lỗi lần check key gần nhất: None / 'unreachable' / 'unauthorized'.
        self.last_check_error: str | None = None
        # Danh sách raw model từ lần GET /v1/models gần nhất (nếu hợp lệ).
        self.last_models: list[dict] | None = None

    async def aclose(self):
        await self._client.aclose()

    async def check_key(self, api_key: str) -> str | None:
        """Key hợp lệ khi flow2api /v1/models chấp nhận Bearer key này.

        Trả None nếu hợp lệ; 'unreachable' nếu không kết nối được endpoint;
        'unauthorized' nếu endpoint từ chối key. Kết quả cũng lưu vào
        ``last_check_error`` để caller phân loại lỗi khi validate_key trả False.
        Đồng thời lưu ``last_models`` để session lấy luôn danh sách model thực.
        """
        self.last_models = None
        try:
            response = await self._client.get(
                "/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=min(FLOW2API_TIMEOUT, 15.0),
            )
        except httpx.HTTPError as exc:
            logger.error("Không kết nối được flow2api %s: %s", self.base_url, exc)
            self.last_check_error = "unreachable"
            return self.last_check_error
        if response.status_code == 200:
            self.last_check_error = None
            try:
                self.last_models = _parse_models_payload(response.json())
            except ValueError:
                self.last_models = None
            return None
        self.last_check_error = "unauthorized"
        return self.last_check_error

    async def validate_key(self, api_key: str) -> bool:
        return await self.check_key(api_key) is None

    async def fetch_models(self, api_key: str) -> list[dict] | None:
        """Gọi GET /v1/models lấy danh sách model thực của API key; None nếu lỗi."""
        try:
            response = await self._client.get(
                "/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=min(FLOW2API_TIMEOUT, 15.0),
            )
        except httpx.HTTPError as exc:
            logger.warning("Không lấy được /v1/models từ %s: %s", self.base_url, exc)
            return None
        if response.status_code != 200:
            return None
        try:
            parsed = _parse_models_payload(response.json())
        except ValueError:
            return None
        if parsed is not None:
            self.last_models = parsed
        return parsed

    async def generate(self, api_key: str, body: dict) -> dict:
        """POST /v1/chat/completions (non-stream); trả payload JSON."""
        response = await self._client.post(
            "/v1/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        try:
            payload = response.json()
        except ValueError:
            payload = {"result": response.text[:2000]}
        if response.status_code >= 400 or payload.get("error"):
            raise RuntimeError(_payload_error_message(payload))
        return payload

    async def generate_stream(self, api_key: str, body: dict) -> dict:
        """POST /v1/chat/completions stream:true — gom SSE chunk, trả kết quả.

        Chunk cuối của video chứa ``<video src='...' data-media-id='...'>`` —
        media id này cần lưu lại để hỗ trợ Extend. Trả dict:
        ``{"url": str|None, "media_id": str|None, "text": str}``.
        """
        media_id_re = re.compile(r"data-media-id=['\"]([^'\"]+)['\"]")
        chunks: list[str] = []
        async with self._client.stream(
            "POST",
            "/v1/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {api_key}"},
        ) as response:
            if response.status_code >= 400:
                raw = (await response.aread()).decode("utf-8", errors="replace")
                try:
                    payload = json.loads(raw)
                except ValueError:
                    payload = {"result": raw[:2000]}
                raise RuntimeError(_payload_error_message(payload))
            async for line in response.aiter_lines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    payload = json.loads(data)
                except ValueError:
                    continue
                choices = payload.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if isinstance(content, str) and content:
                    chunks.append(content)
        full = "".join(chunks)
        url = None
        video_match = HTML_VIDEO_RE.search(full)
        if video_match:
            url = video_match.group(1).strip()
        else:
            image_match = MARKDOWN_IMAGE_RE.search(full)
            if image_match:
                url = image_match.group(1).strip()
        media_id = None
        media_match = media_id_re.search(full)
        if media_match:
            media_id = media_match.group(1).strip()
        if not url:
            raise RuntimeError(f"Không lấy được URL từ kết quả stream: {full[-200:]}")
        return {"url": url, "media_id": media_id, "text": full[-500:]}

    async def download_media(self, url: str) -> tuple[bytes, str]:
        """Tải media bytes; trả (bytes, ext có dấu chấm, vd '.jpg')."""
        if url.startswith("/"):
            url = f"{self.base_url}{url}"
        response = await self._client.get(url)
        response.raise_for_status()
        content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
        ext = mimetypes.guess_extension(content_type) if content_type else None
        if not ext or ext == ".jpe":
            ext = Path(url.split("?")[0]).suffix.lower() or ".jpg"
        if ext not in (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".mp4", ".webm", ".mov"):
            ext = ".jpg" if "image" in content_type else ".mp4"
        return response.content, ext


flow_client = FlowClient()

# Client theo endpoint do user nhập lúc login — cache dùng lại để không tạo
# connection pool mới mỗi job. None/rỗng → client mặc định theo env config.
_flow_clients: dict[str, FlowClient] = {}


def get_flow_client(base_url: str | None = None) -> FlowClient:
    if not base_url:
        return flow_client
    url = base_url.strip().rstrip("/")
    client = _flow_clients.get(url)
    if client is None:
        client = FlowClient(url)
        _flow_clients[url] = client
    return client


# ──────────────────────────────────────────────────────────────────
# Compose body + hậu xử lý kết quả (download + watermark)
# ──────────────────────────────────────────────────────────────────


def _compose_chat_body(params: dict, reference_data_urls: list[str]) -> tuple[str, dict]:
    """params đã validate → (model alias, body JSON cho /v1/chat/completions).

    Video chạy stream:true để bắt mediaGenerationId từ chunk cuối (cần cho
    Extend); non-stream không trả media id.
    """
    mode = params["mode"]
    vmode = params.get("vmode") or "text"
    stream = mode == "video"
    if params.get("resolved_model"):
        request_model = str(params["resolved_model"])
        gen_config = dict(params.get("resolved_config") or {"aspectRatio": params["ratio"]})
    elif mode == "image":
        res = (params.get("res") or "1K").lower()
        request_model = params["base"]
        gen_config = {"aspectRatio": params["ratio"], "imageSize": res}
    elif vmode == "extend":
        composed = _compose_video_extend_request(params["ratio"])
        if composed is None:
            raise RuntimeError("Extend không hỗ trợ tỉ lệ khung hình đã chọn.")
        request_model, gen_config = composed
    elif vmode == "frames":
        composed = _compose_video_frames_request(
            params["base"], params["length"], params["res"], params["ratio"],
            images=len(reference_data_urls),
        )
        if composed is None:
            raise RuntimeError("Model không hỗ trợ tổ hợp Frames đã chọn.")
        request_model, gen_config = composed
    elif vmode == "ingredients":
        composed = _compose_video_reference_request(
            params["base"], params["length"], params["res"], params["ratio"]
        )
        if composed is None:
            raise RuntimeError("Model không hỗ trợ tổ hợp ảnh tham chiếu đã chọn.")
        request_model, gen_config = composed
    else:
        composed = _compose_video_request(
            params["base"], params["length"], params["res"], params["ratio"]
        )
        if composed is None:
            raise RuntimeError("Model không hỗ trợ tổ hợp tham số đã chọn.")
        request_model, gen_config = composed

    parts: list | str = [{"type": "text", "text": params["prompt"]}]
    if reference_data_urls:
        # Ảnh tham chiếu nối vào message dưới dạng image_url data URL —
        # flow2api _normalize_openai_request tự decode thành bytes upload
        # (i2v frame đầu/cuối, r2v ingredients, i2i ảnh tham chiếu).
        parts.extend({"type": "image_url", "image_url": {"url": url}} for url in reference_data_urls)
    if vmode == "extend":
        parts.append(
            {
                "type": "image_url",
                "image_url": {"url": f"extend://{params['extend_media_id']}"},
            }
        )
    # Có ảnh tham chiếu (image i2i / video frames-ingredients-extend) thì gửi
    # content dạng parts; chỉ prompt thuần mới gửi string.
    content = params["prompt"] if not parts[1:] else parts
    body = {
        "model": request_model,
        "messages": [{"role": "user", "content": content}],
        "stream": stream,
        "generationConfig": gen_config,
    }
    return request_model, body


_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


async def _read_reference_data_url(url: str, client: FlowClient | None = None) -> str:
    """Đọc ảnh tham chiếu (file local /tmp hoặc URL) → data URL base64."""
    client = client or flow_client
    if url.startswith("/"):
        # File local của playground (refs upload hoặc kết quả đã lưu local).
        local_path = TMP_DIR / url.removeprefix("/tmp/")
        try:
            raw = local_path.read_bytes()
        except OSError as exc:
            raise RuntimeError(f"Ảnh tham chiếu không đọc được: {url}") from exc
        ext = local_path.suffix.lower()
    elif url.startswith("http://") or url.startswith("https://"):
        raw, ext = await client.download_media(url)
    else:
        raise RuntimeError(f"URL ảnh tham chiếu không hợp lệ: {url[:80]}")
    mime = _MIME_BY_EXT.get(ext, "image/jpeg")
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _mask_keys_for(params: dict) -> tuple[str, list[str]]:
    """(key chính, fallback keys) cho mask WM — chuẩn hoá ở MỘT chỗ duy nhất.

    WM layout KHÁC nhau giữa các res (1K ≠ 2K) nên mask phải theo đúng res —
    một mask theo ratio dùng chung cho 1K/2K sẽ xoá WM sai vùng. Thứ tự tra
    mask (mọi key đều đi qua watermark.load_mask_for_key, thêm prefix-sweep +
    watermark.png dùng chung ở tầng đó):
      1. ``{base}:{ratio}:{res}``  — key playground hiện tại, frontend lưu mask
         theo key này (vd ``gemini-3.0-pro-image:9:16:2k``). Image sau validate
         luôn có res (mặc định 1K).
      2. ``{base}-{orient}-{res}`` — legacy catalog key đã resolve (vd
         ``gemini-3.0-pro-image-portrait-2k``). Orientation là giá trị ĐẦY ĐỦ của
         ASPECT_RATIO_MAP (landscape/square/three-four/four-three/portrait) —
         không được thu gọn về portrait/landscape, nếu không mask đã lưu cho
         4:3/1:1/3:4 sẽ không bao giờ khớp (hoặc tệ hơn, khớp nhầm mask của
         nhóm landscape và xoá WM sai vùng).
      3. ``{base}-{orient}``       — legacy không suffix res.

    Mỗi playground ratio map sang đúng MỘT nhóm orientation, các nhóm không
    được lẫn nhau (mask 9:16 không áp dụng cho 3:4 — khung khác nhau); mask lệch
    suffix res trong cùng nhóm legacy do prefix-sweep của load_mask_for_key xử lý.
    Gọi không có res (legacy) → giữ nguyên key cũ ``{base}:{ratio}``.
    """
    base = params["base"]
    ratio = params["ratio"]
    orient = ASPECT_RATIO_MAP.get(ratio) or _playground_orientation(ratio)
    res = str(params.get("res") or "").strip().lower()
    if res:
        return f"{base}:{ratio}:{res}", [f"{base}-{orient}-{res}", f"{base}-{orient}"]
    return f"{base}:{ratio}", [f"{base}-{orient}"]


async def _finalize_image(url: str, params: dict, client: FlowClient | None = None) -> dict:
    """Tải ảnh kết quả về, xoá WM (nếu bật + có mask), lưu tmp local."""
    client = client or flow_client
    raw, ext = await client.download_media(url)
    key, fallbacks = _mask_keys_for(params)
    result, status = await watermark.remove_watermark(raw, ext, key, fallback_keys=fallbacks)
    logger.info(
        "[WATERMARK] image model=%s res=%s ratio=%s → %s",
        params["base"], params.get("res"), params.get("ratio"), status,
    )
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}{ext}"
    (RESULTS_DIR / filename).write_bytes(result)
    return _media_item("image", f"/tmp/playground_results/{filename}", params["meta"])


async def _finalize_video(url: str, params: dict, client: FlowClient | None = None) -> dict:
    """Tải video về; crop viền WM nếu bật + model lite (hoặc extend từ lite);
    lưu tmp local kèm mediaGenerationId (nếu có) để hỗ trợ Extend.

    Tải không được (CDN chặn, hết hạn...) thì giữ nguyên URL gốc như hành vi cũ.
    """
    client = client or flow_client
    try:
        raw, ext = await client.download_media(url)
    except Exception as exc:
        logger.warning("Không tải được video về local, giữ URL gốc: %s", exc)
        return _media_item("video", url, params["meta"])

    should_crop = WATERMARK_ENABLED and (
        "lite" in str(params["base"]) or params.get("crop_like_lite")
    )
    status = "skipped:disabled"
    if should_crop:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        filename = f"{uuid.uuid4().hex}{ext}"
        local_path = RESULTS_DIR / filename
        local_path.write_bytes(raw)
        status = await watermark.crop_video_percent(local_path, WATERMARK_VIDEO_CROP_PERCENT)
        logger.info(
            "[WATERMARK] video crop model=%s → %s", params["base"], status
        )
        if status == "cropped":
            raw = local_path.read_bytes()
        local_path.unlink(missing_ok=True)
    else:
        logger.info(
            "[WATERMARK] video crop bỏ qua (enabled=%s, base=%s)", WATERMARK_ENABLED, params["base"]
        )

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}{ext}"
    (RESULTS_DIR / filename).write_bytes(raw)
    item = _media_item("video", f"/tmp/playground_results/{filename}", params["meta"])
    if params.get("media_generation_id"):
        # Media id của video gốc trên Flow — cần để Nối tiếp (Extend).
        item["meta"]["media_generation_id"] = params["media_generation_id"]
    return item


async def _generate_single_playground_item(
    params: dict, api_key: str, client: FlowClient | None = None
) -> dict:
    """Chạy một lượt generation; ném exception nếu item này thất bại."""
    client = client or flow_client
    reference_data_urls: list[str] = []
    if params.get("reference_urls"):
        for ref_url in params["reference_urls"]:
            reference_data_urls.append(await _read_reference_data_url(ref_url, client))

    request_model, body = _compose_chat_body(params, reference_data_urls)
    logger.info(
        "Generation → flow2api model=%s mode=%s vmode=%s refs=%d",
        request_model, params["mode"], params.get("vmode"), len(reference_data_urls),
    )
    meta = {
        "mode": params["mode"],
        "model": params["model"],
        "ratio": params["ratio"],
        "res": params["res"],
        "length": params["length"],
        # Contract "Re use prompt": frontend đọc lại các field này từ meta item
        # để đặt lại form (prompt + id ảnh tham chiếu user đã chọn + vmode).
        # Item cũ trong DB thiếu field không sao — additive-only.
        "prompt": params["prompt"],
        "references": list(params.get("references") or []),
    }
    if params["mode"] == "video":
        # vmode chỉ có nghĩa với video (text/frames/ingredients/extend).
        meta["vmode"] = params.get("vmode") or "text"
    if params.get("vmode") == "extend":
        meta["model"] = "veo-3.1-extend"
        meta["extend_source_item"] = params.get("extend_item_id")

    if params["mode"] == "video":
        # Video chạy stream:true — chunk cuối mang mediaGenerationId cần cho Extend.
        streamed = await client.generate_stream(api_key, body)
        url = streamed["url"]
        meta["media_generation_id"] = streamed["media_id"]
    else:
        payload = await client.generate(api_key, body)
        url = _extract_url_from_openai_payload(payload)
        if not url:
            raise RuntimeError(_payload_error_message(payload))

    item_params = dict(params)
    item_params["meta"] = meta
    if params["mode"] == "image":
        return await _finalize_image(url, item_params, client)
    return await _finalize_video(url, item_params, client)


# ── Ảnh ví dụ làm nền vẽ mask WM ─────────────────────────────────────────────


# Prompt trung tính: chỉ cần 1 ảnh đầy khung, chi tiết để nhìn rõ vị trí WM.
SAMPLE_PROMPT = (
    "Ảnh ví dụ để căn vị trí watermark: bức ảnh phong cảnh tự nhiên chi tiết, "
    "ánh sáng đồng đều, nét rõ, không chữ, không logo."
)


async def generate_watermark_sample(
    model_id: str,
    ratio: str,
    res: str,
    api_key: str,
    api_url: str | None = None,
    raw_models: list[dict] | None = None,
) -> dict:
    """Tạo 1 ảnh ví dụ cho model image + ratio + res để làm nền vẽ mask WM.

    Khác pipeline generate thường: KHÔNG qua remove_watermark — ảnh giữ nguyên
    watermark (vì mục đích của sample là thấy đúng chỗ WM cần tô). WM layout
    khác nhau theo res nên sample phải gen đúng res của key đang vẽ. Ảnh được
    lưu vào thư mục mask (prefix sample_) và dùng lại cho các lần mở editor sau.
    Trả {"key", "filename", "url"} — url là endpoint GET sample cho frontend.
    """
    if raw_models is not None:
        options, resolver_meta = _build_dynamic_gen_options(raw_models)
    else:
        options, resolver_meta = playground_gen_options(), None

    entry = next(
        (item for item in options["image"]["models"] if item["id"] == model_id),
        None,
    )
    if entry is None or not isinstance(entry.get("base"), str) or not entry["base"]:
        raise ValueError("Model không phải model image có base alias.")
    if ratio not in entry["ratios"]:
        raise ValueError("Tỉ lệ khung hình không được hỗ trợ cho model này.")
    res = str(res or "").strip() or "1K"
    supported = [str(item).lower() for item in (entry.get("res") or [])]
    if supported and res.lower() not in supported:
        # Không degrade như validate_generate_payload — sample gen sai res thì
        # mask vẽ lên sẽ sai vị trí WM, im lặng còn tệ hơn báo lỗi.
        raise ValueError("Res không được hỗ trợ cho model này.")

    params = {
        "mode": "image",
        "model": model_id,
        "base": entry["base"],
        "ratio": ratio,
        "res": res,
        "length": None,
        "prompt": SAMPLE_PROMPT,
    }
    if resolver_meta is not None:
        meta = resolver_meta.get(("image", entry["id"]))
        if meta:
            target = _resolve_dynamic_target(meta, entry, "image", ratio, res, None, "text", 0)
            if target:
                params["resolved_model"], params["resolved_config"] = target
    client = get_flow_client(api_url)
    _request_model, body = _compose_chat_body(params, [])
    payload = await client.generate(api_key, body)
    url = _extract_url_from_openai_payload(payload)
    if not url:
        raise RuntimeError(_payload_error_message(payload))
    # Tải về giữ nguyên bytes — sample phải phản ánh đúng vị trí watermark.
    raw, ext = await client.download_media(url)
    key = f"{entry['base']}:{ratio}:{res.lower()}"
    path = watermark.save_sample_for_key(key, raw, ext)
    safe_key = watermark.sanitize_key(key)
    return {
        "key": safe_key,
        "filename": path.name,
        "url": f"/playground/api/watermark/sample/{safe_key}",
    }


# ──────────────────────────────────────────────────────────────────
# Job engine (copy từ flow2api src/main.py — đổi pipeline → HTTP client)
# ──────────────────────────────────────────────────────────────────


async def execute_playground_generation(
    params: dict, api_key: str, client: FlowClient | None = None
) -> tuple[list[dict], list[str]]:
    """Chạy qty generation độc lập song song (Semaphore 4), giữ thứ tự index."""
    client = client or flow_client
    semaphore = asyncio.Semaphore(4)

    async def run_one() -> dict:
        async with semaphore:
            return await _generate_single_playground_item(params, api_key, client)

    outcomes = await asyncio.gather(
        *(run_one() for _ in range(params["qty"])), return_exceptions=True
    )
    items: list[dict] = []
    errors: list[str] = []
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            errors.append(str(outcome) or outcome.__class__.__name__)
        elif isinstance(outcome, dict):
            items.append(outcome)
        else:
            errors.append("Kết quả generation không hợp lệ.")
    return items, errors


async def run_playground_generate_job(
    playground_db,  # PlaygroundDatabase — truyền vào để tránh import vòng
    owner_hash: str,
    project_id: str,
    job_id: str,
    params: dict,
    api_key: str,
    api_url: str | None = None,
):
    # Client theo endpoint user đăng nhập (fallback: env FLOW2API_BASE_URL).
    client = get_flow_client(api_url)
    try:
        items, errors = await execute_playground_generation(params, api_key, client)
        if items:
            # Đặt tên item từ prompt TRƯỚC khi append — đọc tên hiện có của
            # project để dedupe. Project bị xoá giữa chừng thì bỏ qua naming,
            # append bên dưới sẽ đánh dấu job lỗi như cũ.
            project = await playground_db.get_project(owner_hash, project_id)
            if project is not None:
                _assign_generated_item_names(project, items, params.get("prompt"))
            project = await playground_db.append_results(owner_hash, project_id, items)
            if project is None:
                await playground_db.update_job_status(
                    job_id,
                    "error",
                    error="Dự án không còn tồn tại.",
                    results=[],
                    owner_hash=owner_hash,
                    project_id=project_id,
                )
                return
            await playground_db.update_job_status(
                job_id,
                "done",
                error=None,
                results=items,
                owner_hash=owner_hash,
                project_id=project_id,
            )
        else:
            message = (errors[0] if errors else "Tạo media thất bại.")[:500]
            await playground_db.update_job_status(
                job_id, "error", error=message, results=[],
                owner_hash=owner_hash, project_id=project_id,
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        try:
            # Chỉ lộ loại exception cho client — message gốc có thể chứa
            # URL upstream / path hệ thống.
            await playground_db.update_job_status(
                job_id,
                "error",
                error=f"Lỗi hệ thống: {type(exc).__name__}"[:500],
                results=[],
                owner_hash=owner_hash,
                project_id=project_id,
            )
        except Exception:
            pass


def spawn_job(coro) -> asyncio.Task:
    """Spawn job nền và giữ tham chiếu để task không bị GC giữa chừng."""
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task


_BACKGROUND_TASKS: set[asyncio.Task] = set()
