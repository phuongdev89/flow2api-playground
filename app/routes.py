"""Playground API routes — copy từ block playground của flow2api src/main.py.

Điểm khác chính sau khi tách project:
- Login session xác thực cặp (api_url, api_key) qua GET /v1/models trên
  endpoint user nhập; cả hai được lưu vào row session để job nền dùng gọi
  generation đúng endpoint đã đăng nhập.
- Thêm nhóm endpoint /playground/api/watermark quản mask (thay tab 水印 Mask
  trong admin console của flow2api cũ).
"""

import base64
import hashlib
import logging
import math
import re
import secrets
import time
import uuid
from urllib.parse import urlsplit

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

from . import generation, studio, watermark
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


class PlaygroundSessionRequest(BaseModel):
    """Login từ form: endpoint + key đều do user nhập (đúng cả hai mới vào được)."""

    model_config = ConfigDict(extra="ignore")

    api_url: str | None = Field(default=None, max_length=2000)
    api_key: str | None = Field(default=None, min_length=1, max_length=500)


def _normalize_flow_url(raw: str | None) -> str | None:
    """Chuẩn hoá endpoint user nhập; sai format thì 422 để form báo lỗi ngay.

    None = không gửi URL (script/curl cũ) → fallback endpoint theo env config.
    Còn gửi mà rỗng/sai scheme thì chặn — form yêu cầu cả hai ô mới vào được.
    """
    if raw is None:
        return None
    url = raw.strip().rstrip("/")
    parsed = urlsplit(url)
    if not url or parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(
            status_code=422,
            detail="Invalid API URL — use an http:// or https:// endpoint.",
            headers=_NO_STORE_HEADERS,
        )
    return url


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


class PlaygroundTrimRequest(BaseModel):
    # extra="ignore" theo pattern PlaygroundGenerateRequest.
    model_config = ConfigDict(extra="ignore")

    # Cắt giữ lại đoạn [start_sec, end_sec) của item video (giây).
    start_sec: float
    end_sec: float


class PlaygroundMaskSaveRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # Data URL PNG từ canvas tô mask trong modal settings.
    image: str = Field(min_length=1, max_length=20_000_000)


class PlaygroundSampleRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # Model image + tỉ lệ + res để tạo 1 ảnh ví dụ (nền vẽ mask WM, giữ nguyên
    # watermark). WM layout khác nhau theo res nên sample gen đúng res của key.
    model: str = Field(min_length=1, max_length=200)
    ratio: str = Field(min_length=1, max_length=20)
    res: str | None = Field(default=None, max_length=20)


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
    payload: PlaygroundSessionRequest | None = None,
    credentials: HTTPAuthorizationCredentials | None = Security(optional_security),
    playground_session: str | None = Cookie(default=None, alias=PLAYGROUND_SESSION_COOKIE),
):
    owner_hash = None
    if playground_session:
        owner_hash = await playground_db.owner_for_session(_cookie_hash(playground_session))
    if request.method == "POST" and owner_hash is None:
        # Ưu tiên JSON body từ form (api_url + api_key); Bearer header giữ lại
        # cho script/curl. Không có api_url → endpoint mặc định theo env config.
        api_key = (payload.api_key if payload else None) or (
            credentials.credentials if credentials else None
        )
        api_url = _normalize_flow_url(payload.api_url if payload else None)
        if api_key:
            client = generation.get_flow_client(api_url)
            # Key xác thực trực tiếp với endpoint đã nhập — sai URL hoặc sai key
            # đều không cấp session. Lỗi được phân loại để form báo đúng chỗ sửa.
            if await client.validate_key(api_key):
                owner_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
                raw_token = secrets.token_urlsafe(48)
                fetched_models = getattr(client, "last_models", None)
                if fetched_models is not None:
                    generation.cache_models_for_credentials(api_key, api_url, fetched_models)
                await playground_db.create_session(
                    _cookie_hash(raw_token), owner_hash, int(time.time()) + PLAYGROUND_SESSION_TTL,
                    api_key=api_key,
                    api_url=api_url,
                    models=fetched_models,
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
            elif client.last_check_error == "unreachable":
                raise HTTPException(
                    status_code=502,
                    detail="Could not connect to the API URL — check the endpoint address.",
                    headers=_NO_STORE_HEADERS,
                )
            else:
                raise HTTPException(
                    status_code=401,
                    detail="Invalid API key for this endpoint.",
                    headers={**_NO_STORE_HEADERS, "WWW-Authenticate": "Bearer"},
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
        token_hash = _cookie_hash(playground_session)
        owner_hash = await playground_db.owner_for_session(token_hash)
        if owner_hash:
            api_key, api_url = await playground_db.api_credentials_for_owner(owner_hash)
            generation.invalidate_models_cache(api_key, api_url)
        await playground_db.revoke_session(token_hash)
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
    raw_models = await generation.get_models_for_session(
        playground_db, owner_hash, refresh=True
    )
    return _response(generation.playground_gen_options(raw_models))


def _resolve_generate_params(project: dict, params: dict) -> dict:
    """Resolve tham chiếu/extend của params theo trạng thái HIỆN TẠI của project.

    Dùng chung cho generate lần đầu và retry job lỗi: references (id item ảnh)
    → reference_urls, extend_item_id → extend_media_id. Id lạ / item đã bị xoá
    → HTTPException 400 để client kịp sửa thay vì job error lúc chạy.
    """
    items_by_id = {
        str(item.get("id")): item
        for item in (project.get("results") or [])
        if isinstance(item, dict)
    }
    params = dict(params)
    if params.get("references"):
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
        source = items_by_id.get(params.get("extend_item_id") or "")
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
    return params


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
    raw_models = await generation.get_models_for_session(playground_db, owner_hash, refresh=False)
    params = generation.validate_generate_payload(payload.model_dump(), raw_models=raw_models)
    params = _resolve_generate_params(project, params)

    api_key, api_url = await playground_db.api_credentials_for_owner(owner_hash)
    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="Phiên hết hạn key flow2api — vui lòng đăng nhập lại.",
            headers=_NO_STORE_HEADERS,
        )

    job = await playground_db.create_job(str(uuid.uuid4()), owner_hash, project_id, params)
    generation.spawn_job(
        generation.run_playground_generate_job(
            playground_db, owner_hash, project_id, job["job_id"], params, api_key, api_url
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


@router.get("/playground/api/projects/{project_id}/jobs", include_in_schema=False)
async def playground_project_jobs(
    project_id: str,
    owner_hash: str = Security(_require_playground_owner),
):
    """Danh sách job gần nhất của project (mới nhất trước, tối đa 20) — frontend
    gọi sau F5 để khôi phục queue.

    GET chỉ cần session owner (không cần marker same-origin). Project không còn
    hoặc chưa có job nào → trả danh sách rỗng (không 404) để client xử lý một
    nhánh duy nhất. Owner khác không bao giờ thấy job của nhau vì query lọc
    theo owner_hash của session.
    """
    jobs = await playground_db.list_jobs_for_project(owner_hash, project_id)
    return _response({"jobs": jobs})


@router.post(
    "/playground/api/projects/{project_id}/jobs/{job_id}/retry",
    status_code=202,
    include_in_schema=False,
)
async def playground_retry_job(
    project_id: str,
    job_id: str,
    request: Request,
    owner_hash: str = Security(_require_playground_owner),
):
    """Chạy lại job LỖI với đúng bộ tham số lúc gửi (giữ nguyên job_id).

    Tham số gốc lưu trên row job (params_json); references/extend được resolve
    lại theo project hiện tại — item đã bị xoá thì 400 thay vì để job chết
    lần nữa. Job về trạng thái 'running' tại chỗ nên thẻ lỗi trên UI biến
    lại thành spinner mà không nhảy vị trí.
    """
    await _require_playground_mutation(request, request.headers.get("x-playground-request"))
    job = await playground_db.get_job(owner_hash, project_id, job_id, include_params=True)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != "error":
        raise HTTPException(status_code=409, detail="Chỉ job thất bại mới thử lại được.")
    stored = job.get("params")
    if not isinstance(stored, dict) or not stored:
        raise HTTPException(
            status_code=409,
            detail="Job này không lưu được tham số gốc (tạo trước khi có tính năng retry) — "
            "hãy gửi lại từ composer.",
        )
    project = await playground_db.get_project(owner_hash, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    # Guard như generate: cap job chạy + key còn hạn; params đã validate lúc gửi
    # nhưng vẫn chạy lại validate_generate_payload để schema cũ/khó hiểu bị chặn sớm.
    if await playground_db.count_running_jobs(owner_hash) >= PLAYGROUND_MAX_RUNNING_JOBS:
        raise HTTPException(
            status_code=429,
            detail="Đang có quá nhiều job chạy cùng lúc — vui lòng đợi job hiện tại xong.",
        )
    raw_models = await generation.get_models_for_session(playground_db, owner_hash, refresh=False)
    try:
        params = generation.validate_generate_payload(stored, raw_models=raw_models)
    except HTTPException as exc:
        raise HTTPException(
            status_code=409,
            detail=f"Tham số gốc không còn hợp lệ: {exc.detail}",
        ) from exc
    params = _resolve_generate_params(project, params)
    api_key, api_url = await playground_db.api_credentials_for_owner(owner_hash)
    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="Phiên hết hạn key flow2api — vui lòng đăng nhập lại.",
            headers=_NO_STORE_HEADERS,
        )
    await playground_db.update_job_status(
        job_id,
        "running",
        error=None,
        results=[],
        owner_hash=owner_hash,
        project_id=project_id,
    )
    generation.spawn_job(
        generation.run_playground_generate_job(
            playground_db, owner_hash, project_id, job_id, params, api_key, api_url
        )
    )
    return _response({"job_id": job_id}, status_code=202)


@router.delete(
    "/playground/api/projects/{project_id}/jobs/{job_id}",
    status_code=204,
    include_in_schema=False,
)
async def playground_delete_job(
    project_id: str,
    job_id: str,
    request: Request,
    owner_hash: str = Security(_require_playground_owner),
):
    """Xoá job thất bại khỏi danh sách (thẻ lỗi biến mất khỏi UI và queue F5)."""
    await _require_playground_mutation(request, request.headers.get("x-playground-request"))
    job = await playground_db.get_job(owner_hash, project_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] == "running":
        raise HTTPException(
            status_code=409,
            detail="Job đang chạy — không xoá được, đợi job xong hoặc lỗi đã.",
        )
    await playground_db.delete_job(owner_hash, project_id, job_id)


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


@router.post(
    "/playground/api/projects/{project_id}/items/{item_id}/trim",
    status_code=201,
    include_in_schema=False,
)
async def playground_trim_item(
    project_id: str,
    item_id: str,
    payload: PlaygroundTrimRequest,
    request: Request,
    owner_hash: str = Security(_require_playground_owner),
):
    """Cắt giữ lại đoạn [start_sec, end_sec) của item video → item mới.

    File và item gốc giữ nguyên; bản trim là mp4 mới trong RESULTS_DIR, không
    mang media_generation_id (không tồn tại trên Flow nên không extend được).
    """
    await _require_playground_mutation(request, request.headers.get("x-playground-request"))
    project = await _load_owned_project_or_404(owner_hash, project_id)
    source = next(
        (
            item
            for item in project.get("results") or []
            if isinstance(item, dict) and str(item.get("id")) == item_id
        ),
        None,
    )
    if source is None:
        raise HTTPException(status_code=404, detail="Item not found")
    if source.get("kind") != "video":
        raise HTTPException(status_code=400, detail="Chỉ cắt được item video.")

    start, end = float(payload.start_sec), float(payload.end_sec)
    if not (math.isfinite(start) and math.isfinite(end)):
        raise HTTPException(status_code=400, detail="Thời gian cắt phải là số hợp lệ.")
    if start < 0 or end <= start:
        raise HTTPException(
            status_code=400, detail="Khoảng cắt không hợp lệ — cần 0 ≤ start < end."
        )
    length = end - start
    if length < generation.TRIM_MIN_DURATION_SEC:
        raise HTTPException(
            status_code=400, detail="Đoạn cắt quá ngắn — tối thiểu 0.2 giây."
        )
    if length > generation.TRIM_MAX_DURATION_SEC:
        raise HTTPException(
            status_code=400, detail="Đoạn cắt quá dài — tối đa 120 giây."
        )

    try:
        item = await generation.trim_playground_video_item(source, start, end)
    except ValueError as exc:
        # Path lạ / file mất / end vượt độ dài — message tự viết, không lộ path.
        raise HTTPException(status_code=400, detail=str(exc))
    except generation.TrimVideoError as exc:
        # Chi tiết ffmpeg/subprocess chỉ log server-side (chứa path hệ thống).
        logger.warning("[TRIM] %s/%s thất bại: %s", project_id, item_id, exc)
        raise HTTPException(
            status_code=500,
            detail="Không cắt được video — ffmpeg lỗi hoặc định dạng không hỗ trợ.",
        )

    # Tên item mới: "{tên gốc} (trim)", dedupe bằng helper dùng chung của generation.
    original_name = str(source.get("name") or "").strip()
    base_name = f"{original_name} (trim)" if original_name else "Video (trim)"
    generation._assign_generated_item_names(project, [item], None, base_name=base_name)
    if await playground_db.append_results(owner_hash, project_id, [item]) is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return _response(item, status_code=201)


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


@router.get("/playground/api/watermark/mask/{mask_key}", include_in_schema=False)
async def playground_get_mask(
    mask_key: str,
    owner_hash: str = Security(_require_playground_owner),
):
    # Exact key (frontend gọi với key đã sanitize) — mask active qua fallback
    # chain là chuyện của remove_watermark, editor chỉ nạp mask của chính key đó.
    raw = watermark.read_mask_file_bytes(mask_key)
    if raw is None:
        raise HTTPException(status_code=404, detail="Mask not found")
    return Response(content=raw, media_type="image/png", headers=_NO_STORE_HEADERS)


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


# Owner đang chạy generate_watermark_sample — chặn double-click/spam sample
# (mỗi lần gọi tốn một lượt generation thật phía upstream).
_sample_generating: set[str] = set()

_MIME_BY_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".bmp": "image/bmp"}


@router.get("/playground/api/watermark/sample/{mask_key}", include_in_schema=False)
async def playground_get_sample(
    mask_key: str,
    owner_hash: str = Security(_require_playground_owner),
):
    found = watermark.read_sample_for_key(mask_key)
    if found is None:
        raise HTTPException(status_code=404, detail="Sample not found")
    raw, ext = found
    return Response(content=raw, media_type=_MIME_BY_EXT.get(ext, "image/png"), headers=_NO_STORE_HEADERS)


@router.post("/playground/api/watermark/sample", include_in_schema=False)
async def playground_create_sample(
    payload: PlaygroundSampleRequest,
    request: Request,
    owner_hash: str = Security(_require_playground_owner),
):
    await _require_playground_mutation(request, request.headers.get("x-playground-request"))
    api_key, api_url = await playground_db.api_credentials_for_owner(owner_hash)
    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="Phiên hết hạn key flow2api — vui lòng đăng nhập lại.",
            headers=_NO_STORE_HEADERS,
        )
    if owner_hash in _sample_generating:
        raise HTTPException(
            status_code=429,
            detail="Đang tạo ảnh ví dụ — vui lòng đợi lần tạo hiện tại xong.",
            headers=_NO_STORE_HEADERS,
        )
    raw_models = await generation.get_models_for_session(playground_db, owner_hash, refresh=False)
    _sample_generating.add(owner_hash)
    try:
        kwargs = {"raw_models": raw_models} if raw_models is not None else {}
        info = await generation.generate_watermark_sample(
            payload.model,
            payload.ratio,
            payload.res or "1K",
            api_key,
            api_url,
            **kwargs,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=f"Không tạo được ảnh ví dụ: {exc}")
    finally:
        _sample_generating.discard(owner_hash)
    return _response(info)


# ── Video Studio Flow (page riêng port từ tool CINEMAFLOW trên Flow) ─────
#
# Studio dùng lại toàn bộ pipeline generate của playground (job nền, refs,
# watermark) qua project riêng "🎬 Video Studio Flow" — chỉ thêm planner
# kịch bản (LLM qua env STUDIO_LLM_*, fallback engine deterministic) và
# bootstrap project. Session/CSRF dùng chung với playground.

STUDIO_PROJECT_NAME = "🎬 Video Studio Flow"


@router.get("/studio", response_class=HTMLResponse, include_in_schema=False)
async def studio_page():
    return _static_page("studio.html")


@router.get("/static/studio.js", include_in_schema=False)
async def studio_script():
    return _static_page("studio.js", "application/javascript")


@router.get("/static/studio.css", include_in_schema=False)
async def studio_stylesheet():
    return _static_page("studio.css", "text/css")


class StudioPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str = Field(min_length=1, max_length=500)
    scene_count: int = Field(default=8, ge=4, le=25)


class StudioImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # JSON export từ tool gốc trên Flow (hoặc từ studio này) — schema lỏng,
    # studio.import_storyboard tự sanitize về chuẩn.
    payload: dict
    topic: str = Field(default="", max_length=500)


@router.post("/playground/api/studio/plan", include_in_schema=False)
async def studio_plan(
    payload: StudioPlanRequest,
    request: Request,
    owner_hash: str = Security(_require_playground_owner),
):
    """Lên kịch bản N cảnh từ chủ đề — LLM (env STUDIO_LLM_*) hoặc template."""
    await _require_playground_mutation(request, request.headers.get("x-playground-request"))
    result = await studio.generate_storyboard_with_ai(payload.topic, payload.scene_count)
    status_code = 200 if "storyboard" in result else 400
    return _response(result, status_code=status_code)


@router.post("/playground/api/studio/import", include_in_schema=False)
async def studio_import(
    payload: StudioImportRequest,
    request: Request,
    owner_hash: str = Security(_require_playground_owner),
):
    await _require_playground_mutation(request, request.headers.get("x-playground-request"))
    return _response(studio.import_storyboard(payload.payload, payload.topic))


@router.post("/playground/api/studio/workspace", include_in_schema=False)
async def studio_workspace(
    request: Request,
    owner_hash: str = Security(_require_playground_owner),
):
    """Find-or-create project riêng của studio; trả project + flag cấu hình LLM."""
    await _require_playground_mutation(request, request.headers.get("x-playground-request"))
    projects = await playground_db.list_projects(owner_hash)
    for project in projects:
        if project.get("name") == STUDIO_PROJECT_NAME:
            return _response({"project": project, "llm_ready": studio.llm_configured()})
    project = await playground_db.create_project(
        secrets.token_urlsafe(16), owner_hash, STUDIO_PROJECT_NAME, "", []
    )
    return _response({"project": project, "llm_ready": studio.llm_configured()})
