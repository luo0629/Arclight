from uuid import UUID, uuid4

from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import select

from arclight.application.model_services import run_settings
from arclight.application.runtime import execute_run
from arclight.persistence.models import ModelService, WorkflowRun
from arclight.providers.text import MockTextProvider


def account(client):
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": f"{uuid4()}@example.com",
            "password": "services-test-password",
            "display_name": "服务验收",
        },
    )
    assert response.status_code == 201
    client.headers["Authorization"] = "Bearer " + response.json()["access_token"]
    return response.json()["user"]["id"]


def profile(**patch):
    return {
        "mode": "server",
        "base_url": "https://api.openai.com/v1",
        "model": "text-model",
        "expected_revision": 0,
        **patch,
    }


def test_account_isolation_encryption_and_revision(client, settings):
    settings.service_encryption_key = SecretStr(Fernet.generate_key().decode())
    uid = account(client)
    secret = "never-return-this-secret"
    saved = client.put("/api/v1/me/model-services/text", json=profile(api_key=secret))
    assert saved.status_code == 200, saved.text
    assert saved.json()["has_key"] is True
    assert saved.json()["revision"] == 1
    assert secret not in saved.text and "encrypted_key" not in saved.text
    fetched = client.get("/api/v1/me/model-services")
    assert fetched.headers["cache-control"] == "no-store"
    assert secret not in fetched.text
    with client.app.state.session_factory() as db:
        row = db.scalar(select(ModelService).where(ModelService.user_id == UUID(uid)))
        assert secret not in row.encrypted_key
        assert (
            Fernet(settings.service_encryption_key.get_secret_value())
            .decrypt(row.encrypted_key.encode())
            .decode()
            == secret
        )
    assert client.put("/api/v1/me/model-services/text", json=profile()).status_code == 409
    updated = client.put("/api/v1/me/model-services/text", json=profile(expected_revision=1))
    assert updated.json()["has_key"] is True
    account(client)
    assert client.get("/api/v1/me/model-services").json()["services"] == []
    assert client.put("/api/v1/me/model-services/text", json=profile()).json()["has_key"] is False


def test_key_storage_disabled_and_media_not_activated(client, settings):
    settings.service_encryption_key = SecretStr("")
    account(client)
    assert client.put("/api/v1/me/model-services/image", json=profile()).status_code == 200
    blocked = client.put("/api/v1/me/model-services/video", json=profile(api_key="private"))
    assert blocked.status_code == 409
    assert "private" not in blocked.text
    assert (
        client.put(
            "/api/v1/me/model-services/image", json=profile(expected_revision=1, mode="custom")
        ).status_code
        == 422
    )
    assert (
        client.put("/api/v1/me/model-services/text", json=profile(mode="custom")).status_code == 422
    )


def test_endpoints_and_allowlist_are_validated_without_network(client, settings):
    settings.service_encryption_key = SecretStr(Fernet.generate_key().decode())
    account(client)
    for url in [
        "http://host.test/v1",
        "https://localhost/v1",
        "https://127.0.0.1/v1",
        "https://name:secret@host.test/v1",
        "https://host.test:broken/v1",
        "https://host.test/v1?key=secret",
    ]:
        response = client.put("/api/v1/me/model-services/text", json=profile(base_url=url))
        assert response.status_code == 422, (url, response.text)
        assert "secret" not in response.text
    blocked = client.put(
        "/api/v1/me/model-services/text",
        json=profile(mode="custom", base_url="https://unapproved.example/v1", api_key="secret"),
    )
    assert blocked.status_code == 422
    assert blocked.json()["error"]["code"] == "SERVICE_HOST_BLOCKED"


def test_active_text_is_snapshotted_and_used_without_real_calls(client, settings, monkeypatch):
    settings.service_encryption_key = SecretStr(Fernet.generate_key().decode())
    account(client)
    response = client.put(
        "/api/v1/me/model-services/text", json=profile(mode="custom", api_key="first-secret")
    )
    assert response.status_code == 200, response.text
    project = client.post("/api/v1/projects", json={"name": "模型配置验收"}).json()
    episode = client.post(
        f"/api/v1/projects/{project['id']}/episodes", json={"episode_no": 1, "title": "隔离测试"}
    ).json()
    started = client.post(
        f"/api/v1/episodes/{episode['id']}/script-runs",
        json={"story": "一颗种子"},
        headers={"Idempotency-Key": "service-snapshot"},
    )
    assert started.status_code == 202, started.text
    run_id = UUID(started.json()["run_id"])
    assert (
        client.put(
            "/api/v1/me/model-services/text",
            json=profile(
                expected_revision=1, mode="server", api_key="second-secret", model="new-model"
            ),
        ).status_code
        == 200
    )
    captured = []

    def provider(configuration):
        captured.append(configuration)
        return MockTextProvider()

    monkeypatch.setattr("arclight.application.runtime.build_provider", provider)
    execute_run(client.app.state.session_factory, settings, run_id, 0)
    assert captured[0].llm_model == "text-model"
    assert captured[0].llm_api_key.get_secret_value() == "first-secret"
    assert captured[0].provider == "openai_compatible"
    with client.app.state.session_factory() as db:
        run = db.get(WorkflowRun, run_id)
        assert run.status == "WAITING_REVIEW"
        assert "first-secret" not in str(run.input_json)
    response = client.get(f"/api/v1/runs/{run_id}")
    assert "encrypted_key" not in response.text and "first-secret" not in response.text


def test_clear_key_and_server_fallback(client, settings):
    settings.service_encryption_key = SecretStr(Fernet.generate_key().decode())
    account(client)
    assert (
        client.put("/api/v1/me/model-services/text", json=profile(api_key="secret")).status_code
        == 200
    )
    response = client.put(
        "/api/v1/me/model-services/text", json=profile(expected_revision=1, clear_key=True)
    )
    assert response.json()["has_key"] is False
    assert run_settings(settings, None) is settings


def test_service_config_requires_personal_session(client):
    assert client.get("/api/v1/me/model-services").status_code == 403
    client.headers.pop("Authorization")
    assert client.get("/api/v1/me/model-services").status_code == 401
