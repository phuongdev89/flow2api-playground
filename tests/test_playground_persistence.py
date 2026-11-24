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

    # Key + endpoint lưu vào session để job nền gọi flow2api.
    owner = hashlib.sha256(b"test-key").hexdigest()
    assert asyncio.run(database.api_credentials_for_owner(owner)) == ("test-key", None)

    assert client.get("/playground/api/projects").status_code == 200
    assert client.post(
        "/playground/logout", headers=_mutation_headers()
    ).status_code == 204
    assert asyncio.run(database.api_credentials_for_owner(owner)) == (None, None)
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


# ── Login với cặp (api_url, api_key) từ form ─────────────────────────────


def test_login_with_api_url_issues_session_and_stores_credentials(
    playground_client, monkeypatch
):
    client, database = playground_client
    seen_urls = []

    async def fake_validate(self, key):
        seen_urls.append(self.base_url)
        self.last_check_error = None
        return key == "test-key"

    # Patch class-level: login có api_url tạo FlowClient riêng cho URL đó.
    monkeypatch.setattr("app.generation.FlowClient.validate_key", fake_validate)
    response = client.post(
        "/playground/session",
        json={"api_url": "http://flow.local:1234/", "api_key": "test-key"},
    )
    assert response.status_code == 204
    # Key được xác thực đúng trên endpoint đã nhập (đã chuẩn hoá, bỏ "/" cuối).
    assert seen_urls == ["http://flow.local:1234"]
    owner = hashlib.sha256(b"test-key").hexdigest()
    assert asyncio.run(database.api_credentials_for_owner(owner)) == (
        "test-key",
        "http://flow.local:1234",
    )


def test_login_rejects_invalid_api_url(playground_client):
    client, _ = playground_client
    for bad_url in ("ftp://x", "not-a-url", ""):
        response = client.post(
            "/playground/session", json={"api_url": bad_url, "api_key": "test-key"}
        )
        assert response.status_code == 422, bad_url


def test_login_with_unreachable_url_returns_502(playground_client, monkeypatch):
    client, _ = playground_client

    async def fake_check(self, key):
        self.last_check_error = "unreachable"
        return "unreachable"

    monkeypatch.setattr("app.generation.FlowClient.check_key", fake_check)
    response = client.post(
        "/playground/session",
        json={"api_url": "http://flow.local:1234", "api_key": "test-key"},
    )
    assert response.status_code == 502
    assert "connect" in response.json()["detail"].lower()


def test_login_with_wrong_key_for_url_returns_401(playground_client, monkeypatch):
    client, _ = playground_client

    async def fake_check(self, key):
        self.last_check_error = "unauthorized"
        return "unauthorized"

    monkeypatch.setattr("app.generation.FlowClient.check_key", fake_check)
    response = client.post(
        "/playground/session",
        json={"api_url": "http://flow.local:1234", "api_key": "nope"},
    )
    assert response.status_code == 401


def test_get_session_without_cookie_stays_401(playground_client):
    # GET không body (restoreSession) không được lỗi 422 vì payload optional.
    client, _ = playground_client
    assert client.get("/playground/session").status_code == 401
