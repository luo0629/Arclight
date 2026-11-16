"""Explicit child retry preserves the original live Producer and its execution limits."""

from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from test_agent_model import model_service as model_service
from test_collaboration import create_episode, create_project, register
from test_generation_budget import set_policy
from test_producer_delegation import delegation_call, dispatched
from test_producer_planning import completed_plan
from test_screenwriter_creation import run_once
from test_screenwriter_revision import critique_operations, install_revision

from arclight.application import agent_limits
from arclight.application.agent_limits import action_usage
from arclight.domain.agent_limits import AgentRunLimits
from arclight.persistence.agent_limits import AgentAction, AgentRunLimit
from arclight.persistence.models import OutboxEvent, Script, User, WorkflowRun


@dataclass
class FailedDelegation:
    root_id: UUID
    child_id: UUID
    sent: list
    errors: list
    ordinary_errors: list


def failed_child(
    client, episode, settings, model_service, monkeypatch, *, actor=None, limits=None, quote="0"
):
    root_id, _, _, reference, _ = completed_plan(
        client, episode, settings, model_service, actor=actor, limits=limits, quote=quote
    )
    receipt = dispatched(client, settings, delegation_call(root_id, reference))
    child_id = UUID(receipt["child_run_id"])
    failed = []

    def mutate(decision, seed):
        if decision.get("result") and not failed:
            failed.append(True)
            decision["result"]["source_sha256"] = "a" * 64

    sent, errors, ordinary_errors = install_revision(
        model_service, monkeypatch, settings, patches=0, mutate_report=mutate
    )
    result = run_once(client, settings, child_id)
    assert result["status"] == "FAILED", (result, errors, ordinary_errors)
    assert result["error_code"] == "SCREENWRITER_CRITIQUE_SCOPE", result
    assert failed and sent and not errors and not ordinary_errors
    with client.app.state.session_factory() as db:
        assert db.get(WorkflowRun, root_id).status == "RUNNING"
        assert db.get(AgentRunLimit, child_id).parent_run_id == root_id
    return FailedDelegation(root_id, child_id, sent, errors, ordinary_errors)


def assert_retry_blocked(client, case, code):
    with client.app.state.session_factory() as db:
        run = db.get(WorkflowRun, case.child_id)
        before = (run.revision, deepcopy(run.agent_state_json), run.error_code)
        outbox = db.scalar(select(func.count()).select_from(OutboxEvent))
        actions = db.scalar(select(func.count()).select_from(AgentAction))
    sent = len(case.sent)
    response = client.post(f"/api/v1/runs/{case.child_id}/retry")
    assert response.status_code in {403, 404, 409}, response.text
    assert response.json()["error"]["code"] == code, response.text
    assert len(case.sent) == sent
    with client.app.state.session_factory() as db:
        run = db.get(WorkflowRun, case.child_id)
        assert run.status == "FAILED"
        assert (run.revision, run.agent_state_json, run.error_code) == before
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == outbox
        assert db.scalar(select(func.count()).select_from(AgentAction)) == actions


def test_real_delegated_failure_retries_same_child_to_original_review_without_reset(
    client, episode, settings, model_service, monkeypatch
):
    case = failed_child(client, episode, settings, model_service, monkeypatch)
    before_loops = deepcopy(critique_operations(client, case.child_id))
    with client.app.state.session_factory() as db:
        root = db.get(AgentRunLimit, case.root_id)
        child = db.get(AgentRunLimit, case.child_id)
        frozen = (child.root_run_id, child.parent_run_id, child.limits_json, child.deadline_at)
        assert child.limits_json == root.limits_json and child.deadline_at == root.deadline_at
        old_actions = {row.id for row in db.scalars(select(AgentAction))}
        outbox = db.scalar(
            select(func.count()).select_from(OutboxEvent).where(OutboxEvent.run_id == case.child_id)
        )
    sent = len(case.sent)
    response = client.post(f"/api/v1/runs/{case.child_id}/retry")
    assert response.status_code == 202, response.text
    assert response.json()["id"] == str(case.child_id)
    assert response.json()["status"] == "QUEUED"
    duplicate = client.post(f"/api/v1/runs/{case.child_id}/retry")
    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.json()["error"]["code"] == "NOT_RETRYABLE"
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.run_id == case.child_id)
            )
            == outbox + 1
        )
    result = run_once(client, settings, case.child_id)
    assert result["status"] == "WAITING_REVIEW", result
    assert result["pending_review"]["resource_type"] == "script"
    assert len(case.sent) == sent + 1 and not case.errors and not case.ordinary_errors
    assert critique_operations(client, case.child_id)[: len(before_loops)] == before_loops
    with client.app.state.session_factory() as db:
        child = db.get(AgentRunLimit, case.child_id)
        assert (
            child.root_run_id,
            child.parent_run_id,
            child.limits_json,
            child.deadline_at,
        ) == frozen
        assert db.get(WorkflowRun, case.root_id).status == "RUNNING"
        assert db.scalar(select(func.count()).select_from(WorkflowRun)) == 2
        assert db.scalar(select(func.count()).select_from(AgentRunLimit)) == 2
        assert old_actions <= {row.id for row in db.scalars(select(AgentAction))}
        assert {row.root_run_id for row in db.scalars(select(AgentAction))} == {case.root_id}
        assert (
            db.scalar(
                select(func.count()).select_from(Script).where(Script.run_id == case.child_id)
            )
            == 1
        )


