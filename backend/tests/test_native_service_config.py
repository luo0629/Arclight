"""Named providers require exact model IDs; custom destinations retain strict isolation."""

from collections import Counter
from uuid import UUID

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import select
from test_model_services import account

from arclight.application.model_services import run_settings, text_snapshot
from arclight.domain.model_suppliers import SUPPLIERS
from arclight.persistence.models import ModelService


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
