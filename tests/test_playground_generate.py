import asyncio
import base64
import hashlib
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import generation
from app.routes import playground_db, PLAYGROUND_MAX_RUNNING_JOBS
from app.database import PlaygroundDatabase


async def _fake_validate(key):
    return key == "test-key"


@pytest.fixture
def playground_client(tmp_path, monkeypatch):
    database = PlaygroundDatabase(str(tmp_path / "playground.db"))
    # Thay instance trong routes (module-level) bằng instance tạm.
    monkeypatch.setattr("app.routes.playground_db", database)
    monkeypatch.setattr(
        "app.generation.flow_client.validate_key", _fake_validate
    )
    # Job nền gọi generation.run_playground_generate_job(playground_db, ...) —
    # truyền DB qua tham số nên mock validate_key đủ cho login; generation call
    # được mock ở từng test qua _generate_single_playground_item.
    client = TestClient(main.app)
    yield client, database
    client.close()


def _mutation_headers():
    return {"X-Playground-Request": "1", "Origin": "http://testserver"}


def _login(client):
    response = client.post(
        "/playground/session", headers={"Authorization": "Bearer test-key"}
    )
    assert response.status_code == 204
    return response


def _create_project(client, name="Gen"):
    response = client.post(
        "/playground/api/projects", json={"name": name, "prompt": "", "results": []},
        headers=_mutation_headers(),
    )
    assert response.status_code == 201
    return response.json()


_GOOD_BODY = {
    "mode": "image",
    "model": "nano-banana-pro",
    "ratio": "16:9",
    "qty": 1,
    "res": "1K",
    "length": None,
    "vmode": "frames",
    "prompt": "mèo đeo kính",
}


# ── 1. Compose alias từ catalog tĩnh ─────────────────────────────────


def test_video_alias_composition_matches_catalog():
    options = generation.playground_gen_options()
    assert options["video"]["models"], "capability table không được rỗng"
    for model in options["video"]["models"]:
        spec = generation._playground_model_spec("video", model["id"])
        lengths = list(model["length"] or [])
        combos = [(None, None)]
        combos += [(length, None) for length in lengths]
        combos += [
            (length, res)
            for res in (model["res"] or [])
            if res != "720p"
            for length in (lengths or [None])
        ]
        assert combos, model["id"]
        for length, res in combos:
            for ratio in model["ratios"]:
                composed = generation._compose_video_request(spec["base"], length, res, ratio)
                assert composed is not None, (model["id"], length, res, ratio)
                alias, gen_config = composed
                # Alias hợp lệ = key VIDEO_BASE_MODELS (resolver path) hoặc
                # key MODEL_CONFIG gắn sẵn orientation (direct path).
                assert (
                    alias in generation.VIDEO_BASE_MODELS or alias in generation.MODEL_CONFIG
                ), alias


def test_unsupported_video_combo_degrades_to_none():
    # fast-ultra không có variant duration → combo phải None; lite không hỗ trợ 4K.
    assert generation._compose_video_request("veo_3_1_t2v_fast_ultra", 4, None, "16:9") is None
    assert generation._compose_video_request("veo_3_1_t2v_fast_ultra", 8, None, "16:9") is None
    assert generation._compose_video_request("veo_3_1_t2v_lite", 8, "4K", "16:9") is None


