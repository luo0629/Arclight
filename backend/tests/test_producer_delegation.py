"""Production delegation creates original professional work, with one inherited root policy."""

from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from art_direction_support import install_direction_model, reference_set
from continuity_reviewer_support import install_reviewer_model
from sqlalchemy import func, select
from test_agent_model import model_service as model_service
from test_collaboration import create_episode, create_project, register
from test_continuity_reviewer import reviewer_case as reviewer_case
from test_continuity_rules import rules_storage as rules_storage
from test_director_creation import approved_script
from test_director_revision import install_revision as install_director
from test_generation_budget import set_policy
from test_producer_planning import completed_plan, writer_task
from test_screenwriter_creation import run_once
from test_screenwriter_revision import install_revision as install_writer

from arclight.application import producer_planning as planning
from arclight.application.agent_limits import action_usage, configure_agent_run
from arclight.application.agent_tools import execute_agent_tool, grant_agent_tools
from arclight.application.gateway_accounting import reconcile
from arclight.core.errors import DomainError
from arclight.domain.agent_limits import AgentRunLimits
from arclight.domain.agent_tools import AgentToolCall, AgentToolGrantRequest
from arclight.domain.model_gateway import GatewayPolicy, GatewayUsage
from arclight.domain.versions import digest
from arclight.persistence.agent_limits import AgentAction, AgentRunLimit
from arclight.persistence.model_gateway import GatewayCall
from arclight.persistence.models import (
    AgentToolGrant,
    AssetVersion,
    OutboxEvent,
    Script,
    User,
    WorkflowRun,
)


def delegation_call(run_id, reference, *, task_key="write", key=None, **changes):
    return AgentToolCall(
        run_id=run_id,
        tool_name="delegate_production",
        idempotency_key=key or uuid4().hex,
        arguments={
            "plan_id": str(reference.plan_id),
            "plan_sha256": reference.plan_sha256,
            "task_key": task_key,
            **changes,
        },
    )


def dispatch(client, settings, call):
    with client.app.state.session_factory.begin() as db:
        return execute_agent_tool(db, planning.producer_registry(settings), "producer", call)


def dispatched(client, settings, call):
    feedback = dispatch(client, settings, call)
    assert feedback.status == "SUCCEEDED", feedback.model_dump_json()
    assert feedback.trust == "UNTRUSTED"
    return feedback.result


def child_for(client, identity):
    with client.app.state.session_factory() as db:
        return db.get(WorkflowRun, UUID(identity))


def test_same_plan_task_across_tool_keys_creates_one_child_and_one_outbox(
    client, episode, settings, model_service
):
    run_id, actor, outcome, reference, sent = completed_plan(
        client, episode, settings, model_service
    )
    call = delegation_call(run_id, reference, key="first-dispatch")
    first = dispatched(client, settings, call)
    assert dispatch(client, settings, call).result == first
    assert dispatched(client, settings, delegation_call(run_id, reference, key="new-key")) == first
    child_id = UUID(first["child_run_id"])
    with client.app.state.session_factory() as db:
        child = db.get(WorkflowRun, child_id)
        root = db.get(AgentRunLimit, run_id)
        policy = db.get(AgentRunLimit, child_id)
        assert child.status == "QUEUED" and child.workflow_type == "script_storyboard"
        assert child.input_json["story"] == outcome.result.tasks[0].task.story
        assert child.input_json["screenwriter"]["agent_limits"] == root.limits_json
        assert policy.parent_run_id == run_id and policy.root_run_id == root.run_id
        assert policy.limits_json == root.limits_json and policy.deadline_at == root.deadline_at
        assert policy.depth == root.depth + 1
        assert len(list(db.scalars(select(WorkflowRun)))) == 2
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 1
        grant = db.scalar(select(AgentToolGrant).where(AgentToolGrant.run_id == child_id))
        assert (
            grant.agent_name == "screenwriter" and "investigate_story" in grant.allowed_tools_json
        )
        assert first["root_run_id"] == str(run_id)
        assert first["plan_sha256"] == digest(outcome.result.model_dump(mode="json"))
    assert len(sent) == 2
    with client.app.state.session_factory.begin() as db, pytest.raises(DomainError) as error:
        configure_agent_run(db, child_id, actor, AgentRunLimits(maximum_cost=None))
    assert error.value.code == "AGENT_LIMITS_CONFLICT"


