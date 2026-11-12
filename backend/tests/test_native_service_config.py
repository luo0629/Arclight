"""Named providers require exact model IDs; custom destinations retain strict isolation."""

import socket
from collections import Counter
from uuid import UUID

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from test_model_services import account

from arclight.application.model_services import run_settings, text_snapshot
from arclight.domain.model_suppliers import SUPPLIERS
from arclight.persistence.models import GatewayCall, GenerationJob, ModelService


@pytest.mark.parametrize("preset", SUPPLIERS, ids=lambda p: p.kind + ":" + p.id)
def test_named_provider_key_model_and_official_endpoint(client, settings, preset):
    settings.service_encryption_key = SecretStr(Fernet.generate_key().decode())
    account(client)
    catalog = client.get("/api/v1/me/model-services?catalog_version=2").json()
    assert Counter(p["kind"] for p in catalog["suppliers"]) == {
        "text": 6,
        "image": 5,
        "video": 5,
        "speech": 3,
    }
    assert preset.model_dump() in catalog["suppliers"]
    assert all("model" not in p for p in catalog["suppliers"])
    body = {
        "contract_version": 2,
        "mode": "custom",
        "supplier": preset.id,
        "api_key": "private-provider-canary",
        "expected_revision": 0,
    }
    path = f"/api/v1/me/model-services/{preset.kind}"
    rejected = client.put(path, json=body)
    assert (
        rejected.status_code == 422 and rejected.json()["error"]["code"] == "SERVICE_MODEL_REQUIRED"
    )
    body["model"] = "exact-api-model-id"
    saved = client.put(path, json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()["base_url"] == preset.base_url
    assert saved.json()["model"] == "exact-api-model-id"
    assert saved.json()["protocol"] == preset.protocol
    assert "private-provider-canary" not in saved.text
    assert client.put(path, json=body).status_code == 409
    # Returning to server mode never requires a personal model ID.
    assert (
        client.put(
            path,
            json={
                "contract_version": 2,
                "mode": "server",
                "supplier": preset.id,
                "expected_revision": 1,
            },
        ).status_code
        == 200
    )


@pytest.mark.parametrize("kind", ["text", "image", "video", "speech"])
def test_custom_requires_destination_key_and_model(client, settings, kind):
    settings.service_encryption_key = SecretStr(Fernet.generate_key().decode())
    account(client)
    path = f"/api/v1/me/model-services/{kind}"
    body = {
        "contract_version": 2,
        "mode": "custom",
        "supplier": "custom",
        "model": "api-id",
        "api_key": "private-custom-canary",
        "base_url": "https://custom.example/v1",
        "expected_revision": 0,
    }
    for missing in ("model", "base_url", "api_key"):
        rejected = client.put(path, json={k: v for k, v in body.items() if k != missing})
        assert rejected.status_code == 422, rejected.text
    saved = client.put(path, json=body)
    assert saved.status_code == 200, saved.text
    switched = client.put(
        path,
        json={
            **body,
            "expected_revision": 1,
            "base_url": "https://another.example/v1",
            "api_key": None,
        },
    )
    assert switched.status_code == 422
    assert switched.json()["error"]["code"] == "SERVICE_KEY_REENTRY_REQUIRED"


@pytest.mark.parametrize("supplier,protocol", [("qwen", "qwen_chat"), ("google", "gemini")])
def test_personal_native_protocol_is_frozen(client, settings, supplier, protocol):
    settings.service_encryption_key = SecretStr(Fernet.generate_key().decode())
    uid = account(client)
    saved = client.put(
        "/api/v1/me/model-services/text",
        json={
            "contract_version": 2,
            "mode": "custom",
            "supplier": supplier,
            "model": "exact-native-model",
            "api_key": "native-test-key",
            "expected_revision": 0,
        },
    )
    assert saved.status_code == 200, saved.text
    with client.app.state.session_factory() as db:
        snapshot = text_snapshot(db, uid)
        assert snapshot["protocol"] == protocol
        restored = run_settings(settings, snapshot, db)
        assert restored.llm_protocol == protocol
        assert restored.llm_api_key.get_secret_value() == "native-test-key"
        assert (
            db.scalar(select(ModelService).where(ModelService.user_id == UUID(uid))).model
            == "exact-native-model"
        )


def test_speech_model_suggestions_are_v2_only_without_automatic_configuration(client):
    account(client)
    legacy = client.get("/api/v1/me/model-services")
    response = client.get("/api/v1/me/model-services?catalog_version=2")
    assert legacy.status_code == response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert legacy.json()["catalog_version"] == 1
    assert "speech_models" not in legacy.json()
    assert all("model" in row for row in legacy.json()["suppliers"])
    catalog = response.json()
    assert catalog["services"] == []
    assert all("model" not in row for row in catalog["suppliers"])
    models = catalog["speech_models"]
    assert models
    speech_suppliers = {row["id"] for row in catalog["suppliers"] if row["kind"] == "speech"}
    identities = {(row["supplier"], row["model"]) for row in models}
    assert len(identities) == len(models)
    assert identities == {
        ("qwen", "qwen3-tts-flash"),
        ("siliconflow", "FunAudioLLM/CosyVoice2-0.5B"),
        ("siliconflow", "fnlp/MOSS-TTSD-v0.5"),
    }
    deprecated_openai_models = {
        "gpt-4o-mini-tts",
        "gpt-4o-mini-tts-2025-12-15",
        "gpt-4o-mini-tts-2025-03-20",
        "tts-1",
        "tts-1-hd",
    }
    assert deprecated_openai_models.isdisjoint(row["model"] for row in models)
    assert "openai" in speech_suppliers
    assert not any(row["supplier"] == "openai" for row in models)
    for row in models:
        assert set(row) == {"supplier", "model", "name", "description"}
        assert row["supplier"] in speech_suppliers
        assert all(isinstance(value, str) and value.strip() for value in row.values())
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(ModelService)) == 0


