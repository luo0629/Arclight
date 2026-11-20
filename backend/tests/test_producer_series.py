"""Multi-episode Producer uses the real runtime, gateway and immutable inputs."""

import copy
import json
from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select
from starlette.testclient import TestClient
from test_agent_model import call_response
from test_agent_model import model_service as model_service  # noqa: F401
from test_agent_runtime import final, tool_feedback
from test_asset_versions import private_objects as private_objects  # noqa: F401
from test_producer_delegation import delegation_call, dispatch, dispatched
from test_producer_planning import writer_task
from test_screenwriter_creation import approve, run_once
from test_series_planning import planned_outline as planned_outline  # noqa: F401
from test_series_screenwriter_runtime import install_series
from test_writing_context import writing_case as writing_case  # noqa: F401

from arclight.application import producer_planning as planning
from arclight.application.agent_limits import action_usage, validate_agent_execution
from arclight.application.producer_series_delivery import control_series, paused
from arclight.core.errors import DomainError
from arclight.domain.agent_limits import AgentRunLimits
from arclight.domain.model_gateway import GatewayPolicy
from arclight.domain.producer_series import SeriesControl, SeriesProducerScope
from arclight.main import create_app
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.agent_loops import AgentLoop
from arclight.persistence.base import utcnow
from arclight.persistence.models import DependencyManifest, Project, Script, WorkflowRun


@pytest.fixture
def series_case(client, writing_case):
    case = copy.deepcopy(writing_case)
    later = client.post(
        f"/api/v1/projects/{case['episode']['project_id']}/episodes",
        json={"episode_no": 2, "title": "第二集", "target_duration_ms": 30000},
    )
    assert later.status_code == 201, later.text
    case["later"] = later.json()
    case["scope"] = {
        key: case["request"][key]
        for key in ("series_plan_version_id", "ledger_version_id", "expected_ledger_head_revision")
    } | {
        "episodes": [{"episode_id": case["episode"]["id"]}, {"episode_id": case["later"]["id"]}],
        "confirmation": "确认按批准规划制作这两集，批次成果不等于整剧交付。",
    }
    return case


@pytest.fixture
def series_transport(series_case, request):
    return request.getfixturevalue("model_service")


def begin(client, settings, case, *, scope=None, limits=None, key=None, quote="0"):
    settings.model_gateway_policy = GatewayPolicy(text_maximum_cost=quote, pricing_version="s07:1")
    with client.app.state.session_factory.begin() as db:
        actor = db.get(Project, UUID(case["episode"]["project_id"])).owner_id
        run = planning.start_producer(
            db,
            settings,
            UUID(case["episode"]["id"]),
            actor,
            "依据批准的全剧规划按集制作",
            key or uuid4().hex,
            "series-test",
            series=SeriesProducerScope.model_validate(scope or case["scope"]),
            limits=limits
            or AgentRunLimits(
                maximum_cost=None,
                max_calls=100,
                max_model_calls=80,
                max_tool_calls=80,
                max_agent_calls=80,
                max_duration_seconds=3600,
            ),
        )
        return run.id, actor


def contexts(case):
    return [
        {"task_key": "first", "episode_id": case["episode"]["id"], "narrative": case["request"]},
        {"task_key": "second", "episode_id": case["later"]["id"], "narrative": case["request"]},
    ]


