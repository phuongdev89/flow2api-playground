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
                "refs": _model_reference_supported("video", base),
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

    (Adapt từ _validate_playground_generate_payload — cap references theo mode.)
    """
    from fastapi import HTTPException

    references = [str(ref).strip() for ref in (payload.get("references") or []) if str(ref).strip()]
    vmode = (payload.get("vmode") or "frames").strip().lower() or "frames"
    if vmode not in ("frames", "ingredients"):
        raise HTTPException(
            status_code=400, detail="Chế độ generation không hợp lệ, chỉ hỗ trợ 'frames'/'ingredients'."
        )
    mode = (payload.get("mode") or "").strip().lower()
    if mode not in ("image", "video"):
        raise HTTPException(status_code=400, detail="Mode phải là 'image' hoặc 'video'.")
    if mode == "video" and references and vmode != "ingredients":
        raise HTTPException(
            status_code=400,
            detail="Video dùng ảnh tham chiếu cần chế độ 'ingredients'.",
        )
    if vmode == "ingredients" and not references:
        raise HTTPException(
            status_code=400, detail="Chế độ ingredients cần chọn ít nhất 1 ảnh tham chiếu."
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

    options = playground_gen_options()
    spec_entry = next(
        (item for item in options[mode]["models"] if item["id"] == payload.get("model")), None
    )
    if spec_entry is None:
        raise HTTPException(status_code=400, detail="Model không được hỗ trợ cho chế độ này.")
    spec = _playground_model_spec(mode, payload["model"])

    # Model phải quảng bá hỗ trợ tham chiếu (gen-options) mới cho phép.
    if references and not spec_entry.get("refs"):
        raise HTTPException(status_code=400, detail="Model này không hỗ trợ ảnh tham chiếu.")

    ratio = (payload.get("ratio") or "").strip()
    if ratio not in spec_entry["ratios"]:
        raise HTTPException(
            status_code=400, detail="Tỉ lệ khung hình không được hỗ trợ cho model này."
        )

    # res/length không quảng bá cho model hiện tại → degrade về mặc định
    # thay vì để resolver fail.
    res = (payload.get("res") or "").strip() or None
    if res is not None and res not in (spec_entry["res"] or []):
        res = None
    if mode == "image" and not res:
        res = "1K"
    length = payload.get("length")
    if length is not None and length not in (spec_entry["length"] or []):
        length = None

    # Video + tham chiếu: probe key r2v thật với combo đã degrade — không có
    # key phù hợp thì chặn sớm thay vì để job chạy rồi mới lỗi.
    if mode == "video" and references:
        if _compose_video_reference_request(spec["base"], length, res, ratio) is None:
            raise HTTPException(
                status_code=400,
                detail="Model này không hỗ trợ tổ hợp ảnh tham chiếu đã chọn.",
            )

    return {
        "mode": mode,
        "model": payload["model"],
        "base": spec["base"],
        "ratio": ratio,
        "res": res,
        "length": length,
        "qty": qty,
        "prompt": prompt,
        "vmode": vmode,
        "references": references,
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
    """params đã validate → (model alias, body JSON cho /v1/chat/completions)."""
    mode = params["mode"]
    if mode == "image":
        res = (params.get("res") or "1K").lower()
        request_model = params["base"]
        gen_config = {"aspectRatio": params["ratio"], "imageSize": res}
    else:
        if reference_data_urls:
            composed = _compose_video_reference_request(
                params["base"], params["length"], params["res"], params["ratio"]
            )
        else:
            composed = _compose_video_request(
                params["base"], params["length"], params["res"], params["ratio"]
            )
        if composed is None:
            raise RuntimeError("Model không hỗ trợ tổ hợp tham số đã chọn.")
        request_model, gen_config = composed
    if reference_data_urls:
        # Ảnh tham chiếu nối vào message dưới dạng image_url data URL —
        # flow2api _normalize_openai_request tự decode thành bytes upload
        # như IMAGE_INPUT_TYPE_REFERENCE (i2i) / r2v inputs.
        content: list | str = [
            {"type": "text", "text": params["prompt"]},
            *(
                {"type": "image_url", "image_url": {"url": url}}
                for url in reference_data_urls
            ),
        ]
    else:
        content = params["prompt"]
    body = {
        "model": request_model,
        "messages": [{"role": "user", "content": content}],
        "stream": False,
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
    """Tải video về; crop viền WM nếu bật + model lite; lưu tmp local.

    Tải không được (CDN chặn, hết hạn...) thì giữ nguyên URL gốc như hành vi cũ.
    """
    try:
        raw, ext = await flow_client.download_media(url)
    except Exception as exc:
        logger.warning("Không tải được video về local, giữ URL gốc: %s", exc)
        return _media_item("video", url, params["meta"])

    status = "skipped:disabled"
    if WATERMARK_ENABLED and "lite" in str(params["base"]):
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
    return _media_item("video", f"/tmp/playground_results/{filename}", params["meta"])


async def _generate_single_playground_item(params: dict, api_key: str) -> dict:
    """Chạy một lượt generation; ném exception nếu item này thất bại."""
    reference_data_urls: list[str] = []
    if params.get("reference_urls"):
        for ref_url in params["reference_urls"]:
            reference_data_urls.append(await _read_reference_data_url(ref_url))

    request_model, body = _compose_chat_body(params, reference_data_urls)
    logger.info(
        "Generation → flow2api model=%s mode=%s refs=%d",
        request_model, params["mode"], len(reference_data_urls),
    )
    payload = await flow_client.generate(api_key, body)
    url = _extract_url_from_openai_payload(payload)
    if not url:
        raise RuntimeError(_payload_error_message(payload))

    meta = {
        "mode": params["mode"],
        "model": params["model"],
        "ratio": params["ratio"],
        "res": params["res"],
        "length": params["length"],
    }
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
