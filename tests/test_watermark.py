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


def test_sample_roundtrip_and_mask_listing_ignores_samples(mask_dir):
    # Sample giữ nguyên bytes gốc (không chuẩn hoá 0/255 như mask).
    raw = b"raw-jpeg-bytes"
    saved = watermark.save_sample_for_key("gemini-3.0-pro-image:9:16", raw, ".jpg")
    assert saved.name == "sample_gemini-3.0-pro-image_9_16.jpg"
    assert watermark.read_sample_for_key("gemini-3.0-pro-image:9:16") == (raw, ".jpg")
    assert watermark.read_sample_for_key("chua-co-key") is None

    # Sample không bao giờ bị nhầm là mask: không nằm trong list_saved_masks,
    # và load_mask_for_key (kể cả prefix sweep) không pick file sample_.
    names = {mask["filename"] for mask in watermark.list_saved_masks()}
    assert saved.name not in names
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


# ── Regression: key compose lúc generate phải khớp mask đã lưu ────────────
#
# Bug gốc: _playground_orientation thu gọn orientation về portrait/landscape
# nên mask lưu theo catalog key đầy đủ (vd gemini-3.0-pro-image-four-three-2k
# cho ratio 4:3) không bao giờ được tra; tệ hơn, generation 4:3 khớp nhầm mask
# -landscape-2k của 16:9 và xoá WM sai vùng. Đồng thời mask lưu suffix -2k
# không áp dụng cho generation res 1K (mặc định) vì fallback đòi đúng suffix res.

from app import generation  # noqa: E402


def _gen_params(ratio="9:16", res="2K", base="gemini-3.0-pro-image"):
    return {
        "mode": "image",
        "model": "nano-banana-pro",
        "base": base,
        "ratio": ratio,
        "res": res,
        "length": None,
        "qty": 1,
        "prompt": "p",
        "vmode": "text",
        "references": [],
    }


def test_mask_keys_cover_all_playground_orientations():
    cases = {
        "16:9": "landscape",
        "9:16": "portrait",
        "1:1": "square",
        "4:3": "four-three",
        "3:4": "three-four",
    }
    for ratio, orient in cases.items():
        key, fallbacks = generation._mask_keys_for(_gen_params(ratio=ratio, res="2K"))
        assert key == f"gemini-3.0-pro-image:{ratio}:2k"
        assert f"gemini-3.0-pro-image-{orient}-2k" in fallbacks, (ratio, fallbacks)
        assert f"gemini-3.0-pro-image-{orient}" in fallbacks, (ratio, fallbacks)


def test_mask_keys_are_isolated_per_orientation_group():
    # Ratio khác nhau → key chính khác nhau (không còn một nhóm landscape chung).
    key_portrait, fallbacks_portrait = generation._mask_keys_for(_gen_params("9:16"))
    key_landscape, fallbacks_landscape = generation._mask_keys_for(_gen_params("16:9"))
    assert key_portrait != key_landscape
    # 9:16 chỉ được fallback vào nhóm portrait, không lẫn landscape.
    assert "gemini-3.0-pro-image-landscape-2k" not in fallbacks_portrait
    assert "gemini-3.0-pro-image-landscape" not in fallbacks_portrait
    # 16:9 là ratio landscape duy nhất → không có key {base}:{ratio} nào khác.
    assert not any(fb.startswith("gemini-3.0-pro-image:") for fb in fallbacks_landscape)
    # 3:4 là nhóm three-four riêng — không được fallback sang portrait của 9:16.
    _, fb_34 = generation._mask_keys_for(_gen_params("3:4"))
    assert "gemini-3.0-pro-image-three-four-2k" in fb_34
    assert "gemini-3.0-pro-image-portrait-2k" not in fb_34
    # 4:3 là nhóm four-three riêng — không lẫn nhóm khác.
    _, fb_43 = generation._mask_keys_for(_gen_params("4:3"))
    assert "gemini-3.0-pro-image-four-three-2k" in fb_43
    assert "gemini-3.0-pro-image-landscape-2k" not in fb_43
    assert "gemini-3.0-pro-image:16:9" not in fb_43


def test_generation_key_matches_saved_mask_for_every_ratio(mask_dir):
    # Mask được lưu đúng key mà frontend dùng ({base}:{ratio}:{res}) → khớp lại.
    for ratio in ("16:9", "9:16", "1:1", "4:3", "3:4"):
        key, fallbacks = generation._mask_keys_for(_gen_params(ratio=ratio))
        assert watermark.load_mask_for_key(key, fallback_keys=fallbacks) is None
        watermark.save_mask_for_key(key, _encode_png(_make_mask()))
        assert watermark.load_mask_for_key(key, fallback_keys=fallbacks) is not None, key


