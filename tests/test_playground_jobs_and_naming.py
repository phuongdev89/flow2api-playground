"""Tests cho endpoint liệt kê job của project + tên item tự sinh từ prompt.

Endpoint GET /playground/api/projects/{project_id}/jobs — frontend khôi phục
queue sau F5. Naming server-side trong run_playground_generate_job: 20 ký tự
đầu của prompt (+ "…" nếu dài hơn), dedupe với tên có sẵn trong project và
trong cùng batch bằng suffix " (2)", " (3)"...
"""

import asyncio
import hashlib
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import generation
from app.database import PlaygroundDatabase


async def _fake_validate(key):
    return key == "test-key"


@pytest.fixture
def playground_client(tmp_path, monkeypatch):
    database = PlaygroundDatabase(str(tmp_path / "playground.db"))
    monkeypatch.setattr("app.routes.playground_db", database)
    monkeypatch.setattr(
        "app.generation.flow_client.validate_key", _fake_validate
    )
    client = TestClient(main.app)
    yield client, database
    client.close()


def _login(client):
    response = client.post(
        "/playground/session", headers={"Authorization": "Bearer test-key"}
    )
    assert response.status_code == 204


def _owner(key: str = "test-key") -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _seed_created_at(database, offsets: dict[str, int]):
    """Đặt created_at/updated_at theo offset để thứ tự job deterministic."""
    conn = sqlite3.connect(str(database.db_path))
    base = int(time.time() * 1000)
    for job_id, offset in offsets.items():
        conn.execute(
            "UPDATE playground_jobs SET created_at = ?, updated_at = ? WHERE id = ?",
            (base + offset, base + offset, job_id),
        )
    conn.commit()
    conn.close()
    return base


# ── 1. Endpoint GET /playground/api/projects/{project_id}/jobs ──────────


def test_jobs_endpoint_shape_order_and_limit(playground_client):
    client, database = playground_client
    _login(client)
    owner = _owner()
    asyncio.run(database.create_project("p1", owner, "P", "", []))
    for index in range(25):
        asyncio.run(database.create_job(f"job-{index:02d}", owner, "p1"))
    base = _seed_created_at(
        database, {f"job-{index:02d}": index * 1000 for index in range(25)}
    )
    # Một job đã xong kèm kết quả — kiểm shape đầy đủ.
    asyncio.run(
        database.update_job_status(
            "job-24", "done", error=None, results=[{"id": "x"}],
            owner_hash=owner, project_id="p1",
        )
    )

    response = client.get("/playground/api/projects/p1/jobs")
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"jobs"}
    jobs = payload["jobs"]
    # Tối đa 20, mới nhất trước.
    assert len(jobs) == 20
    assert [job["job_id"] for job in jobs] == [
        f"job-{index:02d}" for index in range(24, 4, -1)
    ]
    for job in jobs:
        assert set(job) == {
            "job_id", "status", "error", "results", "created_at", "updated_at",
        }
        assert isinstance(job["created_at"], int)
    newest = jobs[0]
    assert newest["status"] == "done"
    assert newest["error"] is None
    assert newest["results"] == [{"id": "x"}]
    assert newest["created_at"] == base + 24 * 1000
    running = jobs[1]
    assert running["status"] == "running"
    assert running["error"] is None
    assert running["results"] == []


def test_jobs_endpoint_unknown_project_returns_empty_list(playground_client):
    client, _ = playground_client
    _login(client)
    # Project không tồn tại / không còn → danh sách rỗng, không 404.
    response = client.get("/playground/api/projects/missing-project/jobs")
    assert response.status_code == 200
    assert response.json() == {"jobs": []}


def test_jobs_endpoint_requires_session_and_scopes_by_owner(playground_client):
    client, database = playground_client
    # Chưa login → 401 (guard owner như các GET khác).
    assert client.get("/playground/api/projects/p1/jobs").status_code == 401

    _login(client)  # owner = hash("test-key")
    other = _owner("other-key")
    asyncio.run(database.create_project("proj-b", other, "B", "", []))
    asyncio.run(database.create_job("job-b1", other, "proj-b"))
    # Trùng project_id nhưng khác owner → job của owner khác không lộ.
    asyncio.run(database.create_project("shared", other, "S", "", []))
    asyncio.run(database.create_job("job-s1", other, "shared"))
    response = client.get("/playground/api/projects/shared/jobs")
    assert response.status_code == 200
    assert response.json() == {"jobs": []}

    # Job của chính owner vẫn thấy (cùng project_id).
    asyncio.run(database.create_job("job-a1", _owner(), "shared"))
    response = client.get("/playground/api/projects/shared/jobs")
    assert [job["job_id"] for job in response.json()["jobs"]] == ["job-a1"]


# ── 2. Tên gốc từ prompt ────────────────────────────────────────────────