@pytest.mark.parametrize("change", ["wrong_hash", "unknown_task", "unknown_plan", "new_limits"])
def test_invalid_delegation_is_rejected_without_child_or_outbox(
    client, episode, settings, model_service, change
):
    run_id, _, _, reference, _ = completed_plan(client, episode, settings, model_service)
    changed = {
        "wrong_hash": {"plan_sha256": "f" * 64},
        "unknown_task": {"task_key": "unplanned"},
        "unknown_plan": {"plan_id": str(uuid4())},
        "new_limits": {"agent_limits": {"maximum_cost": "999"}},
    }[change]
    feedback = dispatch(client, settings, delegation_call(run_id, reference, **changed))
    assert feedback.status == "FAILED" and feedback.error is not None
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(WorkflowRun)) == 1
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 0
        assert db.scalar(select(func.count()).select_from(AgentRunLimit)) == 1


def test_dependencies_and_unrelated_active_work_are_not_bypassed(
    client, episode, settings, model_service
):
    tasks = [writer_task(), writer_task("second", depends_on=("write",))]
    run_id, _, _, reference, _ = completed_plan(
        client, episode, settings, model_service, tasks=tasks
    )
    blocked = dispatch(client, settings, delegation_call(run_id, reference, task_key="second"))
    assert blocked.status == "FAILED"
    first = dispatched(client, settings, delegation_call(run_id, reference))
    blocked = dispatch(client, settings, delegation_call(run_id, reference, task_key="second"))
    assert blocked.status == "FAILED"
    assert child_for(client, first["child_run_id"]).status == "QUEUED"
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(WorkflowRun)) == 2
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 1


def test_new_delegation_rechecks_permission_and_does_not_replay_revoked_grant(
    client, episode, settings, model_service
):
    run_id, actor, _, reference, _ = completed_plan(client, episode, settings, model_service)
    call = delegation_call(run_id, reference)
    first = dispatched(client, settings, call)
    with client.app.state.session_factory.begin() as db:
        grant_agent_tools(
            db,
            AgentToolGrantRequest(run_id=run_id, agent_name="producer", allowed_tools=[]),
            actor,
            planning.producer_registry(settings),
        )
    feedback = dispatch(client, settings, call)
    assert feedback.status == "FAILED" and feedback.error.code == "USER_NOT_AUTHORIZED"
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(WorkflowRun)) == 2
        assert db.get(WorkflowRun, UUID(first["child_run_id"])).status == "QUEUED"


def test_disabled_actor_cannot_delegate_from_completed_plan(client, settings, model_service):
    register(client)
    episode = create_episode(client, create_project(client))
    run_id, actor, _, reference, _ = completed_plan(client, episode, settings, model_service)
    with client.app.state.session_factory.begin() as db:
        db.get(User, UUID(actor)).disabled = True
    feedback = dispatch(client, settings, delegation_call(run_id, reference))
    assert feedback.status == "FAILED" and feedback.error.code == "USER_NOT_AUTHORIZED"
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(WorkflowRun)) == 1


@pytest.mark.parametrize("replay", ["initial", "same-key", "new-key"])
def test_exact_director_input_becoming_stale_blocks_dispatch(
    client, episode, settings, model_service, replay
):
    source = approved_script(client, episode, count=1)
    tasks = [
        {
            "task_key": "direct",
            "depends_on": [],
            "reason": "根据已批准剧本生产分镜。",
            "task": {"kind": "director", "script_version_id": source["id"]},
        }
    ]
    run_id, _, _, reference, _ = completed_plan(
        client, episode, settings, model_service, tasks=tasks
    )
    call = delegation_call(run_id, reference, task_key="direct", key="direct-once")
    if replay != "initial":
        dispatched(client, settings, call)
    if replay == "new-key":
        call = delegation_call(run_id, reference, task_key="direct", key="direct-replay")
    with client.app.state.session_factory.begin() as db:
        db.get(Script, UUID(source["id"])).status = "STALE"
        db.get(AssetVersion, UUID(source["id"])).stale = True
    feedback = dispatch(client, settings, call)
    assert feedback.status == "FAILED"
    expected_children = int(replay != "initial")
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(WorkflowRun)) == 1 + expected_children
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == expected_children


