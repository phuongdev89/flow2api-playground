import asyncio

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import generation, watermark
from app.database import PlaygroundDatabase


async def _fake_validate(key):
    return key == "test-key"


@pytest.fixture
def sample_client(tmp_path, monkeypatch):
    database = PlaygroundDatabase(str(tmp_path / "playground.db"))
    # Thay instance trong routes (module-level) bằng instance tạm — cùng harness
    # với test_playground_generate; mask dir cũng trỏ sang tmp để test iso.
    monkeypatch.setattr("app.routes.playground_db", database)
    monkeypatch.setattr("app.generation.flow_client.validate_key", _fake_validate)
    monkeypatch.setattr(watermark, "WATERMARK_MASK_DIR", str(tmp_path / "masks"))
    client = TestClient(main.app)
    yield client, tmp_path / "masks"
    client.close()


def _mutation_headers():
    return {"X-Playground-Request": "1", "Origin": "http://testserver"}


def _login(client):
    response = client.post(
        "/playground/session", headers={"Authorization": "Bearer test-key"}
    )
    assert response.status_code == 204


def test_create_and_get_sample_route(sample_client, monkeypatch):
    client, mask_dir = sample_client
    _login(client)

    # Chưa có sample cho key → 404.
    missing = client.get("/playground/api/watermark/sample/gemini-3.0-pro-image_9_16_1k")
    assert missing.status_code == 404

    async def fake_generate(model_id, ratio, res, api_key, api_url=None):
        # Endpoint không gửi res → mặc định 1K (sample gen đúng res của key).
        assert (model_id, ratio, res) == ("nano-banana-pro", "9:16", "1K")
        safe = watermark.sanitize_key(f"gemini-3.0-pro-image:{ratio}:{res.lower()}")
        watermark.save_sample_for_key(f"gemini-3.0-pro-image:{ratio}:{res.lower()}", b"SAMPLE-RAW", ".png")
        return {
            "key": safe,
            "filename": f"sample_{safe}.png",
            "url": f"/playground/api/watermark/sample/{safe}",
        }

    monkeypatch.setattr(generation, "generate_watermark_sample", fake_generate)
    created = client.post(
        "/playground/api/watermark/sample",
        json={"model": "nano-banana-pro", "ratio": "9:16"},
        headers=_mutation_headers(),
    )
    assert created.status_code == 200
    body = created.json()
    assert body["url"] == "/playground/api/watermark/sample/gemini-3.0-pro-image_9_16_1k"
    assert (mask_dir / body["filename"]).read_bytes() == b"SAMPLE-RAW"

    served = client.get(body["url"])
    assert served.status_code == 200
    assert served.content == b"SAMPLE-RAW"
    assert served.headers["content-type"].startswith("image/png")

    # Sample KHÔNG được liệt kê như mask trong panel WM.
    info = client.get("/playground/api/watermark").json()
    assert all(not str(m["key"]).startswith("sample_") for m in info["masks"])


def test_create_sample_rejects_unknown_model(sample_client):
    client, _mask_dir = sample_client
    _login(client)
    # Helper thật validate trước khi đụng network → model lạ trả 400.
    response = client.post(
        "/playground/api/watermark/sample",
        json={"model": "khong-ton-tai", "ratio": "9:16"},
        headers=_mutation_headers(),
    )
    assert response.status_code == 400


def test_get_mask_route(sample_client):
    client, mask_dir = sample_client
    _login(client)

    # Chưa có mask cho key → 404.
    missing = client.get("/playground/api/watermark/mask/gemini-3.0-pro-image_9_16_1k")
    assert missing.status_code == 404

    # File mask lưu đúng key (frontend gọi với key đã sanitize) → trả nguyên bytes.
    target = watermark.mask_path_for_key("gemini-3.0-pro-image:9:16:1k")
    assert target.name == "gemini-3.0-pro-image_9_16_1k.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"MASK-PNG-BYTES")
    served = client.get("/playground/api/watermark/mask/gemini-3.0-pro-image_9_16_1k")
    assert served.status_code == 200
    assert served.content == b"MASK-PNG-BYTES"
    assert served.headers["content-type"].startswith("image/png")

    # Key khác (chỉ lệch res) vẫn 404 — editor chỉ nạp mask của chính key đó.
    other = client.get("/playground/api/watermark/mask/gemini-3.0-pro-image_9_16_2k")
    assert other.status_code == 404


def test_generate_watermark_sample_keeps_raw_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(watermark, "WATERMARK_MASK_DIR", str(tmp_path / "masks"))

    class FakeClient:
        def __init__(self):
            self.bodies = []

        async def generate(self, api_key, body):
            self.bodies.append(body)
            return {"url": "https://cdn.example/out.jpg"}

        async def download_media(self, url):
            assert url == "https://cdn.example/out.jpg"
            return b"RAW-IMAGE-BYTES", ".jpg"

    fake = FakeClient()
    monkeypatch.setattr(generation, "get_flow_client", lambda api_url=None: fake)

    info = asyncio.run(
        generation.generate_watermark_sample("nano-banana-pro", "9:16", "2K", "key-1", "http://flow.example")
    )
    # Sample lưu NGUYÊN bytes trả về — mục đích là thấy đúng vị trí watermark;
    # key sample theo đúng res yêu cầu (1K ≠ 2K khác layout WM).
    assert info["filename"] == "sample_gemini-3.0-pro-image_9_16_2k.jpg"
    assert (tmp_path / "masks" / info["filename"]).read_bytes() == b"RAW-IMAGE-BYTES"
    body = fake.bodies[0]
    assert body["model"] == "gemini-3.0-pro-image"
    assert body["generationConfig"]["aspectRatio"] == "9:16"
    assert body["generationConfig"]["imageSize"] == "2k"

    with pytest.raises(ValueError):
        asyncio.run(generation.generate_watermark_sample("khong-ton-tai", "9:16", "1K", "k"))
    with pytest.raises(ValueError):
        asyncio.run(generation.generate_watermark_sample("nano-banana-pro", "7:3", "1K", "k"))
    with pytest.raises(ValueError):
        # Res không được quảng bá cho model → 400, không degrade (mask gen sai
        # res thì tô sai vị trí WM, im lặng còn tệ hơn báo lỗi).
        asyncio.run(generation.generate_watermark_sample("nano-banana-pro", "9:16", "4K", "k"))
