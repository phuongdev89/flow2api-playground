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
import json
import logging
import mimetypes
import re
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
    base: str, length: int | None, res: str | None, ratio: str
) -> tuple[str, dict] | None:
    """Compose key i2v (Frames to Video — 1-2 ảnh làm frame đầu/cuối).

    Chỉ lite (veo_3_1_i2v_lite*) và quality (veo_3_1_i2v_s*) có i2v trong
    catalog; fast/fast-ultra không có → None. Key lite có suffix orientation,
    key quality không (resolver tự map từ generationConfig.aspectRatio).
    """
    if base.endswith("_lite"):
        family = "veo_3_1_i2v_lite"
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


def playground_gen_options() -> dict:
    return {
        "image": {"models": _playground_image_model_options()},
        "video": {"models": _playground_video_model_options()},
    }


def validate_generate_payload(payload: dict) -> dict:
    """Validate payload generate từ frontend; trả params dict cho job nền.

    vmode cho video: 'text' (t2v) / 'frames' (i2v 1-2 ảnh đầu+cuối) /
    'ingredients' (r2v 1-3 ảnh tham chiếu) / 'extend' (nối tiếp video đã tạo).
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

    options = playground_gen_options()
    spec_entry = next(
        (item for item in options[mode]["models"] if item["id"] == payload.get("model")), None
    )
    if spec_entry is None and vmode != "extend":
        raise HTTPException(status_code=400, detail="Model không được hỗ trợ cho chế độ này.")
    spec = _playground_model_spec(mode, payload.get("model")) if spec_entry else None

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

    # Probe key compose thật với combo đã degrade — không có key phù hợp thì
    # chặn sớm thay vì để job chạy rồi mới lỗi.
    if mode == "video" and vmode == "frames":
        if not spec_entry.get("frames"):
            raise HTTPException(
                status_code=400, detail="Model này không hỗ trợ Frames to Video (i2v)."
            )
        if _compose_video_frames_request(spec["base"], length, res, ratio) is None:
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

    return {
        "mode": mode,
        "model": payload.get("model") or "",
        "base": spec["base"] if spec else f"veo_3_1_extend",
        "ratio": ratio,
        "res": res,
        "length": length,
        "qty": qty,
        "prompt": prompt,
        "vmode": vmode,
        "references": references,
        "extend_item_id": str(payload.get("extend_item_id") or "").strip() or None,
    }


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
# HTTP client sang flow2api
# ──────────────────────────────────────────────────────────────────


class FlowClient:
    """Client gọi flow2api (engine generation) qua OpenAI-compatible API."""

    def __init__(self, base_url: str = FLOW2API_BASE_URL):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(FLOW2API_TIMEOUT),
            follow_redirects=True,
        )

    async def aclose(self):
        await self._client.aclose()

    async def validate_key(self, api_key: str) -> bool:
        """Key hợp lệ khi flow2api /v1/models chấp nhận Bearer key này."""
        try:
            response = await self._client.get(
                "/v1/models", headers={"Authorization": f"Bearer {api_key}"}
            )
        except httpx.HTTPError as exc:
            logger.error("Không kết nối được flow2api %s: %s", self.base_url, exc)
            return False
        return response.status_code == 200

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
    if mode == "image":
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
            params["base"], params["length"], params["res"], params["ratio"]
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
    content = params["prompt"] if (mode == "image" or not parts[1:]) else parts
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


async def _read_reference_data_url(url: str) -> str:
    """Đọc ảnh tham chiếu (file local /tmp hoặc URL) → data URL base64."""
    if url.startswith("/"):
        # File local của playground (refs upload hoặc kết quả đã lưu local).
        local_path = TMP_DIR / url.removeprefix("/tmp/")
        try:
            raw = local_path.read_bytes()
        except OSError as exc:
            raise RuntimeError(f"Ảnh tham chiếu không đọc được: {url}") from exc
        ext = local_path.suffix.lower()
    elif url.startswith("http://") or url.startswith("https://"):
        raw, ext = await flow_client.download_media(url)
    else:
        raise RuntimeError(f"URL ảnh tham chiếu không hợp lệ: {url[:80]}")
    mime = _MIME_BY_EXT.get(ext, "image/jpeg")
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _mask_keys_for(params: dict) -> tuple[str, list[str]]:
    """(key chính, fallback keys) cho mask WM: {base}:{ratio} + mask legacy."""
    base = params["base"]
    orient = _playground_orientation(params["ratio"])
    fallbacks = []
    if params.get("res"):
        fallbacks.append(f"{base}-{orient}-{params['res'].lower()}")
    fallbacks.append(f"{base}-{orient}")
    return f"{base}:{params['ratio']}", fallbacks


async def _finalize_image(url: str, params: dict) -> dict:
    """Tải ảnh kết quả về, xoá WM (nếu bật + có mask), lưu tmp local."""
    raw, ext = await flow_client.download_media(url)
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


async def _finalize_video(url: str, params: dict) -> dict:
    """Tải video về; crop viền WM nếu bật + model lite (hoặc extend từ lite);
    lưu tmp local kèm mediaGenerationId (nếu có) để hỗ trợ Extend.

    Tải không được (CDN chặn, hết hạn...) thì giữ nguyên URL gốc như hành vi cũ.
    """
    try:
        raw, ext = await flow_client.download_media(url)
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


async def _generate_single_playground_item(params: dict, api_key: str) -> dict:
    """Chạy một lượt generation; ném exception nếu item này thất bại."""
    reference_data_urls: list[str] = []
    if params.get("reference_urls"):
        for ref_url in params["reference_urls"]:
            reference_data_urls.append(await _read_reference_data_url(ref_url))

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
    }
    if params.get("vmode") == "extend":
        meta["model"] = "veo-3.1-extend"
        meta["extend_source_item"] = params.get("extend_item_id")

    if params["mode"] == "video":
        # Video chạy stream:true — chunk cuối mang mediaGenerationId cần cho Extend.
        streamed = await flow_client.generate_stream(api_key, body)
        url = streamed["url"]
        meta["media_generation_id"] = streamed["media_id"]
    else:
        payload = await flow_client.generate(api_key, body)
        url = _extract_url_from_openai_payload(payload)
        if not url:
            raise RuntimeError(_payload_error_message(payload))

    item_params = dict(params)
    item_params["meta"] = meta
    if params["mode"] == "image":
        return await _finalize_image(url, item_params)
    return await _finalize_video(url, item_params)


# ──────────────────────────────────────────────────────────────────
# Job engine (copy từ flow2api src/main.py — đổi pipeline → HTTP client)
# ──────────────────────────────────────────────────────────────────


async def execute_playground_generation(params: dict, api_key: str) -> tuple[list[dict], list[str]]:
    """Chạy qty generation độc lập song song (Semaphore 4), giữ thứ tự index."""
    semaphore = asyncio.Semaphore(4)

    async def run_one() -> dict:
        async with semaphore:
            return await _generate_single_playground_item(params, api_key)

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
):
    try:
        items, errors = await execute_playground_generation(params, api_key)
        if items:
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
