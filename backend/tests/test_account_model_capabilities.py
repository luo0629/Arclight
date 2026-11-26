"""Account model capability verification uses exact routes and immutable run settings."""

from uuid import UUID

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import select
from test_collaboration import create_episode, create_project
from test_model_services import account
from test_story_analysis import start

from arclight.api import model_services
from arclight.application.production_versions import text_settings
from arclight.domain.agent_model import AgentModelCapabilities
from arclight.domain.model_gateway import GatewayFailure
from arclight.persistence.models import ModelService, WorkflowRun

PATH = "/api/v1/me/model-services/text"
CHECK = PATH + "/capabilities"


@pytest.fixture
def connected_account(client, settings):
    settings.provider = "account"
    settings.service_encryption_key = SecretStr(Fernet.generate_key().decode())
    settings.service_model_policies = []
    return account(client)


def save_custom(client, **changes):
    return client.put(
        PATH,
        json={
            "supplier": "custom",
            "base_url": "https://llm.example/v1",
            "model": "exact-custom-model",
            "api_key": "synthetic-account-token",
            "expected_revision": 0,
        }
        | changes,
    )


def check(client, revision=1):
    return client.post(CHECK, json={"expected_revision": revision, "confirm_charges": True})


def stored_response_format(client, uid):
    with client.app.state.session_factory() as db:
        return db.scalar(
            select(ModelService.response_format).where(
                ModelService.user_id == UUID(uid), ModelService.kind == "text"
            )
        )


