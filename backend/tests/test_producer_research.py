from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select
from test_agent_limits import bounded_run, model_action, sibling_episode, usage_for
from test_agent_memory import memory
from test_agent_preferences import confirm
from test_agent_tools import activate_run, create_run
from test_context_trace import trace
from test_dependency_validation import registry_version
from test_generation_budget import set_policy
from test_model_gateway import request, route
from test_production_state import finish_text, read

from arclight.application.agent_tools import (
    build_agent_tool_registry,
    execute_agent_tool,
    grant_agent_tools,
)
from arclight.application.gateway_accounting import reconcile
from arclight.application.model_gateway import invoke
from arclight.domain.agent_tools import AgentToolCall, AgentToolGrantRequest
from arclight.domain.model_gateway import GatewayPolicy, GatewayUsage
from arclight.persistence.agent_limits import AgentAction
from arclight.persistence.agent_preferences import AgentPreference
from arclight.persistence.model_gateway import GatewayCall, GatewayReceipt
from arclight.persistence.models import (
    AgentToolGrant,
    AgentToolInvocation,
    AssetDependency,
    AssetSelectionLock,
    AssetVersion,
    CharacterConfirmation,
    DependencyManifest,
    DependencyOutput,
    Episode,
    Project,
    Review,
    Script,
    Shot,
    Storyboard,
    StoryEntry,
    VisualAsset,
    WorkflowRun,
)


def prepare_producer(
    client, episode, settings, *, key="producer-investigation", grant_actor_id=None
):
    registry = build_agent_tool_registry(settings=settings)
    run_id = create_run(client, episode, key=key)
    owner_id = activate_run(client, run_id, episode["id"])
    actor_id = grant_actor_id or owner_id
    grant_producer(client, registry, run_id, actor_id)
    return registry, run_id, actor_id


def grant_producer(client, registry, run_id, actor_id, *, allowed=True):
    with client.app.state.session_factory.begin() as db:
        grant_agent_tools(
            db,
            AgentToolGrantRequest(
                run_id=run_id,
                agent_name="producer",
                allowed_tools=["investigate_project"] if allowed else [],
            ),
            actor_id,
            registry,
        )


def producer_call(run_id, *, key="observe", query=None, **parameters):
    return AgentToolCall(
        run_id=run_id,
        tool_name="investigate_project",
        idempotency_key=key,
        arguments={"query": query or {"path": "overview"}, **parameters},
    )


def execute_producer(client, registry, call, *, agent="producer"):
    with client.app.state.session_factory.begin() as db:
        return execute_agent_tool(db, registry, agent, call)


def observed(client, registry, call):
    feedback = execute_producer(client, registry, call)
    assert feedback.status == "SUCCEEDED", feedback
    assert feedback.trust == "UNTRUSTED"
    return feedback.result


def quoted_gateway_call(client, episode):
    selected = route()
    gateway_request = request(episode, selected, task="script")
    assert (
        invoke(
            client.app.state.session_factory,
            gateway_request,
            selected,
            lambda: "ok",
            policy=GatewayPolicy(),
        )
        == "ok"
    )
    with client.app.state.session_factory() as db:
        return db.scalar(
            select(GatewayCall.id).where(GatewayCall.operation_key == gateway_request.operation_key)
        )


def reconcile_call(client, call_id, cost):
    with client.app.state.session_factory.begin() as db:
        reconcile(
            db,
            db.get(GatewayCall, call_id),
            "service-owner",
            "SUCCEEDED",
            GatewayUsage(actual_cost=Decimal(cost)),
            "private-invoice-do-not-expose",
        )


def business_snapshot(client):
    models = (
        AgentPreference,
        Project,
        Episode,
        Script,
        Storyboard,
        Shot,
        Review,
        StoryEntry,
        CharacterConfirmation,
        VisualAsset,
        AssetVersion,
        AssetSelectionLock,
        AssetDependency,
        DependencyManifest,
        DependencyOutput,
        GatewayCall,
        GatewayReceipt,
    )
    with client.app.state.session_factory() as db:
        return {
            model.__tablename__: deepcopy(
                db.execute(select(model.__table__).order_by(model.id)).mappings().all()
            )
            for model in models
        }