@pytest.mark.parametrize("status", ["QUEUED", "RUNNING", "WAITING_REVIEW"])
@pytest.mark.parametrize("workflow_type", ["producer", "script_storyboard"])
def test_other_active_workflow_still_blocks_real_child_retry(
    client, episode, settings, model_service, monkeypatch, status, workflow_type
):
    case = failed_child(client, episode, settings, model_service, monkeypatch)
    with client.app.state.session_factory.begin() as db:
        db.add(
            WorkflowRun(
                episode_id=UUID(episode["id"]),
                trace_id=uuid4().hex,
                idempotency_key=uuid4().hex,
                request_fingerprint="b" * 64,
                workflow_type=workflow_type,
                workflow_version="producer/1" if workflow_type == "producer" else "text/1",
                input_json={},
                status=status,
            )
        )
    assert_retry_blocked(client, case, "EPISODE_BUSY")


def test_input_claim_without_server_parent_record_cannot_bypass_busy_producer(
    client, episode, settings, model_service, monkeypatch
):
    case = failed_child(client, episode, settings, model_service, monkeypatch)
    with client.app.state.session_factory.begin() as db:
        legacy = WorkflowRun(
            episode_id=UUID(episode["id"]),
            trace_id=uuid4().hex,
            idempotency_key=uuid4().hex,
            request_fingerprint="c" * 64,
            workflow_type="script_storyboard",
            workflow_version="text/1",
            input_json={"producer_parent_run_id": str(case.root_id)},
            status="FAILED",
        )
        db.add(legacy)
        db.flush()
        case.child_id = legacy.id
    assert_retry_blocked(client, case, "EPISODE_BUSY")


@pytest.mark.parametrize(
    "status,code",
    [
        ("CANCELLED", "AGENT_CANCELLED"),
        ("WAITING_REVIEW", "AGENT_NOT_ACTIVE"),
        ("FAILED", "AGENT_NOT_ACTIVE"),
        ("SUCCEEDED", "AGENT_NOT_ACTIVE"),
    ],
)
def test_original_producer_must_still_be_live_at_retry(
    client, episode, settings, model_service, monkeypatch, status, code
):
    case = failed_child(client, episode, settings, model_service, monkeypatch)
    with client.app.state.session_factory.begin() as db:
        db.get(WorkflowRun, case.root_id).status = status
    assert_retry_blocked(client, case, code)


def test_absolute_root_deadline_blocks_retry_before_outbox_or_network(
    client, episode, settings, model_service, monkeypatch
):
    case = failed_child(client, episode, settings, model_service, monkeypatch)
    now = agent_limits.utcnow() + timedelta(hours=2)
    monkeypatch.setattr(agent_limits, "utcnow", lambda: now)
    assert_retry_blocked(client, case, "AGENT_DEADLINE_EXCEEDED")


