"""Producer observations re-read real state, receipts and authority in the test database."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import UUID, uuid4, uuid5

import httpx
import pytest
from art_direction_support import install_direction_model, reference_set
from continuity_reviewer_support import install_reviewer_model
from pydantic import ValidationError
from sqlalchemy import func, select
from test_agent_loop_recovery import answer
from test_agent_model import model_service as model_service
from test_art_review_workflow import decide as decide_art
from test_art_workflow import proposal
from test_assets import archive as archive_asset
from test_collaboration import create_episode, create_project, register
from test_concurrency import require_postgres
from test_continuity_feedback import finish_revision, install_feedback_model
from test_continuity_reviewer import reviewer_case as reviewer_case
from test_continuity_revisions import request_revision
from test_continuity_rules import rules_storage as rules_storage
from test_director_creation import approved_script
from test_human_reviews import decide, resume
from test_producer_delegation import delegation_call, dispatch, dispatched
from test_producer_planning import (
    begin_producer,
    completed_plan,
    install_plan_model,
    writer_task,
)
from test_producer_replanning import install_replan_model
from test_screenwriter_creation import run_once
from test_screenwriter_revision import install_revision as install_writer

from arclight.application import producer_observation as observing
from arclight.application import producer_planning as planning
from arclight.application import producer_replanning as replanning
from arclight.application.agent_loop_store import submit_agent_decision
from arclight.application.agent_tools import grant_agent_tools
from arclight.application.art_workflow import delivered
from arclight.application.continuity_review_store import require_review_record
from arclight.application.continuity_workflow import queue_pending_reviews
from arclight.application.producer_observation import (
    observe_producer,
    read_observation,
    validate_observation_current,
)
from arclight.application.production_events import enqueue_source
from arclight.application.production_versions import bind
from arclight.core.errors import DomainError
from arclight.domain.agent_tools import AgentToolCall, AgentToolGrantRequest
from arclight.domain.producer_observation import ProducerObservationRequest
from arclight.domain.versions import digest
from arclight.persistence.agent_loops import AgentLoop, AgentLoopDecision
from arclight.persistence.human_reviews import HumanReviewDecision, HumanReviewRequest
from arclight.persistence.models import (
    AssetVersion,
    AuditEvent,
    Episode,
    OutboxEvent,
    Project,
    User,
    VersionBinding,
    WorkflowEvent,
    WorkflowRun,
)
from arclight.persistence.production_events import ProductionEventReceipt


def request_for(reference, **changes):
    return ProducerObservationRequest.model_validate(
        {
            "base_plan_id": reference.plan_id,
            "base_plan_sha256": reference.plan_sha256,
            "trigger": "RECHECK",
            "reason": "根据当前数据库事实重新检查生产计划。",
            **changes,
        }
    )


def observe(client, settings, run_id, actor, reference, *, key=None, **changes):
    return observe_producer(
        client.app.state.session_factory,
        settings,
        run_id,
        actor,
        key or uuid4().hex,
        request_for(reference, **changes),
    )


def test_observation_persists_exact_snapshot_and_idempotent_reason(
    client, episode, settings, model_service
):
    root, actor, _, reference, sent = completed_plan(client, episode, settings, model_service)
    factory = client.app.state.session_factory
    key = uuid4().hex
    first = observe(client, settings, root, actor, reference, key=key)
    assert first.overview.runtime_budget is not None
    assert first.overview.budget is not None
    assert first.tasks[0].origin.plan_id == reference.plan_id
    assert first.tasks[0].child_run_id is None
    assert first.affected_task_keys == []
    assert first.tasks[0].replaceable is False
    calls = len(sent)
    second = observe(client, settings, root, actor, reference, key=key)
    assert second == first
    assert len(sent) == calls
    with factory.begin() as db:
        assert read_observation(db, root, first.id, actor) == first
        validate_observation_current(db, settings, db.get(WorkflowRun, root), first, actor)
        assert (
            db.scalar(
                select(func.count())
                .select_from(VersionBinding)
                .where(VersionBinding.resource_type == "PRODUCER_OBSERVATION")
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "producer.observed")
            )
            == 1
        )
    with pytest.raises(DomainError) as error:
        observe(client, settings, root, actor, reference, key=key, reason="同一运输键改动理由。")
    assert error.value.code == "PRODUCER_OBSERVATION_CONFLICT"


def test_runtime_usage_has_separate_full_snapshot_hash(client, episode, settings, model_service):
    root, actor, _, reference, _ = completed_plan(client, episode, settings, model_service)
    first = observe(client, settings, root, actor, reference)
    feedback = dispatch(
        client,
        settings,
        AgentToolCall(
            run_id=root,
            tool_name="investigate_project",
            idempotency_key=uuid4().hex,
            arguments={"query": {"path": "overview"}},
        ),
    )
    assert feedback.status == "SUCCEEDED"
    second = observe(client, settings, root, actor, reference)
    assert second.overview.runtime_budget.calls > first.overview.runtime_budget.calls
    assert second.authority_sha256 == first.authority_sha256
    assert second.snapshot_sha256 != first.snapshot_sha256
    with client.app.state.session_factory.begin() as db:
        validate_observation_current(db, settings, db.get(WorkflowRun, root), first, actor)


def test_retired_budget_policy_does_not_invalidate_current_observation(
    client, episode, settings, model_service
):
    root, actor, _, reference, _ = completed_plan(client, episode, settings, model_service)
    first = observe(client, settings, root, actor, reference)
    with client.app.state.session_factory.begin() as db:
        project = db.get(Project, UUID(episode["project_id"]))
        project.budget_limit += 1
        validate_observation_current(db, settings, db.get(WorkflowRun, root), first, actor)
    second = observe(client, settings, root, actor, reference)
    assert second.authority_sha256 == first.authority_sha256

    with client.app.state.session_factory.begin() as db:
        db.get(Project, UUID(episode["project_id"])).name = "更新的项目名称"
        with pytest.raises(DomainError) as stale:
            validate_observation_current(db, settings, db.get(WorkflowRun, root), second, actor)
    assert stale.value.code == "PRODUCER_OBSERVATION_STALE"


def test_failed_task_and_downstream_are_affected_but_active_task_is_protected(
    client,
    episode,
    settings,
    model_service,
):
    root, actor, _, reference, _ = completed_plan(
        client,
        episode,
        settings,
        model_service,
        tasks=[
            writer_task("first"),
            writer_task("later", depends_on=["first"]),
            writer_task("active"),
        ],
    )
    failed = dispatched(client, settings, delegation_call(root, reference, task_key="first"))
    with client.app.state.session_factory.begin() as db:
        child = db.get(WorkflowRun, UUID(failed["child_run_id"]))
        child.status = "FAILED"
        child.revision += 1
    active = dispatched(client, settings, delegation_call(root, reference, task_key="active"))
    observed = observe(
        client, settings, root, actor, reference, trigger="FAILURE", requested_task_keys=["active"]
    )
    assert observed.affected_task_keys == ["first", "later"]
    assert [task.replaceable for task in observed.tasks] == [True, True, False]
    assert observed.tasks[2].child_run_id == UUID(active["child_run_id"])
    assert observed.tasks[2].status in {"QUEUED", "RUNNING"}


def test_budget_change_is_rejected_and_passive_observation_cannot_replace_undelegated_task(
    client, episode, settings, model_service
):
    root, actor, _, reference, _ = completed_plan(client, episode, settings, model_service)
    passive = observe(client, settings, root, actor, reference, requested_task_keys=["write"])
    assert passive.affected_task_keys == []
    assert passive.tasks[0].replaceable is False
    with pytest.raises(ValidationError, match="BUDGET_CHANGED"):
        request_for(reference, trigger="BUDGET_CHANGED", requested_task_keys=["write"])


def test_budget_observation_preserves_original_evidence_and_requires_fresh_observation(
    client, episode, settings, model_service
):
    root, actor, _, reference, _ = completed_plan(client, episode, settings, model_service)
    current = observe(client, settings, root, actor, reference)
    historical_id = uuid4()
    request = request_for(
        reference, reason=current.reason, requested_task_keys=["write"]
    ).model_dump(mode="json") | {"trigger": "BUDGET_CHANGED"}
    historical = current.model_dump(mode="json") | {
        "id": str(historical_id),
        "trigger": "BUDGET_CHANGED",
        "affected_task_keys": ["write"],
    }
    historical["tasks"][0]["replaceable"] = True
    historical["overview"]["budget"].update(project_limit="20.000000", policy_revision=1)
    historical["overview"]["runtime_budget"]["deadline_at"] = current.read_at.isoformat()
    historical["snapshot_sha256"] = digest(
        {
            "request": request,
            "overview": historical["overview"],
            "reviews": historical["reviews"],
            "tasks": historical["tasks"],
            "affected_task_keys": historical["affected_task_keys"],
            "authority_sha256": historical["authority_sha256"],
            "human_evidence": historical["human_evidence"],
            "stage_receipt": historical["stage_receipt"],
        }
    )
    with client.app.state.session_factory.begin() as db:
        bind(
            db,
            current.project_id,
            "PRODUCER_OBSERVATION",
            historical_id,
            {
                "schema_version": 1,
                "request": request,
                "request_sha256": digest(request),
                "observation": historical,
            },
        )
    with client.app.state.session_factory.begin() as db:
        saved = observing.read_frozen_observation(db, root, historical_id, actor)
        assert saved.observation_json == historical
        assert saved.observation.trigger == "BUDGET_CHANGED"
        assert saved.observation.snapshot_sha256 == historical["snapshot_sha256"]
        assert saved.observation.overview.budget == current.overview.budget
        with pytest.raises(DomainError) as failure:
            validate_observation_current(
                db, settings, db.get(WorkflowRun, root), saved.observation, actor
            )
        assert failure.value.code == "PRODUCER_OBSERVATION_STALE"


def test_saved_agent_decision_is_exact_current_plan_evidence(
    client, episode, settings, model_service
):
    sent, errors = install_plan_model(model_service, human=True)
    root, actor = begin_producer(client, episode, settings)
    factory = client.app.state.session_factory
    waiting = planning.plan_producer(factory, settings, root, actor)
    assert waiting.status == "HUMAN_REVIEW"
    result = planning.resume_producer_plan(
        factory, settings, waiting.loop_id, actor, decision=answer(waiting)
    )
    assert result.status == "COMPLETED", (result, errors)
    with factory() as db:
        reference = planning.read_producer_plan(db, result.loop_id, actor)
        decision = db.scalar(select(AgentLoopDecision))
        decision_id = decision.id
    before = len(sent)
    observed = observe(
        client,
        settings,
        root,
        actor,
        reference,
        trigger="HUMAN_DECISION",
        human_decision_id=decision_id,
        requested_task_keys=["write"],
    )
    assert observed.human_decision_id == decision_id
    evidence = observed.model_dump(mode="json")["human_evidence"]
    assert evidence["kind"] == "AGENT"
    assert evidence["decision_id"] == str(decision_id)
    assert evidence["actor_id"] == actor
    assert evidence["source_run_id"] == str(root)
    assert evidence["question_id"] == str(waiting.question_id)
    assert evidence["decision"] == decision.decision_json
    assert evidence["decision"]["answer"] == answer(waiting).answer
    assert evidence["question"]["question"]
    assert observed.affected_task_keys == ["write"]
    assert observed.tasks[0].replaceable
    assert len(sent) == before
    delegated = dispatched(client, settings, delegation_call(root, reference))
    child_id = UUID(delegated["child_run_id"])
    with factory.begin() as db:
        db.get(WorkflowRun, child_id).status = "CANCELLED"
    automatic = observe(client, settings, root, actor, reference, requested_task_keys=["write"])
    assert automatic.affected_task_keys == []
    assert not automatic.tasks[0].replaceable
    human = observe(
        client,
        settings,
        root,
        actor,
        reference,
        trigger="HUMAN_DECISION",
        human_decision_id=decision_id,
        requested_task_keys=["write"],
    )
    assert human.affected_task_keys == ["write"]
    assert human.tasks[0].replaceable
    with factory() as db:
        assert db.get(WorkflowRun, child_id).status == "CANCELLED"
        outbox_count = db.scalar(select(func.count()).select_from(OutboxEvent))
    # STOP is preserved as evidence; it cannot authorize reopening a cancelled child.
    current = observe(client, settings, root, actor, reference)
    install_replan_model(model_service, human=True)
    waiting_again = replanning.replan_producer(factory, settings, root, actor, current.id)
    assert waiting_again.status == "HUMAN_REVIEW"
    with factory.begin() as db:
        stopped = submit_agent_decision(
            db,
            waiting_again.loop_id,
            actor,
            answer(waiting_again, decision="STOP", answer=None),
        )
        stopped_id = stopped.id
    stopped_observation = observe(
        client,
        settings,
        root,
        actor,
        reference,
        trigger="HUMAN_DECISION",
        human_decision_id=stopped_id,
        requested_task_keys=["write"],
    )
    assert stopped_observation.human_evidence.decision.decision == "STOP"
    assert stopped_observation.affected_task_keys == []
    assert not stopped_observation.tasks[0].replaceable
    with factory() as db:
        assert db.get(WorkflowRun, child_id).status == "CANCELLED"
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == outbox_count


def test_research_grant_revocation_blocks_new_observation_and_successful_replay(
    client,
    episode,
    settings,
    model_service,
):
    root, actor, _, reference, sent = completed_plan(client, episode, settings, model_service)
    key = uuid4().hex
    first = observe(client, settings, root, actor, reference, key=key)
    before = len(sent)
    with client.app.state.session_factory.begin() as db:
        grant_agent_tools(
            db,
            AgentToolGrantRequest(
                run_id=root,
                agent_name="producer",
                allowed_tools=[],
            ),
            actor,
            planning.producer_registry(settings),
        )
    for request_key in (key, uuid4().hex):
        with pytest.raises(DomainError) as error:
            observe(client, settings, root, actor, reference, key=request_key)
        assert error.value.code == "USER_NOT_AUTHORIZED"
    with client.app.state.session_factory.begin() as db:
        assert read_observation(db, root, first.id, actor) == first
        with pytest.raises(DomainError) as error:
            validate_observation_current(db, settings, db.get(WorkflowRun, root), first, actor)
        assert error.value.code == "USER_NOT_AUTHORIZED"
    assert len(sent) == before


def test_real_gateway_unknown_result_remains_observable_and_cannot_be_replaced(
    client,
    episode,
    settings,
    model_service,
):
    root, actor, _, reference, _ = completed_plan(client, episode, settings, model_service)
    delegated = dispatched(client, settings, delegation_call(root, reference))
    attempted = []

    def lost_response(request):
        attempted.append(request.url.path)
        raise httpx.ReadTimeout("upstream reply was lost", request=request)

    model_service(lost_response)
    child = run_once(client, settings, UUID(delegated["child_run_id"]))
    assert child["status"] == "FAILED", child
    assert len(attempted) == 1
    observed = observe(
        client, settings, root, actor, reference, trigger="FAILURE", requested_task_keys=["write"]
    )
    assert observed.affected_task_keys == ["write"]
    assert observed.tasks[0].unknown_external_result
    assert not observed.tasks[0].replaceable
    assert len(attempted) == 1


def test_formal_text_decision_is_read_from_original_journal_without_auto_approval(
    client,
    episode,
    settings,
    model_service,
    monkeypatch,
):
    root, actor, _, reference, _ = completed_plan(client, episode, settings, model_service)
    delegated = dispatched(client, settings, delegation_call(root, reference))
    child_id = UUID(delegated["child_run_id"])
    _, errors, ordinary_errors = install_writer(model_service, monkeypatch, settings, patches=0)
    child = run_once(client, settings, child_id)
    assert child["status"] == "WAITING_REVIEW", (child, errors, ordinary_errors)
    rows = client.get(f"/api/v1/projects/{episode['project_id']}/human-reviews").json()
    with client.app.state.session_factory() as db:
        request_id = db.scalar(
            select(HumanReviewRequest.id).where(HumanReviewRequest.run_id == child_id)
        )
    row = next(item for item in rows if item["id"] == str(request_id))
    response = decide(client, row)
    assert response.status_code == 200, response.text
    saved = response.json()
    before = observe(
        client,
        settings,
        root,
        actor,
        reference,
        trigger="HUMAN_DECISION",
        human_decision_id=UUID(saved["id"]),
        requested_task_keys=["write"],
    )
    assert not before.tasks[0].replaceable
    evidence = before.model_dump(mode="json")["human_evidence"]
    assert evidence["kind"] == "FORMAL"
    assert evidence["decision_id"] == saved["id"]
    assert evidence["source_run_id"] == str(child_id)
    assert evidence["actor_id"] == actor
    assert evidence["request_id"] == str(request_id)
    with client.app.state.session_factory() as db:
        assert evidence["decision"] == db.get(HumanReviewDecision, UUID(saved["id"])).decision_json
    assert evidence["resume_id"] is None
    with client.app.state.session_factory() as db:
        assert db.get(WorkflowRun, child_id).status == "WAITING_REVIEW"
        assert db.scalar(select(func.count()).select_from(HumanReviewDecision)) == 1
        assert db.scalar(select(func.count()).select_from(HumanReviewRequest)) == 1
    handoff = resume(client, row, saved)
    assert handoff.status_code == 200, handoff.text
    after = observe(
        client,
        settings,
        root,
        actor,
        reference,
        trigger="HUMAN_DECISION",
        human_decision_id=UUID(saved["id"]),
    )
    assert after.human_decision_id == before.human_decision_id
    assert after.human_evidence.resume_id is not None
    assert not after.tasks[0].replaceable


def test_unknown_human_id_cannot_create_observation(client, episode, settings, model_service):
    root, actor, _, reference, _ = completed_plan(client, episode, settings, model_service)
    with pytest.raises(DomainError) as error:
        observe(
            client,
            settings,
            root,
            actor,
            reference,
            trigger="HUMAN_DECISION",
            human_decision_id=uuid4(),
            requested_task_keys=["write"],
        )
    assert error.value.code == "NOT_FOUND"
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(VersionBinding)
                .where(VersionBinding.resource_type == "PRODUCER_OBSERVATION")
            )
            == 0
        )


def test_stage_event_is_consumed_only_through_existing_coordinator(
    client,
    episode,
    settings,
    model_service,
):
    root, actor, _, reference, _ = completed_plan(client, episode, settings, model_service)
    delegated = dispatched(client, settings, delegation_call(root, reference))
    with client.app.state.session_factory.begin() as db:
        event = enqueue_source(db, "TEXT", UUID(delegated["child_run_id"]), actor)
        event_id = event.id
    observed = observe(client, settings, root, actor, reference, source_event_id=event_id)
    assert observed.stage_receipt is not None
    assert observed.stage_receipt.event_id == event_id
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(ProductionEventReceipt)
                .where(ProductionEventReceipt.event_id == event_id)
            )
            == 1
        )


def test_blocked_event_uses_explicit_coordinator_recheck_once(
    client, episode, settings, model_service, monkeypatch
):
    root, actor, _, reference, _ = completed_plan(client, episode, settings, model_service)
    delegated = dispatched(client, settings, delegation_call(root, reference))
    child_id = UUID(delegated["child_run_id"])
    _, errors, ordinary_errors = install_writer(model_service, monkeypatch, settings, patches=0)
    child = run_once(client, settings, child_id)
    assert child["status"] == "WAITING_REVIEW", (child, errors, ordinary_errors)
    with client.app.state.session_factory() as db:
        request_id = db.scalar(
            select(HumanReviewRequest.id).where(HumanReviewRequest.run_id == child_id)
        )
    rows = client.get(f"/api/v1/projects/{episode['project_id']}/human-reviews").json()
    row = next(item for item in rows if item["id"] == str(request_id))
    response = decide(client, row)
    assert response.status_code == 200, response.text
    handoff = resume(client, row, response.json())
    assert handoff.status_code == 200, handoff.text
    with client.app.state.session_factory.begin() as db:
        event_id = enqueue_source(db, "TEXT", child_id, actor).id
        db.get(AssetVersion, UUID(row["snapshot"]["version_id"])).stale = True
    first = observe(client, settings, root, actor, reference, source_event_id=event_id)
    assert first.stage_receipt.disposition == "BLOCKED"
    key = uuid4().hex
    second = observe(
        client,
        settings,
        root,
        actor,
        reference,
        key=key,
        trigger="RECHECK",
        source_event_id=event_id,
    )
    assert second.stage_receipt.evaluation_key == second.id
    with client.app.state.session_factory() as db:
        claimed = db.scalar(
            select(VersionBinding).where(
                VersionBinding.resource_type == "PRODUCER_OBSERVATION_REQUEST",
                VersionBinding.resource_id == second.id,
            )
        )
        assert claimed.snapshot_json["event_action"] == "RECHECK"
    assert (
        observe(
            client,
            settings,
            root,
            actor,
            reference,
            key=key,
            trigger="RECHECK",
            source_event_id=event_id,
        )
        == second
    )
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(ProductionEventReceipt)
                .where(ProductionEventReceipt.event_id == event_id)
            )
            == 2
        )


def observation_event_pair(client, episode, settings, model_service):
    root, actor, _, reference, _ = completed_plan(
        client,
        episode,
        settings,
        model_service,
        tasks=[writer_task("one"), writer_task("two")],
    )
    first = dispatched(client, settings, delegation_call(root, reference, task_key="one"))
    with client.app.state.session_factory.begin() as db:
        failed = db.get(WorkflowRun, UUID(first["child_run_id"]))
        failed.status = "FAILED"
        failed.revision += 1
        first_event = enqueue_source(db, "TEXT", failed.id, actor).id
    second = dispatched(client, settings, delegation_call(root, reference, task_key="two"))
    with client.app.state.session_factory.begin() as db:
        second_event = enqueue_source(db, "TEXT", UUID(second["child_run_id"]), actor).id
    return root, actor, reference, first_event, second_event


def observation_effects(client, event_ids):
    with client.app.state.session_factory() as db:
        return {
            "receipts": {
                event_id: db.scalar(
                    select(func.count())
                    .select_from(ProductionEventReceipt)
                    .where(ProductionEventReceipt.event_id == event_id)
                )
                for event_id in event_ids
            },
            "reconciled": db.scalar(
                select(func.count())
                .select_from(WorkflowEvent)
                .where(WorkflowEvent.event_type == "production.reconciled")
            ),
            "outbox": db.scalar(select(func.count()).select_from(OutboxEvent)),
        }


def test_observation_conflict_before_coordinator_in_nested_interleaving(
    client, episode, settings, model_service, monkeypatch
):
    root, actor, reference, first_event, second_event = observation_event_pair(
        client, episode, settings, model_service
    )
    key = uuid4().hex
    original = observing.consume_stage_event
    calls, nested = [], {}
    before = observation_effects(client, [first_event, second_event])

    def interleave(factory, event_id):
        calls.append(event_id)
        if event_id == first_event:
            try:
                nested["result"] = observe(
                    client,
                    settings,
                    root,
                    actor,
                    reference,
                    key=key,
                    source_event_id=second_event,
                )
            except DomainError as exc:
                nested["error"] = exc.code
        return original(factory, event_id)

    monkeypatch.setattr(observing, "consume_stage_event", interleave)
    result, failure = None, None
    try:
        result = observe(
            client,
            settings,
            root,
            actor,
            reference,
            key=key,
            source_event_id=first_event,
        )
    except DomainError as exc:
        failure = exc.code
    after = observation_effects(client, [first_event, second_event])
    assert after["receipts"][second_event] == 0, (after, calls, nested, failure)
    assert calls == [first_event]
    assert nested == {"error": "PRODUCER_OBSERVATION_CONFLICT"}
    assert failure is None and result.stage_receipt.event_id == first_event
    assert after["receipts"][first_event] == 1
    assert after["reconciled"] == before["reconciled"] + 1
    assert after["outbox"] == before["outbox"]


def test_postgres_observation_conflict_is_rejected_while_first_coordinator_is_paused(
    client, episode, settings, model_service, monkeypatch
):
    require_postgres(settings)
    root, actor, reference, first_event, second_event = observation_event_pair(
        client, episode, settings, model_service
    )
    key = uuid4().hex
    original = observing.consume_stage_event
    ready, release = Event(), Event()
    calls = []
    before = observation_effects(client, [first_event, second_event])

    def paused(factory, event_id):
        calls.append(event_id)
        if event_id == first_event:
            ready.set()
            assert release.wait(30), "The test must release the first coordinator"
        return original(factory, event_id)

    monkeypatch.setattr(observing, "consume_stage_event", paused)
    first_result, first_failure, second_failure = None, None, None
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            observe,
            client,
            settings,
            root,
            actor,
            reference,
            key=key,
            source_event_id=first_event,
        )
        try:
            assert ready.wait(30), "The first transaction must reach the coordinator"
            second = pool.submit(
                observe,
                client,
                settings,
                root,
                actor,
                reference,
                key=key,
                source_event_id=second_event,
            )
            try:
                second.result(timeout=30)
            except DomainError as exc:
                second_failure = exc.code
        finally:
            release.set()
        try:
            first_result = first.result(timeout=30)
        except DomainError as exc:
            first_failure = exc.code
    after = observation_effects(client, [first_event, second_event])
    assert after["receipts"][second_event] == 0, (after, calls, first_failure, second_failure)
    assert calls == [first_event]
    assert second_failure == "PRODUCER_OBSERVATION_CONFLICT"
    assert first_failure is None and first_result.stage_receipt.event_id == first_event
    assert after["receipts"][first_event] == 1
    assert after["reconciled"] == before["reconciled"] + 1
    assert after["outbox"] == before["outbox"]


@pytest.mark.parametrize("crash", ["BEFORE_COORDINATOR", "AFTER_COORDINATOR"])
def test_observation_claim_recovers_without_upgrading_original_event_action(
    client, episode, settings, model_service, monkeypatch, crash
):
    root, actor, _, reference, _ = completed_plan(client, episode, settings, model_service)
    delegated = dispatched(client, settings, delegation_call(root, reference))
    child_id = UUID(delegated["child_run_id"])
    _, errors, ordinary_errors = install_writer(model_service, monkeypatch, settings, patches=0)
    child = run_once(client, settings, child_id)
    assert child["status"] == "WAITING_REVIEW", (child, errors, ordinary_errors)
    with client.app.state.session_factory() as db:
        request_id = db.scalar(
            select(HumanReviewRequest.id).where(HumanReviewRequest.run_id == child_id)
        )
    rows = client.get(f"/api/v1/projects/{episode['project_id']}/human-reviews").json()
    row = next(item for item in rows if item["id"] == str(request_id))
    decision = decide(client, row)
    assert decision.status_code == 200, decision.text
    handoff = resume(client, row, decision.json())
    assert handoff.status_code == 200, handoff.text
    with client.app.state.session_factory.begin() as db:
        event_id = enqueue_source(db, "TEXT", child_id, actor).id
        db.get(AssetVersion, UUID(row["snapshot"]["version_id"])).stale = True
    key = uuid4().hex
    identity = uuid5(root, "producer-observation:" + key)
    original_consume = observing.consume_stage_event
    original_recheck = observing.recheck_stage_event
    pending_crash = [crash]
    calls = []
    before = observation_effects(client, [event_id])

    def consume(factory, source_id):
        calls.append("CONSUME")
        if pending_crash and pending_crash[0] == "BEFORE_COORDINATOR":
            pending_crash.pop()
            raise RuntimeError("Interrupted before coordinator")
        receipt = original_consume(factory, source_id)
        if pending_crash and pending_crash[0] == "AFTER_COORDINATOR":
            pending_crash.pop()
            assert receipt.disposition == "BLOCKED"
            raise RuntimeError("Interrupted after coordinator")
        return receipt

    def recheck(factory, source_id, initiator, evaluation_key):
        calls.append("RECHECK")
        return original_recheck(factory, source_id, initiator, evaluation_key)

    monkeypatch.setattr(observing, "consume_stage_event", consume)
    monkeypatch.setattr(observing, "recheck_stage_event", recheck)
    with pytest.raises(RuntimeError, match="Interrupted"):
        observe(client, settings, root, actor, reference, key=key, source_event_id=event_id)
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(VersionBinding)
                .where(
                    VersionBinding.resource_type == "PRODUCER_OBSERVATION",
                    VersionBinding.resource_id == identity,
                )
            )
            == 0
        )
    interrupted_effects = observation_effects(client, [event_id])
    with client.app.state.session_factory() as db:
        claimed = db.scalar(
            select(VersionBinding).where(
                VersionBinding.resource_type == "PRODUCER_OBSERVATION_REQUEST",
                VersionBinding.resource_id == identity,
            )
        )
        assert claimed is not None and claimed.snapshot_json["event_action"] == "CONSUME"
    with pytest.raises(DomainError) as pending_conflict:
        observe(
            client,
            settings,
            root,
            actor,
            reference,
            key=key,
            source_event_id=event_id,
            reason="中断后的相同键也不能更换请求。",
        )
    assert pending_conflict.value.code == "PRODUCER_OBSERVATION_CONFLICT"
    assert calls == ["CONSUME"]
    assert observation_effects(client, [event_id]) == interrupted_effects
    recovered = observe(client, settings, root, actor, reference, key=key, source_event_id=event_id)
    assert calls == ["CONSUME", "CONSUME"]
    assert recovered.stage_receipt.disposition == "BLOCKED"
    assert recovered.stage_receipt.evaluation_key == event_id
    with client.app.state.session_factory() as db:
        intent = db.scalar(
            select(VersionBinding).where(
                VersionBinding.resource_type == "PRODUCER_OBSERVATION_REQUEST",
                VersionBinding.resource_id == identity,
            )
        )
        assert intent is not None
        assert intent.snapshot_json["request"] == request_for(
            reference, source_event_id=event_id
        ).model_dump(mode="json")
        assert intent.snapshot_json["event_action"] == "CONSUME"
    assert (
        observe(client, settings, root, actor, reference, key=key, source_event_id=event_id)
        == recovered
    )
    assert calls == ["CONSUME", "CONSUME"]
    with pytest.raises(DomainError) as conflict:
        observe(
            client,
            settings,
            root,
            actor,
            reference,
            key=key,
            source_event_id=event_id,
            reason="相同键不能更改已认领的请求。",
        )
    assert conflict.value.code == "PRODUCER_OBSERVATION_CONFLICT"
    assert calls == ["CONSUME", "CONSUME"]
    after = observation_effects(client, [event_id])
    assert after["receipts"][event_id] == 1
    assert after["outbox"] == before["outbox"]
    assert after["reconciled"] == before["reconciled"]
    explicit = observe(
        client,
        settings,
        root,
        actor,
        reference,
        source_event_id=event_id,
        trigger="RECHECK",
    )
    assert calls == ["CONSUME", "CONSUME", "RECHECK"]
    assert explicit.stage_receipt.evaluation_key == explicit.id
    assert observation_effects(client, [event_id])["receipts"][event_id] == 2


def test_art_observation_reads_original_proposal_version(
    client,
    episode,
    settings,
    model_service,
):
    reference_set(client, episode)
    source = approved_script(client, episode, count=1)
    tasks = [
        {
            "task_key": "visual",
            "depends_on": [],
            "reason": "比较现有参考并提出制作方案。",
            "task": {
                "kind": "art_director",
                "script_version_id": source["id"],
                "task": {"visual_goal": "比较雨夜角色的轮廓与配色，提供制作提示词"},
            },
        }
    ]
    root, actor, _, reference, _ = completed_plan(
        client,
        episode,
        settings,
        model_service,
        tasks=tasks,
    )
    delegated = dispatched(client, settings, delegation_call(root, reference, task_key="visual"))
    _, errors, _ = install_direction_model(model_service)
    child = run_once(client, settings, UUID(delegated["child_run_id"]))
    assert child["status"] == "WAITING_REVIEW", (child, errors)
    observed = observe(client, settings, root, actor, reference)
    assert UUID(child["pending_review"]["version_id"]) in observed.tasks[0].output_version_ids
    assert not observed.tasks[0].replaceable
    with client.app.state.session_factory() as db:
        delivery = delivered(db, UUID(delegated["child_run_id"])).model_dump(mode="json")
    saved = proposal(client, episode, delivery)
    approved = decide_art(client, saved)
    assert approved.status_code == 200, approved.text
    succeeded = run_once(client, settings, UUID(delegated["child_run_id"]))
    assert succeeded["status"] == "SUCCEEDED", succeeded
    approved_snapshot = observe(client, settings, root, actor, reference)
    saved = proposal(client, episode, delivery)
    output = next(item for item in saved["outputs"] if item["asset_id"] is not None)
    asset = client.get(
        f"/api/v1/projects/{episode['project_id']}/assets/{output['asset_id']}"
    ).json()
    archived = archive_asset(client, asset)
    assert archived.status_code == 200, archived.text
    with client.app.state.session_factory() as db:
        with pytest.raises(DomainError) as stale:
            validate_observation_current(
                db, settings, db.get(WorkflowRun, root), approved_snapshot, actor
            )
        assert stale.value.code == "PRODUCER_OBSERVATION_STALE"
    invalid = observe(client, settings, root, actor, reference)
    assert invalid.tasks[0].replaceable
    assert invalid.tasks[0].output_sha256 != approved_snapshot.tasks[0].output_sha256


def test_reviewer_observation_reads_exact_saved_result_and_preserves_approval_gate(
    client,
    episode,
    settings,
    model_service,
    reviewer_case,
):
    with client.app.state.session_factory.begin() as db:
        db.get(WorkflowRun, reviewer_case["run_id"]).status = "CANCELLED"
    target = reviewer_case["target"]
    tasks = [
        {
            "task_key": "check",
            "depends_on": [],
            "reason": "检查图片候选和精确镜头参考。",
            "task": {
                "kind": "continuity_reviewer",
                **{name: target[name] for name in ("candidate_id", "storyboard_id", "shot_id")},
            },
        }
    ]
    root, actor, _, reference, _ = completed_plan(
        client,
        episode,
        settings,
        model_service,
        tasks=tasks,
    )
    delegated = dispatched(client, settings, delegation_call(root, reference, task_key="check"))
    _, errors, completions = install_reviewer_model(model_service, dynamic=False)
    child = run_once(client, settings, UUID(delegated["child_run_id"]))
    assert child["status"] == "WAITING_REVIEW", (child, errors)
    observed = observe(client, settings, root, actor, reference)
    assert not errors and len(completions) == 1, errors
    assert observed.tasks[0].output_version_ids == [UUID(child["pending_review"]["review_id"])]
    assert not observed.tasks[0].replaceable
    assert observed.tasks[0].input_error_code is None


def test_reviewer_observation_after_real_revision_feedback_uses_latest_delivery(
    client, episode, settings, model_service, reviewer_case
):
    with client.app.state.session_factory.begin() as db:
        db.get(WorkflowRun, reviewer_case["run_id"]).status = "CANCELLED"
    target = reviewer_case["target"]
    tasks = [
        {
            "task_key": "check",
            "depends_on": [],
            "reason": "检查精确镜头参考并沿用原审查修订。",
            "task": {
                "kind": "continuity_reviewer",
                **{name: target[name] for name in ("candidate_id", "storyboard_id", "shot_id")},
            },
        }
    ]
    root, actor, _, reference, _ = completed_plan(
        client, episode, settings, model_service, tasks=tasks
    )
    delegated = dispatched(client, settings, delegation_call(root, reference, task_key="check"))
    child_id = UUID(delegated["child_run_id"])
    _, errors, _ = install_reviewer_model(
        model_service, dynamic=False, difference=("STATE", "MEDIUM")
    )
    child = run_once(client, settings, child_id)
    assert child["status"] == "WAITING_REVIEW", (child, errors)
    assert child["pending_review"]["destination"] == "REVISION_REQUIRED", child
    initial = observe(client, settings, root, actor, reference)
    with client.app.state.session_factory() as db:
        reviewed = require_review_record(
            db, UUID(episode["project_id"]), UUID(child["pending_review"]["review_id"]), actor
        ).review
    case = {"project_id": episode["project_id"]}
    response = request_revision(
        client,
        case,
        command={
            "review_id": str(reviewed.result_id),
            "issue_ids": [str(reviewed.issues[0].issue_id)],
            "action": "REGENERATE",
            "amendments": [],
        },
    )
    assert response.status_code == 201, response.text
    revision = response.json()
    candidate = finish_revision(client, episode, case, revision)
    sent, _, reviewed_calls, errors, completions = install_feedback_model(
        model_service, ["INVESTIGATE", "READY_FOR_REVIEW"]
    )
    assert queue_pending_reviews(client.app.state.session_factory, settings) == 1
    child = run_once(client, settings, child_id)
    assert child["status"] == "WAITING_REVIEW", (child, errors)
    assert child["pending_review"]["destination"] == "READY_FOR_REVIEW", child
    assert not errors and len(sent) == len(completions) == 2 and reviewed_calls
    feedback = child["pending_review"]["record"]
    final_review_id = UUID(feedback["review_id"])
    before = len(reviewed_calls)
    observed = observe(client, settings, root, actor, reference)
    assert observed.tasks[0].output_version_ids == [final_review_id]
    assert observed.tasks[0].output_sha256 != initial.tasks[0].output_sha256
    assert observed.tasks[0].input_error_code is None
    assert not observed.tasks[0].replaceable
    assert len(reviewed_calls) == before
    with client.app.state.session_factory() as db:
        validate_observation_current(db, settings, db.get(WorkflowRun, root), observed, actor)
        record = require_review_record(db, UUID(episode["project_id"]), final_review_id, actor)
        assert digest(record.model_dump(mode="json")) == feedback["review_sha256"]
        assert record.review.target.candidate_id == UUID(candidate["id"])
        assert db.get(AssetVersion, UUID(candidate["asset_version_id"])).status == "DRAFT"
        loops = list(db.scalars(select(AgentLoop).where(AgentLoop.run_id == child_id)))
        assert (
            sum(
                row.status == "COMPLETED"
                and row.seed_json["operation_key"].startswith("continuity-feedback/1:")
                for row in loops
            )
            == 2
        )
    assert len(reviewed_calls) == before


def test_another_root_stage_event_cannot_be_advanced(client, episode, settings, model_service):
    other, actor, _, other_plan, _ = completed_plan(client, episode, settings, model_service)
    delegated = dispatched(client, settings, delegation_call(other, other_plan))
    with client.app.state.session_factory.begin() as db:
        event_id = enqueue_source(db, "TEXT", UUID(delegated["child_run_id"]), actor).id
        db.get(WorkflowRun, UUID(delegated["child_run_id"])).status = "FAILED"
        db.get(WorkflowRun, other).status = "CANCELLED"
    root, _, _, reference, _ = completed_plan(client, episode, settings, model_service)
    with pytest.raises(DomainError) as error:
        observe(client, settings, root, actor, reference, source_event_id=event_id)
    assert error.value.code == "PRODUCER_OBSERVATION_SCOPE"
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(ProductionEventReceipt)) == 0


@pytest.mark.parametrize("change", ["disabled", "cancelled", "archived"])
def test_replay_rechecks_current_authority(client, settings, model_service, change):
    register(client)
    episode = create_episode(client, create_project(client))
    root, actor, _, reference, _ = completed_plan(client, episode, settings, model_service)
    key = uuid4().hex
    first = observe(client, settings, root, actor, reference, key=key)
    with client.app.state.session_factory.begin() as db:
        if change == "disabled":
            db.get(User, UUID(actor)).disabled = True
        elif change == "cancelled":
            db.get(WorkflowRun, root).status = "CANCELLED"
        else:
            db.get(Episode, UUID(episode["id"])).status = "ARCHIVED"
    with pytest.raises(DomainError):
        observe(client, settings, root, actor, reference, key=key)
    with client.app.state.session_factory.begin() as db, pytest.raises(DomainError):
        read_observation(db, root, first.id, actor)


@pytest.mark.parametrize(
    "changes",
    [
        {"requested_task_keys": ["write", "write"]},
        {"trigger": "HUMAN_DECISION"},
        {"reason": "   "},
        {"root_status": "SUCCEEDED"},
        {"actual_cost": "0"},
    ],
)
def test_observation_requests_forbid_fabricated_authority(changes):
    with pytest.raises(ValidationError):
        ProducerObservationRequest.model_validate(
            {
                "base_plan_id": uuid4(),
                "base_plan_sha256": "a" * 64,
                "trigger": "RECHECK",
                "reason": "检查真实状态。",
                **changes,
            }
        )


def test_unknown_task_key_is_rejected_before_save(client, episode, settings, model_service):
    root, actor, _, reference, _ = completed_plan(client, episode, settings, model_service)
    with pytest.raises(DomainError) as error:
        observe(client, settings, root, actor, reference, requested_task_keys=["invented"])
    assert error.value.code == "PRODUCER_OBSERVATION_CONFLICT"