def install_plan(transport, case, *, tasks=None, inputs=None, patch=None, replan=False):
    sent, errors = [], []

    def respond(request):
        try:
            payload = json.loads(request.content)
            sent.append(payload)
            seed = json.loads(payload["messages"][1]["content"])["data"]
            feedback = tool_feedback(payload)
            if not feedback:
                calls = [
                    (
                        f"episode-{i}",
                        "investigate_project",
                        {"query": {"path": "overview", "episode_id": item["episode_id"]}},
                    )
                    for i, item in enumerate(case["scope"]["episodes"])
                ]
                value = call_response(*calls)
            else:
                proposed = tasks(seed) if callable(tasks) else tasks
                selected = inputs(seed) if callable(inputs) else inputs
                base = seed.get("base_plan", {}).get("plan", {})
                plan = {
                    "schema_version": "producer-plan/1",
                    "series_schema_version": "producer-series-plan/1",
                    "series_scope_sha256": base.get("series_scope_sha256")
                    or seed["series_scope_sha256"],
                    "goal_sha256": seed["goal_sha256"],
                    "summary": "逐集制作，逐作品验收。",
                    "evidence": [
                        {
                            "tool_call_key": row["idempotency_key"],
                            "evidence_sha256": row["result"]["evidence_sha256"],
                        }
                        for row in feedback
                        if row["status"] == "SUCCEEDED"
                    ],
                    "tasks": copy.deepcopy(
                        proposed or [writer_task("first"), writer_task("second")]
                    ),
                    "task_contexts": copy.deepcopy(selected or contexts(case)),
                }
                if patch:
                    patch(plan)
                result = plan
                if replan:
                    result = {
                        "schema_version": "producer-replan/1",
                        "base_plan_id": seed["base_plan"]["plan_id"],
                        "base_plan_sha256": seed["base_plan"]["plan_sha256"],
                        "observation_id": seed["observation"]["id"],
                        "observation_sha256": seed["observation"]["snapshot_sha256"],
                        "reason": seed["observation"]["reason"],
                        "plan": plan,
                    }
                value = final(result=result)
            value["usage"] = {"prompt_tokens": 10, "completion_tokens": 10, "actual_cost": "0"}
            return httpx.Response(200, json=value)
        except Exception as exc:
            errors.append(repr(exc))
            raise

    transport(respond)
    return sent, errors


def completed(client, settings, case, transport, *, limits=None, quote="0", **options):
    run_id, actor = begin(client, settings, case, limits=limits, quote=quote)
    sent, errors = install_plan(transport, case, **options)
    outcome = planning.plan_producer(client.app.state.session_factory, settings, run_id, actor)
    assert outcome.status == "COMPLETED", (outcome.model_dump_json(), errors)
    assert not errors
    with client.app.state.session_factory() as db:
        reference = planning.read_producer_plan(db, outcome.loop_id, actor)
    return run_id, actor, reference, sent


def test_frozen_scope_replay_and_overlapping_anchors(client, settings, series_case):
    case = series_case
    run_id, actor = begin(client, settings, case, key="series-frozen")
    assert begin(client, settings, case, key="series-frozen")[0] == run_id
    with client.app.state.session_factory() as db:
        run, control = db.get(WorkflowRun, run_id), db.get(AgentRunLimit, run_id)
        assert run.workflow_version == "producer-series/1"
        assert run.input_json["series_scope"]["request"] == SeriesProducerScope.model_validate(
            case["scope"]
        ).model_dump(mode="json")
        assert control.root_run_id == run_id and control.parent_run_id is None
    changed = copy.deepcopy(case)
    changed["episode"] = case["later"]
    changed["scope"]["episodes"] = [{"episode_id": case["later"]["id"]}]
    with pytest.raises(DomainError, match="制作范围") as failure:
        begin(client, settings, changed)
    assert failure.value.code == "EPISODE_BUSY"


