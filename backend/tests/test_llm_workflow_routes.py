"""A saved model card is only accepted when its frozen settings reach the Worker."""

import json
from uuid import UUID

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import func, select
from test_collaboration import create_episode, create_project
from test_llm_service import PROTOCOLS, response
from test_model_services import account
from test_story_analysis import content, execute, path, start

from arclight.application import model_services, story_analysis
from arclight.application.service_credentials import rotate_credentials
from arclight.domain.agent_model import AgentModelCapabilities
from arclight.domain.model_suppliers import SUPPLIERS
from arclight.domain.service_models import ServiceModelPolicy
from arclight.persistence.model_gateway import GatewayCall, GatewayReceipt
from arclight.persistence.models import OutboxEvent, StorySource, WorkflowRun

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
        assert "maxOutputTokens" not in config
        assert config["responseMimeType"] == "application/json"
        assert ("responseJsonSchema" in config) == (output_format == "json_schema")
    else:
        assert req.headers["authorization"] == "Bearer " + key
        assert body["model"] == model
        if protocol == "qwen_chat":
            assert "max_tokens" not in body["parameters"]
            assert body["parameters"]["response_format"] == {"type": "json_object"}
            assert body["parameters"]["enable_thinking"] is False
            first = body["input"]["messages"][0]
            assert first["role"] == "system"
            assert first["content"].startswith("JSON Schema:\n") == (output_format == "json_schema")
        else:
            assert body["response_format"]["type"] == output_format
            if output_format == "json_schema":
                assert body["response_format"]["json_schema"]["name"] == "StoryAnalysisContent"
            assert "max_completion_tokens" not in body
            assert "max_tokens" not in body
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
    profile = {"supplier": supplier}
    if supplier == "custom":
        profile.update(base_url=endpoint, protocol=protocol)
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
        profile.update(base_url=new_endpoint)
        settings.service_model_policies = [
            ServiceModelPolicy(
                kind="text",
                supplier=supplier,
                protocol=protocol,
                base_url=new_endpoint,
                model=new_model,
                agent_capabilities=AgentModelCapabilities(
                    version="test/1",
                    supplier=supplier,
                    model=new_model,
                    response_formats={new_format},
                ),
            )
        ]
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


def test_clearing_account_key_preserves_existing_real_and_mock_runs(client, settings, stub_llm):
    settings.provider = "account"
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
        service_path,
        json={"supplier": "openai", "model": model, "clear_key": True, "expected_revision": 1},
    )
    assert saved.status_code == 200 and saved.json()["has_key"] is False, saved.text
    blocked = start(client, second)
    assert blocked.status_code == 422 and blocked.json()["error"]["code"] == "SERVICE_INCOMPLETE"
    settings.provider = "mock"
    planned = start(client, second)
    assert planned.status_code == 202 and not sent, planned.text
    mock_run = planned.json()["run_id"]
    settings.provider = "account"

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


@pytest.mark.parametrize("provider", ["account", "openai_compatible"])
def test_missing_account_service_cannot_queue_or_dispatch_text(
    client, settings, stub_llm, provider
):
    settings.provider = provider
    settings.llm_api_key = SecretStr("synthetic-retired-deployment-key")
    stub_llm(lambda _: pytest.fail("Unconfigured account reached deployment model"))
    account(client)
    episode = create_episode(client, create_project(client))

    for entry in ("script-runs", "story-analysis-runs", "story-outline-runs"):
        response = client.post(
            path(episode) + "/" + entry,
            json={"story": "虚构的雨夜故事"},
            headers={"Idempotency-Key": entry},
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "SERVICE_INCOMPLETE"

    with client.app.state.session_factory() as db:
        for model in (WorkflowRun, OutboxEvent, GatewayCall, StorySource):
            assert db.scalar(select(func.count()).select_from(model)) == 0
    assert not any(item.is_file() for item in settings.asset_storage_path.rglob("*"))


def test_account_agent_capabilities_require_an_exact_policy(settings):
    settings.provider = "account"
    configuration = {
        "supplier": settings.llm_supplier,
        "base_url": settings.llm_base_url,
        "model": settings.llm_model,
        "protocol": settings.llm_protocol,
    }
    capabilities = AgentModelCapabilities(
        version="fixture:1",
        supplier=settings.llm_supplier,
        model=settings.llm_model,
        tools=True,
        max_input_images=2,
        response_formats=frozenset({"json_schema"}),
    )
    settings.llm_agent_capabilities = capabilities
    assert model_services.agent_capabilities(settings, configuration) is None

    settings.service_model_policies = [
        ServiceModelPolicy(kind="text", **configuration, agent_capabilities=capabilities)
    ]
    assert model_services.agent_capabilities(settings, configuration) == capabilities
    assert model_services.agent_capabilities(settings, configuration | {"model": "other"}) is None


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
    sent = []

    def respond(request):
        sent.append(request)
        return httpx.Response(200, json=response("openai_chat", json.dumps(content())))

    stub_llm(respond)
    # Seed the original input before the workflow and its provenance are frozen.
    with monkeypatch.context() as historic_capture:
        historic_capture.setattr(story_analysis, "require_text_snapshot", lambda *_: snapshot)
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
