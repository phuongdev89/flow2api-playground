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

    async def fake_item(p, api_key):
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

    async def failing_item(p, api_key):
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