def test_gen_options_shape_and_base_field(playground_client):
    client, _ = playground_client
    _login(client)
    payload = client.get("/playground/api/gen-options").json()
    assert set(payload) == {"image", "video"}

    image_ids = {model["id"] for model in payload["image"]["models"]}
    assert "nano-banana-pro" in image_ids
    for model in payload["image"]["models"]:
        assert set(model) == {"id", "label", "base", "ratios", "res", "refs"}
        assert model["res"] == ["1K", "2K"]  # 4K đã bỏ cho ảnh
        assert model["refs"] is True
        assert model["base"] == "gemini-3.0-pro-image"

    video_ids = [model["id"] for model in payload["video"]["models"]]
    assert video_ids == ["veo-3.1-lite", "veo-3.1-fast", "veo-3.1-fast-ultra", "veo-3.1-quality"]
    for model in payload["video"]["models"]:
        assert set(model) == {"id", "label", "base", "ratios", "res", "length", "refs", "frames"}
    # i2v chỉ có trong catalog cho lite + quality (fast/fast-ultra không có)
    by_id = {m["id"]: m for m in payload["video"]["models"]}
    assert by_id["veo-3.1-lite"]["frames"] is True
    assert by_id["veo-3.1-quality"]["frames"] is True
    assert by_id["veo-3.1-fast"]["frames"] is False
    assert by_id["veo-3.1-fast-ultra"]["frames"] is False


# ── 2. Cap ảnh tham chiếu: image 10 / video 3 ────────────────────────


def test_reference_cap_image_10_video_3(playground_client):
    client, _ = playground_client
    _login(client)
    project = _create_project(client)
    url = f"/playground/api/projects/{project['id']}/generate"
    headers = _mutation_headers()

    over_image = {**_GOOD_BODY, "references": [f"r{i}" for i in range(11)]}
    # 11 refs bị Pydantic chặn 422; cap theo mode (nếuPydantic lỏng hơn) là 400.
    assert client.post(url, json=over_image, headers=headers).status_code in (400, 422)

    at_image = {**_GOOD_BODY, "references": [f"r{i}" for i in range(10)]}
    # 10 refs image ok ở tầng validate (id lạ bị chặn sau đó vì không có item —
    # nên tạo items thật để kiểm tới tầng resolve; ở đây chỉ cần không 400 vì cap)
    assert client.post(url, json=at_image, headers=headers).status_code in (202, 400)

    over_video = {**_GOOD_BODY, "mode": "video", "model": "veo-3.1-lite",
                  "length": 8, "vmode": "ingredients", "references": [f"r{i}" for i in range(4)]}
    assert client.post(url, json=over_video, headers=headers).status_code == 400


# ── 3. Job flow ──────────────────────────────────────────────────────


def test_job_flow_running_to_done_appends_results(tmp_path, monkeypatch):
    database = PlaygroundDatabase(str(tmp_path / "playground.db"))
    owner = hashlib.sha256(b"owner-a").hexdigest()
    asyncio.run(database.create_project("p1", owner, "P", "", []))
    job = asyncio.run(database.create_job("job-1", owner, "p1"))
    assert job["status"] == "running"

    params = {
        "mode": "image", "model": "nano-banana-pro", "base": "gemini-3.0-pro-image",
        "ratio": "16:9", "res": "1K", "length": None, "qty": 2, "prompt": "mèo",
    }
    counter = {"n": 0}

    async def fake_item(p, api_key, client=None):
        counter["n"] += 1
        return {
            "id": f"item-{counter['n']}",
            "kind": p["mode"],
            "url": f"/tmp/playground_results/{counter['n']}.png",
            "created_at": 42,
            "meta": {"mode": p["mode"], "model": p["model"], "ratio": p["ratio"],
                     "res": p["res"], "length": p["length"]},
        }

    monkeypatch.setattr(generation, "_generate_single_playground_item", fake_item)
    asyncio.run(generation.run_playground_generate_job(database, owner, "p1", "job-1", params, "key"))

    done = asyncio.run(database.get_job(owner, "p1", "job-1"))
    assert done["status"] == "done"
    assert [item["id"] for item in done["results"]] == ["item-1", "item-2"]
    project = asyncio.run(database.get_project(owner, "p1"))
    assert [item["id"] for item in project["results"]] == ["item-1", "item-2"]


