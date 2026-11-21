"""A saved model card is only accepted when its frozen settings reach the Worker."""

import json
from uuid import UUID

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import select
from test_collaboration import create_episode, create_project
from test_llm_service import PROTOCOLS, response
from test_model_services import account
from test_story_analysis import content, execute, path, start

from arclight.application import story_analysis
from arclight.application.service_credentials import rotate_credentials
from arclight.domain.model_suppliers import SUPPLIERS
from arclight.persistence.model_gateway import GatewayCall, GatewayReceipt
from arclight.persistence.models import WorkflowRun

TEXT_PRESETS = [preset for preset in SUPPLIERS if preset.kind == "text"]
ROUTES = [
    (preset.id, preset.protocol, preset.base_url, preset.response_format) for preset in TEXT_PRESETS
] + [
    ("custom", protocol, "https://custom.example/user/prefix", "json_schema")
    for protocol in PROTOCOLS
]


def assert_request(req, protocol, endpoint, model, key, supplier, *, output_format="json_schema"):
    suffix = {
        "openai_chat": "/chat/completions",
        "gemini": f"/models/{model.removeprefix('models/')}:generateContent",
        "qwen_chat": "/services/aigc/text-generation/generation",
    }[protocol]
    assert str(req.url) == endpoint.rstrip("/") + suffix
    body = json.loads(req.content)
    if protocol == "gemini":
        assert req.headers["x-goog-api-key"] == key
        config = body["generationConfig"]
        assert config["responseMimeType"] == "application/json"
        assert ("responseJsonSchema" in config) == (output_format == "json_schema")
    else:
        assert req.headers["authorization"] == "Bearer " + key
        assert body["model"] == model
        if protocol == "qwen_chat":
            assert body["parameters"]["response_format"] == {"type": "json_object"}
            assert body["parameters"]["enable_thinking"] is False
            first = body["input"]["messages"][0]
            assert first["role"] == "system"
            assert first["content"].startswith("JSON Schema:\n") == (output_format == "json_schema")
        else:
            assert body["response_format"]["type"] == output_format
            if output_format == "json_schema":
                assert body["response_format"]["json_schema"]["name"] == "StoryAnalysisContent"
            assert ("max_completion_tokens" in body) == (supplier == "openai")
            assert ("max_tokens" in body) == (supplier != "openai")
            if supplier == "deepseek":
                assert body["thinking"] == {"type": "disabled"}


def assert_result(client, episode, run_id, model, source):
    state = client.get(f"/api/v1/runs/{run_id}").json()
    assert state.get("status") == "SUCCEEDED", state
    records = client.get(path(episode) + "/story-analyses").json()
    assert len(records) == 1
    detail = client.get(path(episode) + f"/story-analyses/{records[0]['id']}").json()
    assert detail["content"] == content()
    with client.app.state.session_factory() as db:
        call = db.execute(
            select(GatewayCall).where(GatewayCall.workflow_id == UUID(run_id))
        ).scalar_one()
        assert call.task == "analyze" and call.operation_key.startswith(f"text:{run_id}:")
        assert call.route_json["model"] == model and call.route_json["credential_source"] == source
        receipt = db.scalars(select(GatewayReceipt).where(GatewayReceipt.call_id == call.id)).one()
        assert receipt.status == "SUCCEEDED"
        assert receipt.usage_json["input_tokens"] == 11 and receipt.usage_json["output_tokens"] == 5
        assert receipt.usage_json["actual_cost"] is None


@pytest.mark.parametrize("supplier,protocol,endpoint,output_format", ROUTES)
def test_account_cards_dispatch_exact_old_and_new_snapshots(
    client, settings, stub_llm, supplier, protocol, endpoint, output_format
):
    account(client)
    settings.service_encryption_key = SecretStr(Fernet.generate_key().decode())
    project = create_project(client)
    first = create_episode(client, project)
    second = create_episode(client, create_project(client))
    model = "models/exact-text-v1" if protocol == "gemini" else "exact-text-v1"
    key = "synthetic-personal-key-v1"
    profile = {"mode": "custom", "supplier": supplier}
    if supplier == "custom":
        profile.update(base_url=endpoint, protocol=protocol, response_format=output_format)
    sent = []

    def respond(request):
        sent.append(request)
        return httpx.Response(
            200, json=response(protocol, json.dumps(content(), ensure_ascii=False))
        )

    stub_llm(respond)
    service_path = "/api/v1/me/model-services/text"
    saved = client.put(
        service_path,
        json={
            **profile,
            "model": model,
            "api_key": key,
            "expected_revision": 0,
        },
    )
    assert saved.status_code == 200, saved.text
    planned = start(client, first)
    assert planned.status_code == 202, planned.text
    old_run = planned.json()["run_id"]
    new_model, new_key = "exact-text-v2", "synthetic-personal-key-v2"
    new_endpoint, new_format = endpoint, output_format
    if supplier == "custom":
        new_endpoint, new_format = "https://other.example/updated/v1", "json_object"
        profile.update(base_url=new_endpoint, response_format=new_format)
    saved = client.put(
        service_path,
        json={
            **profile,
            "model": new_model,
            "api_key": new_key,
            "expected_revision": 1,
        },
    )
    assert saved.status_code == 200, saved.text
    planned = start(client, second)
    assert planned.status_code == 202, planned.text
    new_run = planned.json()["run_id"]
    assert not sent
    with client.app.state.session_factory() as db:
        frozen = db.get(WorkflowRun, UUID(old_run)).input_json["_text_service"]
        assert frozen["protocol"] == protocol and frozen["model"] == model
        assert frozen["base_url"] == endpoint and frozen["credential_id"]
        assert frozen["response_format"] == output_format
    execute(client, settings, old_run)
    execute(client, settings, new_run)
    assert len(sent) == 2
    assert_request(sent[0], protocol, endpoint, model, key, supplier, output_format=output_format)
    assert_request(
        sent[1], protocol, new_endpoint, new_model, new_key, supplier, output_format=new_format
    )
    assert_result(client, first, old_run, model, "ACCOUNT_SNAPSHOT")
    assert_result(client, second, new_run, new_model, "ACCOUNT_SNAPSHOT")