def test_delegated_writer_reaches_original_script_review_and_keeps_root_active(
    client, episode, settings, model_service, monkeypatch
):
    run_id, _, _, reference, _ = completed_plan(client, episode, settings, model_service)
    receipt = dispatched(client, settings, delegation_call(run_id, reference))
    child_id = UUID(receipt["child_run_id"])
    sent, errors, ordinary_errors = install_writer(model_service, monkeypatch, settings, patches=0)
    waiting = run_once(client, settings, child_id)
    assert waiting["status"] == "WAITING_REVIEW", (waiting, errors, ordinary_errors)
    assert waiting["pending_review"]["resource_type"] == "script"
    assert not errors and not ordinary_errors and sent
    with client.app.state.session_factory() as db:
        assert db.get(WorkflowRun, run_id).status == "RUNNING"
        assert {row.root_run_id for row in db.scalars(select(AgentAction))} == {run_id}
        assert {row.run_id for row in db.scalars(select(AgentAction))} == {run_id, child_id}
        assert db.get(AgentRunLimit, child_id).parent_run_id == run_id


def test_delegated_director_produces_new_candidate_from_exact_script(
    client, episode, settings, model_service, monkeypatch
):
    source = approved_script(client, episode, count=1)
    tasks = [
        {
            "task_key": "direct",
            "depends_on": [],
            "reason": "从批准精确剧本制作分镜。",
            "task": {"kind": "director", "script_version_id": source["id"]},
        }
    ]
    run_id, _, _, reference, _ = completed_plan(
        client, episode, settings, model_service, tasks=tasks
    )
    receipt = dispatched(client, settings, delegation_call(run_id, reference, task_key="direct"))
    child_id = UUID(receipt["child_run_id"])
    sent, errors, base_errors = install_director(model_service, monkeypatch, settings)
    waiting = run_once(client, settings, child_id)
    assert waiting["status"] == "WAITING_REVIEW", (waiting, errors, base_errors)
    assert waiting["pending_review"]["resource_type"] == "storyboard"
    assert not errors and not base_errors and sent
    with client.app.state.session_factory() as db:
        child = db.get(WorkflowRun, child_id)
        assert child.input_json["script_version_id"] == source["id"]
        assert db.get(Script, UUID(source["id"])).status == "APPROVED"
        assert db.scalar(select(func.count()).select_from(Script)) == 1
        assert {row.root_run_id for row in db.scalars(select(AgentAction))} == {run_id}


def test_delegated_art_uses_original_proposal_review_and_shared_actions(
    client, episode, settings, model_service
):
    reference_set(client, episode)
    source = approved_script(client, episode, count=1)
    tasks = [
        {
            "task_key": "visual",
            "depends_on": [],
            "reason": "先比较已有参考并提出制作方案。",
            "task": {
                "kind": "art_director",
                "script_version_id": source["id"],
                "task": {"visual_goal": "比较雨夜角色的轮廓与配色，提供制作提示词"},
            },
        }
    ]
    run_id, _, _, reference, _ = completed_plan(
        client, episode, settings, model_service, tasks=tasks
    )
    receipt = dispatched(client, settings, delegation_call(run_id, reference, task_key="visual"))
    child_id = UUID(receipt["child_run_id"])
    sent, errors, _ = install_direction_model(model_service)
    waiting = run_once(client, settings, child_id)
    assert waiting["status"] == "WAITING_REVIEW", (waiting, errors)
    assert waiting["pending_review"]["resource_type"] == "art_proposal"
    assert sent and not errors
    with client.app.state.session_factory() as db:
        assert db.get(AgentRunLimit, child_id).parent_run_id == run_id
        assert {row.root_run_id for row in db.scalars(select(AgentAction))} == {run_id}


def test_delegated_reviewer_executes_real_checked_candidate_without_approval(
    client, episode, settings, model_service, reviewer_case
):
    with client.app.state.session_factory.begin() as db:
        # Retire the investigation-only fixture container before real production starts.
        db.get(WorkflowRun, reviewer_case["run_id"]).status = "CANCELLED"
    target = reviewer_case["target"]
    tasks = [
        {
            "task_key": "check",
            "depends_on": [],
            "reason": "检查实际图片候选与精确镜头参考。",
            "task": {
                "kind": "continuity_reviewer",
                **{name: target[name] for name in ("candidate_id", "storyboard_id", "shot_id")},
            },
        }
    ]
    run_id, _, _, reference, _ = completed_plan(
        client, episode, settings, model_service, tasks=tasks
    )
    receipt = dispatched(client, settings, delegation_call(run_id, reference, task_key="check"))
    child_id = UUID(receipt["child_run_id"])
    sent, errors, deliveries = install_reviewer_model(model_service, dynamic=False)
    finished = run_once(client, settings, child_id)
    assert finished["status"] == "WAITING_REVIEW", (finished, errors)
    assert finished["pending_review"]["destination"] == "READY_FOR_REVIEW", finished
    assert len(deliveries) == 1 and sent and not errors
    with client.app.state.session_factory() as db:
        assert db.get(AgentRunLimit, child_id).parent_run_id == run_id
        assert db.get(AssetVersion, UUID(target["asset_version_id"])).status != "APPROVED"
        assert db.get(WorkflowRun, run_id).status == "RUNNING"
        actions = list(db.scalars(select(AgentAction).where(AgentAction.root_run_id == run_id)))
        assert {row.run_id for row in actions} == {run_id, child_id}