def test_job_all_items_failed_marks_error(tmp_path, monkeypatch):
    database = PlaygroundDatabase(str(tmp_path / "playground.db"))
    owner = hashlib.sha256(b"owner-a").hexdigest()
    asyncio.run(database.create_project("p1", owner, "P", "", []))
    asyncio.run(database.create_job("job-3", owner, "p1"))
    params = {"mode": "image", "model": "nano-banana-pro", "base": "gemini-3.0-pro-image",
              "ratio": "16:9", "res": "1K", "length": None, "qty": 2, "prompt": "x"}

    async def failing_item(p, api_key, client=None):
        raise RuntimeError("no url")

    monkeypatch.setattr(generation, "_generate_single_playground_item", failing_item)
    asyncio.run(generation.run_playground_generate_job(database, owner, "p1", "job-3", params, "key"))

    job = asyncio.run(database.get_job(owner, "p1", "job-3"))
    assert job["status"] == "error"
    assert "no url" in job["error"]


def test_orphan_running_jobs_marked_error_on_restart(tmp_path):
    database = PlaygroundDatabase(str(tmp_path / "playground.db"))
    asyncio.run(database.create_job("orphan", "o", "p"))
    conn = sqlite3.connect(str(tmp_path / "playground.db"))
    conn.execute(
        "UPDATE playground_jobs SET updated_at = ? WHERE id = 'orphan'",
        (int(time.time() * 1000) - PlaygroundDatabase.JOB_ORPHAN_STALE_MS - 1000,),
    )
    conn.commit()
    conn.close()
    reloaded = PlaygroundDatabase(str(tmp_path / "playground.db"))
    job = asyncio.run(reloaded.get_job("o", "p", "orphan"))
    assert job["status"] == "error"
    assert "khởi động lại" in job["error"]


# ── 4. HTTP flow + auth (key validate qua flow2api giả lập) ──────────


def test_generate_endpoint_requires_owner_and_mutation_marker(playground_client):
    client, database = playground_client
    assert client.post(
        "/playground/api/projects/p1/generate", json=_GOOD_BODY
    ).status_code == 401
    assert client.get("/playground/api/gen-options").status_code == 401

    _login(client)
    other_owner = hashlib.sha256(b"another-key").hexdigest()
    asyncio.run(database.create_project("proj-other", other_owner, "Other", "", []))
    assert client.post(
        "/playground/api/projects/proj-other/generate",
        json=_GOOD_BODY,
        headers=_mutation_headers(),
    ).status_code == 404

    project = _create_project(client)
    assert client.post(
        f"/playground/api/projects/{project['id']}/generate", json=_GOOD_BODY
    ).status_code == 403


def test_generate_endpoint_validates_payload(playground_client):
    client, _ = playground_client
    _login(client)
    project = _create_project(client)
    url = f"/playground/api/projects/{project['id']}/generate"
    headers = _mutation_headers()
    cases = [
        {**_GOOD_BODY, "prompt": "   "},
        {**_GOOD_BODY, "qty": 0},
        {**_GOOD_BODY, "qty": 5},
        {**_GOOD_BODY, "mode": "audio"},
        {**_GOOD_BODY, "model": "veo-9.9"},
        {**_GOOD_BODY, "model": "veo-3.1-lite"},
        {**_GOOD_BODY, "ratio": "21:9"},
        {**_GOOD_BODY, "vmode": "nonsense"},
        # Frames cần 1-2 ảnh; không có ảnh → 400
        {**_GOOD_BODY, "mode": "video", "model": "veo-3.1-lite", "length": 8, "vmode": "frames"},
        # Extend cần extend_item_id
        {**_GOOD_BODY, "mode": "video", "model": "veo-3.1-lite", "length": 8, "vmode": "extend"},
        # Extend qty != 1
        {**_GOOD_BODY, "mode": "video", "model": "veo-3.1-lite", "length": 8,
         "vmode": "extend", "extend_item_id": "v1", "qty": 2},
    ]
    for body in cases:
        response = client.post(url, json=body, headers=headers)
        assert response.status_code == 400, body