@pytest.mark.parametrize(
    "change,code", [("revoke", "FORBIDDEN"), ("remove", "NOT_FOUND"), ("disable", "FORBIDDEN")]
)
def test_owner_retry_rechecks_original_collaborator_permission(
    client, settings, model_service, monkeypatch, change, code
):
    owner = register(client)
    project = create_project(client)
    episode = create_episode(client, project)
    member = register(client, "retry-producer@example.com")
    actor = member["user"]["id"]
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    added = client.post(
        f"/api/v1/projects/{project['id']}/members",
        json={"email": member["user"]["email"], "role": "editor"},
    )
    assert added.status_code == 201, added.text
    client.headers["Authorization"] = "Bearer " + member["access_token"]
    case = failed_child(client, episode, settings, model_service, monkeypatch, actor=actor)
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    if change == "disable":
        with client.app.state.session_factory.begin() as db:
            db.get(User, UUID(actor)).disabled = True
    elif change == "remove":
        removed = client.delete(f"/api/v1/projects/{project['id']}/members/{actor}")
        assert removed.status_code == 204, removed.text
    else:
        revoked = client.patch(
            f"/api/v1/projects/{project['id']}/members/{actor}", json={"role": "viewer"}
        )
        assert revoked.status_code == 200, revoked.text
    assert_retry_blocked(client, case, code)


@pytest.mark.parametrize("change", ["parent_actor", "parent_episode", "child_actor"])
def test_parent_exclusion_requires_same_episode_and_original_actor(
    client, episode, settings, model_service, monkeypatch, change
):
    case = failed_child(client, episode, settings, model_service, monkeypatch)
    target_episode_id = UUID(episode["id"])
    if change == "parent_episode":
        response = client.post(
            f"/api/v1/projects/{episode['project_id']}/episodes",
            json={"episode_no": 2, "title": "另集"},
        )
        assert response.status_code == 201, response.text
        target_episode_id = UUID(response.json()["id"])
    with client.app.state.session_factory.begin() as db:
        root = db.get(AgentRunLimit, case.root_id)
        # Negative only: an inserted record must not impersonate the real delegated child.
        forged = WorkflowRun(
            episode_id=target_episode_id,
            trace_id=uuid4().hex,
            idempotency_key=uuid4().hex,
            request_fingerprint="d" * 64,
            workflow_type="script_storyboard",
            workflow_version="text/1",
            input_json={"created_by": str(uuid4()) if change == "parent_actor" else root.actor_id},
            status="FAILED",
        )
        db.add(forged)
        db.flush()
        db.add(
            AgentRunLimit(
                run_id=forged.id,
                project_id=root.project_id,
                root_run_id=root.run_id,
                parent_run_id=root.run_id,
                actor_id=str(uuid4()) if change == "child_actor" else root.actor_id,
                input_sha256="e" * 64,
                depth=1,
                limits_json=root.limits_json,
                deadline_at=root.deadline_at,
            )
        )
        case.child_id = forged.id
    code = "AGENT_SCOPE_INVALID" if change == "child_actor" else "PRODUCER_PARENT_INVALID"
    assert_retry_blocked(client, case, code)


@pytest.mark.parametrize("constraint", ["model_calls", "budget"])
def test_explicit_retry_cannot_reset_shared_root_call_or_cost_limit(
    client, episode, settings, model_service, monkeypatch, constraint
):
    set_policy(client, episode["project_id"], "100")
    limits = AgentRunLimits(
        maximum_cost=Decimal("0.07") if constraint == "budget" else Decimal("0"),
        max_model_calls=7 if constraint == "model_calls" else 32,
        max_duration_seconds=3600,
    )
    case = failed_child(
        client,
        episode,
        settings,
        model_service,
        monkeypatch,
        limits=limits,
        quote="0.01" if constraint == "budget" else "0",
    )
    with client.app.state.session_factory() as db:
        usage = action_usage(db, case.root_id)
        original_actions = {row.id for row in usage["actions"]}
        deadline = db.get(AgentRunLimit, case.root_id).deadline_at
    assert sum(row.kind == "MODEL" for row in usage["actions"]) == 7
    if constraint == "budget":
        # Two Producer calls and five original child calls exhaust the frozen root budget.
        assert usage["actual_cost"] + usage["pending_estimate"] == Decimal("0.07")
        code = "AGENT_BUDGET_EXCEEDED"
    else:
        code = "AGENT_MODEL_CALL_LIMIT"
    sent = len(case.sent)
    response = client.post(f"/api/v1/runs/{case.child_id}/retry")
    assert response.status_code == 202, response.text
    result = run_once(client, settings, case.child_id)
    assert result["status"] == "FAILED" and result["error_code"] == code, result
    assert len(case.sent) == sent and not case.errors and not case.ordinary_errors
    with client.app.state.session_factory() as db:
        current = action_usage(db, case.root_id)
        assert {row.id for row in current["actions"]} == original_actions
        assert db.get(AgentRunLimit, case.child_id).deadline_at == deadline
        assert db.get(WorkflowRun, case.root_id).status == "RUNNING"
