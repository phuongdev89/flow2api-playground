"""Playground API routes — copy từ block playground của flow2api src/main.py.

Điểm khác chính sau khi tách project:
- Login session xác thực key qua flow2api (GET /v1/models) thay vì AuthManager
  nội bộ; key được lưu vào row session để job nền dùng gọi generation.
- Thêm nhóm endpoint /playground/api/watermark quản mask (thay tab 水印 Mask
  trong admin console của flow2api cũ).
"""

import base64
import hashlib
import logging
import re
import secrets
import time
import uuid

from fastapi import (
    APIRouter,
    Cookie,
    Header,
    HTTPException,
    Request,
    Response,
    Security,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from . import generation, watermark
from .config import (
    PLAYGROUND_MAX_RUNNING_JOBS,
    REFS_DIR,
    STATIC_DIR,
)
from .database import PlaygroundDatabase

logger = logging.getLogger("playground.routes")

router = APIRouter()

playground_db = PlaygroundDatabase()

PLAYGROUND_SESSION_COOKIE = "playground_session"
PLAYGROUND_SESSION_TTL = 30 * 24 * 60 * 60

_NO_STORE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}

optional_security = HTTPBearer(auto_error=False)


class PlaygroundProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    prompt: str = Field(default="", max_length=100_000)
    results: list[dict] = Field(default_factory=list, max_length=500)


class PlaygroundProjectUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    prompt: str | None = Field(default=None, max_length=100_000)
    results: list[dict] | None = Field(default=None, max_length=500)


class PlaygroundGenerateRequest(BaseModel):
    # extra="ignore": frontend có gửi thêm field dự phòng cũng không lỗi 422.
    model_config = ConfigDict(extra="ignore")

    mode: str
    model: str
    ratio: str | None = None
    qty: int | None = None
    res: str | None = None
    length: int | None = None
    vmode: str | None = None
    prompt: str = Field(min_length=1, max_length=100_000)
    # Danh sách id ảnh tham chiếu trong project — cap theo mode (image 10 / video 3).
    references: list[str] = Field(default_factory=list, max_length=10)
    # vmode='extend': id của video item gốc cần nối tiếp (cần meta.media_generation_id).
    extend_item_id: str | None = None


class PlaygroundItemCreateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(default="", max_length=200)
    # Data URL PNG/JPEG/WebP — client crop vùng bôi rồi gửi ảnh đã cắt.
    image: str = Field(min_length=1, max_length=20_000_000)


class PlaygroundItemUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    role: str | None = None
    image: str | None = None


class PlaygroundMaskSaveRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # Data URL PNG từ canvas tô mask trong modal settings.
    image: str = Field(min_length=1, max_length=20_000_000)


def _no_store(response: Response) -> Response:
    for name, value in _NO_STORE_HEADERS.items():
        response.headers[name] = value
    return response


def _response(content, status_code: int = 200) -> JSONResponse:
    return _no_store(JSONResponse(content=content, status_code=status_code))


def _cookie_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def _require_playground_owner(
    playground_session: str | None = Cookie(default=None, alias=PLAYGROUND_SESSION_COOKIE),
) -> str:
    if not playground_session:
        raise HTTPException(
            status_code=401, detail="Playground session required", headers=_NO_STORE_HEADERS
        )
    owner_hash = await playground_db.owner_for_session(_cookie_hash(playground_session))
    if not owner_hash:
        raise HTTPException(
            status_code=401, detail="Playground session expired", headers=_NO_STORE_HEADERS
        )
    return owner_hash


async def _require_playground_mutation(
    request: Request,
    x_playground_request: str | None = Header(default=None, alias="X-Playground-Request"),
):
    if x_playground_request != "1":
        raise HTTPException(
            status_code=403, detail="Same-origin request required", headers=_NO_STORE_HEADERS
        )
    origin = request.headers.get("origin")
    if origin:
        from urllib.parse import urlsplit

        parsed = urlsplit(origin)
        if parsed.scheme != request.url.scheme or parsed.netloc.lower() != request.url.netloc.lower():
            raise HTTPException(
                status_code=403,
                detail="Cross-origin request rejected",
                headers=_NO_STORE_HEADERS,
            )
    elif request.headers.get("sec-fetch-site") != "same-origin":
        raise HTTPException(
            status_code=403, detail="Same-origin request required", headers=_NO_STORE_HEADERS
        )