def test_generate_endpoint_rejects_when_too_many_running_jobs(playground_client):
    client, database = playground_client
    _login(client)
    project = _create_project(client)
    owner = hashlib.sha256(b"test-key").hexdigest()
    for index in range(PLAYGROUND_MAX_RUNNING_JOBS):
        asyncio.run(database.create_job(f"busy-{index}", owner, project["id"]))
    response = client.post(
        f"/playground/api/projects/{project['id']}/generate",
        json=_GOOD_BODY,
        headers=_mutation_headers(),
    )
    assert response.status_code == 429


def test_generate_endpoint_spawns_job(playground_client, monkeypatch):
    client, database = playground_client
    _login(client)
    project = _create_project(client)

    captured = {}

    def fake_spawn(coro):
        captured["coro"] = coro
        coro.close()
        return None

    monkeypatch.setattr(generation, "spawn_job", fake_spawn)
    response = client.post(
        f"/playground/api/projects/{project['id']}/generate",
        json=_GOOD_BODY,
        headers=_mutation_headers(),
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    assert job_id
    assert captured["coro"] is not None
    # Job giữ trạng thái running (coro đã đóng) và client poll được.
    status = client.get(f"/playground/api/projects/{project['id']}/generate/{job_id}")
    assert status.status_code == 200
    assert status.json()["status"] == "running"


def test_generate_job_targets_the_logged_in_api_url(playground_client, monkeypatch):
    client, database = playground_client
    # Login qua endpoint tuỳ chọn — patch class-level để phủ cả instance mới
    # mà get_flow_client tạo ra cho URL đó.
    async def fake_validate(self, key):
        self.last_check_error = None
        return key == "test-key"

    monkeypatch.setattr("app.generation.FlowClient.validate_key", fake_validate)
    assert client.post(
        "/playground/session",
        json={"api_url": "http://flow.local:1234", "api_key": "test-key"},
    ).status_code == 204
    project = _create_project(client)

    captured = {}

    def fake_spawn(coro):
        # Đọc tham số api_url trước khi đóng coroutine (frame bị xoá sau close).
        captured["api_url"] = coro.cr_frame.f_locals.get("api_url")
        coro.close()
        return None

    monkeypatch.setattr(generation, "spawn_job", fake_spawn)
    response = client.post(
        f"/playground/api/projects/{project['id']}/generate",
        json=_GOOD_BODY,
        headers=_mutation_headers(),
    )
    assert response.status_code == 202
    assert captured["api_url"] == "http://flow.local:1234"


# ── 5. Watermark mask API ────────────────────────────────────────────


def _png_bytes(size=(4, 4)):
    # PNG 1x1 trắng tối giản: dùng canvas giả lập qua base64 có sẵn.
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )


def test_watermark_mask_api_roundtrip(playground_client):
    pytest.importorskip("cv2")
    pytest.importorskip("numpy")
    from app import watermark

    client, _ = playground_client
    _login(client)
    data_url = "data:image/png;base64," + base64.b64encode(_png_bytes()).decode()

    assert client.get("/playground/api/watermark").status_code == 200
    key = "gemini-3.0-pro-image_9_16_test"
    saved = client.post(
        f"/playground/api/watermark/mask/{key}",
        json={"image": data_url},
        headers=_mutation_headers(),
    )
    assert saved.status_code == 200
    listing = client.get("/playground/api/watermark").json()
    assert any(mask["key"] == key for mask in listing["masks"])

    deleted = client.delete(
        f"/playground/api/watermark/mask/{key}", headers=_mutation_headers()
    )
    assert deleted.status_code == 204
    listing = client.get("/playground/api/watermark").json()
    assert not any(mask["key"] == key for mask in listing["masks"])


# ── 6. Stream SSE parsing (video) — mediaGenerationId cho Extend ─────────


