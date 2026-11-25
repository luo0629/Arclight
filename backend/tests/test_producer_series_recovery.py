"""Exact previous scripts, local rejection/replanning and shared cost admission."""

import copy
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from starlette.testclient import TestClient
from test_agent_model import model_service as model_service  # noqa: F401
from test_concurrency import require_postgres
from test_producer_delegation import delegation_call, dispatched
from test_producer_planning import writer_task
from test_producer_replanning import accounting, observation
from test_producer_series import begin, completed, contexts, install_plan
from test_producer_series import series_case as series_case  # noqa: F401
from test_producer_series import series_transport as series_transport  # noqa: F401
from test_screenwriter_creation import approve, run_once
from test_script_state_commit import capture, review, save, screenplay
from test_series_planning import planned_outline as planned_outline  # noqa: F401
from test_series_screenwriter_runtime import install_series
from test_writing_context import writing_case as writing_case  # noqa: F401

from arclight.application import producer_planning as planning
from arclight.application.agent_limits import action_usage
from arclight.application.gateway_accounting import reconcile
from arclight.application.producer_replanning import replan_producer
from arclight.application.producer_series_delivery import CONTROL_KIND, control_series
from arclight.domain.model_gateway import GatewayPolicy, GatewayUsage
from arclight.domain.producer_series import SeriesControl
from arclight.main import create_app
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.model_gateway import GatewayCall
from arclight.persistence.models import (
    DependencyManifest,
    StoryLedgerHead,
    VersionBinding,
    WorkflowRun,
)


def test_approved_previous_script_is_frozen_and_consumed_by_later_episode(
    client, settings, series_case, series_transport
):
    case = series_case
    content = screenplay(case, capture(client, case))
    script = save(client, case, content)
    approved = review(client, case, script)
    assert approved.status_code == 200, approved.text
    with client.app.state.session_factory() as db:
        head = db.scalar(
            select(StoryLedgerHead).where(
                StoryLedgerHead.project_id == UUID(case["episode"]["project_id"])
            )
        )
        baseline = {
            "ledger_version_id": str(head.approved_version_id),
            "expected_ledger_head_revision": head.revision,
        }
    case["request"].update(baseline)
    case["scope"].update(baseline)
    case["scope"]["episodes"][1]["requires_episode_ids"] = [case["episode"]["id"]]
    selected = contexts(case)[1:]
    selected[0]["prerequisite_script_version_ids"] = [script["id"]]
    run_id, _, reference, _ = completed(
        client, settings, case, series_transport, tasks=[writer_task("second")], inputs=selected
    )
    receipt = dispatched(client, settings, delegation_call(run_id, reference, task_key="second"))
    with client.app.state.session_factory() as db:
        child = db.get(WorkflowRun, UUID(receipt["child_run_id"]))
        rows = list(
            db.scalars(
                select(DependencyManifest).where(
                    DependencyManifest.snapshot_json["context"]["run_id"].as_string()
                    == str(child.id)
                )
            )
        )
        assert rows
        assert all(
            script["id"]
            in {row["source_asset_version_id"] for row in manifest.snapshot_json["inputs"]}
            for manifest in rows
        )
        assert child.input_json["producer_series_context"]["dependency_script_version_ids"] == [
            script["id"]
        ]