def test_producer_registry_preserves_existing_tools_and_requires_explicit_grant(
    client, episode, settings
):
    assert build_agent_tool_registry().get("read_story_entry") is not None
    registry = build_agent_tool_registry(settings=settings)
    producer = registry.get("investigate_project")
    assert producer.allowed_agents == frozenset({"producer"})
    assert producer.requires_write is True
    assert registry.get("investigate_production").allowed_agents == frozenset({"director"})
    run_id = create_run(client, episode)
    actor_id = activate_run(client, run_id, episode["id"])
    call = producer_call(run_id)
    denied = execute_producer(client, registry, call)
    assert denied.status == "FAILED" and denied.error.code == "USER_NOT_AUTHORIZED"
    grant_producer(client, registry, run_id, actor_id)
    allowed = observed(client, registry, producer_call(run_id, key="granted"))
    assert allowed["project_id"] == episode["project_id"]
    assert allowed["episode_id"] == episode["id"]
    for agent in ("screenwriter", "director", "art_director", "continuity_reviewer"):
        # Simulate a persisted grant from a superseded registry policy.
        with client.app.state.session_factory.begin() as db:
            db.add(
                AgentToolGrant(
                    run_id=run_id,
                    project_id=UUID(episode["project_id"]),
                    agent_name=agent,
                    actor_id=actor_id,
                    allowed_tools_json=["investigate_project"],
                )
            )
        feedback = execute_producer(client, registry, producer_call(run_id, key=agent), agent=agent)
        assert feedback.status == "FAILED" and feedback.error.code == "AGENT_NOT_ALLOWED"


def test_overview_reuses_real_state_and_does_not_mutate_business_or_frozen_inputs(
    client, episode, settings
):
    finish_text(client, episode, settings)
    confirm(client, episode["project_id"])
    registry, run_id, _ = prepare_producer(client, episode, settings)
    expected = read(client, episode)
    frozen_memory = memory(client, run_id)
    frozen_context = trace(client, run_id)
    with client.app.state.session_factory() as db:
        run = db.get(WorkflowRun, run_id)
        frozen_input = deepcopy(run.input_json)
        frozen_state = deepcopy(run.agent_state_json)
    before = business_snapshot(client)
    result = observed(client, registry, producer_call(run_id))
    assert result["schema_version"] == "producer-research/1"
    assert result["production"]["script_version_id"] == expected["script_version_id"]
    assert result["production"]["storyboard_version_id"] == expected["storyboard_version_id"]
    assert result["production"]["issues"] == expected["issues"]
    assert result["production"]["evidence_sha256"] == expected["evidence_sha256"]
    assert "shots" not in result["production"]
    assert result["project_episode_count"] == 1
    assert result["read_versions"]
    for version in result["read_versions"]:
        if version["updated_at"] is not None:
            assert datetime.fromisoformat(version["updated_at"]).tzinfo is not None
    assert datetime.fromisoformat(result["read_at"]).tzinfo is not None
    assert len(result["evidence_sha256"]) == 64
    assert business_snapshot(client) == before
    current_memory = memory(client, run_id)
    assert current_memory["observation"] == frozen_memory["observation"]
    for name in (
        "domain_facts",
        "evidence",
        "long_term_preferences",
        "constraints_sha256",
        "startup_manifest_id",
        "input_sha256",
    ):
        assert current_memory["memory"][name] == frozen_memory["memory"][name]
    assert trace(client, run_id) == frozen_context
    with client.app.state.session_factory() as db:
        run = db.get(WorkflowRun, run_id)
        assert run.input_json == frozen_input
        for name in (
            "input_sha256",
            "plan",
            "steps",
            "working_drafts",
            "extensions",
        ):
            assert run.agent_state_json[name] == frozen_state[name]
        invocation = db.scalar(
            select(AgentToolInvocation).where(AgentToolInvocation.run_id == run_id)
        )
        assert invocation.status == "SUCCEEDED"
        assert run.agent_state_json["tool_call_refs"] == [
            *frozen_state["tool_call_refs"],
            str(invocation.id),
        ]
        assert run.agent_state_json["observation_refs"] == [
            *frozen_state["observation_refs"],
            str(invocation.id),
        ]


