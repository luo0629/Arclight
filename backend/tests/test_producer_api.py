"""PROD-04: actual HTTP, transactions, worker dispatch and public authorization."""

from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select
from test_agent_model import model_service as model_service
from test_collaboration import create_episode, create_project, register
from test_producer_planning import install_plan_model
from test_producer_replanning import install_replan_model

from arclight.application.runtime import execute_run
from arclight.domain.model_gateway import GatewayPolicy
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.agent_loops import AgentLoop, AgentLoopDecision
from arclight.persistence.model_gateway import GatewayCall, GatewayReceipt
from arclight.persistence.models import OutboxEvent, Project, WorkflowRun


def start(client, episode, settings, *, key="producer-http", **changes):
    settings.model_gateway_policy = GatewayPolicy(text_maximum_cost="0", pricing_version="p04:1")
    return client.post(
        f"/api/v1/episodes/{episode['id']}/producer-runs",
        headers={"Idempotency-Key": key},
        json={"goal": "为本集制作纸船故事剧本", **changes},
    )


def progress(client, run_id):
    response = client.get(f"/api/v1/runs/{run_id}/producer")
    assert response.status_code == 200, response.text
    return response.json()


def dispatch(client, settings, run_id):
    value = progress(client, run_id)
    execute_run(client.app.state.session_factory, settings, UUID(str(run_id)), value["revision"])
    return progress(client, run_id)


def command(client, run_id, payload, key=None):
    return client.post(
        f"/api/v1/runs/{run_id}/producer/commands",
        headers={"Idempotency-Key": key or uuid4().hex},
        json=payload,
    )


def test_start_is_committed_202_and_freezes_run_scope(client, episode, settings):
    response = start(client, episode, settings)
    assert response.status_code == 202, response.text
    accepted = response.json()
    run_id = UUID(accepted["run_id"])
    assert accepted["status"] == "RUNNING"
    with client.app.state.session_factory() as db:
        run = db.get(WorkflowRun, run_id)
        assert run and run.revision == 1
        limit = db.get(AgentRunLimit, run_id)
        assert limit.limits_json == {} and limit.deadline_at is None
        assert (
            db.scalar(
                select(func.count()).select_from(OutboxEvent).where(OutboxEvent.run_id == run_id)
            )
            == 1
        )
    assert start(client, episode, settings).json() == accepted
    conflict = start(client, episode, settings, goal="更改冻结目标")
    assert (
        conflict.status_code == 409 and conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    )
    value = progress(client, run_id)
    assert value["pending_command"]["action"] == "PLAN"
    assert "limits" not in value["budget"]
    listing = client.get(f"/api/v1/projects/{episode['project_id']}/tasks")
    assert listing.status_code == 200, listing.text
    task = next(item for item in listing.json()["items"] if item["id"] == str(run_id))
    assert task["workflow_type"] == "producer"
    assert task["detail_url"].endswith(f"?view=tasks&run={run_id}")


@pytest.mark.parametrize(
    "change", [{"goal": " "}, {"goal": "x", "actor": "attacker"}, {"limits": {"maximum_cost": -1}}]
)
def test_invalid_create_has_no_root_or_outbox(client, episode, settings, change):
    assert start(client, episode, settings, **change).status_code == 422
    assert client.get(f"/api/v1/episodes/{episode['id']}/runs").json() == []
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 0


def test_http_queue_executes_original_plan_delegation_and_replan(
    client, episode, settings, model_service
):
    sent, errors = install_plan_model(model_service)
    response = start(client, episode, settings)
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    first = dispatch(client, settings, run_id)
    assert first["commands"][0]["status"] == "COMMITTED", (first, errors)
    assert len(first["plans"]) == 1 and len(sent) == 2 and not errors
    plan = first["plans"][0]
    delegate = {
        "action": "DELEGATE",
        "expected_revision": first["revision"],
        "plan_id": plan["plan_id"],
        "plan_sha256": plan["plan_sha256"],
        "task_key": "write",
    }
    queued = command(client, run_id, delegate, "delegate-once")
    assert queued.status_code == 202, queued.text
    assert command(client, run_id, delegate, "delegate-once").status_code == 202
    delegated = dispatch(client, settings, run_id)
    task = delegated["plans"][0]["tasks"][0]
    assert task["child_run_id"] and task["status"] == "QUEUED"
    assert delegated["budget"]["delegations"] == 1
    revised, replan_errors = install_replan_model(model_service)
    payload = {
        "action": "REPLAN",
        "expected_revision": delegated["revision"],
        "observation": {
            "base_plan_id": plan["plan_id"],
            "base_plan_sha256": plan["plan_sha256"],
            "trigger": "RECHECK",
            "reason": "检查剩余生产任务",
        },
    }
    queued = command(client, run_id, payload, "observe-once")
    assert queued.status_code == 202, queued.text
    after = dispatch(client, settings, run_id)
    assert len(after["plans"]) == 2, (after, replan_errors)
    assert after["plans"][1]["previous_plan_id"] == plan["plan_id"]
    assert after["plans"][1]["tasks"][0]["child_run_id"] == task["child_run_id"]
    assert after["budget"]["delegations"] == 1 and revised and not replan_errors
    before_budget = after["budget"]
    execute_run(client.app.state.session_factory, settings, UUID(run_id), after["revision"])
    assert progress(client, run_id)["budget"] == before_budget