def test_rejection_replans_only_rejected_episode_and_preserves_success_across_restart(
    client, settings, series_case, series_transport
):
    tasks = [writer_task("first"), writer_task("second")]
    for task in tasks:
        task["task"]["revision_rounds"] = 3
    run_id, actor, reference, _ = completed(
        client, settings, series_case, series_transport, tasks=tasks
    )
    first = dispatched(client, settings, delegation_call(run_id, reference, task_key="first"))
    install_series(series_transport, settings)
    child = run_once(client, settings, UUID(first["child_run_id"]))
    assert child["status"] == "WAITING_REVIEW", child
    denied = client.post(
        f"/api/v1/runs/{child['id']}/resume",
        json={
            **child["pending_review"],
            "decision": "REJECT",
            "comment": "返修第一集人物动机",
            "expected_revision": child["revision"],
        },
    )
    assert denied.status_code == 202, denied.text
    rejected = run_once(client, settings, UUID(child["id"]))
    assert rejected["status"] == "REJECTED", rejected
    second = dispatched(client, settings, delegation_call(run_id, reference, task_key="second"))
    succeeded = run_once(client, settings, UUID(second["child_run_id"]))
    assert succeeded["status"] == "WAITING_REVIEW", succeeded
    approve(client, succeeded)
    succeeded = run_once(client, settings, UUID(second["child_run_id"]))
    assert succeeded["pending_review"]["resource_type"] == "storyboard", succeeded
    approve(client, succeeded)
    assert run_once(client, settings, UUID(second["child_run_id"]))["status"] == "SUCCEEDED"
    observed = observation(client, settings, run_id, actor, reference)
    assert observed.affected_task_keys == ["first"]
    assert next(row for row in observed.tasks if row.task_key == "first").replaceable
    kept = next(row for row in observed.tasks if row.task_key == "second")
    assert kept.status == "SUCCEEDED" and not kept.replaceable
    updated = copy.deepcopy(tasks)
    updated[0]["task"]["story"] += "明确送信的承诺和动机。"
    selected = copy.deepcopy(contexts(series_case))
    with client.app.state.session_factory() as db:
        head = db.scalar(
            select(StoryLedgerHead).where(
                StoryLedgerHead.project_id == UUID(series_case["episode"]["project_id"])
            )
        )
        selected[0]["narrative"] = selected[0]["narrative"] | {
            "ledger_version_id": str(head.approved_version_id),
            "expected_ledger_head_revision": head.revision,
        }
    sent, errors = install_plan(
        series_transport, series_case, tasks=updated, inputs=selected, replan=True
    )
    result = replan_producer(client.app.state.session_factory, settings, run_id, actor, observed.id)
    assert result.status == "COMPLETED", (result.model_dump_json(), errors)
    assert not errors and len(sent) == 2
    with client.app.state.session_factory() as db:
        newer = planning.read_producer_plan(db, result.loop_id, actor)
    before = accounting(client, run_id)
    with TestClient(create_app(settings)) as restarted:
        assert (
            dispatched(restarted, settings, delegation_call(run_id, newer, task_key="second"))[
                "child_run_id"
            ]
            == second["child_run_id"]
        )
    after = accounting(client, run_id)
    assert after["calls"] == before["calls"] and after["outbox"] == before["outbox"]
    assert len(sent) == 2 and not errors
    replacement = dispatched(client, settings, delegation_call(run_id, newer, task_key="first"))
    assert replacement["child_run_id"] != first["child_run_id"]
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(AgentRunLimit)
                .where(AgentRunLimit.root_run_id == run_id)
            )
            == 4
        )


def test_unrelated_unknown_invoice_does_not_block_or_duplicate_a_delegation(
    client, settings, series_case, series_transport
):
    run_id, _, reference, _ = completed(client, settings, series_case, series_transport)
    with client.app.state.session_factory.begin() as db:
        original = db.scalar(select(GatewayCall).where(GatewayCall.workflow_id == run_id))
        values = {
            column.name: getattr(original, column.name) for column in GatewayCall.__table__.columns
        }
        unknown = uuid4()
        values.update(
            id=unknown,
            workflow_id=None,
            operation_key="imported-unknown-series",
            sequence=1,
            maximum_cost=None,
        )
        db.add(GatewayCall(**values))
    call = delegation_call(run_id, reference, task_key="second")
    receipt = dispatched(client, settings, call)
    assert dispatched(client, settings, call) == receipt
    with client.app.state.session_factory() as db:
        child = db.get(AgentRunLimit, UUID(receipt["child_run_id"]))
        assert child.root_run_id == run_id
        assert db.get(GatewayCall, unknown).maximum_cost is None
        assert (
            db.scalar(
                select(func.count())
                .select_from(AgentRunLimit)
                .where(AgentRunLimit.root_run_id == run_id)
            )
            == 2
        )