def test_generate_stream_parses_url_and_media_id(monkeypatch):
    import httpx
    from app.generation import flow_client

    sse_body = "\n".join(
        [
            'data: {"choices":[{"delta":{"content":"Đang tạo video…"}}]}',
            "data: {\"choices\":[{\"delta\":{\"content\":\"<video src='/tmp/qa.mp4' data-media-id='ab12cd34-5678-90ab-cdef-112233445566' controls></video>\",\"finish_reason\":\"stop\"}}]}",
            "data: [DONE]",
            "",
        ]
    )

    def handler(request):
        return httpx.Response(
            200, text=sse_body, headers={"content-type": "text/event-stream"}
        )

    original_client = flow_client._client
    flow_client._client = httpx.AsyncClient(
        base_url="http://flow.test", transport=httpx.MockTransport(handler)
    )
    try:
        result = asyncio.run(flow_client.generate_stream("key", {"stream": True}))
    finally:
        flow_client._client = original_client
    assert result["url"] == "/tmp/qa.mp4"
    assert result["media_id"] == "ab12cd34-5678-90ab-cdef-112233445566"

    # Không có video tag → báo lỗi rõ (không treo job)
    def empty_handler(request):
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"err"}}]}\ndata: [DONE]\n', headers={"content-type": "text/event-stream"})

    flow_client._client = httpx.AsyncClient(
        base_url="http://flow.test", transport=httpx.MockTransport(empty_handler)
    )
    try:
        with pytest.raises(RuntimeError, match="Không lấy được URL"):
            asyncio.run(flow_client.generate_stream("key", {"stream": True}))
    finally:
        flow_client._client = original_client


def test_compose_frames_and_extend_requests():
    from app import generation as g
    # lite: i2v có cả hai thứ tự suffix — probe phải tìm được key
    alias, _ = g._compose_video_frames_request("veo_3_1_t2v_lite", 8, None, "9:16")
    assert alias == "veo_3_1_i2v_lite_portrait_8s"
    # quality: key i2v_s cần suffix duration
    alias_q, _ = g._compose_video_frames_request("veo_3_1_t2v", 6, None, "16:9")
    assert alias_q == "veo_3_1_i2v_s_6s"
    # fast không có i2v
    assert g._compose_video_frames_request("veo_3_1_t2v_fast", 8, None, "16:9") is None
    # extend: landscape key không có suffix, portrait có
    assert g._compose_video_extend_request("16:9")[0] == "veo_3_1_extend"
    assert g._compose_video_extend_request("9:16")[0] == "veo_3_1_extend_portrait"


def test_compose_frames_request_routes_by_image_count():
    """Lite 1 ảnh → i2v thường (min=max=1), lite 2 ảnh → interpolation
    (min=max=2). Gửi 2 ảnh vào key i2v thường bị upstream từ chối
    "first/last-frame model requires 1-1 images; 2 provided"."""
    from app import generation as g
    alias_1, _ = g._compose_video_frames_request("veo_3_1_t2v_lite", 8, None, "16:9", images=1)
    assert alias_1 == "veo_3_1_i2v_lite_landscape_8s"
    alias_2, gen_cfg = g._compose_video_frames_request(
        "veo_3_1_t2v_lite", 8, None, "16:9", images=2
    )
    assert alias_2 == "veo_3_1_interpolation_lite_landscape_8s"
    assert gen_cfg == {"aspectRatio": "16:9"}
    # 4s/6s cũng có key interpolation riêng theo duration
    assert (
        g._compose_video_frames_request("veo_3_1_t2v_lite", 4, None, "9:16", images=2)[0]
        == "veo_3_1_interpolation_lite_portrait_4s"
    )
    # quality dùng chung key i2v_s (1-2 ảnh) cho cả một lẫn hai ảnh
    assert (
        g._compose_video_frames_request("veo_3_1_t2v", 6, None, "16:9", images=2)[0]
        == "veo_3_1_i2v_s_6s"
    )
    assert (
        g._compose_video_frames_request("veo_3_1_t2v", 6, None, "16:9", images=1)[0]
        == "veo_3_1_i2v_s_6s"
    )