def test_root_model_call_cap_is_enforced_by_real_child_before_network(
    client, episode, settings, model_service, monkeypatch
):
    limits = AgentRunLimits(max_model_calls=2, maximum_cost=Decimal("0"), max_duration_seconds=3600)
    run_id, _, _, reference, _ = completed_plan(
        client, episode, settings, model_service, limits=limits
    )
    receipt = dispatched(client, settings, delegation_call(run_id, reference))
    sent, errors, ordinary_errors = install_writer(model_service, monkeypatch, settings)
    failed = run_once(client, settings, UUID(receipt["child_run_id"]))
    assert failed["status"] == "FAILED" and failed["error_code"] == "AGENT_MODEL_CALL_LIMIT", failed
    assert sent == [] and not errors and not ordinary_errors
    with client.app.state.session_factory() as db:
        usage = action_usage(db, run_id)
        assert sum(row.kind == "MODEL" for row in usage["actions"]) == 2
        assert db.get(WorkflowRun, run_id).status == "RUNNING"


@pytest.mark.parametrize(
    "quote,code",
    [
        (Decimal("0.02"), "AGENT_BUDGET_EXCEEDED"),
        (None, "BUDGET_QUOTE_UNAVAILABLE"),
    ],
)
def test_real_child_cannot_overspend_producer_actual_cost_or_use_unknown_quote(
    client, episode, settings, model_service, monkeypatch, quote, code
):
    set_policy(client, episode["project_id"], "100")
    limits = AgentRunLimits(maximum_cost=Decimal("0.03"), max_duration_seconds=3600)
    run_id, actor, _, reference, root_sent = completed_plan(
        client, episode, settings, model_service, limits=limits, quote="0.01", cost="0.01"
    )
    assert len(root_sent) == 2
    with client.app.state.session_factory.begin() as db:
        usage = action_usage(db, run_id)
        assert usage["actual_cost"] == 0 and usage["pending_estimate"] == Decimal("0.02")
        # Supplier token usage carries no trusted amount; the owner confirms each invoice.
        calls = [row for row in usage["actions"] if row.kind == "MODEL"]
        assert len(calls) == 2
        for index, action in enumerate(calls):
            reconcile(
                db,
                db.get(GatewayCall, action.gateway_call_id),
                actor,
                "SUCCEEDED",
                GatewayUsage(actual_cost=Decimal("0.01")),
                f"producer-invoice-{index}",
            )
    settings.model_gateway_policy = GatewayPolicy(
        text_maximum_cost=quote,
        pricing_version="prod02-child:1" if quote is not None else "unconfigured",
    )
    receipt = dispatched(client, settings, delegation_call(run_id, reference))
    sent, errors, ordinary_errors = install_writer(model_service, monkeypatch, settings)
    failed = run_once(client, settings, UUID(receipt["child_run_id"]))
    assert failed["status"] == "FAILED" and failed["error_code"] == code, failed
    assert sent == [] and not errors and not ordinary_errors
    with client.app.state.session_factory() as db:
        usage = action_usage(db, run_id)
        assert usage["actual_cost"] == Decimal("0.02")
        assert usage["pending_estimate"] == 0 and not usage["unknown_cost"]
        assert {row.run_id for row in usage["actions"] if row.kind == "MODEL"} == {run_id}
        assert db.get(WorkflowRun, run_id).status == "RUNNING"


