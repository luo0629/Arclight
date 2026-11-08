"""Account media bindings and current-config restart span existing professional paths."""

import base64
from copy import deepcopy
from uuid import UUID, uuid4

import httpx
import pytest
from art_concept_support import install_concept_model, start_concepts
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_agent_model import model_service as model_service
from test_art_workflow import drive, progress
from test_asset_versions import image_bytes
from test_generation_budget import set_policy
from test_generation_delivery import deliver
from test_model_services import account
from test_native_media_integration import attempt_for, configure, project, provider_for

from arclight.application.agent_external_waits import queue_ready_external
from arclight.application.agent_runtime_state import scope_for_run
from arclight.application.generation_candidates import import_attempt
from arclight.application.production_versions import text_settings
from arclight.domain.agent_state import terminate_agent_state
from arclight.main import create_app
from arclight.persistence.models import AgentToolGrant, WorkflowRun


def account_episode(client):
    account(client)
    pid = project(client)
    response = client.post(
        f"/api/v1/projects/{pid}/episodes", json={"episode_no": 1, "title": "原生工作流"}
    )
    assert response.status_code == 201, response.text
    return response.json()


def fail_source(client, run_id, project_id):
    with client.app.state.session_factory.begin() as db:
        source = db.get(WorkflowRun, UUID(str(run_id)))
        source.status = "FAILED"
        source.agent_state_json = terminate_agent_state(
            source.agent_state_json,
            scope_for_run(source, UUID(project_id)),
            reason="EXECUTION_FAILED",
        ).model_dump(mode="json")
        return deepcopy(source.input_json)


def restart(client, source_id):
    response = client.post(
        f"/api/v1/runs/{source_id}/restart", headers={"Idempotency-Key": uuid4().hex}
    )
    assert response.status_code == 202, response.text
    return UUID(response.json()["id"])


def test_native_concept_rounds_preserve_original_key_after_rotation_and_process_restart(
    client, settings, model_service
):
    episode = account_episode(client)
    configure(client, settings)
    set_policy(client, episode["project_id"], "20")
    _, errors, _ = install_concept_model(model_service)
    config = client.get(
        f"/api/v1/projects/{episode['project_id']}/generation-jobs/services/image/configuration"
    ).json()
    run_id, _ = start_concepts(
        client,
        episode,
        settings,
        count=1,
        spec_patch={
            "provider": config["provider"],
            "seed": None,
            "parameters": {},
            "model": config["model"],
            "model_config_version": config["configuration_version"],
            "output_spec": {"width": 1024, "height": 1024, "format": "png"},
        },
    )
    assert drive(client, settings, run_id)["status"] == "RUNNING"
    configure(client, settings, revision=1, key="next-task-key")
    sent_keys = []
    data = image_bytes("PNG", (1024, 1024))

    def handler(request):
        sent_keys.append(request.headers["authorization"])
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(data).decode()}]})

    for round_no in (1, 2):
        current = progress(client, run_id)["concept_rounds"][-1]
        assert current["round_no"] == round_no, (current, errors)
        job = {"id": current["job_id"]}
        provider = provider_for(client, settings, job, handler)
        assert deliver(client, job, provider) == "SUCCEEDED"
        assert (
            import_attempt(
                client.app.state.session_factory, settings, attempt_for(client, job), provider
            )
            == 1
        )
        assert queue_ready_external(client.app.state.session_factory, settings) == 1
        with TestClient(create_app(settings)) as recovered:
            recovered.cookies.update(client.cookies)
            recovered.headers.update(client.headers)
            result = drive(recovered, settings, run_id)
        assert result["status"] == ("RUNNING" if round_no == 1 else "WAITING_REVIEW"), (
            result,
            errors,
        )
    assert not errors
    assert sent_keys == ["Bearer first-key", "Bearer first-key"]
    assert len(progress(client, run_id)["concept_rounds"]) == 2
    assert "credential_id" not in client.get(f"/api/v1/runs/{run_id}").text