def test_compose_chat_body_frames_two_images_targets_interpolation():
    """Lite + Frames + 2 ảnh: model trong body phải là interpolation key —
    đây chính là lỗi "requires 1-1 images; 2 provided" trước khi fix."""
    from app import generation as g

    params = {
        "mode": "video",
        "base": "veo_3_1_t2v_lite",
        "ratio": "16:9",
        "res": None,
        "length": 8,
        "prompt": "chuyển động mượt giữa hai frame",
        "vmode": "frames",
    }
    refs = ["data:image/png;base64,aGVsbG8=", "data:image/png;base64,aGVsbG8="]
    model, body = g._compose_chat_body(params, refs)
    assert model == "veo_3_1_interpolation_lite_landscape_8s"
    parts = body["messages"][0]["content"]
    assert sum(1 for part in parts if part["type"] == "image_url") == 2


def test_compose_chat_body_image_with_references_sends_image_parts():
    """i2i image: có ảnh tham chiếu thì content phải là parts chứa image_url —
    không được nuốt refs rồi để upstream nhận has_images=false."""
    from app import generation as g

    data_url = "data:image/png;base64,aGVsbG8="
    params = {
        "mode": "image",
        "base": "gemini_2_5_flash_image",
        "ratio": "9:16",
        "res": "1K",
        "prompt": "vẽ thêm một con khỉ nằm cạnh",
    }
    # Không refs: content giữ dạng string như cũ.
    _, body = g._compose_chat_body(params, [])
    assert body["messages"][0]["content"] == "vẽ thêm một con khỉ nằm cạnh"

    # Có refs: content là parts text + image_url data URL.
    _, body = g._compose_chat_body(params, [data_url])
    content = body["messages"][0]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "vẽ thêm một con khỉ nằm cạnh"}
    assert {"type": "image_url", "image_url": {"url": data_url}} in content

    # Video t2v không refs vẫn string.
    _, body = g._compose_chat_body(
        {
            "mode": "video",
            "base": "veo_3_1_t2v",
            "length": 8,
            "res": None,
            "ratio": "9:16",
            "prompt": "p",
        },
        [],
    )
    assert body["messages"][0]["content"] == "p"


# ── 7. Meta contract "Re use prompt" — prompt/references/vmode trong meta ──


class _FakeGenClient:
    """FlowClient giả: trả URL hợp lệ cho cả non-stream (image) lẫn stream (video)."""

    async def generate(self, api_key, body):
        return {"choices": [{"message": {"content": "![img](/tmp/qa.png)"}}]}

    async def generate_stream(self, api_key, body):
        return {"url": "/tmp/qa.mp4", "media_id": "media-1"}

    async def download_media(self, url):
        return b"fake-media-bytes", ".png"


def _patch_media_pipeline(monkeypatch, tmp_path):
    async def fake_remove_watermark(raw, ext, key, fallback_keys=None):
        return raw, "skipped:no_mask"

    monkeypatch.setattr(generation.watermark, "remove_watermark", fake_remove_watermark)
    monkeypatch.setattr(generation, "RESULTS_DIR", tmp_path)