def test_snapshot_token_is_stable_across_new_keys_and_rejects_changed_business(
    client, episode, settings
):
    set_policy(client, episode["project_id"], "100")
    registry, run_id, _ = prepare_producer(client, episode, settings)
    first_call = producer_call(run_id, key="first")
    first = observed(client, registry, first_call)
    fresh = observed(
        client,
        registry,
        producer_call(
            run_id, key="same-business", expected_evidence_sha256=first["evidence_sha256"]
        ),
    )
    assert fresh["evidence_sha256"] == first["evidence_sha256"]
    assert fresh["read_versions"] == first["read_versions"]
    set_policy(client, episode["project_id"], "50")
    expired = execute_producer(
        client,
        registry,
        producer_call(run_id, key="expired", expected_evidence_sha256=first["evidence_sha256"]),
    )
    assert expired.status == "FAILED"
    assert expired.error.code == "PRODUCER_SNAPSHOT_CHANGED"
    assert expired.result is None
    assert observed(client, registry, first_call) == first
    current = observed(client, registry, producer_call(run_id, key="current"))
    assert current["evidence_sha256"] != first["evidence_sha256"]
    assert current["budget"]["project_limit"] == "50.000000"
    conflict = execute_producer(
        client,
        registry,
        producer_call(run_id, key="first", query={"path": "budget"}),
    )
    assert conflict.status == "FAILED" and conflict.error.code == "IDEMPOTENCY_CONFLICT"


def test_budget_retains_pending_unknown_and_exact_zero_without_duplicate_bill(
    client, episode, settings
):
    policy = set_policy(
        client,
        episode["project_id"],
        "100",
        task_limit="20",
        episode_limit="80",
        batch_limit="90",
        warning_percent=70,
        max_candidates_per_target=8,
    )
    call_id = quoted_gateway_call(client, episode)
    registry, run_id, _ = prepare_producer(client, episode, settings)
    query = {"path": "budget"}
    pending = observed(client, registry, producer_call(run_id, query=query))["budget"]
    assert pending["policy_revision"] == policy["policy_revision"]
    assert pending["project_limit"] == "100.000000"
    assert pending["task_limit"] == "20.000000"
    assert pending["episode_limit"] == "80.000000"
    assert pending["batch_limit"] == "90.000000"
    assert pending["warning_percent"] == 70
    assert pending["max_candidates_per_target"] == 8
    for name in ("project_costs", "episode_costs"):
        assert pending[name]["entry_count"] == 1
        assert pending[name]["actual_cost"] is None
        assert pending[name]["estimated_count"] == 1
        assert pending[name]["pending_estimate"] == "2.000000"
    reconcile_call(client, call_id, "0")
    zero = observed(client, registry, producer_call(run_id, key="zero", query=query))
    for name in ("project_costs", "episode_costs"):
        totals = zero["budget"][name]
        assert totals["entry_count"] == 1
        assert totals["actual_cost"] == "0.000000"
        assert totals["confirmed_count"] == 1
        assert totals["pending_estimate"] == "0.000000"
        assert not totals["cost_is_estimate"]
    assert "private-invoice-do-not-expose" not in str(zero)


