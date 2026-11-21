import asyncio

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

from app import watermark  # noqa: E402


@pytest.fixture
def mask_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(watermark, "WATERMARK_MASK_DIR", str(tmp_path / "masks"))
    monkeypatch.setattr(watermark, "WATERMARK_ENABLED", True)
    monkeypatch.setattr(watermark, "WATERMARK_ENGINE", "opencv")  # test không cần LaMa
    watermark._mask_cache.clear()
    return tmp_path / "masks"


def _make_mask(shape=(32, 32), region=(0, 0, 8, 8)):
    mask = np.zeros(shape, dtype=np.uint8)
    x0, y0, x1, y1 = region
    mask[y0:y1, x0:x1] = 255
    return mask


def _encode_png(array):
    ok, encoded = cv2.imencode(".png", array)
    assert ok
    return encoded.tobytes()


def _make_image(shape=(64, 64, 3)):
    img = np.full(shape, 200, dtype=np.uint8)
    x0, y0, x1, y1 = (0, 0, 16, 16)
    img[y0:y1, x0:x1] = (0, 0, 255)  # "watermark" đỏ góc trên trái
    return img


def test_save_load_mask_roundtrip(mask_dir):
    mask = _make_mask()
    watermark.save_mask_for_key("gemini-3.0-pro-image:9:16", _encode_png(mask))
    loaded = watermark.load_mask_for_key("gemini-3.0-pro-image:9:16")
    assert loaded is not None
    assert loaded.shape == mask.shape
    assert int((loaded > 0).sum()) == int((mask > 0).sum())
    assert watermark.delete_mask_for_key("gemini-3.0-pro-image:9:16")
    assert watermark.load_mask_for_key("gemini-3.0-pro-image:9:16") is None


def test_mask_fallback_order_primary_then_legacy_then_global(mask_dir):
    # Global có sẵn → mọi key đều load được global.
    watermark.save_mask_for_key("watermark", _encode_png(_make_mask((32, 32), (0, 0, 4, 4))))
    loaded = watermark.load_mask_for_key("missing-key")
    assert loaded is not None

    # Legacy key (model id resolve cũ) được ưu tiên trước global.
    legacy = _make_mask((32, 32), (8, 8, 16, 16))
    watermark.save_mask_for_key("gemini-3.0-pro-image-portrait-2k", _encode_png(legacy))
    loaded = watermark.load_mask_for_key(
        "gemini-3.0-pro-image:9:16",
        fallback_keys=["gemini-3.0-pro-image-portrait-2k"],
    )
    assert loaded is not None
    assert int((loaded > 0).sum()) == int((legacy > 0).sum())


def test_remove_watermark_replaces_region_only(mask_dir):
    img = _make_image()
    watermark.save_mask_for_key("test:key", _encode_png(_make_mask((64, 64), (0, 0, 16, 16))))
    result, status = asyncio.run(
        watermark.remove_watermark(_encode_png(img), ".png", "test:key")
    )
    assert status.startswith("processed:")
    out = cv2.imdecode(np.frombuffer(result, dtype=np.uint8), cv2.IMREAD_COLOR)
    # Vùng ngoài mask giữ nguyên 100%.
    assert np.array_equal(out[20:, :], img[20:, :])
    # Vùng watermark đã bị biến đổi (không còn đỏ thuần).
    assert not np.array_equal(out[0:16, 0:16], img[0:16, 0:16])


def test_remove_watermark_scales_mask_to_image_size(mask_dir):
    # Mask vẽ trên khung 32x32 nhưng ảnh 64x64 — mask phải tự scale.
    img = _make_image()
    watermark.save_mask_for_key("test:scale", _encode_png(_make_mask((32, 32), (0, 0, 8, 8))))
    result, status = asyncio.run(
        watermark.remove_watermark(_encode_png(img), ".png", "test:scale")
    )
    assert status.startswith("processed:")
    out = cv2.imdecode(np.frombuffer(result, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert not np.array_equal(out[0:16, 0:16], img[0:16, 0:16])


def test_remove_watermark_skips_without_mask(mask_dir):
    img = _make_image()
    result, status = asyncio.run(
        watermark.remove_watermark(_encode_png(img), ".png", "no-such-key")
    )
    assert status == "skipped:no_mask"
    assert result == _encode_png(img)


def test_remove_watermark_skips_when_disabled(mask_dir, monkeypatch):
    monkeypatch.setattr(watermark, "WATERMARK_ENABLED", False)
    watermark.save_mask_for_key("test:off", _encode_png(_make_mask()))
    img = _make_image()
    _, status = asyncio.run(
        watermark.remove_watermark(_encode_png(img), ".png", "test:off")
    )
    assert status == "skipped:disabled"
