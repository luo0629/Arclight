"""Actual art API authority, exact question inputs and existing review entry points."""

from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from art_direction_support import install_direction_model, reference_set
from sqlalchemy.exc import IntegrityError
from test_agent_model import model_service as model_service
from test_art_direction import facts
from test_art_review_workflow import decide, decision, effects
from test_art_workflow import drive, progress, proposal, start_art
from test_collaboration import create_episode, create_project, register

from arclight.application.runtime import execute_run
from arclight.domain.versions import digest
from arclight.persistence.models import Project, ProjectMember, User, WorkflowRun


@pytest.mark.parametrize("phase", ["queued", "answer", "review_resume"])
@pytest.mark.parametrize("change", ["disabled", "archived", "revoked", "input"])
def test_execution_and_resume_recheck_original_authority_and_frozen_inputs(
    client, settings, model_service, phase, change
):
    owner = register(client)
    project = create_project(client)
    episode = create_episode(client, project)
    reference_set(client, episode)
    sent, errors, _ = install_direction_model(model_service, human=phase == "answer")
    run_id, _, _ = start_art(client, episode, settings)
    if phase == "answer":
        drive(client, settings, run_id)
        operation = progress(client, run_id)["operations"][0]
        response = client.post(
            f"/api/v1/runs/{run_id}/art/{operation['loop_id']}/answers",
            json={
                "question_id": operation["question_id"],
                "input_sha256": operation["input_sha256"],
                "decision": "ANSWER",
                "answer": operation["human_request"]["options"][0],
            },
        )
        assert response.status_code == 202, response.text
    elif phase == "review_resume":
        drive(client, settings, run_id)
        row = proposal(client, episode, progress(client, run_id)["delivery"])
        assert decide(client, row).status_code == 200
    factory = client.app.state.session_factory
    count, before = len(sent), facts(client)
    if change == "input":
        with factory() as db:
            original = deepcopy(db.get(WorkflowRun, UUID(run_id)).input_json)
        with pytest.raises(IntegrityError, match="IMMUTABLE_HISTORY"), factory.begin() as db:
            run = db.get(WorkflowRun, UUID(run_id))
            run.input_json = run.input_json | {"story": "different frozen input"}
        with factory() as db:
            assert db.get(WorkflowRun, UUID(run_id)).input_json == original
        assert facts(client) == before and len(sent) == count
        return
    with factory.begin() as db:
        run = db.get(WorkflowRun, UUID(run_id))
        revision = run.revision
        if change == "disabled":
            db.get(User, UUID(owner["user"]["id"])).disabled = True
        elif change == "archived":
            db.get(Project, UUID(project["id"])).archived = True
        else:
            db.get(Project, UUID(project["id"])).owner_id = "service-owner"
    execute_run(factory, settings, UUID(run_id), revision)
    with factory() as db:
        assert db.get(WorkflowRun, UUID(run_id)).status == "FAILED"
    assert facts(client) == before and len(sent) == count and not errors


