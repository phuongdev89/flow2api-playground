import asyncio

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import generation, studio
from app.database import PlaygroundDatabase


async def _fake_validate(key):
    return key == "test-key"


async def _fake_fetch_models(key):
    return None


@pytest.fixture
def playground_client(tmp_path, monkeypatch):
    database = PlaygroundDatabase(str(tmp_path / "playground.db"))
    monkeypatch.setattr("app.routes.playground_db", database)
    monkeypatch.setattr("app.generation.flow_client.validate_key", _fake_validate)
    monkeypatch.setattr("app.generation.flow_client.fetch_models", _fake_fetch_models)
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


# ── Deterministic engine (port storyboardEngine.ts) ────────────────────────


def test_engine_matches_kitchen_template_by_keyword():
    result = studio.generate_deterministic_storyboard("Golden artisan pancakes", 8)
    assert result is not None
    assert result["category"] == "kitchen"
    assert len(result["scenes"]) == 8
    assert result["scenes"][0]["role"] == "hook"
    assert result["scenes"][-1]["role"] == "reward"


def test_engine_universal_fallback_for_unknown_topic():
    result = studio.generate_deterministic_storyboard("hòn đá phát sáng", 6)
    assert result is not None
    assert len(result["scenes"]) == 6
    assert all(scene["startPrompt"] and scene["videoPrompt"] for scene in result["scenes"])


def test_engine_empty_topic_returns_none():
    assert studio.generate_deterministic_storyboard("   ", 8) is None


def test_engine_scene_count_clamped_to_range():
    assert len(studio.generate_deterministic_storyboard("pancake", 2)["scenes"]) == 4
    assert len(studio.generate_deterministic_storyboard("pancake", 99)["scenes"]) == 25


def test_engine_renumbering_keeps_sequential_ids():
    result = studio.generate_deterministic_storyboard("trà đọc sách mưa", 10)
    ids = [scene["id"] for scene in result["scenes"]]
    assert ids == [f"S{i + 1:02d}" for i in range(10)]
    assert "Cảnh bổ trợ" not in " ".join(scene["nameVi"] for scene in result["scenes"])


def test_planner_entry_uses_template_when_llm_not_configured(monkeypatch):
    monkeypatch.setattr(studio, "STUDIO_LLM_MODEL", "")
    monkeypatch.setattr(studio, "STUDIO_LLM_API_KEY", "")
    result = asyncio.run(studio.generate_storyboard_with_ai("bánh mì nướng", 6))
    assert result["source"] == "template"
    assert len(result["storyboard"]["scenes"]) == 6


def test_planner_entry_rejects_blank_topic():
    result = asyncio.run(studio.generate_storyboard_with_ai("  ", 8))
    assert "error" in result


def test_planner_falls_back_to_template_when_llm_fails(monkeypatch):
    monkeypatch.setattr(studio, "STUDIO_LLM_MODEL", "text-model")
    monkeypatch.setattr(studio, "STUDIO_LLM_API_KEY", "k")

    class _Boom:
        async def __aenter__(self):
            raise httpx_connect_error()

        async def __aexit__(self, *args):
            return False

    def httpx_connect_error():
        import httpx

        return httpx.ConnectError("boom")

    monkeypatch.setattr(studio.httpx, "AsyncClient", lambda *a, **kw: _Boom())
    result = asyncio.run(studio.generate_storyboard_with_ai("pancake", 5))
    assert result["source"] == "template"


def test_sanitize_storyboard_normalizes_llm_payload():
    raw = {
        "category": "creative",
        "scenes": [
            {
                "nameVi": "A",
                "role": "không-hợp-lệ",
                "durationSeconds": "17s",
                "startPrompt": "p1",
            }
        ]
        * 5,
    }
    cleaned = studio.sanitize_storyboard(raw, "topic", 8)
    assert cleaned is not None
    assert [scene["id"] for scene in cleaned["scenes"]] == [f"S{i + 1:02d}" for i in range(5)]
    assert cleaned["scenes"][0]["role"] == "hook"
    # 17s vượt SUPPORTED_DURATIONS → clamp về 15
    assert cleaned["scenes"][0]["durationSeconds"] == 15


