from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.engine import make_url

from arclight.application.model_services import run_settings
from arclight.application.runtime import execute_run
from arclight.domain.model_suppliers import SUPPLIERS
from arclight.persistence.models import ModelService, ServiceCredential, WorkflowRun
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
        credential = db.get(ServiceCredential, row.credential_id)
        assert row.encrypted_key is None
        assert secret not in credential.encrypted_key
        assert (
            Fernet(settings.service_encryption_key.get_secret_value())
            .decrypt(credential.encrypted_key.encode())
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


def test_endpoints_without_domain_allowlist_or_network(client, settings, monkeypatch):
    import socket

    resolve = socket.getaddrinfo
    database = make_url(settings.database_url)

    def unexpected_dns(host, port, *args, **kwargs):
        # PostgreSQL may resolve its own test host on each new DB connection.
        if (
            database.get_backend_name() == "postgresql"
            and host == database.host
            and str(port) == str(database.port or 5432)
        ):
            return resolve(host, port, *args, **kwargs)
        raise AssertionError("Saving configuration must not resolve or call a supplier")

    monkeypatch.setattr(socket, "getaddrinfo", unexpected_dns)
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
    saved = client.put(
        "/api/v1/me/model-services/text",
        json=profile(mode="custom", base_url="https://unapproved.example/v1", api_key="secret"),
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["base_url"] == "https://unapproved.example/v1"


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


@pytest.mark.parametrize("preset", SUPPLIERS, ids=lambda preset: preset.id)
def test_official_supplier_only_needs_selection_and_key(client, settings, preset):
    settings.service_encryption_key = SecretStr(Fernet.generate_key().decode())
    account(client)
    catalog = client.get("/api/v1/me/model-services").json()
    assert preset.model_dump() in catalog["suppliers"]
    saved = client.put(
        "/api/v1/me/model-services/text",
        json={
            "supplier": preset.id,
            "mode": "custom",
            "api_key": "test-key",
            "expected_revision": 0,
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["base_url"] == preset.base_url
    assert saved.json()["model"] == preset.model
    assert saved.json()["response_format"] == preset.response_format
    assert saved.json()["supplier_name"] == preset.name
    assert client.get("/api/v1/me/model-services").json()["services"][0]["key_status"] == "READY"
    for change in (
        {"base_url": "https://different.example/v1"},
        {
            "response_format": "json_object"
            if preset.response_format == "json_schema"
            else "json_schema"
        },
    ):
        rejected = client.put(
            "/api/v1/me/model-services/text",
            json={"supplier": preset.id, "expected_revision": 1, **change},
        )
        assert rejected.status_code == 422
        assert rejected.json()["error"]["code"] == "SUPPLIER_MISMATCH"


def test_destination_change_requires_new_key_and_audit_is_private(client, settings):
    from arclight.persistence.models import ServiceAudit

    settings.service_encryption_key = SecretStr(Fernet.generate_key().decode())
    uid = account(client)
    assert (
        client.put(
            "/api/v1/me/model-services/text", json=profile(api_key="old-private-key")
        ).status_code
        == 200
    )
    rejected = client.put(
        "/api/v1/me/model-services/text",
        json=profile(expected_revision=1, base_url="https://new.example/v1"),
    )
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "SERVICE_KEY_REENTRY_REQUIRED"
    saved = client.put(
        "/api/v1/me/model-services/text",
        json=profile(
            expected_revision=1,
            mode="custom",
            supplier_name="自定义服务",
            response_format="json_object",
            base_url="https://new.example/v1",
            api_key="new-private-key",
        ),
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["supplier_name"] == "自定义服务"
    # Rejections are rolled back with no misleading successful-change audit.
    with client.app.state.session_factory() as db:
        assert (
            len(list(db.scalars(select(ServiceAudit).where(ServiceAudit.user_id == UUID(uid)))))
            == 2
        )
    first = client.get("/api/v1/me/model-services/audit?limit=1")
    assert first.headers["cache-control"] == "no-store"
    cursor = first.json()["next_cursor"]
    assert cursor and first.json()["items"][0]["details"]["destination_changed"] is True
    second = client.get(f"/api/v1/me/model-services/audit?limit=1&before={cursor}")
    assert second.json()["next_cursor"] is None
    assert first.json()["items"][0]["id"] != second.json()["items"][0]["id"]
    assert "private-key" not in first.text + second.text
    assert "encrypted_key" not in first.text + second.text
    account(client)
    assert client.get("/api/v1/me/model-services/audit").json()["items"] == []
    assert client.get(f"/api/v1/me/model-services/audit?before={cursor}").status_code == 404


def test_storage_diagnostics_are_safe(client, settings):
    settings.service_encryption_key = SecretStr(Fernet.generate_key().decode())
    account(client)
    assert (
        client.put(
            "/api/v1/me/model-services/text", json=profile(api_key="private-value")
        ).status_code
        == 200
    )
    settings.service_encryption_key = SecretStr(Fernet.generate_key().decode())
    response = client.get("/api/v1/me/model-services")
    assert response.json()["key_storage"]["code"] == "KEY_STORAGE_STALE"
    assert response.json()["services"][0]["key_status"] == "SERVICE_KEY_UNAVAILABLE"
    assert response.json()["can_store_keys"] is False
    assert "private-value" not in response.text
    settings.service_encryption_key = SecretStr("bad-secret-key")
    response = client.get("/api/v1/me/model-services")
    assert response.json()["key_storage"]["code"] == "KEY_STORAGE_INVALID"
    assert "bad-secret-key" not in response.text
    settings.service_encryption_key = SecretStr("")
    assert (
        client.get("/api/v1/me/model-services").json()["key_storage"]["code"]
        == "KEY_STORAGE_DISABLED"
    )