@pytest.mark.parametrize("role", ["editor", "viewer", "outsider"])
def test_api_roles_for_start_read_answer_and_owner_review(client, settings, model_service, role):
    owner = register(client)
    project = create_project(client)
    episode = create_episode(client, project)
    reference_set(client, episode)
    sent, errors, _ = install_direction_model(model_service, human=True)
    run_id, source, body = start_art(client, episode, settings)
    drive(client, settings, run_id)
    operation = progress(client, run_id)["operations"][0]
    other = register(client, "other@art-workflow.test")
    if role != "outsider":
        with client.app.state.session_factory.begin() as db:
            db.add(
                ProjectMember(
                    project_id=UUID(project["id"]), user_id=UUID(other["user"]["id"]), role=role
                )
            )
    assert client.get(f"/api/v1/runs/{run_id}/art").status_code == (
        404 if role == "outsider" else 200
    )
    response = client.post(
        f"/api/v1/episodes/{episode['id']}/art-runs",
        headers={"Idempotency-Key": uuid4().hex},
        json=body,
    )
    assert response.status_code == (409 if role == "editor" else 403 if role == "viewer" else 404)
    if role == "editor":
        assert response.json()["error"]["code"] == "EPISODE_BUSY"
    answer = {
        "question_id": operation["question_id"],
        "input_sha256": operation["input_sha256"],
        "decision": "ANSWER",
        "answer": operation["human_request"]["options"][0],
    }
    response = client.post(f"/api/v1/runs/{run_id}/art/{operation['loop_id']}/answers", json=answer)
    assert response.status_code == (202 if role == "editor" else 403 if role == "viewer" else 404)
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    if role != "editor":
        assert (
            client.post(
                f"/api/v1/runs/{run_id}/art/{operation['loop_id']}/answers", json=answer
            ).status_code
            == 202
        )
    assert drive(client, settings, run_id)["status"] == "WAITING_REVIEW"
    row = proposal(client, episode, progress(client, run_id)["delivery"])
    client.headers["Authorization"] = "Bearer " + other["access_token"]
    assert decide(client, row).status_code == (404 if role == "outsider" else 403)
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    assert decide(client, row).status_code == 200
    assert drive(client, settings, run_id)["status"] == "SUCCEEDED"
    if role == "editor":
        client.headers["Authorization"] = "Bearer " + other["access_token"]
        started = client.post(
            f"/api/v1/episodes/{episode['id']}/art-runs",
            headers={"Idempotency-Key": uuid4().hex},
            json=body,
        )
        assert started.status_code == 202, started.text
    assert len(sent) == 5 and not errors
    assert source["id"] in [
        s.get("resource_id") for s in row["version"]["proposal_json"]["sources"]
    ]


@pytest.mark.parametrize("changed", ["loop", "question", "input"])
def test_answers_refuse_foreign_or_stale_targets_without_queuing(
    client, episode, settings, model_service, changed
):
    reference_set(client, episode)
    install_direction_model(model_service, human=True)
    run_id, _, _ = start_art(client, episode, settings)
    drive(client, settings, run_id)
    operation = progress(client, run_id)["operations"][0]
    loop_id = operation["loop_id"]
    body = {
        "question_id": operation["question_id"],
        "input_sha256": operation["input_sha256"],
        "decision": "ANSWER",
        "answer": operation["human_request"]["options"][0],
    }
    if changed == "loop":
        loop_id = str(uuid4())
    elif changed == "question":
        body["question_id"] = str(uuid4())
    else:
        body["input_sha256"] = "0" * 64
    before = client.get(f"/api/v1/runs/{run_id}").json()
    assert client.post(f"/api/v1/runs/{run_id}/art/{loop_id}/answers", json=body).status_code == (
        409 if changed == "input" else 404
    )
    assert client.get(f"/api/v1/runs/{run_id}").json() == before
    assert progress(client, run_id)["delivery"] is None


