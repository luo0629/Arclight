"""Version, exact assets, authority and shared limits across the actual director entry points."""

import json
from uuid import UUID

import pytest
from sqlalchemy import func, select
from test_agent_model import model_service as model_service
from test_collaboration import create_episode, create_project, register
from test_director_creation import approved_script, install, progress, start
from test_director_creation_recovery import answer_body, endpoint
from test_screenwriter_creation import approve, run_once
from test_screenwriter_creation import start as start_writer
from test_text_contract_v2 import make_asset

from arclight.application.runtime import execute_run
from arclight.domain.versions import digest
from arclight.persistence.models import (
    AssetVersion,
    Project,
    ProjectMember,
    Script,
    Storyboard,
    User,
    WorkflowRun,
)


def test_valid_exact_asset_bindings_and_lines_survive_all_scenes(
    client, episode, settings, model_service, monkeypatch
):
    with client.app.state.session_factory.begin() as db:
        refs = {
            kind: make_asset(db, episode["project_id"], kind)
            for kind in ("CHARACTER", "SCENE", "PROP")
        }

    def configure(content):
        content["character_assets"] = [{"character": "主角", "asset": refs["CHARACTER"]}]
        for scene in content["scenes"]:
            scene["scene_asset"] = refs["SCENE"]
            scene["props"] = [refs["PROP"]]

    source = approved_script(client, episode, configure=configure)
    sent, errors = install(model_service, monkeypatch, settings)
    run_id, _ = start(client, episode, source)
    run = run_once(client, settings, run_id)
    assert run["status"] == "WAITING_REVIEW", (run, errors)
    with client.app.state.session_factory() as db:
        board = db.scalar(select(Storyboard).where(Storyboard.run_id == run_id))
        shots = board.content_json["shots"]
        for scene in source["content_json"]["scenes"]:
            selected = [shot for shot in shots if shot["scene_id"] == scene["scene_id"]]
            assert set(line for shot in selected for line in shot["dialogue_line_ids"]) == {
                line["line_id"] for line in scene["dialogue"]
            }
            assert all(
                shot["scene_asset"] == refs["SCENE"] and shot["props"] == [refs["PROP"]]
                for shot in selected
            )
            assert all(
                shot["character_details"][0]["asset"] == refs["CHARACTER"] for shot in selected
            )
    assert len(sent) == 5


@pytest.mark.parametrize(
    "change", ["disabled", "archived", "revoked", "cancelled", "input", "stale"]
)
def test_changes_during_model_io_cannot_publish_candidate(
    client, settings, model_service, monkeypatch, change
):
    owner = register(client, "director-owner@example.test")
    project = create_project(client)
    episode = create_episode(client, project)
    run_ids, sources = [], []

    def on_http(payload):
        seed = json.loads(payload["messages"][1]["content"])["data"]
        if seed["phase"] != "DIRECT_SCENE_ONLY" or seed["scene"]["scene_no"] != 2:
            return
        with client.app.state.session_factory.begin() as db:
            run = db.get(WorkflowRun, run_ids[0])
            if change == "disabled":
                db.get(User, UUID(owner["user"]["id"])).disabled = True
            elif change == "archived":
                db.get(Project, UUID(project["id"])).archived = True
            elif change == "revoked":
                db.get(Project, UUID(project["id"])).owner_id = "service-owner"
            elif change == "cancelled":
                run.status = "CANCELLED"
            elif change == "input":
                run.input_json = run.input_json | {"target_duration_ms": 60000}
            else:
                db.get(AssetVersion, UUID(sources[0]["id"])).stale = True

    _, errors = install(model_service, monkeypatch, settings, on_http=on_http)
    run_id, source = start(client, episode)
    run_ids.append(run_id)
    sources.append(source)
    execute_run(client.app.state.session_factory, settings, run_id, 0)
    with client.app.state.session_factory() as db:
        assert db.get(WorkflowRun, run_id).status in {"FAILED", "CANCELLED"}, errors
        assert db.scalar(select(func.count()).select_from(Storyboard)) == 0
        assert db.get(Script, UUID(source["id"])).status == "APPROVED"