# ── Static pages ─────────────────────────────────────────────────────────


def _static_page(file_name: str, media_type: str = "text/html") -> FileResponse:
    return FileResponse(str(STATIC_DIR / file_name), media_type=media_type, headers=_NO_STORE_HEADERS)


@router.get("/playground", response_class=HTMLResponse, include_in_schema=False)
async def playground_page():
    return _static_page("playground.html")


@router.get("/static/playground.css", include_in_schema=False)
async def playground_stylesheet():
    return _static_page("playground.css", "text/css")


@router.get("/static/playground.js", include_in_schema=False)
async def playground_script():
    return _static_page("playground.js", "application/javascript")


# ── Session (login/logout) ───────────────────────────────────────────────


@router.api_route(
    "/playground/session", methods=["GET", "POST"], status_code=204, include_in_schema=False
)
async def playground_session(
    response: Response,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(optional_security),
    playground_session: str | None = Cookie(default=None, alias=PLAYGROUND_SESSION_COOKIE),
):
    owner_hash = None
    if playground_session:
        owner_hash = await playground_db.owner_for_session(_cookie_hash(playground_session))
    if request.method == "POST" and owner_hash is None and credentials is not None:
        api_key = credentials.credentials
        # Key xác thực trực tiếp với flow2api — playground không giữ danh sách
        # key riêng. Key hợp lệ được lưu vào session để job nền gọi generation.
        if await generation.flow_client.validate_key(api_key):
            owner_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
            raw_token = secrets.token_urlsafe(48)
            await playground_db.create_session(
                _cookie_hash(raw_token), owner_hash, int(time.time()) + PLAYGROUND_SESSION_TTL,
                api_key=api_key,
            )
            response.set_cookie(
                PLAYGROUND_SESSION_COOKIE,
                raw_token,
                max_age=PLAYGROUND_SESSION_TTL,
                httponly=True,
                samesite="lax",
                secure=request.url.scheme == "https",
                path="/playground",
            )
    if owner_hash is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid API key or expired session",
            headers={**_NO_STORE_HEADERS, "WWW-Authenticate": "Bearer"},
        )
    response.status_code = 204
    return _no_store(response)


@router.post("/playground/logout", status_code=204, include_in_schema=False)
async def playground_logout(
    response: Response,
    request: Request,
    playground_session: str | None = Cookie(default=None, alias=PLAYGROUND_SESSION_COOKIE),
):
    await _require_playground_mutation(request, request.headers.get("x-playground-request"))
    if playground_session:
        await playground_db.revoke_session(_cookie_hash(playground_session))
    response.delete_cookie(PLAYGROUND_SESSION_COOKIE, path="/playground")
    response.status_code = 204
    return _no_store(response)


# ── Projects CRUD ────────────────────────────────────────────────────────


@router.get("/playground/api/projects", include_in_schema=False)
async def playground_projects(owner_hash: str = Security(_require_playground_owner)):
    return _response(await playground_db.list_projects(owner_hash))


@router.post("/playground/api/projects", status_code=201, include_in_schema=False)
async def playground_create_project(
    payload: PlaygroundProjectCreate,
    request: Request,
    owner_hash: str = Security(_require_playground_owner),
):
    await _require_playground_mutation(request, request.headers.get("x-playground-request"))
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Project name cannot be blank")
    project = await playground_db.create_project(
        secrets.token_urlsafe(16), owner_hash, name, payload.prompt, payload.results
    )
    return _response(project, status_code=201)


