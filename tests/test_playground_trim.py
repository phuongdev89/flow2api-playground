"""Tests cho endpoint trim video: POST /playground/api/projects/{pid}/items/{iid}/trim.

Happy path cần ffmpeg thật trên máy test (tạo file mp4 bằng lavfi — máy host
có ffmpeg thì chạy, không có thì pytest.skip). Guards / naming / path safety
không cần ffmpeg vì bị chặn trước bước subprocess.
"""

import asyncio
import hashlib
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import generation
from app.database import PlaygroundDatabase

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


async def _fake_validate(key):
    return key == "test-key"


@pytest.fixture
def playground_client(tmp_path, monkeypatch):
    database = PlaygroundDatabase(str(tmp_path / "playground.db"))
    monkeypatch.setattr("app.routes.playground_db", database)
    monkeypatch.setattr("app.generation.flow_client.validate_key", _fake_validate)
    # RESULTS_DIR → tmp_path riêng của test: trim đọc/ghi file thật nhưng không
    # đụng tmp/ của repo (đồng bộ với cách _resolve_results_file đọc module var).
    monkeypatch.setattr(generation, "RESULTS_DIR", tmp_path)
    client = TestClient(main.app)
    yield client, database, tmp_path
    client.close()


def _login(client):
    response = client.post(
        "/playground/session", headers={"Authorization": "Bearer test-key"}
    )
    assert response.status_code == 204


def _mutation_headers():
    return {"X-Playground-Request": "1", "Origin": "http://testserver"}


def _owner(key: str = "test-key") -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _create_project(client, name="Trim"):
    response = client.post(
        "/playground/api/projects", json={"name": name, "prompt": "", "results": []},
        headers=_mutation_headers(),
    )
    assert response.status_code == 201
    return response.json()


def _trim_url(client, project, item_id="vid-1"):
    return f"/playground/api/projects/{project['id']}/items/{item_id}/trim"


def _make_media_file(path, duration=4.0, webm=False):
    """Tạo file video test bằng ffmpeg lavfi (kèm audio); False nếu không tạo được."""
    if not FFMPEG:
        return False
    cmd = [
        FFMPEG, "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc=duration={duration}:size=320x240:rate=10",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
        "-shortest", "-pix_fmt", "yuv420p",
    ]
    if webm:
        # vp8+vorbis — không đóng gói được vào mp4 bằng stream copy (exit 1),
        # đúng kịch bản kích nhánh re-encode fallback.
        cmd += ["-c:v", "libvpx", "-b:v", "200k", "-c:a", "libvorbis"]
    else:
        # -g 10: keyframe mỗi 1s để -ss 1.0 với -c copy seek đúng điểm cắt.
        cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-g", "10", "-c:a", "aac"]
    cmd.append(str(path))
    result = subprocess.run(cmd, capture_output=True)
    return result.returncode == 0 and path.is_file() and path.stat().st_size > 0


def _seed_item(database, owner, project_id, item):
    asyncio.run(database.append_results(owner, project_id, [item]))
    return item


def _video_item(item_id="vid-1", filename="src.mp4", name="Video mèo", duration=4.0, url=None):
    return {
        "id": item_id,
        "kind": "video",
        "url": url or f"/tmp/playground_results/{filename}",
        "created_at": 1,
        "name": name,
        "meta": {
            "mode": "video",
            "model": "veo-3.1-lite",
            "ratio": "16:9",
            "res": None,
            "length": int(duration),
            "prompt": "mèo chạy",
            "media_generation_id": "media-1",
        },
    }


# ── 1. Happy path: cắt đoạn giữa → item mới + file mới ──────────────────


