"""Actual asynchronous API -> durable art Runtime -> exact domain review integration."""

from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from art_direction_support import install_direction_model, reference_set
from sqlalchemy import func, select
from test_agent_model import model_service as model_service
from test_art_direction import facts
from test_art_review_workflow import decide, effects
from test_director_workflow import script

from arclight.application.runtime import execute_run
from arclight.domain.model_gateway import GatewayPolicy
from arclight.persistence.agent_loops import AgentLoop
from arclight.persistence.models import OutboxEvent, WorkflowRun


def start_art(client, episode, settings, *, source=None, key=None, minimum=2, limits=None, **task):
    settings.model_gateway_policy = GatewayPolicy(
        text_maximum_cost="0", pricing_version="art-run:1"
    )
    source = source or script(client, f"/api/v1/episodes/{episode['id']}")
    body = {
        "script_version_id": source["id"],
        "task": {
            "visual_goal": "比较雨夜角色的轮廓与配色，提供制作提示词",
            "minimum_reference_images": minimum,
            **task,
        },
    }
    if limits is not None:
        body["agent_limits"] = limits
    response = client.post(
        f"/api/v1/episodes/{episode['id']}/art-runs",
        headers={"Idempotency-Key": key or uuid4().hex},
        json=body,
    )
    assert response.status_code == 202, response.text
    return response.json()["run_id"], source, body


def progress(client, run_id):
    response = client.get(f"/api/v1/runs/{run_id}/art")
    assert response.status_code == 200, response.text
    return response.json()


def drive(client, settings, run_id, revision=None):
    revision = (
        revision
        if revision is not None
        else client.get(f"/api/v1/runs/{run_id}").json()["revision"]
    )
    execute_run(client.app.state.session_factory, settings, UUID(run_id), revision)
    return client.get(f"/api/v1/runs/{run_id}").json()


def proposal(client, episode, delivery):
    return client.get(
        f"/api/v1/projects/{episode['project_id']}/art-proposals/{delivery['proposal_id']}"
    ).json()


@pytest.mark.parametrize("verdict", ["APPROVE", "REJECT"])
def test_actual_start_delivery_and_exact_review_finish_original_thread(
    client, episode, settings, model_service, verdict
):
    reference_set(client, episode)
    sent, errors, _ = install_direction_model(model_service)
    run_id, source, body = start_art(client, episode, settings, key="same-art-request")
    assert sent == []
    queued = client.get(f"/api/v1/runs/{run_id}").json()
    assert queued["workflow_type"] == "art_proposal" and queued["status"] == "QUEUED"
    assert {c["family"] for c in queued["contract"]["versions"]["components"]} == {"ART"}
    assert queued["contract"]["input_snapshot"]["origin"] == "VERSION_BINDING"
    repeated = client.post(
        f"/api/v1/episodes/{episode['id']}/art-runs",
        headers={"Idempotency-Key": "same-art-request"},
        json=body,
    )
    assert repeated.json()["run_id"] == run_id
    changed = deepcopy(body)
    changed["task"]["visual_goal"] += "修改"
    assert (
        client.post(
            f"/api/v1/episodes/{episode['id']}/art-runs",
            headers={"Idempotency-Key": "same-art-request"},
            json=changed,
        ).status_code
        == 409
    )
    before = facts(client)
    waiting = drive(client, settings, run_id)
    assert waiting["status"] == "WAITING_REVIEW", waiting
    data = progress(client, run_id)
    delivery = data["delivery"]
    assert data["task"] == body["task"] | {"constraints": [], "target_asset_ids": []}
    assert len(data["operations"]) == 1 and len(sent) == 4 and not errors, errors
    row = proposal(client, episode, delivery)
    original = deepcopy(row["version"])
    assert row["review"]["snapshot"]["run_id"] == run_id
    assert row["review"]["snapshot"]["thread_id"] == run_id
    assert waiting["contract"]["result"]["artifact_ids"] == [delivery["version_id"]]
    assert source["id"] in [s.get("resource_id") for s in original["proposal_json"]["sources"]]
    assert row["outputs"] == []
    drive(client, settings, run_id, 0)
    drive(client, settings, run_id)
    assert progress(client, run_id)["delivery"] == delivery and len(sent) == 4
    decision = decide(client, row, verdict)
    assert decision.status_code == 200, decision.text
    after = effects(client)
    reviewed = client.get(f"/api/v1/runs/{run_id}").json()
    assert reviewed["status"] == "QUEUED" and reviewed["pending_review"] is None
    assert drive(client, settings, run_id)["status"] == (
        "SUCCEEDED" if verdict == "APPROVE" else "REJECTED"
    )
    assert drive(client, settings, run_id)["status"] == (
        "SUCCEEDED" if verdict == "APPROVE" else "REJECTED"
    )
    assert decide(client, row, verdict).json() == decision.json()
    assert len(sent) == 4 and effects(client) == after
    assert proposal(client, episode, delivery)["version"] == original
    with client.app.state.session_factory() as db:
        saved = db.get(WorkflowRun, UUID(run_id))
        assert saved.input_json["script_version_id"] == source["id"]
        assert saved.input_json["art_direction"]["task"] == data["task"]
        assert saved.agent_state_json["status"] == saved.status
        assert len(list(db.scalars(select(AgentLoop).where(AgentLoop.run_id == saved.id)))) == 1
        assert (
            db.scalar(
                select(func.count()).select_from(OutboxEvent).where(OutboxEvent.run_id == saved.id)
            )
            == 2
        )
    if verdict == "REJECT":
        expected = before | {"ArtProposalVersion": before["ArtProposalVersion"] + 1}
        assert facts(client) == expected