def test_concept_restart_captures_current_media_configuration_and_new_grant(
    client, settings, model_service
):
    episode = account_episode(client)
    configure(client, settings)
    set_policy(client, episode["project_id"], "20")
    config = client.get(
        f"/api/v1/projects/{episode['project_id']}/generation-jobs/services/image/configuration"
    ).json()
    original_id, _ = start_concepts(
        client,
        episode,
        settings,
        count=1,
        spec_patch={
            "provider": config["provider"],
            "seed": None,
            "parameters": {},
            "model": config["model"],
            "model_config_version": config["configuration_version"],
            "output_spec": {"width": 1024, "height": 1024, "format": "png"},
        },
    )
    before = fail_source(client, original_id, episode["project_id"])
    configure(client, settings, revision=1, key="restarted-media-key")
    current_id = restart(client, original_id)
    with client.app.state.session_factory() as db:
        current = db.get(WorkflowRun, current_id)
        assert db.get(WorkflowRun, UUID(original_id)).input_json == before
        assert (
            current.input_json["_image_service"]["credential_id"]
            != before["_image_service"]["credential_id"]
        )
        assert current.input_json["art_direction"]["task"] == before["art_direction"]["task"]
        assert (
            current.input_json["art_direction"]["concept"]["plan"]["spec"]["model_config_version"]
            != config["configuration_version"]
        )
        grant = db.scalar(select(AgentToolGrant).where(AgentToolGrant.run_id == current_id))
        assert grant is not None and "request_art_concepts" in grant.allowed_tools_json
    _, errors, _ = install_concept_model(model_service)
    assert drive(client, settings, str(current_id))["status"] == "RUNNING", errors
    job_id = progress(client, str(current_id))["concept_rounds"][0]["job_id"]
    selected = provider_for(client, settings, {"id": job_id}, lambda request: httpx.Response(200))
    assert selected.connection.api_key == "restarted-media-key"


@pytest.mark.parametrize("kind", ["screenwriter", "director", "art_director"])
def test_professional_restart_executes_current_exact_model_with_fresh_runtime(
    client, episode, settings, model_service, monkeypatch, kind
):
    if kind == "screenwriter":
        from test_screenwriter_creation import install, start

        sent, errors = install(model_service, monkeypatch, settings)
        original_id = start(client, episode)
    elif kind == "director":
        from test_director_creation import install, start

        sent, errors = install(model_service, monkeypatch, settings)
        original_id, _ = start(client, episode)
    else:
        from art_direction_support import install_direction_model, reference_set
        from test_art_workflow import start_art

        reference_set(client, episode)
        sent, errors, _ = install_direction_model(model_service)
        original_id, _, _ = start_art(client, episode, settings)
    before = fail_source(client, original_id, episode["project_id"])
    # A changed deployment model must also capture its matching capability declaration.
    current_model = "current-professional-model"
    settings.llm_model = current_model
    settings.llm_agent_capabilities = settings.llm_agent_capabilities.model_copy(
        update={"model": current_model}
    )
    current_id = restart(client, original_id)
    with client.app.state.session_factory() as db:
        current = db.get(WorkflowRun, current_id)
        frozen = text_settings(db, UUID(episode["project_id"]), current, settings)
        assert frozen.llm_model == current_model
        assert db.get(WorkflowRun, UUID(str(original_id))).input_json == before
        grant = db.scalar(select(AgentToolGrant).where(AgentToolGrant.run_id == current_id))
        assert grant is not None and grant.agent_name == kind
    result = drive(client, settings, str(current_id))
    assert result["status"] == "WAITING_REVIEW", (result, errors)
    assert sent and all(payload["model"] == current_model for payload in sent)
    assert not errors


@pytest.mark.parametrize("kind", ["storyboard", "rewrite"])
def test_restart_keeps_exact_source_and_selection_for_existing_text_paths(
    client, episode, settings, kind
):
    if kind == "storyboard":
        from test_director_workflow import script

        source = script(client, f"/api/v1/episodes/{episode['id']}")
        response = client.post(
            f"/api/v1/episodes/{episode['id']}/storyboard-runs",
            json={"script_version_id": source["id"]},
            headers={"Idempotency-Key": uuid4().hex},
        )
    else:
        from test_script_rewrite import body, source, start

        response = start(client, episode, body(source(client, episode)))
    assert response.status_code == 202, response.text
    source_id = response.json()["run_id"]
    before = fail_source(client, source_id, episode["project_id"])
    current_id = restart(client, source_id)
    assert drive(client, settings, str(current_id))["status"] == "WAITING_REVIEW"
    with client.app.state.session_factory() as db:
        current = db.get(WorkflowRun, current_id)
        field = "script_version_id" if kind == "storyboard" else "rewrite_input"
        assert current.input_json[field] == before[field]
        assert db.get(WorkflowRun, UUID(source_id)).input_json == before