@pytest.mark.parametrize(
    "policy,code",
    [
        ({"max_delegations": 0}, "AGENT_DELEGATION_LIMIT"),
        ({"max_delegation_depth": 0}, "AGENT_DELEGATION_DEPTH"),
    ],
)
def test_zero_root_delegation_quantity_or_depth_cannot_create_child_or_outbox(
    client, episode, settings, model_service, policy, code
):
    limits = AgentRunLimits(maximum_cost=Decimal("0"), max_duration_seconds=3600, **policy)
    run_id, _, _, reference, _ = completed_plan(
        client, episode, settings, model_service, limits=limits
    )
    feedback = dispatch(client, settings, delegation_call(run_id, reference))
    assert feedback.status == "FAILED" and feedback.error.code == code
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(WorkflowRun)) == 1
        assert db.scalar(select(func.count()).select_from(AgentRunLimit)) == 1
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 0


@pytest.mark.parametrize("status", ["WAITING_REVIEW", "CANCELLED"])
@pytest.mark.parametrize("same_key", [True, False])
def test_successful_delegation_replay_rechecks_paused_or_cancelled_root(
    client, episode, settings, model_service, status, same_key
):
    run_id, _, _, reference, _ = completed_plan(client, episode, settings, model_service)
    call = delegation_call(run_id, reference, key="before-pause")
    first = dispatched(client, settings, call)
    with client.app.state.session_factory.begin() as db:
        db.get(WorkflowRun, run_id).status = status
    if not same_key:
        call = delegation_call(run_id, reference, key="after-pause")
    feedback = dispatch(client, settings, call)
    assert feedback.status == "FAILED" and feedback.error.code in {
        "AGENT_NOT_ACTIVE",
        "AGENT_CANCELLED",
        "AGENT_NOT_ALLOWED",
        "WORKFLOW_NOT_ACTIVE",
    }
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(WorkflowRun)) == 2
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 1
        assert db.get(WorkflowRun, UUID(first["child_run_id"])).status == "QUEUED"


@pytest.mark.parametrize("role", ["editor", "viewer"])
def test_collaborator_permissions_and_actor_identity_survive_real_delegation(
    client, settings, model_service, monkeypatch, role
):
    owner = register(client)
    project = create_project(client)
    episode = create_episode(client, project)
    member = register(client, "producer-member@example.com")
    actor = member["user"]["id"]
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    added = client.post(
        f"/api/v1/projects/{project['id']}/members",
        json={"email": member["user"]["email"], "role": role},
    )
    assert added.status_code == 201, added.text
    client.headers["Authorization"] = "Bearer " + member["access_token"]
    if role == "viewer":
        with pytest.raises(DomainError) as forbidden:
            completed_plan(client, episode, settings, model_service, actor=actor)
        assert forbidden.value.code == "FORBIDDEN"
        with client.app.state.session_factory() as db:
            assert db.scalar(select(func.count()).select_from(WorkflowRun)) == 0
        return
    run_id, _, _, reference, _ = completed_plan(
        client, episode, settings, model_service, actor=actor
    )
    call = delegation_call(run_id, reference)
    receipt = dispatched(client, settings, call)
    sent, errors, ordinary_errors = install_writer(model_service, monkeypatch, settings)
    finished = run_once(client, settings, UUID(receipt["child_run_id"]))
    assert finished["status"] == "WAITING_REVIEW", (finished, errors, ordinary_errors)
    assert finished["pending_review"]["resource_type"] == "script"
    assert sent and not errors and not ordinary_errors
    with client.app.state.session_factory() as db:
        child = db.get(WorkflowRun, UUID(receipt["child_run_id"]))
        assert child.input_json["created_by"] == actor
        assert db.get(AgentRunLimit, child.id).actor_id == actor
        assert db.get(AgentRunLimit, run_id).actor_id == actor
        assert db.get(AgentRunLimit, child.id).parent_run_id == run_id
        original_outbox_count = db.scalar(select(func.count()).select_from(OutboxEvent))

    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    revoked = client.patch(
        f"/api/v1/projects/{project['id']}/members/{actor}", json={"role": "viewer"}
    )
    assert revoked.status_code == 200, revoked.text
    client.headers["Authorization"] = "Bearer " + member["access_token"]
    feedback = dispatch(client, settings, call)
    assert feedback.status == "FAILED" and feedback.error.code == "USER_NOT_AUTHORIZED"
    with client.app.state.session_factory() as db:
        assert db.get(WorkflowRun, UUID(receipt["child_run_id"])).status == "WAITING_REVIEW"
        assert db.scalar(select(func.count()).select_from(WorkflowRun)) == 2
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == original_outbox_count