def test_cross_episode_delegation_and_restart_replay_keep_root_limits(
    client, settings, series_case, series_transport
):
    run_id, actor, reference, sent = completed(client, settings, series_case, series_transport)
    first = dispatched(client, settings, delegation_call(run_id, reference, task_key="second"))
    with TestClient(create_app(settings)) as restarted:
        repeated = dispatched(
            restarted, settings, delegation_call(run_id, reference, task_key="second")
        )
    assert repeated == first and len(sent) == 2
    with client.app.state.session_factory() as db:
        root = db.get(AgentRunLimit, run_id)
        child = db.get(WorkflowRun, UUID(first["child_run_id"]))
        policy = db.get(AgentRunLimit, child.id)
        assert str(child.episode_id) == series_case["later"]["id"]
        assert child.workflow_version == "screenwriter-series/1.0"
        assert (
            child.input_json["narrative_context"]["series_plan_version_id"]
            == series_case["plan"]["id"]
        )
        assert policy.root_run_id == run_id and policy.parent_run_id == run_id
        assert policy.limits_json == root.limits_json and policy.deadline_at == root.deadline_at
        assert (
            db.scalar(
                select(func.count())
                .select_from(AgentRunLimit)
                .where(AgentRunLimit.root_run_id == run_id)
            )
            == 2
        )
        assert action_usage(db, run_id)["actual_cost"] == Decimal("0")


def test_real_writer_approval_does_not_invalidate_frozen_delegation(
    client, settings, series_case, series_transport
):
    tasks = [writer_task("first")]
    tasks[0]["task"]["revision_rounds"] = 3
    run_id, actor, reference, _ = completed(
        client,
        settings,
        series_case,
        series_transport,
        tasks=tasks,
        inputs=contexts(series_case)[:1],
    )
    receipt = dispatched(client, settings, delegation_call(run_id, reference, task_key="first"))
    child_id = UUID(receipt["child_run_id"])
    sent, errors = install_series(series_transport, settings)
    child = run_once(client, settings, child_id)
    assert child["status"] == "WAITING_REVIEW", (child, errors)
    assert not errors
    approve(client, child)
    before = len(sent)
    assert (
        dispatched(client, settings, delegation_call(run_id, reference, task_key="first"))
        == receipt
    )
    assert len(sent) == before
    with client.app.state.session_factory() as db:
        script = db.scalar(select(Script).where(Script.run_id == child_id))
        assert script.status == "APPROVED"
        assert not db.get(WorkflowRun, child_id).input_json["narrative_context"][
            "ledger_version_id"
        ]
        assert (
            db.scalar(
                select(func.count())
                .select_from(AgentRunLimit)
                .where(AgentRunLimit.root_run_id == run_id)
            )
            == 2
        )


def test_pause_resume_is_same_run_and_deadline_and_blocks_child(
    client, settings, series_case, series_transport
):
    run_id, actor, reference, _ = completed(client, settings, series_case, series_transport)
    child_id = UUID(
        dispatched(client, settings, delegation_call(run_id, reference, task_key="second"))[
            "child_run_id"
        ]
    )
    factory = client.app.state.session_factory
    with factory.begin() as db:
        run = db.get(WorkflowRun, run_id)
        original = db.get(AgentRunLimit, run_id).deadline_at
        request = SeriesControl(
            action="PAUSE", expected_revision=run.revision, reason="先核对第一集"
        )
        control_series(db, run_id, actor, request, "pause")
    with factory.begin() as db:
        assert paused(db, db.get(WorkflowRun, run_id))
        assert control_series(db, run_id, actor, request, "pause").id == run_id
        with pytest.raises(DomainError) as failure:
            validate_agent_execution(db, run_id, "producer")
        assert failure.value.code == "PRODUCER_PAUSED"
    from arclight.application.agent_workflows import WorkflowExecutionDeferred

    with pytest.raises(WorkflowExecutionDeferred):
        run_once(client, settings, child_id)
    with factory() as db:
        assert db.get(WorkflowRun, child_id).status == "QUEUED"
    with factory.begin() as db:
        run = db.get(WorkflowRun, run_id)
        control_series(
            db,
            run_id,
            actor,
            SeriesControl(action="RESUME", expected_revision=run.revision, reason="沿原限额恢复"),
            "resume",
        )
        assert not paused(db, run)
        assert db.get(AgentRunLimit, run_id).deadline_at == original
        assert db.get(AgentRunLimit, child_id).deadline_at == original


