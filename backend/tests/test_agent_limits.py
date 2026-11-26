"""ARUN-04: real gateway/tool admission with isolated ledgers and fake external I/O."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from threading import Barrier, Event
from uuid import UUID, uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError
from test_agent_model import (
    FinalResult,
    add_image,
    final_response,
    model_request,
)
from test_agent_model import model_service as model_service
from test_agent_tools import activate_run, create_entry, create_run, grant, registry_for
from test_collaboration import create_episode, create_project, register
from test_concurrency import require_postgres
from test_context_trace import evidence
from test_generation_budget import priced
from test_generation_delivery import Provider, deliver
from test_generation_jobs import capture_ok
from test_model_gateway import request, route

from arclight.application import agent_limits, gateway_media, model_gateway
from arclight.application.agent_limits import action_usage, configure_agent_run
from arclight.application.agent_tools import build_agent_tool_registry, execute_agent_tool
from arclight.application.gateway_accounting import reconcile
from arclight.application.gateway_agent import invoke_agent
from arclight.application.model_gateway import admit, invoke, utc
from arclight.core.errors import DomainError
from arclight.domain.agent_limits import AgentExecutionContext, AgentRunLimits
from arclight.domain.agent_tools import AgentToolCall
from arclight.domain.model_gateway import GatewayFailure, GatewayPolicy, GatewayUsage
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.base import utcnow
from arclight.persistence.generation_accounting import GenerationBudgetReservation
from arclight.persistence.model_gateway import GatewayCall, GatewayReceipt
from arclight.persistence.models import (
    AgentToolGrant,
    AgentToolInvocation,
    Project,
    StoryEntry,
    User,
    WorkflowRun,
)


def configure_with_historical_limits(db, identity, actor, limits, *, parent=None):
    if limits:

        def seed_historical_policy(session, *_):
            control = next(row for row in session.new if isinstance(row, AgentRunLimit))
            control.limits_json = {"schema_version": 1, **limits}
            control.deadline_at = utcnow() + timedelta(
                seconds=limits.get("max_duration_seconds", 300)
            )

        event.listen(db, "before_flush", seed_historical_policy, once=True)
    return configure_agent_run(db, identity, actor, parent_run_id=parent)


def bounded_run(client, episode, *, parent=None, **limits):
    identity = create_run(client, episode, key=uuid4().hex)
    actor = activate_run(client, identity, episode["id"])
    with client.app.state.session_factory.begin() as db:
        configure_with_historical_limits(db, identity, actor, limits, parent=parent)
    return identity, actor


def sibling_episode(client, episode, number):
    response = client.post(
        f"/api/v1/projects/{episode['project_id']}/episodes",
        json={"episode_no": number, "title": f"委派 {number}"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def execution(run_id, step="turn-1", *, kind="MODEL", agent="screenwriter"):
    return AgentExecutionContext(
        run_id=run_id,
        agent_name=agent,
        stage="script",
        step_key=step,
        kind=kind,
        tool_name="read_story_entry" if kind == "TOOL" else None,
    )


def gateway_request(episode, run_id, actor, selected, *, step="turn-1", **patch):
    return request(
        episode,
        selected,
        workflow_id=run_id,
        actor=actor,
        task="script",
        execution=execution(run_id, step),
        **patch,
    )


def model_action(client, episode, run_id, actor, *, step="turn-1", selected=None, usage=None):
    selected = selected or route(maximum_cost=Decimal("0"))
    return invoke(
        client.app.state.session_factory,
        gateway_request(episode, run_id, actor, selected, step=step),
        selected,
        lambda: "observed",
        policy=GatewayPolicy(),
        usage=lambda _: usage or GatewayUsage(actual_cost=Decimal("0")),
    )


def tool_setup(client, episode, run_id, actor, *, handler=None):
    calls = []

    def read(context, parameters):
        calls.append(parameters.entry_id)
        return {"entry_id": parameters.entry_id, "project_id": context.project_id, "data": {}}

    registry = registry_for(handler or read)
    grant(client, run_id, actor, registry)
    call = AgentToolCall(
        run_id=run_id,
        tool_name="read_story_entry",
        idempotency_key=uuid4().hex,
        arguments={"entry_id": str(create_entry(client, episode["project_id"]))},
    )
    return registry, call, calls


def tool_action(client, registry, call, *, step="turn-1"):
    with client.app.state.session_factory.begin() as db:
        return execute_agent_tool(db, registry, "screenwriter", call, step_key=step)


def usage_for(client, run_id):
    with client.app.state.session_factory() as db:
        return action_usage(db, run_id)


@pytest.mark.parametrize(
    "patch",
    [
        {"schema_version": 2},
        {"schema_version": True},
        {"schema_version": 1.0},
        {"max_steps": -1},
        {"max_steps": 1025},
        {"max_calls": True},
        {"max_calls": "1"},
        {"max_model_calls": 1.2},
        {"max_tool_calls": -1},
        {"max_agent_calls": 4097},
        {"max_duration_seconds": 0},
        {"max_duration_seconds": 86401},
        {"max_delegations": 129},
        {"max_delegation_depth": 9},
        {"maximum_cost": "-1"},
        {"maximum_cost": "0.0000001"},
        {"maximum_cost": "NaN"},
        {"maximum_cost": "Infinity"},
        {"unbounded": True},
    ],
)
def test_policy_rejects_unknown_versions_invalid_types_and_unbounded_values(patch):
    with pytest.raises(ValidationError):
        AgentRunLimits.model_validate(patch)


def test_contract_is_frozen_and_execution_is_explicit_server_scope():
    limits = AgentRunLimits()
    assert AgentRunLimits.model_validate_json(limits.model_dump_json()) == limits
    with pytest.raises(ValidationError):
        limits.max_calls = 100
    context = execution(uuid4())
    for patch in (
        {"run_id": "invalid"},
        {"step_key": ""},
        {"step_key": "a" * 129},
        {"step_key": "SQL; DROP"},
        {"agent_name": "root"},
        {"stage": "shell"},
        {"kind": "TOOL"},
        {"tool_name": "read_story_entry"},
        {"budget": "1000"},
    ):
        with pytest.raises(ValidationError):
            AgentExecutionContext.model_validate(context.model_dump(mode="json") | patch)


def test_control_binds_actor_input_and_cannot_be_installed_late(client, episode, monkeypatch):
    run_id, actor = bounded_run(client, episode, max_calls=1)
    factory = client.app.state.session_factory
    with factory.begin() as db:
        original = db.get(AgentRunLimit, run_id)
        repeated = configure_agent_run(
            db,
            run_id,
            actor,
        )
        assert repeated.run_id == original.run_id
        assert repeated.created_at == original.created_at
        assert repeated.input_sha256 and repeated.actor_id == actor
        assert repeated.limits_json["max_calls"] == 1
    scope_for_run = agent_limits.scope_for_run
    with monkeypatch.context() as patch:
        patch.setattr(
            agent_limits,
            "scope_for_run",
            lambda *args: scope_for_run(*args).model_copy(update={"input_sha256": "f" * 64}),
        )
        with factory.begin() as db, pytest.raises(DomainError) as changed:
            configure_agent_run(db, run_id, actor)
        assert changed.value.code == "AGENT_LIMITS_CONFLICT"
    with factory.begin() as db, pytest.raises(DomainError):
        configure_agent_run(
            db,
            run_id,
            str(uuid4()),
        )
    other_episode = sibling_episode(client, episode, 2)
    other_run = create_run(client, other_episode)
    other_actor = activate_run(client, other_run, other_episode["id"])
    selected = route(maximum_cost=Decimal("0"))
    invoke(
        factory,
        request(other_episode, selected, workflow_id=other_run, actor=other_actor, task="script"),
        selected,
        lambda: "unbounded legacy call",
        policy=GatewayPolicy(),
    )
    with factory.begin() as db, pytest.raises(DomainError) as late:
        configure_agent_run(db, other_run, other_actor)
    assert late.value.code == "AGENT_LIMITS_LATE"


@pytest.mark.parametrize(
    "field,kind",
    [
        ("max_calls", "MODEL"),
        ("max_model_calls", "MODEL"),
        ("max_tool_calls", "TOOL"),
        ("max_agent_calls", "MODEL"),
        ("max_steps", "MODEL"),
    ],
)
def test_historical_zero_limit_allows_calls_with_action_receipts(client, episode, field, kind):
    run_id, actor = bounded_run(client, episode, **{field: 0})
    if kind == "TOOL":
        registry, call, calls = tool_setup(client, episode, run_id, actor)
        feedback = tool_action(client, registry, call)
        assert feedback.status == "SUCCEEDED" and len(calls) == 1
    else:
        assert model_action(client, episode, run_id, actor) == "observed"
    assert [row.kind for row in usage_for(client, run_id)["actions"]] == [kind]


def test_one_turn_shares_step_but_counts_model_and_tool_separately(client, episode):
    run_id, actor = bounded_run(client, episode, max_steps=1, max_calls=3, max_model_calls=1)
    registry, call, calls = tool_setup(client, episode, run_id, actor)
    assert model_action(client, episode, run_id, actor) == "observed"
    assert tool_action(client, registry, call).status == "SUCCEEDED"
    assert len(calls) == 1
    next_call = call.model_copy(update={"idempotency_key": "another-logical-tool"})
    assert tool_action(client, registry, next_call, step="turn-2").status == "SUCCEEDED"
    third = call.model_copy(update={"idempotency_key": "third-logical-tool"})
    assert tool_action(client, registry, third).status == "SUCCEEDED"
    assert len(calls) == 3
    records = usage_for(client, run_id)["actions"]
    assert len(records) == 4 and {row.step_key for row in records} == {"turn-1", "turn-2"}
    assert sorted(row.kind for row in records) == ["MODEL", "TOOL", "TOOL", "TOOL"]


def test_failed_tool_consumes_once_rolls_back_domain_write_and_replays_without_new_slot(
    client,
    episode,
):
    run_id, actor = bounded_run(client, episode, max_calls=1)

    def broken(context, parameters):
        entry = context.db.get(StoryEntry, parameters.entry_id)
        entry.name = "must-roll-back"
        context.db.flush()
        raise RuntimeError("private-tool-failure")

    registry, call, _ = tool_setup(client, episode, run_id, actor, handler=broken)
    failed = tool_action(client, registry, call)
    assert failed.error.code == "EXECUTION_FAILED"
    assert (
        tool_action(client, registry, call, step="replay-step").model_dump() == failed.model_dump()
    )
    different = call.model_copy(update={"idempotency_key": "new-call"})
    assert tool_action(client, registry, different).error.code == "EXECUTION_FAILED"
    with client.app.state.session_factory() as db:
        assert db.get(StoryEntry, UUID(call.arguments["entry_id"])).name != "must-roll-back"
        assert db.scalar(select(func.count()).select_from(AgentToolInvocation)) == 2
    assert len(usage_for(client, run_id)["actions"]) == 2


@pytest.mark.parametrize("missing", [None, "unconfigured"])
@pytest.mark.parametrize("kind", ["MODEL", "TOOL"])
def test_paid_call_can_execute_without_a_verified_quote(client, episode, missing, kind):
    run_id, actor = bounded_run(client, episode, maximum_cost="10")
    if kind == "TOOL":
        tool_setup(client, episode, run_id, actor)
    selected = route(
        maximum_cost=None if missing is None else Decimal("2"),
        pricing_version="trusted:1" if missing is None else missing,
    )
    assert (
        invoke(
            client.app.state.session_factory,
            gateway_request(episode, run_id, actor, selected).model_copy(
                update={"execution": execution(run_id, kind=kind)}
            ),
            selected,
            lambda: "observed",
            policy=GatewayPolicy(),
        )
        == "observed"
    )
    assert [row.kind for row in usage_for(client, run_id)["actions"]] == [kind]


def test_unknown_money_reserves_until_existing_owner_reconciliation_then_releases_difference(
    client,
    episode,
):
    run_id, actor = bounded_run(client, episode, maximum_cost="3")
    selected = route()
    assert (
        model_action(
            client, episode, run_id, actor, selected=selected, usage=GatewayUsage(input_tokens=11)
        )
        == "observed"
    )
    pending = usage_for(client, run_id)
    assert pending["actual_cost"] == 0 and pending["pending_estimate"] == 2
    assert (
        model_action(
            client,
            episode,
            run_id,
            actor,
            step="turn-2",
            selected=selected,
            usage=GatewayUsage(actual_cost=Decimal("2")),
        )
        == "observed"
    )
    with client.app.state.session_factory.begin() as db:
        call = db.get(GatewayCall, pending["actions"][0].gateway_call_id)
        reconcile(db, call, actor, "SUCCEEDED", GatewayUsage(actual_cost=Decimal("1")), "invoice-1")
    settled = usage_for(client, run_id)
    assert settled["actual_cost"] == 3 and settled["pending_estimate"] == 0
    assert (
        model_action(
            client,
            episode,
            run_id,
            actor,
            step="turn-3",
            selected=selected,
            usage=GatewayUsage(actual_cost=Decimal("2")),
        )
        == "observed"
    )
    assert model_action(client, episode, run_id, actor, step="free") == "observed"
    final = usage_for(client, run_id)
    assert final["actual_cost"] == 5 and final["pending_estimate"] == 0


@pytest.mark.parametrize("maximum", [1, 2])
def test_each_safe_retry_is_recorded_despite_historical_call_limits(client, episode, maximum):
    run_id, actor = bounded_run(client, episode, max_model_calls=maximum, max_steps=1)
    selected = route(maximum_cost=Decimal("0"))
    calls = []

    def safe_failure():
        calls.append(1)
        raise GatewayFailure("PROVIDER_UNAVAILABLE", not_accepted=True)

    request = gateway_request(episode, run_id, actor, selected)
    for _ in range(2):
        with pytest.raises(DomainError) as caught:
            invoke(
                client.app.state.session_factory,
                request,
                selected,
                safe_failure,
                policy=GatewayPolicy(),
            )
        assert caught.value.code == "GATEWAY_NOT_ACCEPTED"
    assert len(calls) == 2
    records = usage_for(client, run_id)
    assert len(records["actions"]) == 2 and records["pending_estimate"] == 0
    assert {row.step_key for row in records["actions"]} == {"turn-1"}


def test_nested_delegates_share_action_accounting_and_run_lineage(client, episode):
    root, actor = bounded_run(
        client,
        episode,
        maximum_cost="3",
        max_steps=2,
        max_agent_calls=2,
        max_delegation_depth=2,
        max_delegations=2,
    )
    child_episode = sibling_episode(client, episode, 2)
    child, _ = bounded_run(client, child_episode, parent=root)
    grand_episode = sibling_episode(client, episode, 3)
    grandchild, _ = bounded_run(client, grand_episode, parent=child)
    with client.app.state.session_factory() as db:
        controls = [db.get(AgentRunLimit, key) for key in (root, child, grandchild)]
        assert controls[0].deadline_at is not None
        assert all(row.deadline_at is None for row in controls[1:])
        assert [row.depth for row in controls] == [0, 1, 2]
        assert all(row.root_run_id == root for row in controls)
    assert (
        model_action(
            client,
            child_episode,
            child,
            actor,
            selected=route(),
            usage=GatewayUsage(input_tokens=4),
        )
        == "observed"
    )
    assert model_action(client, grand_episode, grandchild, actor, selected=route()) == "observed"
    assert model_action(client, episode, root, actor) == "observed"
    assert len(usage_for(client, root)["actions"]) == 3


@pytest.mark.parametrize(
    "limits,parent_kind",
    [
        ({"max_delegation_depth": 0}, "root"),
        ({"max_delegations": 0}, "root"),
        ({"max_delegation_depth": 1}, "child"),
    ],
)
def test_delegation_can_exceed_historical_quantity_and_depth(client, episode, limits, parent_kind):
    root, _ = bounded_run(client, episode, **limits)
    parent = root
    if parent_kind == "child":
        parent, _ = bounded_run(client, sibling_episode(client, episode, 2), parent=root)
    child, _ = bounded_run(client, sibling_episode(client, episode, 3), parent=parent)
    with client.app.state.session_factory() as db:
        row = db.get(AgentRunLimit, child)
        assert (row.parent_run_id, row.root_run_id, row.depth) == (
            parent,
            root,
            2 if parent_kind == "child" else 1,
        )


def test_delegation_rejects_cross_project_and_parent_rebinding(client, episode):
    root, actor = bounded_run(client, episode, max_calls=2)
    child_episode = sibling_episode(client, episode, 2)
    child = create_run(client, child_episode)
    activate_run(client, child, child_episode["id"])
    with client.app.state.session_factory.begin() as db:
        assert configure_agent_run(db, child, actor, parent_run_id=root).root_run_id == root
        assert configure_agent_run(db, child, actor, parent_run_id=root).root_run_id == root
    with client.app.state.session_factory.begin() as db, pytest.raises(DomainError) as rebound:
        configure_agent_run(
            db,
            child,
            actor,
        )
    assert rebound.value.code == "AGENT_LIMITS_CONFLICT"
    foreign_episode = create_episode(client, create_project(client))
    with pytest.raises(DomainError) as foreign:
        bounded_run(client, foreign_episode, parent=root)
    assert foreign.value.code == "AGENT_SCOPE_INVALID"


@pytest.mark.parametrize(
    "change,code",
    [
        ("cancel", "AGENT_CANCELLED"),
        ("inactive", "AGENT_NOT_ACTIVE"),
        ("input", "AGENT_INPUT_CHANGED"),
        ("state", "RUN_STATE_INVALID"),
        ("missing_state", "RUN_STATE_INVALID"),
        ("stage", "AGENT_NOT_ACTIVE"),
        ("project", "PROJECT_ARCHIVED"),
    ],
)
def test_each_admission_rechecks_live_run_and_scope(client, episode, monkeypatch, change, code):
    run_id, actor = bounded_run(client, episode)
    selected = route(maximum_cost=Decimal("0"))
    call = gateway_request(episode, run_id, actor, selected)
    with client.app.state.session_factory.begin() as db:
        run = db.get(WorkflowRun, run_id)
        if change == "cancel":
            run.status = "CANCELLED"
        elif change == "inactive":
            run.status = "WAITING_REVIEW"
        elif change == "input":
            # Existing DB guards already reject input rewrites. Also exercise the
            # new boundary against a stale/corrupt executor scope without dropping guards.
            with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"), db.begin_nested():
                run.input_json = {**run.input_json, "story": "changed frozen story"}
                db.flush()
            original_scope = agent_limits.scope_for_run
            monkeypatch.setattr(
                agent_limits,
                "scope_for_run",
                lambda *args: original_scope(*args).model_copy(update={"input_sha256": "f" * 64}),
            )
        elif change == "state":
            run.agent_state_json = {**run.agent_state_json, "run_id": str(uuid4())}
        elif change == "missing_state":
            from sqlalchemy import null

            run.agent_state_json = null()
        elif change == "stage":
            call = call.model_copy(
                update={"execution": execution(run_id).model_copy(update={"stage": "outline"})}
            )
        else:
            db.get(Project, UUID(episode["project_id"])).archived = True
    with pytest.raises(DomainError) as caught:
        invoke(
            client.app.state.session_factory,
            call,
            selected,
            lambda: pytest.fail("Invalid live scope reached I/O"),
            policy=GatewayPolicy(),
        )
    assert caught.value.code == code
    assert usage_for(client, run_id)["actions"] == []


def test_parent_cancellation_stops_children_even_with_an_expired_historical_control(
    client, episode
):
    root, actor = bounded_run(client, episode, max_duration_seconds=0)
    child_episode = sibling_episode(client, episode, 2)
    child, _ = bounded_run(client, child_episode, parent=root)
    with client.app.state.session_factory.begin() as db:
        db.get(WorkflowRun, root).status = "CANCELLED"
    with pytest.raises(DomainError) as cancelled:
        model_action(client, child_episode, child, actor)
    assert cancelled.value.code == "AGENT_CANCELLED"
    with client.app.state.session_factory.begin() as db:
        db.get(WorkflowRun, root).status = "RUNNING"
        assert utc(db.get(AgentRunLimit, root).deadline_at) <= utcnow()
    assert model_action(client, child_episode, child, actor) == "observed"


@pytest.mark.parametrize("disabled", [False, True])
def test_current_user_permission_is_rechecked_for_root_and_delegates(client, disabled):
    account = register(client)
    episode = create_episode(client, create_project(client))
    root, actor = bounded_run(client, episode)
    child_episode = sibling_episode(client, episode, 2)
    child, _ = bounded_run(client, child_episode, parent=root)
    with client.app.state.session_factory.begin() as db:
        if disabled:
            db.get(User, UUID(account["user"]["id"])).disabled = True
        else:
            db.get(Project, UUID(episode["project_id"])).owner_id = "service-owner"
    with pytest.raises(DomainError) as caught:
        model_action(client, child_episode, child, actor)
    assert caught.value.code in {"FORBIDDEN", "NOT_FOUND"}
    assert usage_for(client, root)["actions"] == []


def test_bounded_run_cannot_omit_or_change_executor_context(client, episode):
    run_id, actor = bounded_run(client, episode)
    selected = route(maximum_cost=Decimal("0"))
    call = gateway_request(episode, run_id, actor, selected)
    for context, code in (
        (None, "AGENT_CONTEXT_REQUIRED"),
        (execution(uuid4()), "AGENT_SCOPE_INVALID"),
    ):
        with client.app.state.session_factory.begin() as db, pytest.raises(DomainError) as caught:
            admit(db, call.model_copy(update={"execution": context}), selected, GatewayPolicy())
        assert caught.value.code == code


@pytest.mark.parametrize("change", ["cancel", "tool_grant"])
def test_dispatch_rechecks_after_admission_and_records_safe_zero_without_sending(
    client,
    episode,
    monkeypatch,
    change,
):
    run_id, actor = bounded_run(client, episode)
    selected = route(maximum_cost=Decimal("0"))
    registry, _, _ = tool_setup(client, episode, run_id, actor)
    call = gateway_request(episode, run_id, actor, selected)
    if change == "tool_grant":
        call = call.model_copy(update={"execution": execution(run_id, kind="TOOL")})
    original = model_gateway.admit

    def admit_then_revoke(db, *args):
        row = original(db, *args)
        if change == "cancel":
            db.get(WorkflowRun, run_id).status = "CANCELLED"
        else:
            grant_row = db.scalar(select(AgentToolGrant).where(AgentToolGrant.run_id == run_id))
            grant_row.allowed_tools_json = []
        return row

    monkeypatch.setattr(model_gateway, "admit", admit_then_revoke)
    with pytest.raises(DomainError) as caught:
        invoke(
            client.app.state.session_factory,
            call,
            selected,
            lambda: pytest.fail("Revoked call reached provider"),
            policy=GatewayPolicy(),
        )
    assert caught.value.code == (
        "AGENT_CANCELLED" if change == "cancel" else "AGENT_TOOL_GRANT_FORBIDDEN"
    )
    with client.app.state.session_factory() as db:
        receipt = db.scalar(select(GatewayReceipt))
        assert receipt.status == "NOT_ACCEPTED" and receipt.actual_cost == 0
    assert len(usage_for(client, run_id)["actions"]) == 1
    assert usage_for(client, run_id)["pending_estimate"] == 0


def test_external_io_has_no_business_locks_and_next_call_sees_cancellation(client, episode):
    run_id, actor = bounded_run(client, episode)
    selected = route(maximum_cost=Decimal("0"))
    factory = client.app.state.session_factory

    def provider():
        # This transaction runs on the bounded adapter thread, on another DB connection.
        with factory.begin() as db:
            if db.get_bind().dialect.name == "postgresql":
                db.execute(text("SET LOCAL lock_timeout = '1000ms'"))
            db.get(Project, UUID(episode["project_id"])).name = "updated during external I/O"
            db.get(WorkflowRun, run_id).status = "CANCELLED"
            db.flush()
        return "completed-before-cancel-observed"

    assert (
        invoke(
            factory,
            gateway_request(episode, run_id, actor, selected),
            selected,
            provider,
            policy=GatewayPolicy(),
            usage=lambda _: GatewayUsage(actual_cost=Decimal("0")),
        )
        == "completed-before-cancel-observed"
    )
    with pytest.raises(DomainError) as caught:
        model_action(client, episode, run_id, actor, step="turn-2")
    assert caught.value.code == "AGENT_CANCELLED"


def test_single_request_timeout_keeps_unknown_result_reservation_without_retry(client, episode):
    run_id, actor = bounded_run(client, episode, maximum_cost="2")
    entered, release = Event(), Event()
    calls = []

    def slow():
        calls.append(1)
        entered.set()
        release.wait(timeout=3)
        return "late-result"

    selected = route()
    call = gateway_request(episode, run_id, actor, selected)
    try:
        with pytest.raises(DomainError) as timed_out:
            invoke(
                client.app.state.session_factory,
                call,
                selected,
                slow,
                policy=GatewayPolicy(timeout_seconds=0.25),
            )
        assert entered.is_set() and timed_out.value.code == "GATEWAY_RECONCILIATION_REQUIRED"
        with pytest.raises(DomainError) as repeated:
            invoke(
                client.app.state.session_factory,
                call,
                selected,
                lambda: pytest.fail("Unknown call was re-sent"),
                policy=GatewayPolicy(),
            )
        assert repeated.value.code == "GATEWAY_RECONCILIATION_REQUIRED" and calls == [1]
    finally:
        release.set()
    pending = usage_for(client, run_id)
    assert pending["actual_cost"] == 0 and pending["pending_estimate"] == 2
    with client.app.state.session_factory() as db:
        receipt = db.scalar(select(GatewayReceipt))
        assert receipt.status == "RESULT_UNKNOWN" and receipt.actual_cost is None


@pytest.mark.parametrize("vision", [False, True])
@pytest.mark.parametrize("quoted", [False, True])
def test_real_text_and_vision_record_unknown_money_with_or_without_a_quote(
    client,
    episode,
    settings,
    model_service,
    vision,
    quoted,
):
    images = [add_image(client, episode), add_image(client, episode)] if vision else []
    if quoted:
        # The production version freezes this policy when the run is created.
        settings.model_gateway_policy = GatewayPolicy(
            text_maximum_cost=Decimal("2"), pricing_version="trusted-test:1"
        )
    run_id, _ = bounded_run(client, episode, maximum_cost="2")
    patch = {}
    if images:
        added = evidence(client, str(run_id), asset_version_ids=[row["id"] for row in images])
        assert added.status_code == 201, added.text
        patch = {
            "evidence_round": 1,
            "messages": [
                {
                    "role": "user",
                    "data": {},
                    "image_file_ids": [
                        row["object_ref"]["file_id"] for row in added.json()["sources"]
                    ],
                }
            ],
        }
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json=final_response())

    model_service(handler)
    registry = build_agent_tool_registry()
    call = model_request(run_id, **patch)
    result = invoke_agent(
        client.app.state.session_factory,
        settings,
        call,
        registry,
        FinalResult,
        execution=execution(run_id),
    )
    assert result.status == "FINAL" and len(seen) == 1
    another = invoke_agent(
        client.app.state.session_factory,
        settings,
        model_request(run_id, operation_key="another-call", **patch),
        registry,
        FinalResult,
        execution=execution(run_id, "turn-2"),
    )
    assert another.status == "FINAL" and len(seen) == 2
    current = usage_for(client, run_id)
    assert current["actual_cost"] == 0 and current["pending_estimate"] == (4 if quoted else 0)
    assert len(current["actions"]) == 2
    with client.app.state.session_factory() as db:
        call_row = db.get(GatewayCall, current["actions"][0].gateway_call_id)
        assert call_row.parameters_json["input_images"] == (2 if vision else 0)
        receipt = db.scalar(select(GatewayReceipt).where(GatewayReceipt.call_id == call_row.id))
        assert receipt.actual_cost is None and receipt.usage_json["input_tokens"] == 11


@pytest.mark.parametrize("actual", [None, Decimal("1")])
def test_paid_media_tool_uses_original_gen_reservation_and_settlement_once(
    client,
    episode,
    monkeypatch,
    actual,
):
    priced(monkeypatch, "2")
    run_id, actor = bounded_run(client, episode, maximum_cost="2")
    tool_setup(client, episode, run_id, actor)
    job = capture_ok(client, episode["project_id"])
    original = gateway_media.submit

    def bounded_submit(*args, **kwargs):
        return original(*args, **kwargs, execution=execution(run_id, kind="TOOL"))

    monkeypatch.setattr(gateway_media, "submit", bounded_submit)
    provider = Provider()
    provider.receipt = provider.receipt.model_copy(update={"cost": actual})
    deliver(client, job, provider)
    assert len(provider.requests) == 1
    current = usage_for(client, run_id)
    assert len(current["actions"]) == 1 and current["actions"][0].kind == "TOOL"
    assert current["actual_cost"] == (actual or 0)
    assert current["pending_estimate"] == (2 if actual is None else 0)
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(GenerationBudgetReservation)) == 1
        assert db.scalar(select(func.count()).select_from(GatewayCall)) == 1
    second = capture_ok(client, episode["project_id"])
    assert deliver(client, second, provider) == "SUCCEEDED"
    assert len(provider.requests) == 2
    after = usage_for(client, run_id)
    assert len(after["actions"]) == 2
    assert after["actual_cost"] == 2 * (actual or 0)
    assert after["pending_estimate"] == (4 if actual is None else 0)
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(GenerationBudgetReservation)) == 2
        assert db.scalar(select(func.count()).select_from(GatewayCall)) == 2


@pytest.mark.parametrize(
    "change",
    [
        "max_calls",
        "max_tool_calls",
        "max_steps",
        "max_agent_calls",
        "budget",
        "deadline",
        "cancel",
        "grant",
    ],
)
def test_media_executor_rechecks_authority_and_ignores_historical_caps(
    client, episode, monkeypatch, change
):
    from arclight.persistence.generation import GenerationDispatch
    from arclight.persistence.generation_scheduling import GenerationDeadLetter

    priced(monkeypatch, "2")
    limits = {"maximum_cost": "0" if change == "budget" else "10"}
    if change.startswith("max_"):
        limits[change] = 0
    if change == "deadline":
        limits["max_duration_seconds"] = 0
    run_id, actor = bounded_run(client, episode, **limits)
    tool_setup(client, episode, run_id, actor)
    job = capture_ok(client, episode["project_id"])
    with client.app.state.session_factory.begin() as db:
        if change == "cancel":
            db.get(WorkflowRun, run_id).status = "CANCELLED"
        elif change == "grant":
            db.scalar(
                select(AgentToolGrant).where(AgentToolGrant.run_id == run_id)
            ).allowed_tools_json = []
    original = gateway_media.submit

    def bounded_submit(*args, **kwargs):
        return original(*args, **kwargs, execution=execution(run_id, kind="TOOL"))

    monkeypatch.setattr(gateway_media, "submit", bounded_submit)
    provider = Provider()
    refused = change in {"cancel", "grant"}
    assert deliver(client, job, provider) == ("FAILED" if refused else "SUCCEEDED")
    assert len(provider.requests) == len(usage_for(client, run_id)["actions"]) == int(not refused)
    with client.app.state.session_factory() as db:
        dead = db.scalar(
            select(GenerationDeadLetter).where(GenerationDeadLetter.job_id == UUID(job["id"]))
        )
        if refused:
            assert dead.reason == "NOT_RETRYABLE" and dead.error_code == "INPUT_INVALID"
        else:
            assert dead is None
        assert db.scalar(select(func.count()).select_from(GenerationDispatch)) == 1
        assert db.scalar(select(func.count()).select_from(GatewayCall)) == int(not refused)


@pytest.mark.parametrize(
    "limits",
    [
        {"maximum_cost": "2"},
        {"max_calls": 1},
        {"max_steps": 1},
    ],
)
def test_postgres_parallel_delegates_record_both_calls_past_shared_historical_limits(
    client,
    episode,
    settings,
    limits,
):
    require_postgres(settings)
    root, actor = bounded_run(client, episode, **limits)
    children = []
    for number in (2, 3):
        child_episode = sibling_episode(client, episode, number)
        child, _ = bounded_run(client, child_episode, parent=root)
        children.append((child_episode, child))
    barrier = Barrier(2)
    calls = []

    def attempt(child):
        child_episode, identity = child
        selected = route() if "maximum_cost" in limits else route(maximum_cost=Decimal("0"))
        barrier.wait(timeout=10)
        try:
            invoke(
                client.app.state.session_factory,
                gateway_request(child_episode, identity, actor, selected),
                selected,
                lambda: calls.append(identity),
                policy=GatewayPolicy(),
            )
            return "SUCCEEDED"
        except DomainError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, children))
    assert outcomes == ["SUCCEEDED", "SUCCEEDED"]
    assert sorted(calls) == sorted(identity for _, identity in children)
    assert len(usage_for(client, root)["actions"]) == 2
    assert usage_for(client, root)["pending_estimate"] == (4 if "maximum_cost" in limits else 0)


def test_postgres_parallel_delegations_keep_shared_root_past_historical_quantity(
    client, episode, settings
):
    require_postgres(settings)
    root, actor = bounded_run(client, episode, max_delegations=1)
    children = []
    for number in (2, 3):
        child_episode = sibling_episode(client, episode, number)
        identity = create_run(client, child_episode)
        activate_run(client, identity, child_episode["id"])
        children.append(identity)
    barrier = Barrier(2)

    def configure(identity):
        barrier.wait(timeout=10)
        try:
            with client.app.state.session_factory.begin() as db:
                configure_agent_run(db, identity, actor, parent_run_id=root)
            return "CONFIGURED"
        except DomainError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(configure, children))
    assert outcomes == ["CONFIGURED", "CONFIGURED"]
    with client.app.state.session_factory() as db:
        assert {
            row.run_id
            for row in db.scalars(select(AgentRunLimit).where(AgentRunLimit.root_run_id == root))
        } == {root, *children}


def test_nested_free_tools_are_counted_past_historical_tool_call_caps(client, episode):
    root, actor = bounded_run(client, episode, max_tool_calls=1)
    registry, call, calls = tool_setup(client, episode, root, actor)
    assert tool_action(client, registry, call).status == "SUCCEEDED" and len(calls) == 1
    child_episode = sibling_episode(client, episode, 2)
    child, _ = bounded_run(client, child_episode, parent=root)
    child_registry, child_call, child_calls = tool_setup(client, child_episode, child, actor)
    result = tool_action(client, child_registry, child_call)
    assert result.status == "SUCCEEDED" and len(child_calls) == 1
    assert len(usage_for(client, root)["actions"]) == 2


def test_paid_tool_requires_live_grant_and_runs_without_a_budget(client, episode):
    run_id, actor = bounded_run(client, episode)
    selected = route(maximum_cost=Decimal("0"))
    call = gateway_request(episode, run_id, actor, selected).model_copy(
        update={"execution": execution(run_id, kind="TOOL")}
    )
    with client.app.state.session_factory.begin() as db, pytest.raises(DomainError) as grant_error:
        admit(db, call, selected, GatewayPolicy())
    assert grant_error.value.code == "AGENT_TOOL_GRANT_FORBIDDEN"
    tool_setup(client, episode, run_id, actor)
    paid_route = route()
    paid = gateway_request(episode, run_id, actor, paid_route).model_copy(
        update={"execution": execution(run_id, kind="TOOL")}
    )
    assert (
        invoke(
            client.app.state.session_factory,
            paid,
            paid_route,
            lambda: "observed",
            policy=GatewayPolicy(),
        )
        == "observed"
    )
    assert [row.kind for row in usage_for(client, run_id)["actions"]] == ["TOOL"]


@pytest.mark.parametrize("change", [{"step_key": "changed"}, {"agent_name": "director"}])
def test_safe_retry_cannot_rebind_executor_identity(client, episode, change):
    run_id, actor = bounded_run(client, episode)
    selected = route(maximum_cost=Decimal("0"))
    call = gateway_request(episode, run_id, actor, selected)
    policy = GatewayPolicy()

    def not_accepted():
        raise GatewayFailure("PROVIDER_UNAVAILABLE", not_accepted=True)

    with pytest.raises(DomainError) as failed:
        invoke(client.app.state.session_factory, call, selected, not_accepted, policy=policy)
    assert failed.value.code == "GATEWAY_NOT_ACCEPTED"
    changed = call.model_copy(update={"execution": call.execution.model_copy(update=change)})
    with pytest.raises(DomainError) as rebound:
        invoke(
            client.app.state.session_factory,
            changed,
            selected,
            lambda: pytest.fail("Rebound retry reached supplier"),
            policy=policy,
        )
    assert rebound.value.code == "GATEWAY_IDEMPOTENCY_CONFLICT"
    assert len(usage_for(client, run_id)["actions"]) == 1


@pytest.mark.parametrize("kind", ["MODEL", "TOOL"])
def test_postgres_first_policy_and_first_action_are_atomic(client, episode, settings, kind):
    require_postgres(settings)
    run_id = create_run(client, episode)
    actor = activate_run(client, run_id, episode["id"])
    registry, call, calls = tool_setup(client, episode, run_id, actor)
    barrier = Barrier(2)

    def configure():
        barrier.wait(timeout=10)
        try:
            with client.app.state.session_factory.begin() as db:
                configure_agent_run(db, run_id, actor)
            return "CONFIGURED"
        except DomainError as exc:
            return exc.code

    def first_action():
        barrier.wait(timeout=10)
        if kind == "TOOL":
            feedback = tool_action(client, registry, call)
            return feedback.status if feedback.error is None else feedback.error.code
        selected = route(maximum_cost=Decimal("0"))
        try:
            invoke(
                client.app.state.session_factory,
                request(episode, selected, workflow_id=run_id, actor=actor, task="script"),
                selected,
                lambda: calls.append(1),
                policy=GatewayPolicy(),
            )
            return "SUCCEEDED"
        except DomainError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        configured, invoked = [
            future.result(timeout=20)
            for future in (
                pool.submit(configure),
                pool.submit(first_action),
            )
        ]
    with client.app.state.session_factory() as db:
        controls = db.scalar(select(func.count()).select_from(AgentRunLimit))
        from arclight.persistence.agent_limits import AgentAction

        actions = db.scalar(select(func.count()).select_from(AgentAction))
    if configured == "AGENT_LIMITS_LATE":
        assert invoked == "SUCCEEDED" and controls == actions == 0
        assert len(calls) == 1
    else:
        assert configured == "CONFIGURED" and controls == 1
        if kind == "TOOL":
            assert invoked == "SUCCEEDED" and actions == len(calls) == 1
        else:
            assert invoked == "AGENT_CONTEXT_REQUIRED" and actions == len(calls) == 0


@pytest.mark.usefixtures("legacy_execution_deadlines")
def test_limits_and_action_history_are_immutable_and_block_downgrade(client, episode, migrated):
    run_id, actor = bounded_run(client, episode)
    assert model_action(client, episode, run_id, actor) == "observed"
    engine = client.app.state.engine
    statements = [
        "UPDATE agent_run_limits SET actor_id = actor_id",
        "DELETE FROM agent_run_limits",
        "UPDATE agent_actions SET step_key = step_key",
        "DELETE FROM agent_actions",
    ]
    if engine.dialect.name == "sqlite":
        statements.extend(
            f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}"
            for table in ("agent_run_limits", "agent_actions")
        )
    else:
        statements.extend(
            f"TRUNCATE TABLE {table} CASCADE" for table in ("agent_run_limits", "agent_actions")
        )
    for statement in statements:
        with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"), engine.begin() as db:
            db.execute(text(statement))
    with pytest.raises(RuntimeError, match="Agent execution limit history exists"):
        command.downgrade(migrated, "0044")
    assert len(usage_for(client, run_id)["actions"]) == 1
    command.check(migrated)


@pytest.mark.parametrize("dialect", ["sqlite:///unused.db", "postgresql+psycopg://unused/unused"])
def test_offline_upgrade_and_online_only_history_preserving_downgrade(dialect):
    output = StringIO()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"), output_buffer=output)
    config.attributes["database_url"] = dialect
    command.upgrade(config, "0044:0045", sql=True)
    sql = output.getvalue()
    assert "CREATE TABLE agent_run_limits" in sql and "CREATE TABLE agent_actions" in sql
    assert "IMMUTABLE_HISTORY" in sql
    if dialect.startswith("postgresql"):
        assert "JSONB" in sql and "BEFORE TRUNCATE" in sql
    with pytest.raises(RuntimeError, match="online history preservation check"):
        command.downgrade(config, "0045:0044", sql=True)