@pytest.mark.parametrize("decision", ["ANSWER", "STOP"])
def test_precise_answer_replay_and_conflict_keep_one_decision(
    client, episode, settings, model_service, decision
):
    sent, errors = install_plan_model(model_service, human=True)
    created = start(client, episode, settings).json()
    run_id = created["run_id"]
    waiting = dispatch(client, settings, run_id)
    operation = waiting["operations"][0]
    assert operation["can_answer"] and waiting["pending_command"] is None, (waiting, errors)
    payload = {
        "question_id": operation["question_id"],
        "input_sha256": operation["input_sha256"],
        "decision": decision,
        "answer": operation["human_request"]["options"][0] if decision == "ANSWER" else None,
    }
    url = f"/api/v1/runs/{run_id}/producer/{operation['loop_id']}/answers"
    first = client.post(url, json=payload)
    assert first.status_code == 202, first.text
    queued = progress(client, run_id)
    assert not queued["operations"][0]["can_answer"]
    assert client.post(url, json=payload).status_code == 202
    conflict = client.post(url, json={**payload, "decision": "ANSWER", "answer": "另一个回答"})
    assert conflict.status_code == 409
    after = dispatch(client, settings, run_id)
    assert after["operations"][0]["loop_id"] == operation["loop_id"]
    assert len(after["commands"]) == 2 and not errors
    assert after["status"] == ("RUNNING" if decision == "ANSWER" else "CANCELLED")
    assert len(after["plans"]) == (1 if decision == "ANSWER" else 0)
    calls = len(sent)
    assert client.post(url, json=payload).status_code == 202
    execute_run(client.app.state.session_factory, settings, UUID(run_id), after["revision"])
    assert len(sent) == calls
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(AgentLoopDecision)) == 1


@pytest.mark.parametrize("role", ["viewer", "editor", "outsider"])
def test_role_and_cross_project_controls_are_enforced_by_http(client, settings, role):
    owner = register(client)
    project = create_project(client)
    episode = create_episode(client, project)
    created = start(client, episode, settings)
    assert created.status_code == 202, created.text
    run_id = created.json()["run_id"]
    member = register(client, f"{role}@example.test")
    if role != "outsider":
        client.headers["Authorization"] = "Bearer " + owner["access_token"]
        response = client.post(
            f"/api/v1/projects/{project['id']}/members",
            json={"email": member["user"]["email"], "role": role},
        )
        assert response.status_code == 201, response.text
        client.headers["Authorization"] = "Bearer " + member["access_token"]
    read = client.get(f"/api/v1/runs/{run_id}/producer")
    if role == "outsider":
        assert read.status_code == 404
    else:
        assert read.status_code == 200 and not read.json()["can_operate"]
    stop = command(client, run_id, {"action": "CANCEL", "expected_revision": 1})
    assert stop.status_code == (404 if role == "outsider" else 403), stop.text
    if role == "viewer":
        assert start(client, episode, settings, key="viewer-create").status_code == 403
        client.headers["Authorization"] = "Bearer " + owner["access_token"]
        assert (
            client.delete(
                f"/api/v1/projects/{project['id']}/members/{member['user']['id']}"
            ).status_code
            == 204
        )
        client.headers["Authorization"] = "Bearer " + member["access_token"]
        assert client.get(f"/api/v1/runs/{run_id}/producer").status_code == 404


