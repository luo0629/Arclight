"""A new run is explicit, idempotent, scoped and cannot duplicate an uncertain call."""

from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from test_model_gateway import request as gateway_request
from test_model_gateway import route
from test_model_services import account, profile

from arclight.application.agent_runtime_state import scope_for_run
from arclight.application.model_gateway import invoke
from arclight.application.model_services import run_settings
from arclight.application.runtime import execute_run
from arclight.core.errors import DomainError
from arclight.domain.agent_state import terminate_agent_state
from arclight.domain.model_gateway import GatewayFailure, GatewayPolicy, GatewayUsage
from arclight.persistence.models import WorkflowRun
from arclight.providers.text import MockTextProvider


@pytest.mark.parametrize("entry", ["script-runs", "story-analysis-runs", "story-outline-runs"])
def test_restart_preserves_inputs_freezes_current_key_and_original_retry_stays_old(
    client, settings, monkeypatch, entry
):
    settings.service_encryption_key = SecretStr(Fernet.generate_key().decode())
    account(client)
    pid = client.post("/api/v1/projects", json={"name": "重新发起"}).json()["id"]
    eid = client.post(
        f"/api/v1/projects/{pid}/episodes",
        json={
            "episode_no": 1,
            "title": "一封信",
        },
    ).json()["id"]
    assert (
        client.put(
            "/api/v1/me/model-services/text", json=profile(mode="custom", api_key="original-key")
        ).status_code
        == 200
    )
    started = client.post(
        f"/api/v1/episodes/{eid}/{entry}",
        json={"story": "一封旧信"},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert started.status_code == 202, started.text
    source_id = UUID(started.json()["run_id"])
    with client.app.state.session_factory.begin() as db:
        source = db.get(WorkflowRun, source_id)
        source.status = "FAILED"
        source.agent_state_json = terminate_agent_state(
            source.agent_state_json, scope_for_run(source, UUID(pid)), reason="EXECUTION_FAILED"
        ).model_dump(mode="json")
        original_inputs = deepcopy(source.input_json)
    assert (
        client.put(
            "/api/v1/me/model-services/text",
            json=profile(
                mode="custom", api_key="current-key", model="new-model", expected_revision=1
            ),
        ).status_code
        == 200
    )
    # The existing retry API still retains the same run and private service snapshot.
    retry = client.post(f"/api/v1/runs/{source_id}/retry")
    assert retry.status_code == 202, retry.text
    with client.app.state.session_factory.begin() as db:
        source = db.get(WorkflowRun, source_id)
        assert source.input_json == original_inputs
        assert (
            run_settings(
                settings, source.input_json["_text_service"], db
            ).llm_api_key.get_secret_value()
            == "original-key"
        )
        source.status = "FAILED"
    key = str(uuid4())
    endpoint = f"/api/v1/runs/{source_id}/restart"
    result = client.post(endpoint, headers={"Idempotency-Key": key})
    assert result.status_code == 202, result.text
    identity = UUID(result.json()["id"])
    assert identity != source_id and result.json()["revision"] == 0
    repeated = client.post(endpoint, headers={"Idempotency-Key": key})
    assert repeated.status_code == 202 and repeated.json()["id"] == str(identity)
    with client.app.state.session_factory() as db:
        run = db.get(WorkflowRun, identity)

        def public(data):
            return {k: v for k, v in data.items() if not k.startswith("_") and k != "created_by"}

        assert public(run.input_json) == public(original_inputs)
        assert run.input_json["_restart_from_run_id"] == str(source_id)
        assert db.get(WorkflowRun, source_id).input_json == original_inputs
        assert (
            run_settings(
                settings, run.input_json["_text_service"], db
            ).llm_api_key.get_secret_value()
            == "current-key"
        )
    captured = []

    def provider(config):
        captured.append(config)
        return MockTextProvider()

    for module in ("runtime", "story_analysis", "story_outline"):
        monkeypatch.setattr(f"arclight.application.{module}.build_provider", provider)
    execute_run(client.app.state.session_factory, settings, identity, 0)
    assert captured and all(config.llm_model == "new-model" for config in captured)
    assert all(config.llm_api_key.get_secret_value() == "current-key" for config in captured)
    assert "current-key" not in client.get(f"/api/v1/runs/{identity}").text


@pytest.mark.parametrize("unknown", [False, True])
def test_restart_rechecks_permission_and_rejects_unresolved_original_calls(
    client, episode, settings, unknown
):
    started = client.post(
        f"/api/v1/episodes/{episode['id']}/script-runs",
        json={"story": "synthetic"},
        headers={"Idempotency-Key": str(uuid4())},
    )
    source_id = UUID(started.json()["run_id"])
    selected = route(
        maximum_cost=None,
        capabilities=route().capabilities.model_copy(update={"parameters": frozenset()}),
    )
    call = gateway_request(episode, selected, workflow_id=source_id, task="script", parameters={})

    def fail():
        raise GatewayFailure("PROVIDER_UNAVAILABLE", not_accepted=not unknown)

    with pytest.raises(DomainError):
        invoke(
            client.app.state.session_factory,
            call,
            selected,
            fail,
            policy=GatewayPolicy(),
            usage=lambda _: GatewayUsage(),
        )
    with client.app.state.session_factory.begin() as db:
        db.get(WorkflowRun, source_id).status = "FAILED"
    endpoint = f"/api/v1/runs/{source_id}/restart"
    result = client.post(endpoint, headers={"Idempotency-Key": str(uuid4())})
    assert result.status_code == (409 if unknown else 202), result.text
    if unknown:
        assert result.json()["error"]["code"] == "RESTART_RECONCILIATION_REQUIRED"
    account(client)
    assert client.post(endpoint, headers={"Idempotency-Key": str(uuid4())}).status_code == 404