def test_account_configuration_remains_frozen_after_switch_to_mock(client, settings, stub_llm):
    account(client)
    settings.service_encryption_key = SecretStr(Fernet.generate_key().decode())
    first = create_episode(client, create_project(client))
    second = create_episode(client, create_project(client))
    sent = []

    def respond(request):
        sent.append(request)
        return httpx.Response(200, json=response("openai_chat", json.dumps(content())))

    stub_llm(respond)
    key = "synthetic-account-model-key"
    model = "explicit-model-id"
    service_path = "/api/v1/me/model-services/text"
    saved = client.put(
        service_path,
        json={
            "mode": "custom",
            "supplier": "openai",
            "api_key": key,
            "model": model,
            "expected_revision": 0,
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["model"] == model
    planned = start(client, first)
    assert planned.status_code == 202, planned.text
    old_run = planned.json()["run_id"]
    saved = client.put(
        service_path, json={"mode": "server", "clear_key": True, "expected_revision": 1}
    )
    assert saved.status_code == 200, saved.text
    planned = start(client, second)
    assert planned.status_code == 202 and not sent, planned.text
    mock_run = planned.json()["run_id"]

    execute(client, settings, old_run)
    execute(client, settings, mock_run)
    (request,) = sent
    endpoint = next(preset.base_url for preset in TEXT_PRESETS if preset.id == "openai")
    assert_request(request, "openai_chat", endpoint, model, key, "openai")
    assert_result(client, first, old_run, model, "ACCOUNT_SNAPSHOT")
    assert client.get(f"/api/v1/runs/{mock_run}").json()["status"] == "SUCCEEDED"
    with client.app.state.session_factory() as db:
        call = db.scalars(
            select(GatewayCall).where(GatewayCall.workflow_id == UUID(mock_run))
        ).one()
        assert call.route_json["provider"] == "mock"
        assert call.route_json["credential_source"] == "NONE"
        receipt = db.scalars(select(GatewayReceipt).where(GatewayReceipt.call_id == call.id)).one()
        assert receipt.status == "SUCCEEDED" and receipt.actual_cost == 0


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_server_default_dispatches_through_the_same_factory(
    client, episode, settings, stub_llm, protocol
):
    settings.provider = "openai_compatible"
    settings.llm_protocol = protocol
    settings.llm_base_url = "https://server.example/model-proxy/v1"
    settings.llm_model = "exact-default-model"
    settings.llm_api_key = SecretStr("synthetic-server-credential")
    sent = []

    def respond(request):
        sent.append(request)
        return httpx.Response(
            200, json=response(protocol, json.dumps(content(), ensure_ascii=False))
        )

    stub_llm(respond)
    planned = start(client, episode)
    assert planned.status_code == 202 and not sent
    run_id = planned.json()["run_id"]
    execute(client, settings, run_id)
    assert len(sent) == 1
    assert_request(
        sent[0],
        protocol,
        settings.llm_base_url,
        settings.llm_model,
        settings.llm_api_key.get_secret_value(),
        settings.llm_supplier,
    )
    assert_result(client, episode, run_id, settings.llm_model, "SERVER")


def test_legacy_encrypted_snapshot_dispatches_after_original_key_is_retired(
    client, episode, settings, stub_llm, monkeypatch
):
    old_key = Fernet.generate_key()
    encryption = Fernet(old_key)
    settings.service_encryption_key = SecretStr(old_key.decode())
    key = "synthetic-legacy-credential"
    snapshot = {
        "base_url": "https://legacy.example/frozen/v1",
        "model": "legacy-exact-model",
        "encrypted_key": encryption.encrypt(key.encode()).decode(),
    }
    monkeypatch.setattr(story_analysis, "text_snapshot", lambda *_: snapshot)
    sent = []

    def respond(request):
        sent.append(request)
        return httpx.Response(200, json=response("openai_chat", json.dumps(content())))

    stub_llm(respond)
    planned = start(client, episode)
    assert planned.status_code == 202, planned.text
    run_id = planned.json()["run_id"]
    rotated = settings.model_copy(
        update={
            "service_encryption_key": SecretStr(Fernet.generate_key().decode()),
            "service_encryption_previous_keys": [settings.service_encryption_key],
        }
    )
    with client.app.state.session_factory.begin() as db:
        rotate_credentials(db, rotated, "fixture-rotation")
    retired = rotated.model_copy(update={"service_encryption_previous_keys": []})
    execute(client, retired, run_id)
    assert len(sent) == 1
    assert_request(sent[0], "openai_chat", snapshot["base_url"], snapshot["model"], key, "custom")
    assert_result(client, episode, run_id, snapshot["model"], "ACCOUNT_SNAPSHOT")
    with client.app.state.session_factory() as db:
        assert db.get(WorkflowRun, UUID(run_id)).input_json["_text_service"] == snapshot