@pytest.mark.parametrize("supplier", ["qwen", "siliconflow", "openai"])
def test_speech_model_suggestions_save_exact_ids_and_preserve_unlisted_models_without_calls(
    client, settings, monkeypatch, supplier
):
    def unexpected_call(*args, **kwargs):
        pytest.fail("Reading suggestions and saving model configuration must not call a provider")

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
        unexpected_call()

    monkeypatch.setattr(socket, "getaddrinfo", unexpected_dns)
    monkeypatch.setattr(
        "arclight.providers.native_media.NativeMediaProvider.submit", unexpected_call
    )
    settings.service_encryption_key = SecretStr(Fernet.generate_key().decode())
    uid = account(client)
    response = client.get("/api/v1/me/model-services?catalog_version=2")
    assert response.status_code == 200
    catalog = response.json()
    suggestion = next(
        (row for row in catalog["speech_models"] if row["supplier"] == supplier), None
    )
    if supplier == "openai":
        assert suggestion is None
        # Removing suggestions must not reject an operator-specified existing ID.
        selected_model = "gpt-4o-mini-tts"
    else:
        assert suggestion is not None
        selected_model = suggestion["model"]
    provider = next(
        row for row in catalog["suppliers"] if row["id"] == supplier and row["kind"] == "speech"
    )
    path = "/api/v1/me/model-services/speech"
    payload = {
        "contract_version": 2,
        "mode": "custom",
        "supplier": supplier,
        "model": selected_model,
        "api_key": "private-speech-suggestion-canary",
        "expected_revision": 0,
    }
    saved = client.put(path, json=payload)
    assert saved.status_code == 200, saved.text
    assert saved.json()["model"] == selected_model
    initial_refresh = client.get("/api/v1/me/model-services?catalog_version=2")
    assert initial_refresh.status_code == 200
    assert initial_refresh.json()["services"][0]["model"] == selected_model
    assert saved.json()["protocol"] == provider["protocol"]
    assert saved.json()["base_url"] == provider["base_url"]
    assert saved.json()["has_key"] is True
    with client.app.state.session_factory() as db:
        service = db.scalar(
            select(ModelService).where(
                ModelService.user_id == UUID(uid), ModelService.kind == "speech"
            )
        )
        original_credential = service.credential_id
        assert original_credential is not None
    unlisted_model = f"operator-specific-{supplier}-tts-id"
    assert unlisted_model not in {row["model"] for row in catalog["speech_models"]}
    changed = client.put(
        path,
        json={**payload, "model": unlisted_model, "api_key": None, "expected_revision": 1},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["model"] == unlisted_model
    assert changed.json()["revision"] == 2 and changed.json()["has_key"] is True
    fetched = client.get("/api/v1/me/model-services?catalog_version=2")
    assert fetched.status_code == 200
    assert "private-speech-suggestion-canary" not in fetched.text + saved.text + changed.text
    assert fetched.json()["speech_models"] == catalog["speech_models"]
    assert fetched.json()["services"][0]["model"] == unlisted_model
    assert fetched.json()["services"][0]["key_status"] == "READY"
    with client.app.state.session_factory() as db:
        service = db.scalar(
            select(ModelService).where(
                ModelService.user_id == UUID(uid), ModelService.kind == "speech"
            )
        )
        assert service.credential_id == original_credential
        assert db.scalar(select(func.count()).select_from(GenerationJob)) == 0
        assert db.scalar(select(func.count()).select_from(GatewayCall)) == 0