@pytest.mark.parametrize("quote", ["0.02", None])
def test_second_episode_preserves_settled_cost_and_can_use_an_unquoted_route(
    client, settings, series_case, series_transport, quote
):
    run_id, actor, reference, _ = completed(
        client, settings, series_case, series_transport, quote="0.01"
    )
    with client.app.state.session_factory.begin() as db:
        usage = action_usage(db, run_id)
        assert usage["pending_estimate"] == Decimal("0.02")
        for index, action in enumerate(row for row in usage["actions"] if row.kind == "MODEL"):
            reconcile(
                db,
                db.get(GatewayCall, action.gateway_call_id),
                actor,
                "SUCCEEDED",
                GatewayUsage(actual_cost=Decimal("0.01")),
                f"series-invoice-{index}",
            )
    policy = GatewayPolicy(
        text_maximum_cost=quote, pricing_version="series-child:1" if quote else "unconfigured"
    )
    settings.model_gateway_policy = policy
    receipt = dispatched(client, settings, delegation_call(run_id, reference, task_key="second"))
    sent, errors = install_series(series_transport, settings)
    settings.model_gateway_policy = policy
    child = run_once(client, settings, UUID(receipt["child_run_id"]))
    assert child["status"] == "WAITING_REVIEW" and sent and not errors, (child, errors)
    with client.app.state.session_factory() as db:
        usage = action_usage(db, run_id)
        assert usage["actual_cost"] == Decimal("0.02")
        assert db.get(WorkflowRun, run_id).status == "RUNNING"
        assert bool(usage["unknown_cost"]) == (quote is None)


def test_standalone_work_cannot_enter_reserved_non_anchor_episode(client, settings, series_case):
    begin(client, settings, series_case)
    result = client.post(
        f"/api/v1/episodes/{series_case['later']['id']}/script-runs",
        json={"story": "新任务不能覆盖原多集范围"},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert result.status_code == 409 and result.json()["error"]["code"] == "EPISODE_BUSY"


def test_postgres_concurrent_series_delegations_create_one_physical_child(
    client, settings, series_case, series_transport
):
    require_postgres(settings)
    run_id, _, reference, _ = completed(client, settings, series_case, series_transport)
    barrier = Barrier(2)

    def execute(_):
        barrier.wait(timeout=15)
        return dispatched(client, settings, delegation_call(run_id, reference, task_key="second"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(execute, range(2)))
    assert values[0] == values[1]
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(AgentRunLimit)
                .where(AgentRunLimit.root_run_id == run_id)
            )
            == 2
        )


def test_postgres_duplicate_pause_is_one_atomic_receipt(client, settings, series_case):
    require_postgres(settings)
    run_id, actor = begin(client, settings, series_case)
    with client.app.state.session_factory() as db:
        revision = db.get(WorkflowRun, run_id).revision
    barrier = Barrier(2)
    payload = SeriesControl(action="PAUSE", expected_revision=revision, reason="同一暂停投递")

    def apply(_):
        barrier.wait(timeout=15)
        with client.app.state.session_factory.begin() as db:
            return control_series(db, run_id, actor, payload, "same-pause").revision

    with ThreadPoolExecutor(max_workers=2) as pool:
        revisions = list(pool.map(apply, range(2)))
    assert revisions == [revision + 1, revision + 1]
    with client.app.state.session_factory() as db:
        rows = list(
            db.scalars(
                select(VersionBinding).where(
                    VersionBinding.resource_type == CONTROL_KIND,
                    VersionBinding.snapshot_json["run_id"].as_string() == str(run_id),
                )
            )
        )
        assert len(rows) == 1