def test_import_storyboard_validates_schema():
    ok = studio.import_storyboard(
        {"topic": "t", "scenes": [{"nameVi": "x"}] * 4}, "hint"
    )
    assert ok["source"] == "import"
    assert len(ok["storyboard"]["scenes"]) == 4
    assert studio.import_storyboard({"foo": 1})["error"]


# ── Vá frames lite 2 ảnh (dynamic resolver) ────────────────────────────────

_DYNAMIC_MODELS = [
    {"id": "veo-3.1-lite", "object": "model", "description": "Veo 3.1 Lite · 8s · landscape"},
    {"id": "veo-3.1-lite-4s-portrait", "object": "model", "description": "Veo 3.1 Lite · 4s · portrait"},
    {"id": "veo-3.1-lite-6s-portrait", "object": "model", "description": "Veo 3.1 Lite · 6s · portrait"},
    {"id": "veo-3.1-lite-8s-portrait", "object": "model", "description": "Veo 3.1 Lite · 8s · portrait"},
    {"id": "veo-3.1-lite-i2v-4s-portrait", "object": "model", "description": "Veo 3.1 Lite I2V · 4s · portrait"},
    {"id": "veo-3.1-lite-i2v-6s-portrait", "object": "model", "description": "Veo 3.1 Lite I2V · 6s · portrait"},
    {
        "id": "omni-1.2-flash",
        "object": "model",
        "description": "Omni 1.2 Flash · 8s · landscape",
    },
] + [
    {"id": f"omni-1.2-flash-{dur}s-portrait", "object": "model", "description": f"Omni 1.2 Flash · {dur}s · portrait"}
    for dur in (3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15)
]


def test_dynamic_lite_frames_two_images_routes_to_interpolation():
    payload = {
        "mode": "video", "model": "veo-3.1-lite", "vmode": "frames",
        "ratio": "9:16", "res": None, "length": 8, "qty": 1,
        "prompt": "x", "references": ["a", "b"],
    }
    params = generation.validate_generate_payload(payload, raw_models=_DYNAMIC_MODELS)
    assert params["resolved_model"] == "veo_3_1_interpolation_lite_portrait_8s"


def test_dynamic_lite_frames_one_image_keeps_i2v_key():
    payload = {
        "mode": "video", "model": "veo-3.1-lite", "vmode": "frames",
        "ratio": "9:16", "res": None, "length": 4, "qty": 1,
        "prompt": "x", "references": ["a"],
    }
    params = generation.validate_generate_payload(payload, raw_models=_DYNAMIC_MODELS)
    assert params["resolved_model"] == "veo-3.1-lite-i2v-4s-portrait"


def test_dynamic_omni_frames_maps_to_duration_variant():
    payload = {
        "mode": "video", "model": "omni-1.2-flash", "vmode": "frames",
        "ratio": "9:16", "res": None, "length": 12, "qty": 1,
        "prompt": "x", "references": ["a", "b"],
    }
    params = generation.validate_generate_payload(payload, raw_models=_DYNAMIC_MODELS)
    assert params["resolved_model"] == "omni-1.2-flash-12s-portrait"