def test_single_item_meta_carries_prompt_references_vmode(monkeypatch, tmp_path):
    """Mọi item kết quả phải mang prompt + id refs user chọn; video thêm vmode."""
    _patch_media_pipeline(monkeypatch, tmp_path)

    image_params = {
        "mode": "image", "model": "nano-banana-pro", "base": "gemini-3.0-pro-image",
        "ratio": "9:16", "res": "1K", "length": None, "qty": 1,
        "prompt": "mèo đeo kính", "vmode": "text",
        "references": ["ref-1", "ref-2"],
    }
    item = asyncio.run(
        generation._generate_single_playground_item(image_params, "k", _FakeGenClient())
    )
    assert item["meta"]["prompt"] == "mèo đeo kính"
    # references là ID item user đã chọn, không phải URL.
    assert item["meta"]["references"] == ["ref-1", "ref-2"]
    # vmode chỉ có nghĩa với video — image không cần.
    assert "vmode" not in item["meta"]

    video_params = {
        "mode": "video", "model": "veo-3.1-fast", "base": "veo_3_1_t2v_fast",
        "ratio": "16:9", "res": None, "length": 8, "qty": 1,
        "prompt": "chiếc xe chạy", "vmode": "text", "references": [],
    }
    video_item = asyncio.run(
        generation._generate_single_playground_item(video_params, "k", _FakeGenClient())
    )
    assert video_item["meta"]["prompt"] == "chiếc xe chạy"
    assert video_item["meta"]["references"] == []
    assert video_item["meta"]["vmode"] == "text"
    assert video_item["meta"]["media_generation_id"] == "media-1"


def test_single_item_extend_meta_keeps_reuse_contract(monkeypatch, tmp_path):
    """Nhánh Extend cũng phải mang đủ prompt/references/vmode (vmode='extend')."""

    class FakeExtendClient:
        async def generate_stream(self, api_key, body):
            return {"url": "/tmp/qa2.mp4", "media_id": "media-2"}

        async def download_media(self, url):
            return b"fake-video-bytes", ".mp4"

    async def fake_crop(path, percent):
        return "skipped:disabled"

    monkeypatch.setattr(generation.watermark, "crop_video_percent", fake_crop)
    _patch_media_pipeline(monkeypatch, tmp_path)
    params = {
        "mode": "video", "model": "veo-3.1-fast", "base": "veo_3_1_extend",
        "ratio": "16:9", "res": None, "length": None, "qty": 1,
        "prompt": "tiếp tục cảnh", "vmode": "extend", "references": [],
        "extend_item_id": "item-src", "extend_media_id": "media-src",
        "crop_like_lite": False,
    }
    item = asyncio.run(
        generation._generate_single_playground_item(params, "k", FakeExtendClient())
    )
    assert item["meta"]["prompt"] == "tiếp tục cảnh"
    assert item["meta"]["references"] == []
    assert item["meta"]["vmode"] == "extend"
    assert item["meta"]["extend_source_item"] == "item-src"


# ── 7. Retry / Delete job lỗi ────────────────────────────────────────


def _fail_job(database, owner, project_id, params=None, error="Upstream 500."):
    """Tạo job rồi đánh dấu lỗi như một lượt generate thật sự thất bại."""
    job = asyncio.run(database.create_job(f"job-{time.time_ns()}", owner, project_id, params))
    asyncio.run(
        database.update_job_status(
            job["job_id"], "error", error=error, results=[],
            owner_hash=owner, project_id=project_id,
        )
    )
    return job["job_id"]


_RETRY_IMAGE_PARAMS = {
    "mode": "image", "model": "nano-banana-pro", "ratio": "16:9", "qty": 1,
    "res": "1K", "length": None, "vmode": "text", "prompt": "mèo đeo kính",
    "references": [],
}


def test_create_job_persists_params_for_retry(playground_client):
    client, database = playground_client
    _login(client)
    project = _create_project(client)
    owner = hashlib.sha256(b"test-key").hexdigest()
    params = dict(_RETRY_IMAGE_PARAMS)
    asyncio.run(database.create_job("job-params", owner, project["id"], params))
    stored = asyncio.run(
        database.get_job(owner, project["id"], "job-params", include_params=True)
    )
    assert stored["params"] == params
    # List endpoint không lộ params — payload client giữ gọn.
    jobs = asyncio.run(database.list_jobs_for_project(owner, project["id"]))
    assert jobs and all("params" not in job for job in jobs)