def test_trim_creates_new_item_file_and_meta(playground_client):
    if not (FFMPEG and FFPROBE):
        pytest.skip("Cần ffmpeg + ffprobe trên máy test")
    client, database, results_dir = playground_client
    _login(client)
    project = _create_project(client)
    source_path = results_dir / "src.mp4"
    assert _make_media_file(source_path, duration=4.0), "Không tạo được mp4 test"
    _seed_item(database, _owner(), project["id"], _video_item())

    response = client.post(
        _trim_url(client, project),
        json={"start_sec": 1.0, "end_sec": 3.0},
        headers=_mutation_headers(),
    )
    assert response.status_code == 201
    item = response.json()
    # Item mới: id/url khác gốc, kind video, có created_at và name dedupe.
    assert item["id"] != "vid-1"
    assert item["kind"] == "video"
    assert item["created_at"] > 0
    assert item["name"] == "Video mèo (trim)"
    assert item["url"].startswith("/tmp/playground_results/")
    assert item["url"] != "/tmp/playground_results/src.mp4"
    # Meta: copy từ gốc nhưng bỏ media_generation_id, thêm trimmed_from, cập nhật length.
    assert item["meta"]["trimmed_from"] == "vid-1"
    assert "media_generation_id" not in item["meta"]
    assert item["meta"]["length"] == 2
    assert item["meta"]["model"] == "veo-3.1-lite"
    assert item["meta"]["ratio"] == "16:9"
    assert item["meta"]["prompt"] == "mèo chạy"

    # File mới tồn tại + duration ≈ 2s (±1s do keyframe với -c copy).
    new_file = results_dir / item["url"].removeprefix("/tmp/playground_results/")
    assert new_file.is_file() and new_file.stat().st_size > 0
    duration = generation.probe_video_duration_sync(new_file)
    assert duration is not None
    assert abs(duration - 2.0) <= 1.0

    # Item gốc + file gốc nguyên vẹn trong project.
    stored = asyncio.run(database.get_project(_owner(), project["id"]))
    assert [res["id"] for res in stored["results"]] == ["vid-1", item["id"]]
    original = stored["results"][0]
    assert original["url"] == "/tmp/playground_results/src.mp4"
    assert original["name"] == "Video mèo"
    assert original["meta"]["media_generation_id"] == "media-1"
    assert original["meta"]["length"] == 4
    assert source_path.is_file()


def test_trim_reencode_fallback_when_copy_not_encapsulable(playground_client):
    if not (FFMPEG and FFPROBE):
        pytest.skip("Cần ffmpeg + ffprobe trên máy test")
    client, database, results_dir = playground_client
    _login(client)
    project = _create_project(client)
    assert _make_media_file(results_dir / "src.webm", duration=4.0, webm=True), (
        "ffmpeg máy test thiếu libvpx/libvorbis"
    )
    _seed_item(database, _owner(), project["id"], _video_item(filename="src.webm"))

    response = client.post(
        _trim_url(client, project),
        json={"start_sec": 1.0, "end_sec": 3.0},
        headers=_mutation_headers(),
    )
    # -c copy vp8/vorbis → mp4 phải fail rồi re-encode libx264+aac thành công.
    assert response.status_code == 201
    item = response.json()
    new_file = results_dir / item["url"].removeprefix("/tmp/playground_results/")
    assert new_file.is_file() and new_file.stat().st_size > 0
    duration = generation.probe_video_duration_sync(new_file)
    assert duration is not None
    assert abs(duration - 2.0) <= 1.0
    assert item["meta"]["length"] == 2


def test_trim_without_ffprobe_duration_allows_any_end(playground_client, monkeypatch):
    if not FFMPEG:
        pytest.skip("Cần ffmpeg trên máy test")
    client, database, results_dir = playground_client
    # ffprobe không đọc được duration → cho phép end tùy ý (chỉ cần > start).
    async def probe_none(_path):
        return None

    monkeypatch.setattr(generation, "probe_video_duration", probe_none)
    _login(client)
    project = _create_project(client)
    assert _make_media_file(results_dir / "src.mp4", duration=4.0)
    _seed_item(database, _owner(), project["id"], _video_item())

    response = client.post(
        _trim_url(client, project),
        json={"start_sec": 1.0, "end_sec": 120.0},
        headers=_mutation_headers(),
    )
    assert response.status_code == 201
    # Không probe được duration mới → length fallback về end - start.
    assert response.json()["meta"]["length"] == 119


# ── 2. Naming: "{tên gốc} (trim)" + dedupe suffix " (2)" ────────────────