@pytest.mark.parametrize("answer", ["ANSWER", "STOP"])
def test_persisted_art_question_uses_actual_answer_api_and_reuses_loop(
    client, episode, settings, model_service, answer
):
    reference_set(client, episode)
    sent, errors, _ = install_direction_model(model_service, human=True)
    run_id, _, _ = start_art(client, episode, settings)
    assert drive(client, settings, run_id)["status"] == "RUNNING"
    data = progress(client, run_id)
    operation = data["operations"][0]
    assert operation["status"] == "WAITING_HUMAN" and data["can_cancel"]
    assert data["delivery"] is None and len(sent) == 2
    body = {
        "question_id": operation["question_id"],
        "input_sha256": operation["input_sha256"],
        "decision": answer,
        "answer": (
            operation["human_request"]["options"][0]
            if operation["human_request"]["options"]
            else "继续调查"
        )
        if answer == "ANSWER"
        else None,
    }
    endpoint = f"/api/v1/runs/{run_id}/art/{operation['loop_id']}/answers"
    assert client.post(endpoint, json=body).status_code == 202
    duplicate = client.post(endpoint, json=body)
    assert duplicate.status_code == 202, duplicate.text
    outcome = drive(client, settings, run_id)
    assert not errors, errors
    current = progress(client, run_id)
    assert (
        len(current["operations"]) == 1
        and current["operations"][0]["loop_id"] == operation["loop_id"]
    )
    if answer == "ANSWER":
        assert outcome["status"] == "WAITING_REVIEW", outcome
        assert current["delivery"] is not None and len(sent) == 5
        assert proposal(client, episode, current["delivery"])["outputs"] == []
    else:
        assert outcome["status"] == "CANCELLED", outcome
        assert current["delivery"] is None and len(sent) == 2


def test_failed_handoff_can_retry_without_regenerating_or_duplicate_candidate(
    client, episode, settings, model_service, monkeypatch
):
    reference_set(client, episode)
    sent, errors, _ = install_direction_model(model_service)
    run_id, _, _ = start_art(client, episode, settings)
    import arclight.application.art_execution as execution

    original = execution._review_graph
    monkeypatch.setattr(
        execution, "_review_graph", lambda *args: (_ for _ in ()).throw(RuntimeError("crash"))
    )
    failed = drive(client, settings, run_id)
    assert failed["status"] == "FAILED" and failed["error_code"] == "WORKFLOW_EXECUTION_FAILED"
    saved = progress(client, run_id)["delivery"]
    assert saved is not None and proposal(client, episode, saved)["outputs"] == []
    monkeypatch.setattr(execution, "_review_graph", original)
    assert client.post(f"/api/v1/runs/{run_id}/retry").status_code == 202
    assert drive(client, settings, run_id)["status"] == "WAITING_REVIEW"
    assert progress(client, run_id)["delivery"] == saved and len(sent) == 4 and not errors


def test_cancellation_blocks_generated_review_without_formal_changes(
    client, episode, settings, model_service
):
    reference_set(client, episode)
    sent, errors, _ = install_direction_model(model_service)
    run_id, _, _ = start_art(client, episode, settings)
    assert drive(client, settings, run_id)["status"] == "WAITING_REVIEW"
    data = progress(client, run_id)
    row = proposal(client, episode, data["delivery"])
    before = effects(client)
    assert client.post(f"/api/v1/runs/{run_id}/cancel").status_code == 200
    response = decide(client, row, "APPROVE")
    assert response.status_code == 409 and response.json()["error"]["code"] == "ART_REVIEW_STALE"
    assert (
        effects(client) == before and proposal(client, episode, data["delivery"])["outputs"] == []
    )
    assert (
        drive(client, settings, run_id)["status"] == "CANCELLED" and len(sent) == 4 and not errors
    )