@pytest.mark.parametrize("case", ["foreign", "viewer", "stale_question"])
def test_working_progress_and_answers_recheck_authority_and_exact_question(
    client, settings, model_service, monkeypatch, case
):
    owner = register(client, "director-question-owner@example.test")
    project = create_project(client)
    episode = create_episode(client, project)
    install(model_service, monkeypatch, settings, human=2)
    run_id, _ = start(client, episode)
    run_once(client, settings, run_id)
    question = progress(client, run_id)["operations"][-1]
    body = answer_body(question)
    if case == "stale_question":
        body["input_sha256"] = "0" * 64
        expected = 409
    else:
        other = register(client, "director-question-other@example.test")
        if case == "viewer":
            with client.app.state.session_factory.begin() as db:
                db.add(
                    ProjectMember(
                        project_id=UUID(project["id"]),
                        user_id=UUID(other["user"]["id"]),
                        role="viewer",
                    )
                )
            assert client.get(f"/api/v1/runs/{run_id}/director").status_code == 200
            expected = 403
        else:
            assert client.get(f"/api/v1/runs/{run_id}/director").status_code == 404
            expected = 404
    assert client.post(endpoint(run_id, question), json=body).status_code == expected
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    assert progress(client, run_id)["can_cancel"]


@pytest.mark.parametrize("combined", [False, True])
def test_model_quota_cannot_reset_on_director_handoff_or_retry(
    client, episode, settings, model_service, monkeypatch, combined
):
    sent, errors = install(model_service, monkeypatch, settings)
    if combined:
        run_id = start_writer(
            client, episode, professional_director=True, agent_limits={"max_model_calls": 5}
        )
        run = run_once(client, settings, run_id)
        assert run["status"] == "WAITING_REVIEW", (run, errors)
        approve(client, run)
    else:
        run_id, _ = start(client, episode, agent_limits={"max_model_calls": 3})
    run = run_once(client, settings, run_id)
    assert run["status"] == "FAILED" and run["error_code"] == "AGENT_MODEL_CALL_LIMIT", (
        run,
        errors,
    )
    count = len(sent)
    assert client.post(f"/api/v1/runs/{run_id}/retry").status_code == 202
    assert run_once(client, settings, run_id)["status"] == "FAILED"
    assert len(sent) == count
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Storyboard)) == 0


def test_registered_director_definition_changes_stop_before_more_http(
    client, episode, settings, model_service, monkeypatch
):
    from arclight.domain import director_creation

    sent, errors = install(model_service, monkeypatch, settings, human=2)
    run_id, _ = start(client, episode)
    run_once(client, settings, run_id)
    question = progress(client, run_id)["operations"][-1]
    assert client.post(endpoint(run_id, question), json=answer_body(question)).status_code == 202
    monkeypatch.setattr(
        director_creation, "FRAGMENT_PROMPT", director_creation.FRAGMENT_PROMPT + " changed"
    )
    run = run_once(client, settings, run_id)
    assert run["status"] == "FAILED" and run["error_code"] == "PRODUCTION_VERSION_INCOMPATIBLE", (
        run,
        errors,
    )
    assert len(sent) == 4


def test_revised_writer_hands_final_approved_script_to_new_director(
    client, episode, settings, model_service, monkeypatch
):
    _, errors = install(model_service, monkeypatch, settings, revised=True)
    run_id = start_writer(client, episode, professional_director=True, revision_rounds=2)
    run = run_once(client, settings, run_id)
    assert run["status"] == "WAITING_REVIEW" and run["workflow_version"] == "production/2.0", (
        run,
        errors,
    )
    with client.app.state.session_factory() as db:
        script = db.scalar(select(Script).where(Script.run_id == run_id))
        source_hash = digest(script.content_json)
    approve(client, run)
    run = run_once(client, settings, run_id)
    assert run["status"] == "WAITING_REVIEW", (run, errors)
    assert progress(client, run_id)["operations"][0]["result"]["script_sha256"] == source_hash
    approve(client, run)
    assert run_once(client, settings, run_id)["status"] == "SUCCEEDED"


def test_api_options_are_explicit_and_idempotency_includes_director_choice(client, episode):
    source = approved_script(client, episode)
    prefix = f"/api/v1/episodes/{episode['id']}"
    assert (
        client.post(
            prefix + "/script-runs",
            headers={"Idempotency-Key": "invalid"},
            json={"story": "故事", "professional_director": True},
        ).status_code
        == 422
    )
    headers = {"Idempotency-Key": "same"}
    body = {"script_version_id": source["id"], "professional": True}
    response = client.post(prefix + "/storyboard-runs", headers=headers, json=body)
    assert response.status_code == 202
    assert (
        client.post(prefix + "/storyboard-runs", headers=headers, json=body).json()
        == response.json()
    )
    assert (
        client.post(
            prefix + "/storyboard-runs", headers=headers, json=body | {"professional": False}
        ).status_code
        == 409
    )
    assert (
        client.post(
            prefix + "/storyboard-runs",
            headers={"Idempotency-Key": "limits"},
            json={"script_version_id": source["id"], "agent_limits": {}},
        ).status_code
        == 422
    )