def test_trim_name_dedupes_with_existing_trim_name(playground_client):
    if not (FFMPEG and FFPROBE):
        pytest.skip("Cần ffmpeg + ffprobe trên máy test")
    client, database, results_dir = playground_client
    _login(client)
    project = _create_project(client)
    assert _make_media_file(results_dir / "src.mp4", duration=4.0)
    _seed_item(database, _owner(), project["id"], _video_item())
    # Item khác trong project đã chiếm đúng tên base → item trim đếm tiếp.
    _seed_item(
        database, _owner(), project["id"],
        {"id": "vid-2", "kind": "video", "url": "/tmp/playground_results/other.mp4",
         "created_at": 2, "name": "Video mèo (trim)", "meta": {}},
    )

    response = client.post(
        _trim_url(client, project),
        json={"start_sec": 0.0, "end_sec": 2.0},
        headers=_mutation_headers(),
    )
    assert response.status_code == 201
    assert response.json()["name"] == "Video mèo (trim) (2)"


def test_trim_name_without_source_name_falls_back_to_video_trim(playground_client):
    if not (FFMPEG and FFPROBE):
        pytest.skip("Cần ffmpeg + ffprobe trên máy test")
    client, database, results_dir = playground_client
    _login(client)
    project = _create_project(client)
    assert _make_media_file(results_dir / "src.mp4", duration=4.0)
    _seed_item(database, _owner(), project["id"], _video_item(name="  "))

    response = client.post(
        _trim_url(client, project),
        json={"start_sec": 0.0, "end_sec": 2.0},
        headers=_mutation_headers(),
    )
    assert response.status_code == 201
    assert response.json()["name"] == "Video (trim)"


def test_assign_generated_item_names_accepts_base_name_override():
    # base_name truyền sẵn (trim) — prompt None vẫn đặt tên được, dedupe như cũ.
    project = {"results": [{"name": "Video (trim)"}]}
    items = [{"id": "i1"}, {"id": "i2"}]
    generation._assign_generated_item_names(project, items, None, base_name="Video (trim)")
    assert [item["name"] for item in items] == ["Video (trim) (2)", "Video (trim) (3)"]

    # base_name rỗng → rơi về hành vi cũ suy tên từ prompt (generation không đổi).
    legacy = [{"id": "g1"}]
    generation._assign_generated_item_names({"results": []}, legacy, "chó phốc", base_name="")
    assert legacy[0]["name"] == "chó phốc"


# ── 3. Guards: kind, item lạ, khoảng cắt, marker same-origin ────────────


def test_trim_guards_reject_bad_item_and_range(playground_client):
    client, database, _results_dir = playground_client
    _login(client)
    project = _create_project(client)
    url = _trim_url(client, project)
    headers = _mutation_headers()
    # Item ảnh → 400 (không cần file thật — bị chặn ở tầng kind).
    _seed_item(database, _owner(), project["id"], {
        "id": "img-1", "kind": "image", "url": "/tmp/playground_results/x.png",
        "created_at": 1, "meta": {},
    })
    assert client.post(
        f"/playground/api/projects/{project['id']}/items/img-1/trim",
        json={"start_sec": 1.0, "end_sec": 2.0}, headers=headers,
    ).status_code == 400

    # Item lạ trong project có thật → 404.
    assert client.post(
        f"/playground/api/projects/{project['id']}/items/ghost-item/trim",
        json={"start_sec": 1.0, "end_sec": 2.0}, headers=headers,
    ).status_code == 404
    # Project lạ → 404.
    assert client.post(
        "/playground/api/projects/ghost-project/items/vid-1/trim",
        json={"start_sec": 1.0, "end_sec": 2.0}, headers=headers,
    ).status_code == 404

    # Item video hợp lệ (chưa cần file vì range sai bị chặn trước).
    _seed_item(database, _owner(), project["id"], _video_item())
    bad_ranges = [
        {"start_sec": 2.0, "end_sec": 2.0},   # start == end
        {"start_sec": 3.0, "end_sec": 2.0},   # start > end
        {"start_sec": -1.0, "end_sec": 2.0},  # start âm
        {"start_sec": 0.0, "end_sec": 0.1},   # ngắn hơn 0.2s
        {"start_sec": 0.0, "end_sec": 121.0},  # dài hơn 120s
    ]
    for body in bad_ranges:
        response = client.post(url, json=body, headers=headers)
        assert response.status_code == 400, body
    # NaN: httpx từ chối serialize NaN qua json= → gửi raw JSON cho server tự parse.
    raw_nan = client.post(
        url,
        content='{"start_sec": NaN, "end_sec": 2.0}',
        headers={**headers, "content-type": "application/json"},
    )
    assert raw_nan.status_code == 400