def test_catalog_routes_work_with_empty_deployment_policy(client, connected_account, settings):
    saved = client.put(
        PATH,
        json={
            "supplier": "openai",
            "model": "gpt-5.4-mini",
            "api_key": "synthetic-token",
            "expected_revision": 0,
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["capability_check"] == {
        "status": "ready",
        "tools": True,
        "max_input_images": 8,
        "can_check": False,
    }
    project = create_project(client)
    episode = create_episode(client, project)
    started = start(client, episode)
    assert started.status_code == 202, started.text
    with client.app.state.session_factory() as db:
        run = db.get(WorkflowRun, UUID(started.json()["run_id"]))
        frozen = text_settings(db, UUID(project["id"]), run, settings)
        assert frozen.llm_response_format == "json_schema"
        assert frozen.llm_agent_capabilities.model == "gpt-5.4-mini"
        assert frozen.llm_agent_capabilities.max_input_images == 8
    other = save_custom(client, model="gpt-5.4-mini", expected_revision=1)
    assert other.status_code == 200, other.text
    assert other.json()["capability_check"]["status"] == "unverified"


def test_only_probed_capabilities_are_saved_and_frozen(
    client, connected_account, settings, monkeypatch
):
    declaration = AgentModelCapabilities(
        version="synthetic-test/1",
        supplier="custom",
        model="exact-custom-model",
        tools=True,
        response_formats={"json_object"},
    )
    calls = []

    def probe(route, identity):
        calls.append((route.llm_model, identity[1]))
        assert route.llm_base_url == "https://llm.example/v1"
        assert route.llm_api_key.get_secret_value() == "synthetic-account-token"
        return declaration

    monkeypatch.setattr(model_services, "probe_model_capabilities", probe)
    saved = save_custom(client)
    assert saved.status_code == 200, saved.text
    assert saved.json()["capability_check"]["status"] == "unverified"
    assert client.post(CHECK, json={"expected_revision": 1}).status_code == 422
    assert (
        client.post(CHECK, json={"expected_revision": 1, "confirm_charges": False}).status_code
        == 422
    )
    assert not calls
    verified = check(client)
    assert verified.status_code == 200, verified.text
    profile = verified.json()
    assert profile["revision"] == 2 and "response_format" not in profile
    assert stored_response_format(client, connected_account) == "json_object"
    assert profile["key_status"] == "READY"
    assert profile["capability_check"] == {
        "status": "partial",
        "tools": True,
        "max_input_images": 0,
        "can_check": False,
    }
    assert "synthetic-account-token" not in verified.text
    assert check(client).status_code == 409
    assert check(client, 2).status_code == 200
    assert len(calls) == 1
    listed = client.get("/api/v1/me/model-services").json()["services"]
    assert listed == [profile]

    project = create_project(client)
    episode = create_episode(client, project)
    started = start(client, episode)
    assert started.status_code == 202, started.text
    updated = save_custom(client, expected_revision=2, api_key="changed-account-token")
    assert updated.status_code == 200, updated.text
    assert updated.json()["capability_check"]["status"] == "unverified"
    assert stored_response_format(client, connected_account) == "json_object"
    with client.app.state.session_factory() as db:
        run = db.get(WorkflowRun, UUID(started.json()["run_id"]))
        prior = text_settings(db, UUID(project["id"]), run, settings)
        assert prior.llm_response_format == "json_object"
        assert prior.llm_agent_capabilities == declaration


def test_provider_failures_do_not_change_the_profile(client, connected_account, monkeypatch):
    assert save_custom(client).status_code == 200

    def probe(*_):
        raise GatewayFailure("PROVIDER_INSUFFICIENT_BALANCE", not_accepted=True)

    monkeypatch.setattr(model_services, "probe_model_capabilities", probe)
    failure = check(client)
    assert failure.status_code == 402
    assert failure.json()["error"]["code"] == "ACCOUNT_BALANCE_INSUFFICIENT"
    with client.app.state.session_factory() as db:
        row = db.scalar(select(ModelService).where(ModelService.user_id == UUID(connected_account)))
        assert row.revision == 1 and row.verified_capabilities is None
        assert row.capability_probe_id is None


def test_same_configuration_verifies_once_at_a_time(client, connected_account, monkeypatch):
    assert save_custom(client).status_code == 200
    calls = 0

    def probe(route, identity):
        nonlocal calls
        calls += 1
        if calls == 1:
            duplicate = check(client)
            assert duplicate.status_code == 409, duplicate.text
            assert duplicate.json()["error"]["code"] == "SERVICE_CAPABILITY_CHECK_RUNNING"
        return AgentModelCapabilities(
            version="synthetic-test/1",
            supplier=route.llm_supplier,
            model=route.llm_model,
            response_formats={"json_schema"},
        )

    monkeypatch.setattr(model_services, "probe_model_capabilities", probe)
    checked = check(client)
    assert checked.status_code == 200, checked.text
    assert calls == 1


def test_renaming_an_unverified_service_preserves_stored_response_format(client, connected_account):
    assert save_custom(client).status_code == 200
    with client.app.state.session_factory.begin() as db:
        row = db.scalar(select(ModelService).where(ModelService.user_id == UUID(connected_account)))
        row.response_format = "json_object"

    renamed = save_custom(client, supplier_name="New label", expected_revision=1, api_key=None)
    assert renamed.status_code == 200, renamed.text
    assert stored_response_format(client, connected_account) == "json_object"

    changed = save_custom(client, model="another-model", expected_revision=2, api_key=None)
    assert changed.status_code == 200, changed.text
    assert stored_response_format(client, connected_account) == "json_schema"


def test_switching_protocol_rechecks_the_key_and_capabilities(client, connected_account):
    assert save_custom(client).status_code == 200
    with client.app.state.session_factory.begin() as db:
        row = db.scalar(select(ModelService).where(ModelService.user_id == UUID(connected_account)))
        assert row.protocol == "openai_chat"
        row.verified_capabilities = AgentModelCapabilities(
            version="synthetic-test/1",
            supplier="custom",
            model=row.model,
            response_formats={"json_schema"},
        ).model_dump(mode="json")

    rejected = save_custom(client, protocol="gemini", expected_revision=1, api_key=None)
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "SERVICE_KEY_REENTRY_REQUIRED"
    updated = save_custom(client, protocol="gemini", expected_revision=1, api_key="new-test-key")
    assert updated.status_code == 200, updated.text
    assert updated.json()["capability_check"]["status"] == "unverified"


def test_verification_cannot_apply_to_a_changed_route(client, connected_account, monkeypatch):
    assert save_custom(client).status_code == 200

    def probe(route, identity):
        result = save_custom(client, model="replacement-model", expected_revision=1, api_key=None)
        assert result.status_code == 200, result.text
        return AgentModelCapabilities(
            version="synthetic-test/1",
            supplier=route.llm_supplier,
            model=route.llm_model,
            response_formats={"json_schema"},
        )

    monkeypatch.setattr(model_services, "probe_model_capabilities", probe)
    rejected = check(client)
    assert rejected.status_code == 409, rejected.text
    profile = client.get("/api/v1/me/model-services").json()["services"][0]
    assert profile["model"] == "replacement-model"
    assert profile["capability_check"]["status"] == "unverified"