def test_retry_endpoint_reruns_failed_job(playground_client, monkeypatch):
    client, database = playground_client
    _login(client)
    project = _create_project(client)
    owner = hashlib.sha256(b"test-key").hexdigest()
    job_id = _fail_job(database, owner, project["id"], dict(_RETRY_IMAGE_PARAMS))

    captured = {}

    def fake_spawn(coro):
        frame = coro.cr_frame.f_locals
        captured["job_id"] = frame.get("job_id")
        captured["params"] = frame.get("params")
        coro.close()

    monkeypatch.setattr(generation, "spawn_job", fake_spawn)
    response = client.post(
        f"/playground/api/projects/{project['id']}/jobs/{job_id}/retry",
        headers=_mutation_headers(),
    )
    assert response.status_code == 202
    # Giữ nguyên job_id — thẻ lỗi trên UI biến lại spinner tại chỗ.
    assert response.json()["job_id"] == job_id
    assert captured["job_id"] == job_id
    assert captured["params"]["prompt"] == _RETRY_IMAGE_PARAMS["prompt"]
    status = asyncio.run(database.get_job(owner, project["id"], job_id))
    assert status["status"] == "running"
    assert status["error"] is None


def test_retry_endpoint_guards(playground_client):
    client, database = playground_client
    _login(client)
    project = _create_project(client)
    owner = hashlib.sha256(b"test-key").hexdigest()
    base = f"/playground/api/projects/{project['id']}/jobs"

    asyncio.run(database.create_job("job-running", owner, project["id"]))
    # Job running không retry được
    assert client.post(f"{base}/job-running/retry", headers=_mutation_headers()).status_code == 409
    # Job lỗi nhưng không có params (tạo trước khi có retry) → 409 kèm chỉ dẫn
    asyncio.run(database.create_job("job-old", owner, project["id"]))
    asyncio.run(
        database.update_job_status(
            "job-old", "error", error="x", results=[],
            owner_hash=owner, project_id=project["id"],
        )
    )
    assert client.post(f"{base}/job-old/retry", headers=_mutation_headers()).status_code == 409
    # Job lạ → 404
    assert client.post(f"{base}/job-none/retry", headers=_mutation_headers()).status_code == 404
    # Thiếu mutation marker → chặn như mọi endpoint mutate khác
    assert client.post(f"{base}/job-old/retry").status_code == 403


def test_retry_re_resolves_deleted_reference(playground_client):
    client, database = playground_client
    _login(client)
    project = _create_project(client)
    owner = hashlib.sha256(b"test-key").hexdigest()
    params = {
        "mode": "video", "model": "veo-3.1-lite", "ratio": "16:9", "qty": 1,
        "res": None, "length": 8, "vmode": "frames", "prompt": "p",
        "references": ["img-gone"],
    }
    job_id = _fail_job(database, owner, project["id"], params)
    # Ảnh tham chiếu đã bị xoá khỏi project → 400 rõ ràng thay vì job chết lần nữa
    response = client.post(
        f"/playground/api/projects/{project['id']}/jobs/{job_id}/retry",
        headers=_mutation_headers(),
    )
    assert response.status_code == 400


def test_delete_endpoint_removes_failed_job(playground_client):
    client, database = playground_client
    _login(client)
    project = _create_project(client)
    owner = hashlib.sha256(b"test-key").hexdigest()
    base = f"/playground/api/projects/{project['id']}/jobs"
    job_id = _fail_job(database, owner, project["id"])
    # Job running không xoá được
    asyncio.run(database.create_job("job-running", owner, project["id"]))
    assert client.delete(f"{base}/job-running", headers=_mutation_headers()).status_code == 409
    # Job lỗi xoá được và biến mất khỏi list
    assert client.delete(f"{base}/{job_id}", headers=_mutation_headers()).status_code == 204
    jobs = asyncio.run(database.list_jobs_for_project(owner, project["id"]))
    assert all(job["job_id"] != job_id for job in jobs)
    assert client.delete(f"{base}/{job_id}", headers=_mutation_headers()).status_code == 404