def test_trim_requires_session_and_mutation_marker(playground_client):
    client, database, _results_dir = playground_client
    # Chưa login → 401 như mọi mutation khác.
    assert client.post(
        "/playground/api/projects/p1/items/vid-1/trim",
        json={"start_sec": 1.0, "end_sec": 2.0},
    ).status_code == 401

    _login(client)
    project = _create_project(client)
    # Thiếu marker X-Playground-Request → 403.
    assert client.post(
        _trim_url(client, project), json={"start_sec": 1.0, "end_sec": 2.0}
    ).status_code == 403


# ── 4. Path safety: RESULTS_DIR containment + file tồn tại ──────────────


def test_resolve_results_file_rejects_traversal_and_foreign_url(tmp_path, monkeypatch):
    monkeypatch.setattr(generation, "RESULTS_DIR", tmp_path)
    (tmp_path / "ok.mp4").write_bytes(b"x")
    (tmp_path / "refs").mkdir()
    (tmp_path / "refs" / "secret.png").write_bytes(b"x")  # mục tiêu traversal có thật

    assert generation._resolve_results_file("/tmp/playground_results/ok.mp4") == (
        tmp_path / "ok.mp4"
    )
    for bad_url in (
        "/tmp/playground_results/../refs/secret.png",  # traversal ra ngoài RESULTS_DIR
        "/tmp/playground_refs/secret.png",             # prefix lạ
        "https://evil.example/x.mp4",                  # URL tuyệt đối
        None,
    ):
        with pytest.raises(ValueError):
            generation._resolve_results_file(bad_url)


def test_trim_rejects_traversal_and_missing_file(playground_client):
    client, database, _results_dir = playground_client
    _login(client)
    project = _create_project(client)
    headers = _mutation_headers()
    # URL traversal (file đích nằm ngoài RESULTS_DIR) → 400.
    _seed_item(database, _owner(), project["id"], _video_item(
        item_id="vid-trav",
        url="/tmp/playground_results/../playground_refs/secret.png"))
    assert client.post(
        _trim_url(client, project, "vid-trav"),
        json={"start_sec": 1.0, "end_sec": 2.0}, headers=headers,
    ).status_code == 400

    # File không còn tồn tại trong RESULTS_DIR → 400.
    _seed_item(database, _owner(), project["id"], _video_item(item_id="vid-ghost"))
    assert client.post(
        _trim_url(client, project, "vid-ghost"),
        json={"start_sec": 1.0, "end_sec": 2.0}, headers=headers,
    ).status_code == 400


def test_trim_rejects_end_beyond_duration(playground_client):
    if not (FFMPEG and FFPROBE):
        pytest.skip("Cần ffmpeg + ffprobe trên máy test")
    client, database, results_dir = playground_client
    _login(client)
    project = _create_project(client)
    assert _make_media_file(results_dir / "src.mp4", duration=4.0)
    _seed_item(database, _owner(), project["id"], _video_item())
    # Video dài 4s, dung sai 0.5s → end=10 vượt quá → 400 (chưa đụng ffmpeg).
    response = client.post(
        _trim_url(client, project),
        json={"start_sec": 0.0, "end_sec": 10.0},
        headers=_mutation_headers(),
    )
    assert response.status_code == 400
    assert "độ dài" in response.json()["detail"]
    # end trong dung sai thì vẫn cắt được (duration làm tròn của ffprobe).
    ok = client.post(
        _trim_url(client, project),
        json={"start_sec": 0.0, "end_sec": 4.2},
        headers=_mutation_headers(),
    )
    assert ok.status_code == 201