def test_pause_rejects_live_model_lease_and_expired_resume_rolls_back(
    client, settings, series_case, series_transport, monkeypatch
):
    run_id, actor, _, _ = completed(client, settings, series_case, series_transport)
    factory = client.app.state.session_factory
    with factory.begin() as db:
        loop = db.scalar(select(AgentLoop).where(AgentLoop.run_id == run_id))
        loop.lease_token, loop.lease_expires_at = uuid4(), utcnow() + timedelta(minutes=1)
    with factory.begin() as db, pytest.raises(DomainError) as failure:
        run = db.get(WorkflowRun, run_id)
        control_series(
            db,
            run_id,
            actor,
            SeriesControl(action="PAUSE", expected_revision=run.revision, reason="暂停"),
            "live",
        )
    assert failure.value.code == "AGENT_LOOP_BUSY"
    with factory.begin() as db:
        db.scalar(select(AgentLoop).where(AgentLoop.run_id == run_id)).lease_token = None
        run = db.get(WorkflowRun, run_id)
        control_series(
            db,
            run_id,
            actor,
            SeriesControl(action="PAUSE", expected_revision=run.revision, reason="暂停"),
            "paused",
        )
    future = utcnow() + timedelta(hours=2)
    monkeypatch.setattr("arclight.application.agent_limits.utcnow", lambda: future)
    with factory.begin() as db, pytest.raises(DomainError) as failure:
        run = db.get(WorkflowRun, run_id)
        control_series(
            db,
            run_id,
            actor,
            SeriesControl(action="RESUME", expected_revision=run.revision, reason="恢复"),
            "expired",
        )
    assert failure.value.code == "AGENT_DEADLINE_EXCEEDED"
    with factory() as db:
        assert paused(db, db.get(WorkflowRun, run_id))


def test_declared_previous_episode_blocks_before_any_new_child(
    client, settings, series_case, series_transport
):
    case = series_case
    case["scope"]["episodes"][1]["requires_episode_ids"] = [case["episode"]["id"]]
    tasks = [writer_task("first"), writer_task("second", depends_on=["first"])]
    run_id, _, reference, _ = completed(client, settings, case, series_transport, tasks=tasks)
    feedback = dispatch(client, settings, delegation_call(run_id, reference, task_key="second"))
    assert feedback.status == "FAILED" and feedback.error.code == "PRODUCER_PREREQUISITE_PENDING"
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(AgentRunLimit)
                .where(AgentRunLimit.root_run_id == run_id)
            )
            == 1
        )


@pytest.mark.parametrize("kind", ["CHARACTER", "SCENE"])
def test_shared_exact_version_is_consumed_instead_of_current_pointer(
    client, settings, series_case, series_transport, private_objects, kind
):
    from test_asset_versions import save, state
    from test_assets import create

    asset = create(client, series_case["episode"]["project_id"], kind)
    version = save(client, asset)
    ready = state(client, asset, version, "READY_FOR_REVIEW")
    assert ready.status_code == 200, ready.text
    approved = state(client, asset, ready.json(), "APPROVED")
    assert approved.status_code == 200, approved.text
    identity = UUID(version["id"])
    selected = contexts(series_case)
    for item in selected:
        item["shared_version_ids"] = [str(identity)]
    run_id, _, reference, _ = completed(
        client, settings, series_case, series_transport, inputs=selected
    )
    first = dispatched(client, settings, delegation_call(run_id, reference, task_key="first"))
    second = dispatched(client, settings, delegation_call(run_id, reference, task_key="second"))
    with client.app.state.session_factory() as db:
        for receipt in (first, second):
            child = db.get(WorkflowRun, UUID(receipt["child_run_id"]))
            # The manifest is stored independently, not inferred from directory selection.
            rows = list(
                db.scalars(
                    select(DependencyManifest).where(
                        DependencyManifest.snapshot_json["context"]["run_id"].as_string()
                        == str(child.id)
                    )
                )
            )
            assert rows and any(
                str(identity)
                in {row["source_asset_version_id"] for row in manifest.snapshot_json["inputs"]}
                for manifest in rows
            )
