import asyncio
import hashlib

import pytest
from fastapi.testclient import TestClient

import app.main as main
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


def _mutation_headers():
    return {"X-Playground-Request": "1", "Origin": "http://testserver"}


def _login(client):
    response = client.post(
        "/playground/session", headers={"Authorization": "Bearer test-key"}
    )
    assert response.status_code == 204
    return response


def test_session_cookie_is_persistent_opaque_and_revocable(playground_client):
    client, database = playground_client
    response = _login(client)
    cookie = response.cookies.get("playground_session")
    assert cookie and cookie != "test-key"
    assert "httponly" in response.headers["set-cookie"].lower()
    assert "samesite=lax" in response.headers["set-cookie"].lower()

    # Key lưu vào session để job nền gọi flow2api.
    owner = hashlib.sha256(b"test-key").hexdigest()
    assert asyncio.run(database.api_key_for_owner(owner)) == "test-key"

    assert client.get("/playground/api/projects").status_code == 200
    assert client.post(
        "/playground/logout", headers=_mutation_headers()
    ).status_code == 204
    assert asyncio.run(database.api_key_for_owner(owner)) is None
    assert client.get("/playground/api/projects").status_code == 401


def test_projects_persist_and_are_scoped_to_the_api_key_owner(playground_client, tmp_path):
    client, database = playground_client
    _login(client)
    created = client.post(
        "/playground/api/projects",
        json={"name": "QA", "prompt": "abc", "results": []},
        headers=_mutation_headers(),
    ).json()

    # Instance khác trên cùng file DB vẫn thấy project (persistance).
    other = PlaygroundDatabase(database.db_path)
    owner = hashlib.sha256(b"test-key").hexdigest()
    assert asyncio.run(other.get_project(owner, created["id"]))["name"] == "QA"

    # Owner khác không thấy project của owner này.
    other_owner = hashlib.sha256(b"other-key").hexdigest()
    assert asyncio.run(other.get_project(other_owner, created["id"])) is None


def test_mutations_reject_missing_marker_or_cross_origin(playground_client):
    client, _ = playground_client
    _login(client)
    created = client.post(
        "/playground/api/projects",
        json={"name": "QA", "prompt": "", "results": []},
        headers=_mutation_headers(),
    ).json()
    # Thiếu marker
    assert client.patch(
        f"/playground/api/projects/{created['id']}", json={"name": "X"}
    ).status_code == 403
    # Origin khác host
    assert client.patch(
        f"/playground/api/projects/{created['id']}",
        json={"name": "X"},
        headers={"X-Playground-Request": "1", "Origin": "http://evil.example"},
    ).status_code == 403


def test_bad_key_does_not_issue_session(playground_client):
    client, _ = playground_client
    response = client.post(
        "/playground/session", headers={"Authorization": "Bearer wrong-key"}
    )
    assert response.status_code == 401
    assert client.get("/playground/api/projects").status_code == 401