def test_mask_isolated_per_res(mask_dir):
    # 1K và 2K có WM layout khác nhau → mask 1K KHÔNG được áp dụng cho 2K.
    watermark.save_mask_for_key("gemini-3.0-pro-image:9:16:1k", _encode_png(_make_mask()))
    key_2k, fallbacks_2k = generation._mask_keys_for(_gen_params(ratio="9:16", res="2K"))
    assert key_2k == "gemini-3.0-pro-image:9:16:2k"
    assert watermark.load_mask_for_key(key_2k, fallback_keys=fallbacks_2k) is None
    # Ngược lại generation 1K vẫn dùng đúng mask của mình.
    key_1k, fallbacks_1k = generation._mask_keys_for(_gen_params(ratio="9:16", res="1K"))
    assert watermark.load_mask_for_key(key_1k, fallback_keys=fallbacks_1k) is not None


def test_saved_mask_legacy_res_suffix_applies_to_other_res(mask_dir):
    # Mask lưu legacy suffix -2k (từ tab mask cũ) vẫn phải áp dụng cho
    # generation res 1K cùng nhóm portrait — qua prefix sweep của fallback
    # {base}-portrait (mask legacy là thuộc nhóm orientation, không theo res mới).
    legacy = _make_mask((32, 32), (8, 8, 16, 16))
    watermark.save_mask_for_key("gemini-3.0-pro-image-portrait-2k", _encode_png(legacy))
    key, fallbacks = generation._mask_keys_for(_gen_params(ratio="9:16", res="1K"))
    assert key == "gemini-3.0-pro-image:9:16:1k"  # new-format chưa có file
    loaded = watermark.load_mask_for_key(key, fallback_keys=fallbacks)
    assert loaded is not None
    assert int((loaded > 0).sum()) == int((legacy > 0).sum())


def test_saved_mask_does_not_leak_across_orientation_groups(mask_dir):
    # Mask vẽ cho 3:4 res 2K KHÔNG được áp dụng cho generation 9:16 — khác nhóm
    # orientation, WM ở vị trí khác nhau; phải skip rõ ràng.
    key_34, fallbacks_34 = generation._mask_keys_for(_gen_params(ratio="3:4", res="2K"))
    watermark.save_mask_for_key(key_34, _encode_png(_make_mask()))
    key_916, fallbacks_916 = generation._mask_keys_for(_gen_params(ratio="9:16", res="1K"))
    assert watermark.load_mask_for_key(key_916, fallback_keys=fallbacks_916) is None
    # Ngược lại generation 3:4 res 2K vẫn khớp mask của mình.
    assert watermark.load_mask_for_key(key_34, fallback_keys=fallbacks_34) is not None


def test_remove_watermark_with_generation_keys_end_to_end(mask_dir):
    # Đủ đường: save theo key frontend → remove_watermark với keys từ
    # _mask_keys_for phải processed (không skipped) — nhánh engine opencv.
    img = _make_image()
    key, fallbacks = generation._mask_keys_for(_gen_params(ratio="9:16", res="1K"))
    watermark.save_mask_for_key("gemini-3.0-pro-image-portrait-2k", _encode_png(_make_mask((64, 64), (0, 0, 16, 16))))
    result, status = asyncio.run(
        watermark.remove_watermark(_encode_png(img), ".png", key, fallback_keys=fallbacks)
    )
    assert status.startswith("processed:"), status
    out = cv2.imdecode(np.frombuffer(result, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert not np.array_equal(out[0:16, 0:16], img[0:16, 0:16])

    # Key hoàn toàn lạ + không có global → skipped:no_mask, trả ảnh gốc, không crash.
    result2, status2 = asyncio.run(
        watermark.remove_watermark(_encode_png(img), ".png", "other-model:16:9", fallback_keys=["other-model-landscape-2k"])
    )
    assert status2 == "skipped:no_mask"
    assert result2 == _encode_png(img)


def test_finalize_image_passes_normalized_keys(monkeypatch, tmp_path):
    # _finalize_image phải truyền đúng (key, fallbacks) từ _mask_keys_for vào
    # watermark.remove_watermark — không được tự compose lại lệch format.
    from app import generation as g

    captured = {}

    async def fake_remove_watermark(raw, ext, key, fallback_keys=None):
        captured["key"] = key
        captured["fallbacks"] = fallback_keys
        return raw, "skipped:no_mask"

    class FakeClient:
        async def download_media(self, url):
            return b"fake-image-bytes", ".png"

    monkeypatch.setattr(generation.watermark, "remove_watermark", fake_remove_watermark)
    monkeypatch.setattr(generation, "RESULTS_DIR", tmp_path)
    params = _gen_params(ratio="4:3", res="2K")
    params["meta"] = {"mode": "image"}
    item = asyncio.run(generation._finalize_image("http://x/img.png", params, FakeClient()))
    assert item["url"].startswith("/tmp/playground_results/")
    key, fallbacks = generation._mask_keys_for(_gen_params(ratio="4:3", res="2K"))
    assert captured["key"] == key == "gemini-3.0-pro-image:4:3:2k"
    assert "gemini-3.0-pro-image-four-three-2k" in captured["fallbacks"]
    assert "gemini-3.0-pro-image-four-three" in captured["fallbacks"]
