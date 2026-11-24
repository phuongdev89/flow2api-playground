"""Tests cho lấy danh sách model thực theo (api_url, api_key) từ GET /v1/models."""

import asyncio
import hashlib
import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import generation
from app.database import PlaygroundDatabase


def _mutation_headers():
    return {"X-Playground-Request": "1", "Origin": "http://testserver"}


def test_login_fetches_models_per_api_key_and_gen_options_reflects_them(
    tmp_path, monkeypatch
):
    database = PlaygroundDatabase(str(tmp_path / "playground.db"))
    monkeypatch.setattr("app.routes.playground_db", database)
    monkeypatch.setattr("app.generation._flow_clients", {})

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        auth = request.headers.get("authorization")
        if auth == "Bearer key-flash":
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {
                            "id": "gemini-3.1-flash-image-landscape-2k",
                            "object": "model",
                            "description": "Nano Banana 2.1",
                        },
                        {
                            "id": "gemini-3.1-flash-image-portrait-2k",
                            "object": "model",
                            "description": "Nano Banana 2.1",
                        },
                        {
                            "id": "gemini-3.1-flash-lite-image",
                            "object": "model",
                            "description": "Nano Banana 2 Lite",
                        },
                        {"id": "veo-3.1-lite-4s-landscape", "object": "model"},
                        {"id": "veo-3.1-lite-6s-portrait", "object": "model"},
                        {"id": "veo-3.1-lite-i2v-4s-landscape", "object": "model"},
                        {"id": "veo_3_1_t2v_fast", "object": "model"},
                        {"id": "veo-3.1-quality", "object": "model"},
                        {"id": "omni-1.1-flash-8s-landscape", "object": "model"},
                        {"id": "veo-3.1-lite-extend-8s-landscape", "object": "model"},
                        {"id": "gpt-4o", "object": "model"},
                    ],
                },
            )
        if auth == "Bearer key-omni":
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        *[
                            {
                                "id": f"omni-1.2-flash-{sec}s-{orient}",
                                "object": "model",
                                "description": f"Omni 1.2 Flash · {sec}s · {orient}",
                            }
                            for sec in range(3, 16)
                            for orient in ("landscape", "portrait")
                        ],
                        {
                            "id": "omni-1.2-flash",
                            "object": "model",
                            "description": "Omni 1.2 Flash · 8s · landscape",
                        },
                        {"id": "veo-3.1-lite", "object": "model"},
                    ],
                },
            )
        return httpx.Response(401, json={"error": "bad key"})

    orig_init = generation.FlowClient.__init__

    def fake_init(self, base_url=generation.FLOW2API_BASE_URL):
        orig_init(self, base_url)
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(generation.FlowClient, "__init__", fake_init)

    # Key A: Nano Banana 2.1 (2K, 16:9+9:16), Nano Banana 2 Lite, Veo 3.1 - Lite;
    # bỏ qua model discontinue (veo-3.1-fast/quality, omni-1.1-flash), extend, gpt-4o.
    with TestClient(main.app) as client_a:
        res_login = client_a.post(
            "/playground/session",
            json={"api_url": "http://flow-a.test", "api_key": "key-flash"},
        )
        assert res_login.status_code == 204
        opts = client_a.get("/playground/api/gen-options").json()

        img_models = opts["image"]["models"]
        assert [m["id"] for m in img_models] == [
            "gemini-3.1-flash-image",
            "gemini-3.1-flash-lite-image",
        ]
        for m in img_models:
            assert set(m) == {"id", "label", "base", "ratios", "res", "refs"}
        assert img_models[0]["label"] == "Nano Banana 2.1"
        assert img_models[0]["base"] == "gemini-3.1-flash-image"
        assert img_models[0]["ratios"] == ["16:9", "9:16"]
        assert img_models[0]["res"] == ["2K"]
        assert img_models[1]["label"] == "Nano Banana 2 Lite"
        assert img_models[1]["res"] == ["1K", "2K"]

        vid_models = opts["video"]["models"]
        assert [m["id"] for m in vid_models] == ["veo-3.1-lite"]
        assert vid_models[0]["length"] == [4, 6]
        assert vid_models[0]["frames"] is True
        assert set(vid_models[0]) == {
            "id", "label", "base", "ratios", "res", "length", "refs", "frames",
        }

        # Model không có trong key-flash hoặc đã discontinue bị chặn 400 khi generate
        proj = client_a.post(
            "/playground/api/projects",
            json={"name": "P", "prompt": "", "results": []},
            headers=_mutation_headers(),
        ).json()
        for missing_id in ("nano-banana-pro", "veo-3.1-fast"):
            bad = client_a.post(
                f"/playground/api/projects/{proj['id']}/generate",
                json={
                    "mode": "image" if missing_id == "nano-banana-pro" else "video",
                    "model": missing_id,
                    "ratio": "16:9",
                    "qty": 1,
                    "prompt": "test",
                },
                headers=_mutation_headers(),
            )
            assert bad.status_code == 400

    # Key B: 0 image models, video có Omni 1.2 Flash (3..15s) và Veo 3.1 - Lite (4-6-8s)
    with TestClient(main.app) as client_b:
        res_login_b = client_b.post(
            "/playground/session",
            json={"api_url": "http://flow-b.test", "api_key": "key-omni"},
        )
        assert res_login_b.status_code == 204
        opts_b = client_b.get("/playground/api/gen-options").json()
        assert opts_b["image"]["models"] == []
        assert [m["id"] for m in opts_b["video"]["models"]] == [
            "omni-1.2-flash",
            "veo-3.1-lite",
        ]
        assert opts_b["video"]["models"][0]["label"] == "Omni 1.2 Flash"
        assert opts_b["video"]["models"][0]["length"] == list(range(3, 16))
        assert opts_b["video"]["models"][1]["length"] == [4, 6, 8]