def test_producer_observation_shares_root_limits_and_replay_does_not_spend_another_call(
    client, episode, settings
):
    root, actor = bounded_run(client, episode, max_tool_calls=3, max_calls=10)
    child_episode = sibling_episode(client, episode, 2)
    child, _ = bounded_run(client, child_episode, parent=root)
    registry = build_agent_tool_registry(settings=settings)
    grant_producer(client, registry, root, actor)
    grant_producer(client, registry, child, actor)
    root_call = producer_call(root, query={"path": "budget"})
    first = observed(client, registry, root_call)
    runtime = first["runtime_budget"]
    assert runtime["root_run_id"] == str(root)
    assert runtime["limits"]["max_tool_calls"] == 3
    assert runtime["limits"]["max_calls"] == 10
    assert datetime.fromisoformat(runtime["deadline_at"]).tzinfo is not None
    assert runtime["calls"] == runtime["tool_calls"] == 1
    assert runtime["model_calls"] == 0
    assert runtime["actual_cost"] == runtime["pending_estimate"] == "0.000000"
    assert runtime["unknown_cost"] is False
    assert observed(client, registry, root_call) == first
    assert len(usage_for(client, root)["actions"]) == 1
    child_first = observed(client, registry, producer_call(child, query={"path": "budget"}))
    assert child_first["runtime_budget"]["root_run_id"] == str(root)
    assert child_first["runtime_budget"]["tool_calls"] == 2
    child_next = observed(
        client,
        registry,
        producer_call(
            child,
            key="same-business",
            query={"path": "budget"},
            expected_evidence_sha256=child_first["evidence_sha256"],
        ),
    )
    assert child_next["runtime_budget"]["tool_calls"] == 3
    assert child_next["evidence_sha256"] == child_first["evidence_sha256"]
    stopped = execute_producer(client, registry, producer_call(root, key="exhausted"))
    assert stopped.status == "FAILED" and stopped.error.code == "AGENT_TOOL_CALL_LIMIT"
    assert len(usage_for(client, root)["actions"]) == 3
    assert observed(client, registry, root_call) == first


def test_historical_unknown_runtime_cost_is_explicit_without_invalidating_business_token(
    client, episode, settings
):
    root, actor = bounded_run(client, episode, maximum_cost=None)
    assert model_action(client, episode, root, actor) == "observed"
    registry = build_agent_tool_registry(settings=settings)
    grant_producer(client, registry, root, actor)
    with client.app.state.session_factory.begin() as db:
        _, selected = registry_version(db, UUID(episode["project_id"]))
        selected_id = str(selected.id)
    query = {"path": "dependencies", "version_ids": [selected_id]}
    first = observed(client, registry, producer_call(root, query=query))
    assert first["runtime_budget"]["unknown_cost"] is False
    with client.app.state.session_factory.begin() as db:
        original_action = db.scalar(
            select(AgentAction).where(AgentAction.run_id == root, AgentAction.kind == "MODEL")
        )
        original = db.get(GatewayCall, original_action.gateway_call_id)
        values = {
            column.name: getattr(original, column.name) for column in GatewayCall.__table__.columns
        }
        # Append an imported pre-pricing observation; preserve all immutable rows.
        values.update(id=uuid4(), operation_key=uuid4().hex, maximum_cost=None)
        historical = GatewayCall(**values)
        db.add(historical)
        db.flush()
        db.add(
            AgentAction(
                run_id=root,
                root_run_id=root,
                agent_name="producer",
                stage="script",
                step_key="historical-unquoted-call",
                kind="MODEL",
                gateway_call_id=historical.id,
            )
        )
    second = observed(
        client,
        registry,
        producer_call(
            root,
            key="historical-unknown",
            query=query,
            expected_evidence_sha256=first["evidence_sha256"],
        ),
    )
    assert second["runtime_budget"]["unknown_cost"] is True
    assert second["runtime_budget"]["model_calls"] == 2
    assert second["runtime_budget"]["tool_calls"] == 2
    assert second["evidence_sha256"] == first["evidence_sha256"]
    assert any(gap["code"] == "AGENT_COST_UNKNOWN" for gap in second["unknown"])
    budget = observed(
        client, registry, producer_call(root, key="unknown-budget", query={"path": "budget"})
    )
    assert budget["budget"]["project_costs"]["unpriced_count"] == 1
    assert budget["budget"]["project_costs"]["projected_cost"] is None
    assert any(gap["code"] == "UNPRICED_COST" for gap in budget["unknown"])
