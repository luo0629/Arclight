"""FLOW-02: authoritative coordination, atomic receipts and explicit recovery."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from test_api_workflow import advance, review, start
from test_asset_selection import approved
from test_assets import create
from test_collaboration import register
from test_concurrency import require_postgres
from test_generation_budget import priced, set_policy
from test_generation_delivery import Provider, deliver
from test_generation_jobs import capture_ok, payload
from test_model_gateway import request, route

from arclight.application import production_events
from arclight.application.model_gateway import invoke
from arclight.application.production_event_handlers import MediaStageHandler, StagePlan
from arclight.application.production_event_sources import read_source
from arclight.application.production_events import (
    consume_stage_event,
    enqueue_source,
    publish_stage_events,
    recheck_stage_event,
)
from arclight.core.errors import DomainError
from arclight.domain.model_gateway import GatewayPolicy, GatewayUsage
from arclight.domain.production_events import StageEvent
from arclight.domain.versions import digest
from arclight.main import create_app
from arclight.persistence.models import (
    AssetVersion,
    AuditEvent,
    Episode,
    GenerationAttempt,
    GenerationJob,
    OutboxEvent,
    Project,
    ProjectMember,
    Review,
    Script,
    WorkflowEvent,
    WorkflowRun,
)
from arclight.persistence.production_events import ProductionEvent, ProductionEventReceipt
from arclight.providers.media_submission import SubmissionReceipt


def count(client, model):
    with client.app.state.session_factory() as db:
        return db.scalar(select(func.count()).select_from(model))


def latest(client, run_id):
    with client.app.state.session_factory() as db:
        return db.scalar(
            select(ProductionEvent.id)
            .where(ProductionEvent.source_id == UUID(str(run_id)))
            .order_by(ProductionEvent.source_revision.desc())
            .limit(1)
        )


def media(client, episode, actor="service-owner"):
    job = capture_ok(client, episode["project_id"], payload(episode_id=episode["id"]))
    return job, record(client, job["id"], actor=actor)


def record(client, source_id, kind="MEDIA", actor="service-owner"):
    with client.app.state.session_factory.begin() as db:
        return enqueue_source(db, kind, UUID(str(source_id)), actor).id


def consume(client, identity, **kwargs):
    return consume_stage_event(client.app.state.session_factory, identity, **kwargs)


def reconciled(client):
    with client.app.state.session_factory() as db:
        return db.scalar(
            select(func.count())
            .select_from(WorkflowEvent)
            .where(WorkflowEvent.event_type == "production.reconciled")
        )


def test_text_dual_review_uses_original_execution_and_survives_app_restart(
    client, episode, settings
):
    run_id = start(client, episode).json()["run_id"]
    assert latest(client, run_id) is None
    for review_no in range(2):
        pending = advance(client, settings, run_id)
        waiting = consume(client, latest(client, run_id))
        assert waiting.disposition == "APPLIED" and waiting.action == "WAIT_REVIEW"
        assert review(client, pending).status_code == 202
        identity = latest(client, run_id)
        if review_no == 1:
            # Simulate a missing execution notification. Coordination restores
            # the existing runtime's logical message instead of executing a model.
            with client.app.state.session_factory.begin() as db:
                event = db.get(ProductionEvent, identity)
                message = db.scalar(
                    select(OutboxEvent).where(
                        OutboxEvent.run_id == event.source_id,
                        OutboxEvent.revision == event.source_revision,
                    )
                )
                db.delete(message)
        before = count(client, OutboxEvent)
        resumed = consume(client, identity)
        assert resumed.action == "CONTINUE_TEXT" and resumed.disposition == "APPLIED"
        assert count(client, OutboxEvent) == before + int(review_no == 1)
        with TestClient(create_app(settings)) as restarted:
            assert consume(restarted, identity) == resumed
    assert advance(client, settings, run_id)["status"] == "SUCCEEDED"
    done = consume(client, latest(client, run_id))
    assert done.action == "OBSERVED" and done.state.phase == "GENERATING_MEDIA"
    assert done.state.approved_shot_count == 0
    assert reconciled(client) == 5
    assert count(client, Review) == 2


def test_out_of_order_old_revision_cannot_regress_current_stage(client, episode, settings):
    run_id = start(client, episode).json()["run_id"]
    pending = advance(client, settings, run_id)
    older = latest(client, run_id)
    assert review(client, pending).status_code == 202
    assert consume(client, latest(client, run_id)).action == "CONTINUE_TEXT"
    stale = consume(client, older)
    assert stale.disposition == "STALE" and stale.reason == "STAGE_SOURCE_STALE"
    assert consume(client, older) == stale
    assert reconciled(client) == 1


def test_frozen_source_input_and_actor_conflict_cannot_replace_event(client, episode, settings):
    run_id = start(client, episode).json()["run_id"]
    advance(client, settings, run_id)
    identity = latest(client, run_id)
    with (
        pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
        client.app.state.session_factory.begin() as db,
    ):
        run = db.get(WorkflowRun, UUID(run_id))
        run.input_json = run.input_json | {"story": "changed after notification"}
    actor = register(client, "conflicting-producer@example.com")["user"]["id"]
    with client.app.state.session_factory.begin() as db:
        db.add(
            ProjectMember(
                project_id=UUID(episode["project_id"]), user_id=UUID(actor), role="editor"
            )
        )
    with pytest.raises(DomainError, match="精确输入不能改变"):
        record(client, run_id, "TEXT", actor)
    assert consume(client, identity).action == "WAIT_REVIEW"
    assert count(client, ProductionEvent) == 1


def test_new_current_script_invalidates_unprocessed_event(client, episode, settings):
    run_id = start(client, episode).json()["run_id"]
    pending = advance(client, settings, run_id)
    assert review(client, pending).status_code == 202
    assert review(client, advance(client, settings, run_id)).status_code == 202
    assert advance(client, settings, run_id)["status"] == "SUCCEEDED"
    identity = latest(client, run_id)
    with client.app.state.session_factory() as db:
        content = db.get(Script, UUID(pending["pending_review"]["version_id"])).content_json
    saved = client.post(
        f"/api/v1/episodes/{episode['id']}/scripts",
        json={"expected_version_no": 1, "content": content},
    )
    assert saved.status_code == 201, saved.text
    assert consume(client, identity).reason == "STAGE_VERSION_STALE"


def test_resume_requires_real_review_record(client, episode, settings):
    run_id = start(client, episode).json()["run_id"]
    pending = advance(client, settings, run_id)
    with client.app.state.session_factory.begin() as db:
        run = db.get(WorkflowRun, UUID(run_id))
        run.status = "QUEUED"
        run.revision += 1
        run.resume_json = pending["pending_review"] | {"decision": "APPROVE"}
        identity = enqueue_source(db, "TEXT", run.id, "service-owner").id
    result = consume(client, identity)
    assert result.disposition == "BLOCKED" and result.reason == "STAGE_REVIEW_MISSING"
    assert reconciled(client) == 0


def test_approved_review_still_requires_current_dependencies(client, episode, settings):
    run_id = start(client, episode).json()["run_id"]
    pending = advance(client, settings, run_id)
    assert review(client, pending).status_code == 202
    with client.app.state.session_factory.begin() as db:
        db.get(AssetVersion, UUID(pending["pending_review"]["version_id"])).stale = True
    result = consume(client, latest(client, run_id))
    assert result.disposition == "BLOCKED" and result.reason == "STAGE_DEPENDENCY_BLOCKED"
    assert reconciled(client) == 0


def test_review_business_change_and_notification_roll_back_together(
    client, episode, settings, monkeypatch
):
    run_id = start(client, episode).json()["run_id"]
    pending = advance(client, settings, run_id)
    before = tuple(count(client, model) for model in (ProductionEvent, OutboxEvent, Review))
    original = production_events._record_source

    def fail_after_write(*args, **kwargs):
        original(*args, **kwargs)
        raise DomainError("TEST_TRANSACTION_FAILURE", "模拟事务失败", 409)

    monkeypatch.setattr(production_events, "_record_source", fail_after_write)
    assert review(client, pending).status_code == 409
    after = tuple(count(client, model) for model in (ProductionEvent, OutboxEvent, Review))
    assert after == before
    current = client.get(f"/api/v1/runs/{run_id}").json()
    assert current["status"] == "WAITING_REVIEW" and current["revision"] == pending["revision"]
    with client.app.state.session_factory() as db:
        assert (
            db.get(Script, UUID(pending["pending_review"]["version_id"])).status
            == "READY_FOR_REVIEW"
        )


@pytest.mark.parametrize(
    "change,code",
    [
        ("project_archive", "PROJECT_ARCHIVED"),
        ("episode_archive", "EPISODE_ARCHIVED"),
        ("delete", "NOT_FOUND"),
    ],
)
def test_archiving_or_deletion_after_enqueue_blocks_advancement(client, episode, change, code):
    _, identity = media(client, episode)
    with client.app.state.session_factory.begin() as db:
        project = db.get(Project, UUID(episode["project_id"]))
        if change == "project_archive":
            project.archived = True
        elif change == "delete":
            project.deleted_at = datetime.now(UTC)
        else:
            db.get(Episode, UUID(episode["id"])).status = "ARCHIVED"
    result = consume(client, identity)
    assert result.disposition == "BLOCKED" and result.reason == code
    assert count(client, ProductionEventReceipt) == 1


@pytest.mark.parametrize("change,code", [("viewer", "FORBIDDEN"), ("revoke", "NOT_FOUND")])
def test_revocation_blocks_but_owner_review_and_explicit_recheck_remain_safe(
    client, episode, settings, change, code
):
    editor = register(client, "event-editor@example.com")
    actor = editor["user"]["id"]
    with client.app.state.session_factory.begin() as db:
        member = ProjectMember(
            project_id=UUID(episode["project_id"]), user_id=UUID(actor), role="editor"
        )
        db.add(member)
        db.flush()
        membership_id = member.id
    run_id = start(client, episode).json()["run_id"]
    pending = advance(client, settings, run_id)
    with client.app.state.session_factory.begin() as db:
        member = db.get(ProjectMember, membership_id)
        if change == "viewer":
            member.role = "viewer"
        else:
            db.delete(member)
    client.headers["Authorization"] = "Bearer test-token"
    assert review(client, pending).status_code == 202
    identity = latest(client, run_id)
    blocked = consume(client, identity)
    assert blocked.reason == code and blocked.disposition == "BLOCKED"
    with pytest.raises(DomainError):
        recheck_stage_event(client.app.state.session_factory, identity, actor, uuid4())
    with client.app.state.session_factory.begin() as db:
        if change == "viewer":
            db.get(ProjectMember, membership_id).role = "editor"
        else:
            db.add(
                ProjectMember(
                    project_id=UUID(episode["project_id"]), user_id=UUID(actor), role="editor"
                )
            )
    key = uuid4()
    restored = recheck_stage_event(client.app.state.session_factory, identity, actor, key)
    assert restored.disposition == "APPLIED" and restored.action == "CONTINUE_TEXT"
    assert recheck_stage_event(client.app.state.session_factory, identity, actor, key) == restored
    assert consume(client, identity) == restored
    assert reconciled(client) == 1 and count(client, ProductionEventReceipt) == 2


def test_cross_project_enqueue_and_actor_transfer_are_denied(client, episode):
    _, identity = media(client, episode)
    outsider = register(client, "event-outsider@example.com")["user"]["id"]
    with client.app.state.session_factory() as db:
        source_id = db.get(ProductionEvent, identity).source_id
    with pytest.raises(DomainError):
        record(client, source_id, actor=outsider)
    with pytest.raises(DomainError, match="转授"):
        recheck_stage_event(client.app.state.session_factory, identity, outsider, uuid4())
    assert count(client, ProductionEvent) == 1 and count(client, ProductionEventReceipt) == 0


@pytest.mark.parametrize(
    "status,action",
    [
        ("SUCCEEDED", "WAIT_MEDIA"),
        ("RESULT_UNKNOWN", "WAIT_RECOVERY"),
        ("FAILED", "PARTIAL_FAILURE"),
        ("CANCELLED", "OBSERVED"),
    ],
)
def test_controlled_media_uses_real_job_outcome_without_inventing_approval(
    client, episode, settings, status, action
):
    # A real existing text run supplies partial-success evidence alongside this job.
    run_id = start(client, episode).json()["run_id"]
    advance(client, settings, run_id)
    job, older = media(client, episode)
    receipt = (
        SubmissionReceipt(
            status=status,
            error_code="RESULT_UNKNOWN"
            if status == "RESULT_UNKNOWN"
            else "CANCELLED"
            if status == "CANCELLED"
            else "PROVIDER_FAILED",
        )
        if status != "SUCCEEDED"
        else None
    )
    provider = Provider(receipt=receipt)
    assert deliver(client, job, provider) == status
    identity = record(client, job["id"])
    before = count(client, GenerationAttempt)
    result = consume(client, identity)
    assert result.action == action and result.disposition == "APPLIED"
    assert result.state.unbound_task_counts[status] == 1
    assert result.state.approved_shot_count == 0
    assert count(client, GenerationAttempt) == before and len(provider.requests) == 1
    assert consume(client, older).disposition == "STALE"
    assert consume(client, identity) == result


def test_media_partial_failure_does_not_cancel_independent_success(client, episode):
    success, _ = media(client, episode)
    failed, _ = media(client, episode)
    assert deliver(client, success) == "SUCCEEDED"
    assert (
        deliver(
            client,
            failed,
            Provider(receipt=SubmissionReceipt(status="FAILED", error_code="PROVIDER_FAILED")),
        )
        == "FAILED"
    )
    result = consume(client, record(client, failed["id"]))
    assert result.action == "PARTIAL_FAILURE" and result.state.has_partial_failure
    with client.app.state.session_factory() as db:
        assert db.get(GenerationJob, UUID(success["id"])).status == "SUCCEEDED"


class FailingMedia(MediaStageHandler):
    def advance(self, db, event, plan, state):
        super().advance(db, event, plan, state)
        db.flush()
        raise RuntimeError("crash after database side effect")


def test_handler_failure_rolls_back_effect_and_receipt_then_retries_once(client, episode):
    _, identity = media(client, episode)
    before = count(client, AuditEvent)
    with pytest.raises(RuntimeError, match="side effect"):
        consume(client, identity, handlers={"MEDIA": FailingMedia()})
    assert count(client, AuditEvent) == before and count(client, ProductionEventReceipt) == 0
    assert consume(client, identity).disposition == "APPLIED"
    assert count(client, AuditEvent) == before + 1
    consume(client, identity)
    assert count(client, AuditEvent) == before + 1


def test_dispatch_republishes_broker_failure_lost_message_and_crash_before_ack(client, episode):
    _, identity = media(client, episode)
    factory = client.app.state.session_factory

    def fail(_):
        raise ConnectionError("isolated broker failure")

    with pytest.raises(ConnectionError):
        publish_stage_events(factory, fail)
    deliveries = []

    def publish(value):
        # A new transaction can acquire the same project; publisher holds no business lock.
        with factory.begin() as db:
            db.get(Project, UUID(episode["project_id"]), with_for_update={"nowait": True})
        deliveries.append(value)

    assert publish_stage_events(factory, publish) == 1
    assert publish_stage_events(factory, publish) == 1
    assert deliveries == [identity, identity]
    first = consume(client, identity)
    assert consume(client, identity) == first
    assert publish_stage_events(factory, publish) == 0
    with factory() as db:
        message = db.scalar(select(OutboxEvent).where(OutboxEvent.production_event_id == identity))
        assert message.published_at is not None and message.attempt_count == 2


class QuotedMedia(MediaStageHandler):
    def __init__(self, maximum_cost="2"):
        self.maximum_cost = Decimal(maximum_cost)

    def prepare(self, db, event, state):
        return StagePlan("WAIT_MEDIA", maximum_cost=self.maximum_cost)


@pytest.mark.parametrize(
    "scope,code",
    [
        ("project", "BUDGET_EXCEEDED"),
        ("episode", "BUDGET_EPISODE_EXCEEDED"),
        ("task", "BUDGET_TASK_EXCEEDED"),
    ],
)
def test_scoped_budget_change_is_rechecked_and_can_resume_explicitly(client, episode, scope, code):
    set_policy(client, episode["project_id"], "10")
    _, identity = media(client, episode)
    limits = {f"{scope}_limit": "1"} if scope != "project" else {}
    set_policy(client, episode["project_id"], "1" if scope == "project" else "10", **limits)
    handlers = {"MEDIA": QuotedMedia()}
    blocked = consume(client, identity, handlers=handlers)
    assert blocked.reason == code and blocked.disposition == "BLOCKED"
    set_policy(client, episode["project_id"], "10", **{name: "10" for name in limits})
    assert consume(client, identity, handlers=handlers) == blocked
    key = uuid4()
    resumed = recheck_stage_event(
        client.app.state.session_factory, identity, "service-owner", key, handlers=handlers
    )
    assert resumed.disposition == "APPLIED"
    assert (
        recheck_stage_event(
            client.app.state.session_factory, identity, "service-owner", uuid4(), handlers=handlers
        )
        == resumed
    )
    assert count(client, ProductionEventReceipt) == 2 and count(client, GenerationAttempt) == 0


def test_budget_reads_actual_and_outstanding_shared_ledger(client, episode, monkeypatch):
    set_policy(client, episode["project_id"], "10")
    priced(monkeypatch, "3")
    job, identity = media(client, episode)
    reserved = consume(client, identity)
    assert Decimal(reserved.budget["reserved"]) == 3
    assert deliver(client, job) == "SUCCEEDED"
    selected = route()
    call = request(episode, selected, task="script", workflow_id=None)
    invoke(
        client.app.state.session_factory,
        call,
        selected,
        lambda: "mock",
        policy=GatewayPolicy(),
        usage=lambda _: GatewayUsage(actual_cost=Decimal("1")),
    )
    result = consume(client, record(client, job["id"]))
    assert Decimal(result.budget["actual"]) == 1
    # The provider returned no media bill: the original reservation remains held.
    assert Decimal(result.budget["reserved"]) == 3
    set_policy(client, episode["project_id"], "5")
    # The next zero-cost job retains the same outstanding shared ledger.
    priced(monkeypatch, "0")
    _, identity = media(client, episode)
    stopped = consume(client, identity, handlers={"MEDIA": QuotedMedia()})
    assert stopped.reason == "BUDGET_EXCEEDED" and count(client, GenerationAttempt) == 1


def test_unknown_text_cost_cannot_be_assumed_free_by_quoted_handler(client, episode):
    set_policy(client, episode["project_id"], "10")
    selected = route(maximum_cost=None, pricing_version="unpriced:1")
    call = request(
        episode, selected, task="script", workflow_id=UUID(start(client, episode).json()["run_id"])
    )
    invoke(
        client.app.state.session_factory,
        call,
        selected,
        lambda: "mock",
        policy=GatewayPolicy(),
        usage=lambda _: GatewayUsage(),
    )
    _, identity = media(client, episode)
    blocked = consume(client, identity, handlers={"MEDIA": QuotedMedia()})
    assert blocked.reason == "BUDGET_UNPRICED_HISTORY" and blocked.disposition == "BLOCKED"
    assert count(client, GenerationAttempt) == 0


def test_exact_manifest_input_lifecycle_is_rechecked(client, episode):
    asset = create(client, episode["project_id"])
    version = approved(client, asset)
    job = capture_ok(
        client,
        episode["project_id"],
        payload(
            episode_id=episode["id"],
            inputs=[
                {
                    "source_asset_version_id": version["id"],
                    "dependency_type": "REFERENCE_DEPENDENCY",
                }
            ],
        ),
    )
    identity = record(client, job["id"])
    with client.app.state.session_factory.begin() as db:
        db.get(AssetVersion, UUID(version["id"])).stale = True
    result = consume(client, identity)
    assert result.disposition == "STALE" and result.reason == "STAGE_INPUT_STALE"
    assert count(client, GenerationAttempt) == 0


@pytest.mark.parametrize(
    "fault,code",
    [
        ("input", "STAGE_SOURCE_STALE"),
        ("missing", "STAGE_SOURCE_NOT_FOUND"),
        ("header", "STAGE_EVENT_INVALID"),
    ],
)
def test_consumer_rechecks_saved_notification_against_actual_source(client, episode, fault, code):
    job = capture_ok(client, episode["project_id"], payload(episode_id=episode["id"]))
    with client.app.state.session_factory.begin() as db:
        source = read_source(db, "MEDIA", UUID(job["id"]))
        value = StageEvent(
            event_id=uuid4(),
            project_id=source.project_id,
            episode_id=source.episode_id,
            source_kind="MEDIA",
            source_id=uuid4() if fault == "missing" else UUID(job["id"]),
            source_revision=source.revision,
            actor_id="service-owner",
            input_sha256="0" * 64 if fault == "input" else source.input_sha256,
            script_version_id=None,
            storyboard_version_id=None,
        ).model_dump(mode="json")
        identity = UUID(value["event_id"])
        db.add(
            ProductionEvent(
                id=identity,
                project_id=source.project_id,
                episode_id=source.episode_id,
                source_kind="MEDIA",
                source_id=UUID(value["source_id"]),
                source_revision=source.revision,
                request_sha256="f" * 64 if fault == "header" else digest(value),
                event_json=value,
            )
        )
    if fault == "header":
        with pytest.raises(DomainError) as error:
            consume(client, identity)
        assert error.value.code == code
    else:
        assert consume(client, identity).reason == code
    assert count(client, GenerationAttempt) == 0


@pytest.mark.parametrize("cost", ["-1", "NaN", "Infinity"])
def test_invalid_handler_cost_cannot_advance(client, episode, cost):
    _, identity = media(client, episode)
    result = consume(client, identity, handlers={"MEDIA": QuotedMedia(cost)})
    assert result.disposition == "BLOCKED" and result.reason == "STAGE_COST_INVALID"


def test_missing_handler_remains_blocked_until_explicit_recheck(client, episode):
    _, identity = media(client, episode)
    with pytest.raises(DomainError) as error:
        recheck_stage_event(client.app.state.session_factory, identity, "service-owner", uuid4())
    assert error.value.code == "STAGE_NOT_EVALUATED"
    blocked = consume(client, identity, handlers={})
    assert blocked.reason == "STAGE_HANDLER_UNAVAILABLE"
    assert consume(client, identity) == blocked
    resumed = recheck_stage_event(
        client.app.state.session_factory, identity, "service-owner", uuid4()
    )
    assert resumed.disposition == "APPLIED" and count(client, ProductionEventReceipt) == 2


def test_recheck_cannot_reinterpret_changed_inputs_after_pause(client, episode):
    job, identity = media(client, episode)
    assert consume(client, identity, handlers={}).disposition == "BLOCKED"
    assert deliver(client, job) == "SUCCEEDED"
    stale = recheck_stage_event(
        client.app.state.session_factory, identity, "service-owner", uuid4()
    )
    assert stale.disposition == "STALE" and stale.reason == "STAGE_SOURCE_STALE"
    assert (
        recheck_stage_event(client.app.state.session_factory, identity, "service-owner", uuid4())
        == stale
    )
    assert count(client, ProductionEventReceipt) == 2
    assert consume(client, record(client, job["id"])).disposition == "APPLIED"


@pytest.mark.parametrize("kind", ["TEXT", "MEDIA"])
def test_postgres_two_consumers_commit_only_one_effect(client, episode, settings, kind):
    require_postgres(settings)
    if kind == "TEXT":
        run_id = start(client, episode).json()["run_id"]
        advance(client, settings, run_id)
        identity = latest(client, run_id)
    else:
        _, identity = media(client, episode)
    barrier = Barrier(2)
    before = count(client, AuditEvent)

    def worker(_):
        barrier.wait(timeout=15)
        return consume(client, identity)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(worker, range(2)))
    assert first == second and count(client, ProductionEventReceipt) == 1
    if kind == "TEXT":
        assert reconciled(client) == 1
    else:
        assert count(client, AuditEvent) == before + 1


def test_postgres_concurrent_explicit_rechecks_advance_only_once(client, episode, settings):
    require_postgres(settings)
    _, identity = media(client, episode)
    assert consume(client, identity, handlers={}).disposition == "BLOCKED"
    before = count(client, AuditEvent)
    barrier = Barrier(2)

    def resume(_):
        barrier.wait(timeout=15)
        return recheck_stage_event(
            client.app.state.session_factory, identity, "service-owner", uuid4()
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(resume, range(2)))
    assert first == second and first.disposition == "APPLIED"
    assert count(client, ProductionEventReceipt) == 2
    assert count(client, AuditEvent) == before + 1


@pytest.mark.parametrize(
    "patch",
    [
        {"schema_version": "production-event/2"},
        {"source_revision": True},
        {"source_revision": -1},
        {"input_sha256": "invalid"},
        {"source_kind": "RENDER"},
        {"actor_id": ""},
        {"status": "SUCCEEDED"},
    ],
)
def test_stage_contract_rejects_untrusted_facts_and_invalid_headers(patch):
    value = dict(
        event_id=uuid4(),
        project_id=uuid4(),
        episode_id=uuid4(),
        source_kind="TEXT",
        source_id=uuid4(),
        source_revision=1,
        actor_id="service-owner",
        input_sha256="a" * 64,
        script_version_id=None,
        storyboard_version_id=None,
    )
    with pytest.raises(ValidationError):
        StageEvent.model_validate(value | patch)