def test_terminal_and_archived_history_remains_readable_with_safe_projection(
    client, episode, settings, model_service
):
    install_plan_model(model_service)
    run_id = start(client, episode, settings).json()["run_id"]
    ready = dispatch(client, settings, run_id)
    stopped = command(client, run_id, {"action": "CANCEL", "expected_revision": ready["revision"]})
    assert stopped.status_code == 202, stopped.text
    assert (
        client.patch(
            f"/api/v1/projects/{episode['project_id']}", json={"archived": True}
        ).status_code
        == 200
    )
    result = client.get(f"/api/v1/runs/{run_id}/producer")
    value = result.json()
    assert result.status_code == 200 and len(value["plans"]) == 1
    assert not value["can_operate"] and not value["can_cancel"]
    assert value["readonly_reason"] == "PROJECT_ARCHIVED"
    for private in [
        "private-agent-transport-key",
        "system_instruction",
        "service_snapshot",
        "tool_arguments",
        "seed_json",
    ]:
        assert private not in result.text
    assert start(client, episode, settings, key="archived-start").status_code == 409


@pytest.mark.parametrize("action", ["cancel", "retry", "restart"])
def test_generic_controls_cannot_recreate_or_requeue_producer(client, episode, settings, action):
    run_id = start(client, episode, settings).json()["run_id"]
    if action != "cancel":
        with client.app.state.session_factory.begin() as db:
            db.get(WorkflowRun, UUID(run_id)).status = "FAILED"
    response = client.post(
        f"/api/v1/runs/{run_id}/{action}", headers={"Idempotency-Key": "generic-restart"}
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "PRODUCER_CONTROL_REQUIRED"
    assert len(client.get(f"/api/v1/episodes/{episode['id']}/runs").json()) == 1


def test_revoked_actor_cannot_execute_already_queued_command(client, settings, model_service):
    owner = register(client)
    project = create_project(client)
    episode = create_episode(client, project)
    editor = register(client, "producer-editor@example.test")
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    assert (
        client.post(
            f"/api/v1/projects/{project['id']}/members",
            json={"email": editor["user"]["email"], "role": "editor"},
        ).status_code
        == 201
    )
    client.headers["Authorization"] = "Bearer " + editor["access_token"]
    sent, errors = install_plan_model(model_service)
    run_id = start(client, episode, settings).json()["run_id"]
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    assert (
        client.delete(
            f"/api/v1/projects/{project['id']}/members/{editor['user']['id']}"
        ).status_code
        == 204
    )
    execute_run(client.app.state.session_factory, settings, UUID(run_id), 1)
    value = progress(client, run_id)
    assert value["commands"][0]["status"] == "FAILED"
    assert value["commands"][0]["error_code"] == "NOT_FOUND"
    assert not value["plans"] and not sent and not errors
    with client.app.state.session_factory() as db:
        assert db.get(Project, UUID(project["id"])).owner_id == owner["user"]["id"]


def test_public_child_and_loop_diagnostics_never_expose_private_provider_text(
    client, episode, settings, model_service
):
    install_plan_model(model_service)
    run_id = start(client, episode, settings).json()["run_id"]
    ready = dispatch(client, settings, run_id)
    plan = ready["plans"][0]
    queued = command(
        client,
        run_id,
        {
            "action": "DELEGATE",
            "expected_revision": ready["revision"],
            "plan_id": plan["plan_id"],
            "plan_sha256": plan["plan_sha256"],
            "task_key": "write",
        },
    )
    assert queued.status_code == 202, queued.text
    current = dispatch(client, settings, run_id)
    secret = "GATEWAY_PROVIDER_FAILED: private-token=sentinel-secret"
    with client.app.state.session_factory.begin() as db:
        db.get(
            WorkflowRun, UUID(current["plans"][0]["tasks"][0]["child_run_id"])
        ).error_code = secret
        loop = db.get(AgentLoop, UUID(current["operations"][0]["loop_id"]))
        loop.outcome_json = {**loop.outcome_json, "stop_code": secret}
    response = client.get(f"/api/v1/runs/{run_id}/producer")
    assert response.status_code == 200, response.text
    assert "sentinel-secret" not in response.text
    assert response.json()["operations"][0]["stop_code"] == "INTERNAL_ERROR"
    assert response.json()["plans"][0]["tasks"][0]["error_code"] == "INTERNAL_ERROR"


@pytest.mark.parametrize(
    "code", ["AGENT_DEADLINE_EXCEEDED", "AGENT_COST_LIMIT", "private-token=sentinel-secret"]
)
def test_command_events_and_progress_share_public_error_vocabulary(
    client, episode, settings, monkeypatch, code
):
    from arclight.application import producer_execution
    from arclight.core.errors import DomainError

    def fail(*args, **kwargs):
        raise DomainError(code, "private diagnostic details", 409)

    monkeypatch.setattr(producer_execution, "plan_producer", fail)
    run_id = start(client, episode, settings).json()["run_id"]
    result = dispatch(client, settings, run_id)
    public = "INTERNAL_ERROR" if "sentinel" in code else code
    assert result["commands"][0]["error_code"] == public
    response = client.get(f"/api/v1/runs/{run_id}/events")
    assert response.status_code == 200, response.text
    assert "sentinel-secret" not in response.text
    finished = next(
        item for item in response.json() if item["event_type"] == "producer.command.finished"
    )
    assert finished["payload_json"]["error_code"] == public


def test_unknown_model_result_recovers_original_intent_without_resubmission(
    client, episode, settings, model_service
):
    def lose_response(_payload):
        raise httpx.ReadTimeout("isolated response lost after submission")

    sent, _ = install_plan_model(model_service, on_http=lose_response)
    run_id = start(client, episode, settings).json()["run_id"]
    unknown = dispatch(client, settings, run_id)
    assert len(sent) == 1
    assert unknown["pending_command"] and unknown["can_recover"]
    assert unknown["pending_command"]["status"] == "WAITING_EXTERNAL"
    with client.app.state.session_factory() as db:
        receipt = db.scalar(
            select(GatewayReceipt).join(GatewayCall).where(GatewayCall.workflow_id == UUID(run_id))
        )
        assert receipt.status == "RESULT_UNKNOWN" and receipt.actual_cost is None
    pending = unknown["pending_command"]
    request = {
        "action": "RECOVER",
        "expected_revision": unknown["revision"],
        "command_id": pending["command_id"],
    }
    response = command(client, run_id, request, "reconcile-original")
    assert response.status_code == 202, response.text
    recovered = dispatch(client, settings, run_id)
    assert recovered["revision"] == unknown["revision"]
    assert recovered["pending_command"]["command_id"] == pending["command_id"]
    assert recovered["budget"] == unknown["budget"] and len(sent) == 1


def test_stopped_root_preserves_child_but_blocks_new_family_model_calls(
    client, episode, settings, model_service
):
    sent, errors = install_plan_model(model_service)
    run_id = start(client, episode, settings).json()["run_id"]
    ready = dispatch(client, settings, run_id)
    plan = ready["plans"][0]
    response = command(
        client,
        run_id,
        {
            "action": "DELEGATE",
            "expected_revision": ready["revision"],
            "plan_id": plan["plan_id"],
            "plan_sha256": plan["plan_sha256"],
            "task_key": "write",
        },
    )
    assert response.status_code == 202, response.text
    delegated = dispatch(client, settings, run_id)
    child_id = UUID(delegated["plans"][0]["tasks"][0]["child_run_id"])
    stopped = command(
        client, run_id, {"action": "CANCEL", "expected_revision": delegated["revision"]}
    )
    assert stopped.status_code == 202, stopped.text
    with client.app.state.session_factory() as db:
        child = db.get(WorkflowRun, child_id)
        assert child.status == "QUEUED"
        revision = child.revision
    calls = len(sent)
    execute_run(client.app.state.session_factory, settings, child_id, revision)
    assert len(sent) == calls and not errors
    with client.app.state.session_factory() as db:
        child = db.get(WorkflowRun, child_id)
        assert child.status == "FAILED" and child.error_code == "AGENT_CANCELLED"
    final = progress(client, run_id)
    assert final["status"] == "CANCELLED" and final["budget"]["delegations"] == 1
    assert final["plans"][0]["tasks"][0]["child_run_id"] == str(child_id)


def test_no_execution_deadline_and_explicit_stop_remains_available(
    client, episode, settings, model_service
):
    sent, errors = install_plan_model(model_service)
    run_id = start(client, episode, settings).json()["run_id"]
    with client.app.state.session_factory() as db:
        assert db.get(AgentRunLimit, UUID(run_id)).deadline_at is None
    planned = dispatch(client, settings, run_id)
    assert planned["commands"][0]["status"] == "COMMITTED"
    assert planned["readonly_reason"] is None
    assert planned["can_operate"] and planned["can_cancel"]
    assert len(sent) == 2 and not errors
    response = command(
        client, run_id, {"action": "CANCEL", "expected_revision": planned["revision"]}
    )
    assert response.status_code == 202, response.text
    assert progress(client, run_id)["status"] == "CANCELLED"