@router.patch("/playground/api/projects/{project_id}", include_in_schema=False)
async def playground_update_project(
    project_id: str,
    payload: PlaygroundProjectUpdate,
    request: Request,
    owner_hash: str = Security(_require_playground_owner),
):
    await _require_playground_mutation(request, request.headers.get("x-playground-request"))
    fields = payload.model_dump(exclude_unset=True)
    if "name" in fields:
        fields["name"] = fields["name"].strip()
        if not fields["name"]:
            raise HTTPException(status_code=422, detail="Project name cannot be blank")
    project = await playground_db.update_project(owner_hash, project_id, fields)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return _response(project)


@router.post("/playground/api/projects/{project_id}/activity", include_in_schema=False)
async def playground_touch_project(
    project_id: str,
    request: Request,
    owner_hash: str = Security(_require_playground_owner),
):
    await _require_playground_mutation(request, request.headers.get("x-playground-request"))
    project = await playground_db.touch_project(owner_hash, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return _response(project)


@router.delete("/playground/api/projects/{project_id}", status_code=204, include_in_schema=False)
async def playground_delete_project(
    project_id: str,
    request: Request,
    response: Response,
    owner_hash: str = Security(_require_playground_owner),
):
    await _require_playground_mutation(request, request.headers.get("x-playground-request"))
    if not await playground_db.delete_project(owner_hash, project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    response.status_code = 204
    return _no_store(response)


# ── Generation (job nền) ─────────────────────────────────────────────────


@router.get("/playground/api/gen-options", include_in_schema=False)
async def playground_gen_options(owner_hash: str = Security(_require_playground_owner)):
    return _response(generation.playground_gen_options())


@router.post(
    "/playground/api/projects/{project_id}/generate",
    status_code=202,
    include_in_schema=False,
)
async def playground_generate(
    project_id: str,
    payload: PlaygroundGenerateRequest,
    request: Request,
    owner_hash: str = Security(_require_playground_owner),
):
    await _require_playground_mutation(request, request.headers.get("x-playground-request"))
    project = await playground_db.get_project(owner_hash, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    # Cap server-side: client chỉ chặn double-send ở UI — ai curl trực tiếp
    # vẫn phải bị giới hạn để bảo vệ quota upstream.
    if await playground_db.count_running_jobs(owner_hash) >= PLAYGROUND_MAX_RUNNING_JOBS:
        raise HTTPException(
            status_code=429,
            detail="Đang có quá nhiều job chạy cùng lúc — vui lòng đợi job hiện tại xong.",
        )
    params = generation.validate_generate_payload(payload.model_dump())
    items_by_id = {
        str(item.get("id")): item
        for item in (project.get("results") or [])
        if isinstance(item, dict)
    }
    if params["references"]:
        # Resolve id tham chiếu → URL ảnh thật trong project; id lạ thì 400
        # để client kịp sửa thay vì job error lúc chạy.
        reference_urls = []
        for ref_id in params["references"]:
            item = items_by_id.get(ref_id)
            url = item.get("url") if isinstance(item, dict) else None
            if item is None or item.get("kind") != "image" or not isinstance(url, str) or not url:
                raise HTTPException(
                    status_code=400,
                    detail="Ảnh tham chiếu không hợp lệ hoặc không còn tồn tại trong dự án.",
                )
            reference_urls.append(url)
        params["reference_urls"] = reference_urls
    if params.get("vmode") == "extend":
        source = items_by_id.get(params["extend_item_id"] or "")
        media_id = (source or {}).get("meta", {}).get("media_generation_id") if source else None
        if (
            source is None
            or source.get("kind") != "video"
            or not isinstance(media_id, str)
            or not media_id
        ):
            raise HTTPException(
                status_code=400,
                detail="Video gốc không hợp lệ hoặc thiếu media id — chỉ video tạo trong "
                "playground mới nối tiếp được.",
            )
        params["extend_media_id"] = media_id
        # Video extend từ nguồn lite: đoạn mới vẫn có WM viền → crop giống lite.
        params["crop_like_lite"] = "lite" in str((source.get("meta") or {}).get("model", ""))

    api_key = await playground_db.api_key_for_owner(owner_hash)
    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="Phiên hết hạn key flow2api — vui lòng đăng nhập lại.",
            headers=_NO_STORE_HEADERS,
        )

    job = await playground_db.create_job(str(uuid.uuid4()), owner_hash, project_id)
    generation.spawn_job(
        generation.run_playground_generate_job(
            playground_db, owner_hash, project_id, job["job_id"], params, api_key
        )
    )
    return _response({"job_id": job["job_id"]}, status_code=202)


@router.get(
    "/playground/api/projects/{project_id}/generate/{job_id}",
    include_in_schema=False,
)
async def playground_generate_status(
    project_id: str,
    job_id: str,
    owner_hash: str = Security(_require_playground_owner),
):
    job = await playground_db.get_job(owner_hash, project_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return _response(job)


# ── Playground items (results / ảnh tham chiếu) ──────────────────────────

_PLAYGROUND_REFERENCE_DATA_URL_RE = re.compile(
    r"^data:image/(png|jpeg|jpg|webp);base64,([A-Za-z0-9+/=\s]+)$"
)
_PLAYGROUND_REFERENCE_MAX_BYTES = 12 * 1024 * 1024


def _decode_image_data_url(data_url: str, error_detail: str) -> tuple[bytes, str]:
    match = _PLAYGROUND_REFERENCE_DATA_URL_RE.match((data_url or "").strip())
    if not match:
        raise HTTPException(status_code=400, detail=error_detail)
    ext = {"jpeg": "jpg", "jpg": "jpg", "png": "png", "webp": "webp"}[match.group(1)]
    try:
        raw = base64.b64decode(re.sub(r"\s+", "", match.group(2)), validate=True)
    except Exception:
        raise HTTPException(status_code=400, detail="Ảnh base64 không hợp lệ.")
    if not raw:
        raise HTTPException(status_code=400, detail="Ảnh rỗng.")
    if len(raw) > _PLAYGROUND_REFERENCE_MAX_BYTES:
        raise HTTPException(status_code=400, detail="Ảnh quá lớn (tối đa 12MB).")
    return raw, ext


def _save_playground_reference_image(raw: bytes, ext: str) -> str:
    REFS_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.{ext}"
    (REFS_DIR / filename).write_bytes(raw)
    return f"/tmp/playground_refs/{filename}"


def _mutate_project_results(project: dict, item_id: str, mutate):
    """Trả results mới sau mutate(item); None nếu item_id không tồn tại.

    mutate trả dict (item thay thế) hoặc None (xóa item).
    """
    results = project.get("results") or []
    new_results = []
    found = False
    for item in results:
        if isinstance(item, dict) and str(item.get("id")) == item_id:
            found = True
            mutated = mutate(dict(item))
            if mutated is not None:
                new_results.append(mutated)
        else:
            new_results.append(item)
    return new_results if found else None


async def _load_owned_project_or_404(owner_hash: str, project_id: str) -> dict:
    project = await playground_db.get_project(owner_hash, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.post(
    "/playground/api/projects/{project_id}/items",
    status_code=201,
    include_in_schema=False,
)
async def playground_create_item(
    project_id: str,
    payload: PlaygroundItemCreateRequest,
    request: Request,
    owner_hash: str = Security(_require_playground_owner),
):
    await _require_playground_mutation(request, request.headers.get("x-playground-request"))
    await _load_owned_project_or_404(owner_hash, project_id)
    raw, ext = _decode_image_data_url(payload.image, "Ảnh tham chiếu phải là data URL PNG/JPEG/WebP.")
    url = _save_playground_reference_image(raw, ext)
    item = {
        "id": str(uuid.uuid4()),
        "kind": "image",
        "url": url,
        "created_at": int(time.time() * 1000),
        "meta": {"source": "playground-reference"},
        "role": "reference",
        "name": payload.name.strip() or "Tham chiếu",
    }
    updated = await playground_db.append_results(owner_hash, project_id, [item])
    if updated is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return _response(updated, status_code=201)


@router.patch(
    "/playground/api/projects/{project_id}/items/{item_id}",
    include_in_schema=False,
)
async def playground_update_item(
    project_id: str,
    item_id: str,
    payload: PlaygroundItemUpdateRequest,
    request: Request,
    owner_hash: str = Security(_require_playground_owner),
):
    await _require_playground_mutation(request, request.headers.get("x-playground-request"))
    await _load_owned_project_or_404(owner_hash, project_id)
    if payload.image is not None:
        _decode_image_data_url(payload.image, "Ảnh tham chiếu phải là data URL PNG/JPEG/WebP.")
    if payload.role is not None and payload.role not in ("reference", "result"):
        raise HTTPException(
            status_code=400, detail="Role chỉ nhận 'reference' hoặc 'result'."
        )
    if payload.name is None and payload.role is None and payload.image is None:
        raise HTTPException(status_code=400, detail="Không có trường nào để cập nhật.")

    def mutate(item: dict) -> dict:
        if payload.image is not None:
            raw, ext = _decode_image_data_url(
                payload.image, "Ảnh tham chiếu phải là data URL PNG/JPEG/WebP."
            )
            item["url"] = _save_playground_reference_image(raw, ext)
            item.setdefault("meta", {})["source"] = "playground-reference"
        if payload.name is not None:
            item["name"] = payload.name.strip()
        if payload.role is not None:
            item["role"] = payload.role
        item["updated_at"] = int(time.time() * 1000)
        return item

    project = await _load_owned_project_or_404(owner_hash, project_id)
    new_results = _mutate_project_results(project, item_id, mutate)
    if new_results is None:
        raise HTTPException(status_code=404, detail="Item not found")
    updated = await playground_db.update_project(owner_hash, project_id, {"results": new_results})
    if updated is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return _response(updated)


@router.delete(
    "/playground/api/projects/{project_id}/items/{item_id}",
    status_code=204,
    include_in_schema=False,
)
async def playground_delete_item(
    project_id: str,
    item_id: str,
    request: Request,
    owner_hash: str = Security(_require_playground_owner),
):
    await _require_playground_mutation(request, request.headers.get("x-playground-request"))
    project = await _load_owned_project_or_404(owner_hash, project_id)

    def drop(_: dict):
        return None

    new_results = _mutate_project_results(project, item_id, drop)
    if new_results is None:
        raise HTTPException(status_code=404, detail="Item not found")
    updated = await playground_db.update_project(owner_hash, project_id, {"results": new_results})
    if updated is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return _no_store(Response(status_code=204))


# ── Watermark mask management (thay tab 水印 Mask admin console cũ) ──────


@router.get("/playground/api/watermark", include_in_schema=False)
async def playground_watermark_info(owner_hash: str = Security(_require_playground_owner)):
    return _response(
        {
            "enabled": watermark.WATERMARK_ENABLED,
            "engine": watermark.get_engine_name(),
            "masks": watermark.list_saved_masks(),
        }
    )


@router.post("/playground/api/watermark/mask/{mask_key}", include_in_schema=False)
async def playground_save_mask(
    mask_key: str,
    payload: PlaygroundMaskSaveRequest,
    request: Request,
    owner_hash: str = Security(_require_playground_owner),
):
    await _require_playground_mutation(request, request.headers.get("x-playground-request"))
    raw, _ext = _decode_image_data_url(payload.image, "Mask phải là data URL PNG/JPEG/WebP.")
    # Mask luôn lưu PNG 0/255 để remove resize INTER_NEAREST khớp từng pixel.
    try:
        path = watermark.save_mask_for_key(mask_key, raw)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _response({"saved": path.name, "engine": watermark.get_engine_name()})


@router.delete("/playground/api/watermark/mask/{mask_key}", status_code=204, include_in_schema=False)
async def playground_delete_mask(
    mask_key: str,
    request: Request,
    response: Response,
    owner_hash: str = Security(_require_playground_owner),
):
    await _require_playground_mutation(request, request.headers.get("x-playground-request"))
    if not watermark.delete_mask_for_key(mask_key):
        raise HTTPException(status_code=404, detail="Mask not found")
    response.status_code = 204
    return _no_store(response)