@pytest.mark.parametrize("verdict", ["APPROVE", "REJECT"])
def test_generic_saved_decision_and_resume_keep_original_art_run(
    client, episode, settings, model_service, verdict
):
    reference_set(client, episode)
    sent, errors, _ = install_direction_model(model_service)
    run_id, _, _ = start_art(client, episode, settings)
    drive(client, settings, run_id)
    delivery = progress(client, run_id)["delivery"]
    row = proposal(client, episode, delivery)
    confirmation = decision(row)["confirmation"]
    endpoint = f"/api/v1/human-reviews/{row['review']['id']}"
    before = effects(client)
    saved = client.post(
        endpoint + "/decisions",
        json={
            "version_id": row["version"]["id"],
            "request_sha256": row["review"]["request_sha256"],
            "decision": verdict,
            "impact_digest": digest(confirmation) if verdict == "APPROVE" else None,
        },
    )
    assert saved.status_code == 200, saved.text
    after_decision = effects(client)
    for table in (
        "visual_assets",
        "visual_bible_versions",
        "art_approved_versions",
        "human_review_resumes",
    ):
        assert after_decision[table] == before[table]
    body = {"decision_id": saved.json()["id"], "request_sha256": row["review"]["request_sha256"]}
    resumed = client.post(endpoint + "/resume", json=body)
    assert resumed.status_code == 200, resumed.text
    assert client.post(endpoint + "/resume", json=body).json() == resumed.json()
    assert drive(client, settings, run_id)["status"] == (
        "SUCCEEDED" if verdict == "APPROVE" else "REJECTED"
    )
    assert progress(client, run_id)["delivery"] == delivery and len(sent) == 4 and not errors
    # A later manual revision owns its normal review; it cannot reopen the completed run.
    path = f"/api/v1/projects/{episode['project_id']}/art-proposals/{row['id']}"
    original = client.get(path).json()
    revised = deepcopy(original["version"]["proposal_json"])
    revised["visual_goal"] += "人工修订"
    revised["origin"] = "MANUAL"
    response = client.put(
        path,
        json={
            "creation_key": str(uuid4()),
            "expected_revision": original["revision"],
            "proposal": revised,
        },
    )
    assert response.status_code == 201, response.text
    assert progress(client, run_id)["delivery"] == delivery
    assert drive(client, settings, run_id)["status"] in {"SUCCEEDED", "REJECTED"}


def test_art_runtime_honors_frozen_root_limits_without_blind_retry(
    client, episode, settings, model_service
):
    reference_set(client, episode)
    sent, errors, _ = install_direction_model(model_service)
    run_id, _, _ = start_art(client, episode, settings, limits={"max_model_calls": 1})
    before = facts(client)
    final = drive(client, settings, run_id)
    assert final["status"] == "FAILED" and final["error_code"] == "AGENT_MODEL_CALL_LIMIT"
    response = client.post(f"/api/v1/runs/{run_id}/retry")
    assert response.status_code == 409 and response.json()["error"]["code"] == "ART_TASK_STOPPED"
    assert facts(client) == before and len(sent) == 1 and not errors


def test_generated_locked_bible_requires_exact_confirmation_and_replay_keeps_history(
    client, episode, settings, model_service
):
    reference_set(client, episode)
    prefix = f"/api/v1/projects/{episode['project_id']}/visual-bible"
    assert (
        client.post(
            prefix + "/versions",
            json={
                "expected_version_no": 0,
                "creation_key": str(uuid4()),
                "fields": {"art_style": "水墨"},
            },
        ).status_code
        == 201
    )
    assert (
        client.post(
            prefix + "/locks",
            json={
                "expected_version_no": 1,
                "creation_key": str(uuid4()),
                "locked_fields": ["art_style"],
            },
        ).status_code
        == 201
    )
    original = client.get(prefix).json()
    sent, errors, _ = install_direction_model(model_service, bible=True)
    run_id, _, _ = start_art(client, episode, settings)
    assert drive(client, settings, run_id)["status"] == "WAITING_REVIEW"
    assert client.get(prefix).json() == original
    row = proposal(client, episode, progress(client, run_id)["delivery"])
    body = decision(row)
    body.pop("confirmation")
    path = f"/api/v1/projects/{episode['project_id']}/art-proposals/{row['id']}/decisions"
    assert client.post(path, json=body).status_code == 422
    assert client.get(prefix).json() == original
    assert decide(client, row).status_code == 200
    current = client.get(prefix).json()
    assert current["version_no"] == 3 and current["fields"]["art_style"] == "油画"
    assert current["locked_fields"] == ["art_style"]
    assert drive(client, settings, run_id)["status"] == "SUCCEEDED"
    assert drive(client, settings, run_id)["status"] == "SUCCEEDED"
    assert client.get(prefix).json() == current and len(sent) == 4 and not errors