def test_dynamic_omni_appears_in_options_with_frames():
    options, _meta = generation._build_dynamic_gen_options(_DYNAMIC_MODELS)
    omni = next(m for m in options["video"]["models"] if m["id"] == "omni-1.2-flash")
    assert omni["frames"] is True
    assert omni["length"] == [3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
    # omni-1.1 vẫn bị loại khỏi dropdown (discontinued)
    assert all(m["id"] != "omni-1.1-flash" for m in options["video"]["models"])


# ── Endpoints ──────────────────────────────────────────────────────────────


def test_studio_pages_served():
    client = TestClient(main.app)
    assert client.get("/studio").status_code == 200
    assert client.get("/static/studio.js").status_code == 200
    assert client.get("/static/studio.css").status_code == 200
    client.close()


def test_studio_endpoints_require_session():
    client = TestClient(main.app)
    assert client.post(
        "/playground/api/studio/plan",
        json={"topic": "x", "scene_count": 8},
        headers=_mutation_headers(),
    ).status_code == 401
    assert client.post(
        "/playground/api/studio/workspace", headers=_mutation_headers()
    ).status_code == 401
    client.close()


def test_studio_workspace_find_or_creates_project(playground_client):
    client, _db = playground_client
    _login(client)
    first = client.post(
        "/playground/api/studio/workspace", headers=_mutation_headers()
    )
    assert first.status_code == 200
    project = first.json()["project"]
    assert project["name"].endswith("Video Studio Flow")
    again = client.post("/playground/api/studio/workspace", headers=_mutation_headers())
    assert again.json()["project"]["id"] == project["id"]


def test_studio_plan_endpoint_template_flow(playground_client):
    client, _db = playground_client
    _login(client)
    response = client.post(
        "/playground/api/studio/plan",
        json={"topic": "pancake mật ong", "scene_count": 5},
        headers=_mutation_headers(),
    )
    assert response.status_code == 200
    data = response.json()
    assert data["source"] == "template"
    assert len(data["storyboard"]["scenes"]) == 5


def test_studio_plan_validates_payload(playground_client):
    client, _db = playground_client
    _login(client)
    assert client.post(
        "/playground/api/studio/plan",
        json={"topic": "", "scene_count": 8},
        headers=_mutation_headers(),
    ).status_code == 422
    assert client.post(
        "/playground/api/studio/plan",
        json={"topic": "x", "scene_count": 99},
        headers=_mutation_headers(),
    ).status_code == 422


def test_studio_import_endpoint(playground_client):
    client, _db = playground_client
    _login(client)
    ok = client.post(
        "/playground/api/studio/import",
        json={"payload": {"topic": "t", "scenes": [{"nameVi": "a"}] * 6}},
        headers=_mutation_headers(),
    )
    assert ok.status_code == 200
    assert ok.json()["storyboard"]["topic"] == "t"
    bad = client.post(
        "/playground/api/studio/import",
        json={"payload": {"nope": True}},
        headers=_mutation_headers(),
    )
    assert bad.status_code == 200
    assert "error" in bad.json()


def test_studio_generate_via_project_pipeline(playground_client, monkeypatch):
    """Kịch bản end-to-end: plan → generate START frame qua pipeline playground."""
    client, db = playground_client
    _login(client)
    workspace = client.post(
        "/playground/api/studio/workspace", headers=_mutation_headers()
    ).json()
    project_id = workspace["project"]["id"]

    plan = client.post(
        "/playground/api/studio/plan",
        json={"topic": "pancake", "scene_count": 4},
        headers=_mutation_headers(),
    ).json()["storyboard"]

    async def fake_item(params, api_key, client=None):
        return {
            "id": "item-1",
            "kind": "image",
            "url": "/tmp/playground_results/fake.jpg",
            "created_at": 0,
            "meta": {},
        }

    monkeypatch.setattr(generation, "_generate_single_playground_item", fake_item)

    captured = {}

    def fake_spawn(coro):
        # TestClient không chạy event loop nền sau response — giữ coroutine lại
        # chạy trực tiếp bằng asyncio.run (pattern của test_playground_generate).
        captured["coro"] = coro
        return None

    monkeypatch.setattr(generation, "spawn_job", fake_spawn)
    response = client.post(
        f"/playground/api/projects/{project_id}/generate",
        json={
            "mode": "image",
            "model": "nano-banana-pro",
            "ratio": "9:16",
            "qty": 1,
            "res": "1K",
            "prompt": plan["scenes"][0]["startPrompt"],
            "references": [],
        },
        headers=_mutation_headers(),
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    asyncio.run(captured["coro"])

    status = client.get(f"/playground/api/projects/{project_id}/generate/{job_id}")
    assert status.json()["status"] == "done"
    assert status.json()["results"][0]["kind"] == "image"