def test_dynamic_models_validate_and_compose_exact_upstream_ids(tmp_path, monkeypatch):
    database = PlaygroundDatabase(str(tmp_path / "playground.db"))
    monkeypatch.setattr("app.routes.playground_db", database)
    monkeypatch.setattr("app.generation._flow_clients", {})

    raw_models = [
        {"id": "gemini-3.1-flash-image-portrait-2k", "description": "Nano Banana 2.1"},
        {"id": "gemini-3.1-flash-image-landscape-2k", "description": "Nano Banana 2.1"},
        {"id": "veo-3.1-lite-4s-landscape", "description": "Veo 3.1 - Lite · 4s · landscape"},
        {"id": "veo-3.1-lite-6s-portrait", "description": "Veo 3.1 - Lite · 6s · portrait"},
        {"id": "veo-3.1-lite-8s-landscape", "description": "Veo 3.1 - Lite · 8s · landscape"},
        {"id": "veo-3.1-lite-i2v-4s-landscape", "description": "Veo 3.1 - Lite I2V · 4s · landscape"},
        {"id": "veo-3.1-lite-i2v-6s-portrait", "description": "Veo 3.1 - Lite I2V · 6s · portrait"},
        {"id": "veo-3.1-lite-r2v-8s-portrait", "description": "Veo 3.1 - Lite R2V · 8s · portrait"},
        {"id": "veo-3.1-lite-extend-8s-landscape", "description": "Veo 3.1 - Lite Extend · landscape"},
        {"id": "veo-3.1-lite-extend-8s-portrait", "description": "Veo 3.1 - Lite Extend · portrait"},
        {"id": "veo-3.1-lite", "description": "Veo 3.1 - Lite · 8s · landscape"},
        {"id": "omni-1.2-flash-3s-landscape", "description": "Omni 1.2 Flash · 3s · landscape"},
        {"id": "omni-1.2-flash-13s-portrait", "description": "Omni 1.2 Flash · 13s · portrait"},
        {"id": "omni-1.2-flash-15s-landscape", "description": "Omni 1.2 Flash · 15s · landscape"},
        {"id": "omni-1.2-flash", "description": "Omni 1.2 Flash · 8s · landscape"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"object": "list", "data": raw_models})

    orig_init = generation.FlowClient.__init__

    def fake_init(self, base_url=generation.FLOW2API_BASE_URL):
        orig_init(self, base_url)
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(generation.FlowClient, "__init__", fake_init)

    with TestClient(main.app) as client:
        assert (
            client.post(
                "/playground/session",
                json={"api_url": "http://flow.test", "api_key": "k1"},
            ).status_code
            == 204
        )
        proj = client.post(
            "/playground/api/projects",
            json={"name": "Dyn", "prompt": "", "results": []},
            headers=_mutation_headers(),
        ).json()
        # Tạo ảnh tham chiếu & video nguồn trong project
        import base64
        tiny_png = "data:image/png;base64," + base64.b64encode(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc`\x00\x00\x00\x02\x00\x01\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82"
        ).decode()
        proj_ref = client.post(
            f"/playground/api/projects/{proj['id']}/items",
            json={"name": "RefImg", "image": tiny_png},
            headers=_mutation_headers(),
        ).json()
        ref_id = proj_ref["results"][0]["id"]
        owner = hashlib.sha256(b"k1").hexdigest()
        import asyncio as _asyncio
        _asyncio.run(
            database.append_results(
                owner,
                proj["id"],
                [{"id": "vid-src", "kind": "video", "url": "/tmp/playground_results/v.mp4", "meta": {"media_generation_id": "mgen-1", "model": "veo-3.1-lite"}}],
            )
        )

        captured = []

        def fake_spawn(coro):
            captured.append(dict(coro.cr_frame.f_locals["params"]))
            coro.close()

        monkeypatch.setattr(generation, "spawn_job", fake_spawn)

        # Image variant từ /v1/models → compose dùng đúng variant ID thực của endpoint
        res_img = client.post(
            f"/playground/api/projects/{proj['id']}/generate",
            json={
                "mode": "image",
                "model": "gemini-3.1-flash-image",
                "ratio": "9:16",
                "qty": 1,
                "res": "2K",
                "prompt": "cô gái",
            },
            headers=_mutation_headers(),
        )
        assert res_img.status_code == 202
        _req_model, body = generation._compose_chat_body(captured[-1], [])
        assert body["model"] == "gemini-3.1-flash-image-portrait-2k"
        assert body["generationConfig"] == {"aspectRatio": "9:16", "imageSize": "2k"}
        _asyncio.run(
            database.update_job_status(
                res_img.json()["job_id"], "done", results=[], owner_hash=owner, project_id=proj["id"]
            )
        )

        # Veo 3.1 - Lite: T2V 4s, Frames(I2V) 8s fallback 6s, Ingredients(R2V) 6s->8s, Extend portrait
        for payload_in, expected_model in [
            ({"mode": "video", "model": "veo-3.1-lite", "ratio": "16:9", "length": 4, "qty": 1, "vmode": "text", "prompt": "a"}, "veo-3.1-lite-4s-landscape"),
            ({"mode": "video", "model": "veo-3.1-lite", "ratio": "9:16", "length": 8, "qty": 1, "vmode": "frames", "references": [ref_id], "prompt": "a"}, "veo-3.1-lite-i2v-6s-portrait"),
            ({"mode": "video", "model": "veo-3.1-lite", "ratio": "9:16", "length": 6, "qty": 1, "vmode": "ingredients", "references": [ref_id], "prompt": "a"}, "veo-3.1-lite-r2v-8s-portrait"),
            ({"mode": "video", "model": "veo-3.1-lite", "ratio": "9:16", "qty": 1, "vmode": "extend", "extend_item_id": "vid-src", "prompt": "tiếp"}, "veo-3.1-lite-extend-8s-portrait"),
            ({"mode": "video", "model": "omni-1.2-flash", "ratio": "9:16", "length": 13, "qty": 1, "vmode": "text", "prompt": "xe chạy"}, "omni-1.2-flash-13s-portrait"),
            ({"mode": "video", "model": "omni-1.2-flash", "ratio": "9:16", "length": 13, "qty": 1, "vmode": "frames", "references": [ref_id], "prompt": "xe chạy"}, "omni-1.2-flash-13s-portrait"),
            ({"mode": "video", "model": "omni-1.2-flash", "ratio": "16:9", "length": 15, "qty": 1, "vmode": "text", "prompt": "biển"}, "omni-1.2-flash-15s-landscape"),
        ]:
            rv = client.post(
                f"/playground/api/projects/{proj['id']}/generate",
                json=payload_in,
                headers=_mutation_headers(),
            )
            assert rv.status_code == 202, (payload_in, rv.text)
            _m, vbody = generation._compose_chat_body(captured[-1], [])
            assert vbody["model"] == expected_model
            assert vbody["stream"] is True
            _asyncio.run(
                database.update_job_status(
                    rv.json()["job_id"], "done", results=[], owner_hash=owner, project_id=proj["id"]
                )
            )

        # Retry job dùng model động vẫn pass validate & giữ resolved_model
        owner = hashlib.sha256(b"k1").hexdigest()
        job_id = res_img.json()["job_id"]
        asyncio.run(
            database.update_job_status(
                job_id, "error", error="boom", results=[],
                owner_hash=owner, project_id=proj["id"],
            )
        )
        res_retry = client.post(
            f"/playground/api/projects/{proj['id']}/jobs/{job_id}/retry",
            headers=_mutation_headers(),
        )
        assert res_retry.status_code == 202
        assert captured[-1]["resolved_model"] == "gemini-3.1-flash-image-portrait-2k"


def test_gen_options_refreshes_after_ttl_and_falls_back_to_stored_models(
    tmp_path, monkeypatch
):
    database = PlaygroundDatabase(str(tmp_path / "playground.db"))
    monkeypatch.setattr("app.routes.playground_db", database)
    monkeypatch.setattr("app.generation._flow_clients", {})

    state = {"models": [{"id": "gemini-3.0-pro-image"}], "fail": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["fail"]:
            raise httpx.ConnectError("offline")
        return httpx.Response(200, json={"object": "list", "data": state["models"]})

    orig_init = generation.FlowClient.__init__

    def fake_init(self, base_url=generation.FLOW2API_BASE_URL):
        orig_init(self, base_url)
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(generation.FlowClient, "__init__", fake_init)

    with TestClient(main.app) as client:
        assert (
            client.post(
                "/playground/session",
                json={"api_url": "http://flow.test", "api_key": "k2"},
            ).status_code
            == 204
        )
        first = client.get("/playground/api/gen-options").json()
        assert [m["id"] for m in first["image"]["models"]] == ["gemini-3.0-pro-image"]

        # Hết cache TTL + upstream cập nhật thêm model mới cho API key → lấy danh sách mới
        generation._models_cache.clear()
        state["models"] = [
            {"id": "gemini-3.0-pro-image"},
            {"id": "gemini-3.1-flash-image"},
        ]
        refreshed = client.get("/playground/api/gen-options").json()
        assert [m["id"] for m in refreshed["image"]["models"]] == [
            "gemini-3.0-pro-image",
            "gemini-3.1-flash-image",
        ]

        # Nếu /v1/models tạm lỗi lúc F5 → fallback về danh sách đã lưu trong session DB
        generation._models_cache.clear()
        state["fail"] = True
        fallback = client.get("/playground/api/gen-options").json()
        assert [m["id"] for m in fallback["image"]["models"]] == [
            "gemini-3.0-pro-image",
            "gemini-3.1-flash-image",
        ]