def test_base_name_short_prompt_kept_without_ellipsis():
    # Prompt ≤ 20 ký tự giữ nguyên, không thêm "…".
    assert generation._item_base_name("mèo đeo kính") == "mèo đeo kính"
    exactly_twenty = "a" * 20
    assert generation._item_base_name(exactly_twenty) == exactly_twenty
    # Trim trước khi cắt.
    assert generation._item_base_name("   mèo nằm   ") == "mèo nằm"
    assert generation._item_base_name(None) == ""
    assert generation._item_base_name("   ") == ""


def test_base_name_long_prompt_truncated_to_20_chars_with_ellipsis():
    name = generation._item_base_name("mèo nằm sưởi nắng trên cỏ")
    assert name == "mèo nằm sưởi nắng tr…"
    assert len(name) == 21  # 20 codepoint + "…"


def test_base_name_unicode_slice_is_codepoint_safe():
    # Tiếng Nhật + emoji (astral plane) — cắt theo codepoint, không đứt giữa
    # surrogate pair.
    prompt = "猫の画像" + "🫠" * 24
    name = generation._item_base_name(prompt)
    assert len(name) == 21
    assert name[:4] == "猫の画像"
    assert name.endswith("…")


# ── 3. Dedupe tên: trong batch, với item có sẵn, item user đặt tên ──────


def test_assign_names_dedupes_within_batch():
    items = [{"id": "i1"}, {"id": "i2"}, {"id": "i3"}]
    result = generation._assign_generated_item_names(
        {"results": []}, items, "chó phốc mini"
    )
    assert result is items
    assert [item["name"] for item in items] == ["chó phốc mini", "chó phốc mini (2)", "chó phốc mini (3)"]


def test_assign_names_dedupes_with_existing_project_items():
    project = {"results": [{"name": "mèo"}, {"name": "mèo (2)"}]}
    items = [{"id": "i1"}, {"id": "i2"}]
    generation._assign_generated_item_names(project, items, "mèo")
    assert [item["name"] for item in items] == ["mèo (3)", "mèo (4)"]


def test_assign_names_keeps_user_named_items_untouched():
    project = {"results": []}
    items = [
        {"id": "ref-1", "name": "Ảnh của tôi", "role": "reference"},
        {"id": "gen-1"},
        {"id": "gen-2", "name": "Ảnh của tôi"},  # trùng tên user — vẫn giữ nguyên
    ]
    generation._assign_generated_item_names(project, items, "mèo")
    assert items[0]["name"] == "Ảnh của tôi"
    assert items[1]["name"] == "mèo"  # không trùng "Ảnh của tôi" → giữ base
    assert items[2]["name"] == "Ảnh của tôi"


def test_assign_names_avoids_user_name_collision():
    # Item trong project tên đúng base → item mới đếm tiếp thay vì trùng.
    project = {"results": [{"name": "mèo nằm sưởi nắng tr…"}]}
    items = [{"id": "gen-1"}]
    generation._assign_generated_item_names(project, items, "mèo nằm sưởi nắng trên cỏ")
    assert items[0]["name"] == "mèo nằm sưởi nắng tr… (2)"


def test_assign_names_without_prompt_leaves_items_untouched():
    items = [{"id": "gen-1"}]
    generation._assign_generated_item_names({"results": []}, items, "   ")
    assert "name" not in items[0]


# ── 4. Job runner gán tên trước khi append (integration) ────────────────


def test_job_runner_assigns_names_before_append(tmp_path, monkeypatch):
    database = PlaygroundDatabase(str(tmp_path / "playground.db"))
    owner = _owner("owner-a")
    asyncio.run(database.create_project("p1", owner, "P", "", []))
    # Item cũ trong project đã chiếm đúng tên base → item mới đếm tiếp.
    asyncio.run(
        database.append_results(
            owner, "p1", [{"id": "old", "name": "mèo nằm sưởi nắng tr…"}]
        )
    )
    asyncio.run(database.create_job("job-n1", owner, "p1"))
    params = {
        "mode": "image", "model": "nano-banana-pro", "base": "gemini-3.0-pro-image",
        "ratio": "16:9", "res": "1K", "length": None, "qty": 2,
        "prompt": "mèo nằm sưởi nắng trên cỏ",
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
                     "res": p["res"], "length": p["length"], "prompt": p["prompt"]},
        }

    monkeypatch.setattr(generation, "_generate_single_playground_item", fake_item)
    asyncio.run(
        generation.run_playground_generate_job(
            database, owner, "p1", "job-n1", params, "key"
        )
    )

    project = asyncio.run(database.get_project(owner, "p1"))
    names = [item.get("name") for item in project["results"]]
    assert names == [
        "mèo nằm sưởi nắng tr…",
        "mèo nằm sưởi nắng tr… (2)",
        "mèo nằm sưởi nắng tr… (3)",
    ]
    # Meta item mới giữ nguyên contract (prompt/references...) — naming chỉ set name.
    assert project["results"][1]["meta"]["prompt"] == "mèo nằm sưởi nắng trên cỏ"

    done = asyncio.run(database.get_job(owner, "p1", "job-n1"))
    assert done["status"] == "done"
    assert [item["name"] for item in done["results"]] == [
        "mèo nằm sưởi nắng tr… (2)",
        "mèo nằm sưởi nắng tr… (3)",
    ]
